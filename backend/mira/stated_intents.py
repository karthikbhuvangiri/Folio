"""Subject-scoped stated money commitments for Mira.

Phase 31 keeps this layer as a companion to approved memory. It does not
route chat. A memory is saved first; then this module tries to map that
durable statement onto a known finance subject and evaluate it deterministically.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from merchant_identity import canonicalize_merchant_key, display_from_key


FALSE_ENV_VALUES = {"0", "false", "no", "off"}
SUBJECT_TYPES = {"merchant", "category", "habit", "account", "cashflow"}
INTENT_KINDS = {"cut", "hold", "grow", "monitor", "avoid", "increase"}
STATUS_STATES = {"active", "paused", "completed", "dismissed"}
FEEDBACK_STATES = {"neutral", "more_like_this", "less_like_this", "too_sensitive"}
BASELINE_SCOPE = "mtd_vs_prior_3_full_months"
NON_SPENDING_CATEGORIES = {
    "Income",
    "Savings Transfer",
    "Credit Card Payment",
    "Cash Deposit",
    "Cash Withdrawal",
    "Investment Transfer",
    "Transfer",
    "Transfers",
    "Personal Transfer",
    "Credits & Refunds",
}
TRANSFER_EXPENSE_TYPES = {"transfer_internal", "transfer_household", "transfer_external"}


def stated_intent_memory_enabled() -> bool:
    return os.getenv("MIRA_STATED_INTENT_MEMORY_ENABLED", "0").strip().lower() not in FALSE_ENV_VALUES


def ensure_stated_intent_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_stated_intents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id          TEXT DEFAULT NULL,
            memory_id           INTEGER DEFAULT NULL REFERENCES mira_memories(id),
            subject_type        TEXT NOT NULL CHECK(subject_type IN ('merchant', 'category', 'habit', 'account', 'cashflow')),
            subject_key         TEXT NOT NULL DEFAULT '',
            subject_label       TEXT NOT NULL DEFAULT '',
            intent_kind         TEXT NOT NULL CHECK(intent_kind IN ('cut', 'hold', 'grow', 'monitor', 'avoid', 'increase')),
            baseline_scope      TEXT NOT NULL DEFAULT 'mtd_vs_prior_3_full_months',
            target_text         TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'completed', 'dismissed')),
            feedback_state      TEXT NOT NULL DEFAULT 'neutral' CHECK(feedback_state IN ('neutral', 'more_like_this', 'less_like_this', 'too_sensitive')),
            last_evaluated_at   TEXT DEFAULT NULL,
            evaluation_json     TEXT NOT NULL DEFAULT '{}',
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_mira_stated_intents_profile_status
            ON mira_stated_intents(profile_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_stated_intents_subject
            ON mira_stated_intents(profile_id, subject_type, subject_key, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mira_stated_intents_memory
            ON mira_stated_intents(profile_id, memory_id)
            WHERE memory_id IS NOT NULL;
        """
    )


def maybe_create_stated_intent_from_memory(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    memory: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Create a finance intent only after approved Memory V2 has saved."""

    if not stated_intent_memory_enabled() or not memory:
        return None
    if str(memory.get("memory_type") or "").strip() not in {"goal", "constraint", "commitment"}:
        return None
    source_text = " ".join(
        str(value or "").strip()
        for value in (memory.get("normalized_text"), memory.get("original_text"), memory.get("topic"))
        if str(value or "").strip()
    )
    intent_kind = infer_intent_kind(source_text)
    if not intent_kind:
        return None
    subject = infer_subject_from_text(conn=conn, profile=profile, text=source_text)
    if not subject:
        return None
    return create_stated_intent(
        conn=conn,
        profile=profile,
        memory_id=_optional_int(memory.get("id")),
        subject_type=subject["subject_type"],
        subject_key=subject["subject_key"],
        subject_label=subject["subject_label"],
        intent_kind=intent_kind,
        target_text=_clean_text(memory.get("normalized_text") or source_text, 280),
    )


def infer_intent_kind(text: str) -> str:
    lowered = _clean_text(text, 600).lower()
    if re.search(r"\b(?:avoid|stop|quit|skip)\b", lowered):
        return "avoid"
    if re.search(r"\b(?:cut|reduce|lower|trim|spend less|less on|dial down)\b", lowered):
        return "cut"
    if re.search(r"\b(?:cap|limit|keep .+ under|stay under|hold)\b", lowered):
        return "hold"
    if re.search(r"\b(?:increase|raise|put more|save more)\b", lowered):
        return "increase"
    if re.search(r"\b(?:grow|build)\b", lowered):
        return "grow"
    if re.search(r"\b(?:monitor|watch|keep an eye)\b", lowered):
        return "monitor"
    return ""


def infer_subject_from_text(*, conn: sqlite3.Connection, profile: str | None, text: str) -> dict[str, str] | None:
    haystack = _matchable_text(text)
    if not haystack:
        return None
    candidates = _subject_candidates(conn, profile=profile)
    for candidate in sorted(candidates, key=lambda item: len(item["alias"]), reverse=True):
        alias = candidate["alias"]
        if alias and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", haystack):
            return {
                "subject_type": candidate["subject_type"],
                "subject_key": candidate["subject_key"],
                "subject_label": candidate["subject_label"],
            }
    return None


def create_stated_intent(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    subject_type: str,
    subject_key: str,
    intent_kind: str,
    target_text: str,
    memory_id: int | None = None,
    subject_label: str = "",
    baseline_scope: str = BASELINE_SCOPE,
    status: str = "active",
    feedback_state: str = "neutral",
) -> dict[str, Any]:
    ensure_stated_intent_tables(conn)
    clean = {
        "profile_id": _profile_scope(profile),
        "memory_id": _optional_int(memory_id),
        "subject_type": _enum(subject_type, SUBJECT_TYPES, "category"),
        "subject_key": normalize_subject_key(subject_type, subject_key),
        "subject_label": _clean_text(subject_label or _subject_label(subject_type, subject_key), 120),
        "intent_kind": _enum(intent_kind, INTENT_KINDS, "monitor"),
        "baseline_scope": _clean_text(baseline_scope, 80) or BASELINE_SCOPE,
        "target_text": _clean_text(target_text, 280),
        "status": _enum(status, STATUS_STATES, "active"),
        "feedback_state": _enum(feedback_state, FEEDBACK_STATES, "neutral"),
    }
    if not clean["target_text"]:
        raise ValueError("target_text is required.")
    if not clean["subject_key"]:
        raise ValueError("subject_key is required.")
    existing = _find_existing_intent(conn, clean["profile_id"], clean["memory_id"])
    if existing:
        return existing
    cur = conn.execute(
        """
        INSERT INTO mira_stated_intents (
            profile_id, memory_id, subject_type, subject_key, subject_label,
            intent_kind, baseline_scope, target_text, status, feedback_state
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clean["profile_id"],
            clean["memory_id"],
            clean["subject_type"],
            clean["subject_key"],
            clean["subject_label"],
            clean["intent_kind"],
            clean["baseline_scope"],
            clean["target_text"],
            clean["status"],
            clean["feedback_state"],
        ),
    )
    intent_id = int(cur.lastrowid)
    if clean["status"] == "active":
        evaluate_stated_intent(conn=conn, profile=profile, intent_id=intent_id)
    return get_stated_intent(conn=conn, profile=profile, intent_id=intent_id) or {}


def get_stated_intent(*, conn: sqlite3.Connection, profile: str | None, intent_id: int) -> dict[str, Any] | None:
    ensure_stated_intent_tables(conn)
    row = conn.execute(
        """
        SELECT *
          FROM mira_stated_intents
         WHERE id = ?
           AND profile_id = ?
         LIMIT 1
        """,
        (int(intent_id), _profile_scope(profile)),
    ).fetchone()
    return _public_intent(dict(row)) if row else None


def list_stated_intents(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    include_inactive: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_stated_intent_tables(conn)
    where = ["profile_id = ?"]
    params: list[Any] = [_profile_scope(profile)]
    if not include_inactive:
        where.append("status = 'active'")
    if subject_type:
        clean_subject_type = _enum(subject_type, SUBJECT_TYPES, "")
        if clean_subject_type:
            where.append("subject_type = ?")
            params.append(clean_subject_type)
    if subject_key:
        where.append("subject_key = ?")
        params.append(normalize_subject_key(subject_type or "category", subject_key))
    rows = conn.execute(
        f"""
        SELECT *
          FROM mira_stated_intents
         WHERE {' AND '.join(where)}
         ORDER BY updated_at DESC, id DESC
         LIMIT ?
        """,
        [*params, max(1, min(int(limit or 100), 500))],
    ).fetchall()
    return [_public_intent(dict(row)) for row in rows]


def update_stated_intent(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    intent_id: int,
    target_text: str | None = None,
    status: str | None = None,
    feedback_state: str | None = None,
) -> dict[str, Any] | None:
    ensure_stated_intent_tables(conn)
    existing = get_stated_intent(conn=conn, profile=profile, intent_id=intent_id)
    if not existing:
        return None
    updates = ["updated_at = datetime('now')"]
    params: list[Any] = []
    if target_text is not None:
        clean_text = _clean_text(target_text, 280)
        if not clean_text:
            raise ValueError("target_text is required.")
        updates.append("target_text = ?")
        params.append(clean_text)
    if status is not None:
        updates.append("status = ?")
        params.append(_enum(status, STATUS_STATES, existing["status"]))
    if feedback_state is not None:
        updates.append("feedback_state = ?")
        params.append(_enum(feedback_state, FEEDBACK_STATES, existing["feedback_state"]))
    params.extend([int(intent_id), _profile_scope(profile)])
    conn.execute(
        f"""
        UPDATE mira_stated_intents
           SET {', '.join(updates)}
         WHERE id = ?
           AND profile_id = ?
        """,
        params,
    )
    updated = get_stated_intent(conn=conn, profile=profile, intent_id=intent_id)
    if updated and updated.get("status") == "active":
        evaluate_stated_intent(conn=conn, profile=profile, intent_id=intent_id)
        updated = get_stated_intent(conn=conn, profile=profile, intent_id=intent_id)
    return updated


def clear_stated_intent(*, conn: sqlite3.Connection, profile: str | None, intent_id: int) -> dict[str, Any] | None:
    return update_stated_intent(conn=conn, profile=profile, intent_id=intent_id, status="dismissed")


def evaluate_stated_intent(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    intent_id: int,
    as_of: str | date | None = None,
) -> dict[str, Any] | None:
    ensure_stated_intent_tables(conn)
    intent = get_stated_intent(conn=conn, profile=profile, intent_id=intent_id)
    if not intent:
        return None
    evaluation = evaluate_subject_intent(conn=conn, profile=profile, intent=intent, as_of=as_of)
    conn.execute(
        """
        UPDATE mira_stated_intents
           SET last_evaluated_at = datetime('now'),
               evaluation_json = ?,
               updated_at = datetime('now')
         WHERE id = ?
           AND profile_id = ?
        """,
        (json.dumps(evaluation, ensure_ascii=True, sort_keys=True), int(intent_id), _profile_scope(profile)),
    )
    return get_stated_intent(conn=conn, profile=profile, intent_id=intent_id)


def evaluate_subject_intent(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    intent: dict[str, Any],
    as_of: str | date | None = None,
) -> dict[str, Any]:
    as_of_date = _parse_date(as_of)
    subject_type = str(intent.get("subject_type") or "")
    if subject_type not in {"merchant", "category"}:
        return {
            "status_label": "watch",
            "summary": f"{intent.get('subject_label') or 'This commitment'} is stored, but this subject does not have an automatic spend baseline yet.",
            "as_of": as_of_date.isoformat(),
            "baseline_scope": intent.get("baseline_scope") or BASELINE_SCOPE,
            "caveats": ["This commitment needs user review instead of automatic spend scoring."],
        }
    current_start = as_of_date.replace(day=1)
    current_days = max(1, (as_of_date - current_start).days + 1)
    month_days = calendar.monthrange(as_of_date.year, as_of_date.month)[1]
    baseline_start = _add_months(current_start, -3)
    baseline_end = current_start - timedelta(days=1)
    current = _spend_stats(conn, profile=profile, intent=intent, start=current_start, end=as_of_date)
    baseline = _spend_stats(conn, profile=profile, intent=intent, start=baseline_start, end=baseline_end)
    baseline_months = _month_count(baseline_start, baseline_end)
    baseline_amount = round(baseline["amount"] / baseline_months, 2) if baseline_months else 0.0
    baseline_count = round(baseline["count"] / baseline_months, 2) if baseline_months else 0.0
    projected_amount = round(current["amount"] / current_days * month_days, 2)
    projected_count = round(current["count"] / current_days * month_days, 2)
    label = str(intent.get("subject_label") or _subject_label(subject_type, intent.get("subject_key")) or "This area")
    status_label = _intent_status_label(str(intent.get("intent_kind") or ""), projected_amount, baseline_amount, current["amount"])
    summary = _evaluation_summary(label, str(intent.get("intent_kind") or ""), status_label, projected_amount, baseline_amount, current["amount"])
    return {
        "status_label": status_label,
        "summary": summary,
        "as_of": as_of_date.isoformat(),
        "period": {
            "current_start": current_start.isoformat(),
            "current_end": as_of_date.isoformat(),
            "baseline_start": baseline_start.isoformat(),
            "baseline_end": baseline_end.isoformat(),
        },
        "baseline_scope": intent.get("baseline_scope") or BASELINE_SCOPE,
        "numbers": {
            "current_month_to_date_amount": round(current["amount"], 2),
            "current_month_to_date_count": int(current["count"]),
            "projected_month_amount": projected_amount,
            "projected_month_count": projected_count,
            "baseline_monthly_amount": baseline_amount,
            "baseline_monthly_count": baseline_count,
        },
        "evidence": {
            "current_sample_transaction_ids": current["sample_ids"],
            "baseline_sample_transaction_ids": baseline["sample_ids"],
        },
        "caveats": _evaluation_caveats(current, baseline, baseline_months),
    }


def stated_intent_context_for_subject(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    subject_type: str | None,
    subject_key: str | None,
) -> dict[str, Any] | None:
    if not stated_intent_memory_enabled() or not subject_type or not subject_key:
        return None
    rows = list_stated_intents(
        conn=conn,
        profile=profile,
        subject_type=subject_type,
        subject_key=subject_key,
        include_inactive=False,
        limit=3,
    )
    for row in rows:
        if row.get("feedback_state") == "too_sensitive":
            continue
        evaluation = row.get("evaluation") if isinstance(row.get("evaluation"), dict) else {}
        if not evaluation:
            continue
        return {
            "family": "stated_intent",
            "kind": "commitment_status",
            "subject_type": row.get("subject_type"),
            "subject_key": row.get("subject_key"),
            "summary": evaluation.get("summary") or row.get("target_text"),
            "numbers": evaluation.get("numbers") if isinstance(evaluation.get("numbers"), dict) else {},
            "traits": [row.get("intent_kind"), evaluation.get("status_label")],
            "confidence": "medium",
            "sensitivity": "low",
            "target_text": row.get("target_text"),
            "status": row.get("status"),
            "last_evaluated_at": row.get("last_evaluated_at"),
        }
    return None


def normalize_subject_key(subject_type: str | None, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if str(subject_type or "").strip().lower() == "merchant":
        raw = display_from_key(canonicalize_merchant_key(raw)) or raw
    return _key(raw)


def _subject_candidates(conn: sqlite3.Connection, *, profile: str | None) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for row in _safe_fetch_all(
        conn,
        "SELECT name FROM categories WHERE COALESCE(is_active, 1) = 1",
        [],
    ):
        label = str(row.get("name") or "").strip()
        if label and label not in NON_SPENDING_CATEGORIES:
            candidates.append(_candidate("category", label, label))
    profile_where, params = _profile_where(profile, "profile_id")
    for row in _safe_fetch_all(
        conn,
        f"""
        SELECT DISTINCT
               COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, '')) AS merchant_key,
               COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, '')) AS merchant_label
          FROM transactions_visible
         WHERE amount < 0
           AND COALESCE(category, '') NOT IN ({','.join('?' for _ in NON_SPENDING_CATEGORIES)})
           {profile_where}
           AND TRIM(COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''))) != ''
         LIMIT 500
        """,
        [*NON_SPENDING_CATEGORIES, *params],
    ):
        key = canonicalize_merchant_key(row.get("merchant_key"))
        label = str(row.get("merchant_label") or display_from_key(key)).strip()
        if key:
            candidates.append(_candidate("merchant", key, label or display_from_key(key)))
    for row in _safe_fetch_all(
        conn,
        f"""
        SELECT DISTINCT merchant_key, COALESCE(NULLIF(clean_name, ''), merchant_key) AS merchant_label
          FROM merchants
         WHERE TRIM(COALESCE(merchant_key, '')) != ''
           {profile_where}
         LIMIT 500
        """,
        params,
    ):
        key = canonicalize_merchant_key(row.get("merchant_key"))
        label = str(row.get("merchant_label") or display_from_key(key)).strip()
        if key:
            candidates.append(_candidate("merchant", key, label or display_from_key(key)))
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for candidate in candidates:
        unique[(candidate["subject_type"], candidate["subject_key"], candidate["alias"])] = candidate
    return list(unique.values())


def _candidate(subject_type: str, key_source: str, label: str) -> dict[str, str]:
    subject_key = normalize_subject_key(subject_type, key_source)
    subject_label = _clean_text(label, 120) or _subject_label(subject_type, subject_key)
    alias_values = {subject_label, key_source, subject_key.replace("_", " ")}
    best_alias = max((_matchable_text(value) for value in alias_values), key=len, default="")
    return {
        "subject_type": subject_type,
        "subject_key": subject_key,
        "subject_label": subject_label,
        "alias": best_alias,
    }


def _spend_stats(
    conn: sqlite3.Connection,
    *,
    profile: str | None,
    intent: dict[str, Any],
    start: date,
    end: date,
) -> dict[str, Any]:
    profile_where, params = _profile_where(profile, "profile_id")
    rows = _safe_fetch_all(
        conn,
        f"""
        SELECT id, date, amount, category, expense_type, merchant_key, merchant_name, description
          FROM transactions_visible
         WHERE date >= ?
           AND date <= ?
           AND amount < 0
           {profile_where}
        """,
        [start.isoformat(), end.isoformat(), *params],
    )
    amount = 0.0
    count = 0
    sample_ids: list[str] = []
    for row in rows:
        if not _row_matches_intent(row, intent):
            continue
        amount += abs(float(row.get("amount") or 0))
        count += 1
        if len(sample_ids) < 5:
            sample_ids.append(str(row.get("id") or ""))
    return {"amount": round(amount, 2), "count": count, "sample_ids": [item for item in sample_ids if item]}


def _row_matches_intent(row: dict[str, Any], intent: dict[str, Any]) -> bool:
    category = str(row.get("category") or "").strip()
    expense_type = str(row.get("expense_type") or "").strip()
    if category in NON_SPENDING_CATEGORIES or expense_type in TRANSFER_EXPENSE_TYPES:
        return False
    subject_type = str(intent.get("subject_type") or "")
    subject_key = str(intent.get("subject_key") or "")
    if subject_type == "category":
        return normalize_subject_key("category", category) == subject_key
    if subject_type == "merchant":
        key = canonicalize_merchant_key(row.get("merchant_key") or row.get("merchant_name") or row.get("description"))
        return normalize_subject_key("merchant", key) == subject_key
    return False


def _intent_status_label(intent_kind: str, projected: float, baseline: float, current: float) -> str:
    if current <= 0 and intent_kind in {"cut", "avoid"}:
        return "tracking"
    if baseline <= 0:
        return "watch"
    ratio = projected / baseline if baseline else 0.0
    if intent_kind in {"cut", "avoid"}:
        if ratio <= 0.85:
            return "tracking"
        if ratio <= 1.05:
            return "holding"
        return "watch"
    if intent_kind in {"increase", "grow"}:
        return "tracking" if ratio >= 1.1 else "watch"
    if ratio <= 1.15:
        return "holding"
    return "watch"


def _evaluation_summary(label: str, intent_kind: str, status_label: str, projected: float, baseline: float, current: float) -> str:
    if baseline <= 0 and current <= 0:
        return f"{label} has no matched spend in the current period yet, so Mira is treating this as something to watch quietly."
    if status_label == "tracking" and intent_kind in {"cut", "avoid"}:
        return f"{label} is below its usual monthly pace: ${projected:,.2f} projected versus a ${baseline:,.2f} monthly baseline."
    if status_label == "holding":
        return f"{label} is near its usual monthly pace: ${projected:,.2f} projected versus a ${baseline:,.2f} monthly baseline."
    if intent_kind in {"cut", "avoid"}:
        return f"{label} is running above the target direction: ${projected:,.2f} projected versus a ${baseline:,.2f} monthly baseline. Treat it as a check-in, not a scolding."
    return f"{label} is at ${current:,.2f} month-to-date, with a ${projected:,.2f} projected month against a ${baseline:,.2f} baseline."


def _evaluation_caveats(current: dict[str, Any], baseline: dict[str, Any], baseline_months: int) -> list[str]:
    caveats: list[str] = []
    if baseline["count"] <= 0:
        caveats.append("No prior matched spend baseline was found.")
    if baseline_months < 3:
        caveats.append("Baseline has fewer than three complete months.")
    if current["count"] <= 0:
        caveats.append("No matched current-month transactions yet.")
    return caveats[:3]


def _find_existing_intent(conn: sqlite3.Connection, profile: str, memory_id: int | None) -> dict[str, Any] | None:
    if memory_id is None:
        return None
    row = conn.execute(
        """
        SELECT *
          FROM mira_stated_intents
         WHERE profile_id = ?
           AND memory_id = ?
         LIMIT 1
        """,
        (profile, memory_id),
    ).fetchone()
    return _public_intent(dict(row)) if row else None


def _public_intent(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "evaluation": _json_load(row.get("evaluation_json"), {}),
    }


def _safe_fetch_all(conn: sqlite3.Connection, query: str, params: list[Any]) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    except Exception:
        return []


def _profile_where(profile: str | None, column: str) -> tuple[str, list[Any]]:
    scope = _profile_scope(profile)
    if not scope or scope == "household":
        return "", []
    return f"AND {column} = ?", [scope]


def _profile_scope(profile: str | None) -> str:
    return str(profile or "household").strip() or "household"


def _enum(value: Any, allowed: set[str], default: str) -> str:
    text = _key(value)
    return text if text in allowed else default


def _key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:100]


def _matchable_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _subject_label(subject_type: str | None, subject_key: Any) -> str:
    if str(subject_type or "").strip().lower() == "merchant":
        return display_from_key(canonicalize_merchant_key(str(subject_key or ""))) or str(subject_key or "")
    return " ".join(str(subject_key or "").replace("_", " ").split()).title()


def _parse_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return date.today()


def _add_months(value: date, months: int) -> date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _month_count(start: date, end: date) -> int:
    if end < start:
        return 0
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _json_load(raw: Any, default: Any) -> Any:
    try:
        parsed = json.loads(raw or "")
    except Exception:
        return default
    return parsed if parsed is not None else default
