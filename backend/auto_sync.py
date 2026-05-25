"""
Background provider sync scheduler.

The scheduler is deliberately based on persisted provider sync timestamps, not
process uptime. If Folio is stopped before the interval elapses and restarted
after the interval, startup will see that the last successful sync is stale and
queue a new provider sync.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from database import close_thread_local_connection, get_db
from log_config import get_logger
from sync_status import finish_sync, get_sync_status, start_sync


logger = get_logger(__name__)

LAST_ATTEMPT_KEY = "folio_auto_sync_last_attempt_at"
LAST_STATUS_KEY = "folio_auto_sync_last_status"
LAST_ERROR_KEY = "folio_auto_sync_last_error"
LAST_SUCCESS_KEY = "folio_auto_sync_last_success_at"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_RETRY_STATUSES = {"failed", "no_provider_update", "running"}

_scheduler_lock = threading.Lock()
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None
_run_lock = threading.Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.2f", name, raw, default)
        return default
    return max(minimum, value)


def auto_sync_enabled() -> bool:
    return _env_bool("FOLIO_AUTO_SYNC_ENABLED", True)


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _get_setting(conn, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def _set_setting(conn, key: str, value: str | None) -> None:
    conn.execute(
        """INSERT INTO app_settings (key, value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(key) DO UPDATE SET
             value = excluded.value,
             updated_at = datetime('now')""",
        (key, value),
    )


def _count_table(conn, table: str, where: str = "1=1") -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["count"] if row else 0)


def active_provider_connections(conn) -> dict[str, int]:
    teller = _count_table(conn, "enrolled_tokens", "is_active = 1")
    simplefin = _count_table(conn, "simplefin_connections", "is_active = 1")
    return {
        "teller": teller,
        "simplefin": simplefin,
        "total": teller + simplefin,
    }


def most_recent_provider_sync(conn) -> datetime | None:
    candidates: list[datetime] = []
    queries = [
        """SELECT MAX(last_synced_at) AS last_synced_at
           FROM accounts
           WHERE is_active = 1
             AND last_synced_at IS NOT NULL
             AND COALESCE(provider, 'teller') IN ('teller', 'simplefin')""",
        """SELECT MAX(last_synced_at) AS last_synced_at
           FROM simplefin_connections
           WHERE is_active = 1
             AND last_synced_at IS NOT NULL""",
    ]
    for sql in queries:
        try:
            row = conn.execute(sql).fetchone()
        except sqlite3.OperationalError:
            continue
        parsed = _parse_datetime(row["last_synced_at"] if row else None)
        if parsed:
            candidates.append(parsed)
    return max(candidates) if candidates else None


def auto_sync_due_status(
    conn,
    *,
    now: datetime | None = None,
    interval_hours: float | None = None,
    failure_backoff_minutes: float | None = None,
) -> dict[str, Any]:
    current = (now or _now()).replace(microsecond=0)
    interval = timedelta(
        hours=interval_hours
        if interval_hours is not None
        else _env_float("FOLIO_AUTO_SYNC_INTERVAL_HOURS", 12.0, 0.1)
    )
    failure_backoff = timedelta(
        minutes=failure_backoff_minutes
        if failure_backoff_minutes is not None
        else _env_float("FOLIO_AUTO_SYNC_FAILURE_BACKOFF_MINUTES", 60.0, 1.0)
    )

    connections = active_provider_connections(conn)
    last_success = most_recent_provider_sync(conn)
    last_attempt = _parse_datetime(_get_setting(conn, LAST_ATTEMPT_KEY))
    last_status = _get_setting(conn, LAST_STATUS_KEY)
    last_error = _get_setting(conn, LAST_ERROR_KEY)

    next_sync_at = last_success + interval if last_success else current
    due = last_success is None or current >= next_sync_at
    reason = "due" if due else "fresh"

    if connections["total"] == 0:
        due = False
        reason = "no_active_connections"
    elif due and last_attempt and last_status in _RETRY_STATUSES:
        next_retry_at = last_attempt + failure_backoff
        if current < next_retry_at:
            due = False
            reason = "recent_failed_attempt"
        else:
            reason = "retry_after_failed_attempt"

    return {
        "enabled": auto_sync_enabled(),
        "due": due,
        "reason": reason,
        "interval_hours": interval.total_seconds() / 3600,
        "failure_backoff_minutes": failure_backoff.total_seconds() / 60,
        "now": _iso(current),
        "last_success_at": _iso(last_success),
        "next_sync_at": _iso(next_sync_at),
        "last_attempt_at": _iso(last_attempt),
        "last_status": last_status,
        "last_error": last_error,
        "active_connections": connections,
    }


def _record_attempt(conn, *, status: str, attempted_at: datetime, error: str | None = None) -> None:
    _set_setting(conn, LAST_ATTEMPT_KEY, _iso(attempted_at))
    _set_setting(conn, LAST_STATUS_KEY, status)
    _set_setting(conn, LAST_ERROR_KEY, error)


def run_auto_sync_once(
    *,
    on_success: Callable[[dict], None] | None = None,
    sync_func: Callable[..., dict] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not auto_sync_enabled():
        return {"status": "disabled", "reason": "env_disabled"}

    if not _run_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "already_running"}

    job_id: str | None = None
    try:
        attempted_at = (now or _now()).replace(microsecond=0)
        with get_db() as conn:
            due_status = auto_sync_due_status(conn, now=attempted_at)
            if not due_status["due"]:
                return {"status": "skipped", **due_status}
            before_success = most_recent_provider_sync(conn)
            _record_attempt(conn, status="running", attempted_at=attempted_at)

        current_sync = get_sync_status()
        if current_sync.get("active"):
            with get_db() as conn:
                _record_attempt(conn, status="skipped_active_sync", attempted_at=attempted_at)
            return {"status": "skipped", "reason": "sync_already_active", "sync_status": current_sync}

        job_id = start_sync("auto-sync", phase="starting", detail="Starting automatic provider sync")
        if sync_func is None:
            from data_manager import fetch_fresh_data

            sync_func = fetch_fresh_data

        data = sync_func(sync_job_id=job_id)

        with get_db() as conn:
            after_success = most_recent_provider_sync(conn)
            if after_success and (before_success is None or after_success > before_success):
                _record_attempt(conn, status="completed", attempted_at=attempted_at)
                _set_setting(conn, LAST_SUCCESS_KEY, _iso(after_success))
                status = "completed"
            else:
                _record_attempt(
                    conn,
                    status="no_provider_update",
                    attempted_at=attempted_at,
                    error="Provider sync completed without updating provider timestamps.",
                )
                status = "no_provider_update"

        finish_sync(job_id, status="completed")
        if status == "completed" and on_success is not None:
            try:
                on_success(data)
            except Exception:
                logger.debug("Auto sync success callback failed", exc_info=True)
        return {
            "status": status,
            "last_success_at": _iso(after_success),
            "accounts": len(data.get("accounts", [])) if isinstance(data, dict) else None,
            "transactions": len(data.get("transactions", [])) if isinstance(data, dict) else None,
        }
    except Exception as exc:
        if job_id:
            finish_sync(job_id, status="failed", error=str(exc))
        with get_db() as conn:
            _record_attempt(conn, status="failed", attempted_at=now or _now(), error=str(exc))
        logger.warning("Automatic provider sync failed: %s", exc)
        return {"status": "failed", "error": str(exc)}
    finally:
        _run_lock.release()


def _scheduler_loop(on_success: Callable[[dict], None] | None) -> None:
    try:
        startup_delay = _env_float("FOLIO_AUTO_SYNC_STARTUP_DELAY_SECONDS", 30.0, 0.0)
        check_seconds = _env_float("FOLIO_AUTO_SYNC_CHECK_SECONDS", 300.0, 30.0)

        if _scheduler_stop.wait(startup_delay):
            return

        while not _scheduler_stop.is_set():
            run_auto_sync_once(on_success=on_success)
            if _scheduler_stop.wait(check_seconds):
                break
    finally:
        close_thread_local_connection()


def start_auto_sync_scheduler(on_success: Callable[[dict], None] | None = None) -> bool:
    global _scheduler_thread

    if not auto_sync_enabled():
        logger.info("Automatic provider sync is disabled.")
        return False

    with _scheduler_lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return False
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(
            target=_scheduler_loop,
            args=(on_success,),
            name="folio-auto-sync",
            daemon=True,
        )
        _scheduler_thread.start()
        logger.info("Automatic provider sync scheduler started.")
        return True


def stop_auto_sync_scheduler(timeout: float = 2.0) -> None:
    global _scheduler_thread

    with _scheduler_lock:
        thread = _scheduler_thread
        if thread is None:
            return
        _scheduler_stop.set()
    thread.join(timeout=timeout)
    with _scheduler_lock:
        if _scheduler_thread is thread:
            _scheduler_thread = None
