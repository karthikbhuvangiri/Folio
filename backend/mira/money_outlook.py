"""Deterministic month-ahead outlook snapshots for Mira.

Phase 29 keeps projection math in Python. LLMs may later narrate the stored
snapshot, but they do not calculate balances, totals, percentages, or dates.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from mira.safe_finance_query import NON_SPENDING_CATEGORIES, execute_safe_finance_queries


MONEY_OUTLOOK_VERSION = "mira_money_outlook_v1"
CONFIDENCE_STATES = {"high", "medium", "low"}
BUFFER_STATES = {"above_buffer", "near_buffer", "below_buffer", "unknown"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def money_outlook_enabled() -> bool:
    return os.getenv("MIRA_MONEY_OUTLOOK_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def safe_to_spend_enabled() -> bool:
    return os.getenv("MIRA_SAFE_TO_SPEND_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def cash_low_point_radar_enabled() -> bool:
    return os.getenv("MIRA_CASH_LOW_POINT_RADAR_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def ensure_money_outlook_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_outlook_snapshots (
            id                                      INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id                              TEXT DEFAULT NULL,
            generated_at                            TEXT NOT NULL,
            valid_until                             TEXT NOT NULL,
            month_key                               TEXT NOT NULL,
            projected_end_balance                   REAL DEFAULT NULL,
            projected_income_remaining              REAL NOT NULL DEFAULT 0,
            projected_obligations_remaining         REAL NOT NULL DEFAULT 0,
            projected_flexible_spend_remaining      REAL NOT NULL DEFAULT 0,
            projected_savings_delta                 REAL DEFAULT NULL,
            target_savings_amount                   REAL NOT NULL DEFAULT 0,
            safe_to_spend_today                     REAL DEFAULT NULL,
            safe_to_spend_this_week                 REAL DEFAULT NULL,
            buffer_amount                           REAL DEFAULT NULL,
            buffer_status                           TEXT NOT NULL DEFAULT 'unknown',
            low_point_date                          TEXT DEFAULT NULL,
            low_point_amount                        REAL DEFAULT NULL,
            buffer_breach                           INTEGER NOT NULL DEFAULT 0,
            low_point_drivers_json                  TEXT NOT NULL DEFAULT '[]',
            confidence                              TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
            drivers_json                            TEXT NOT NULL DEFAULT '[]',
            caveats_json                            TEXT NOT NULL DEFAULT '[]',
            evidence_json                           TEXT NOT NULL DEFAULT '{}',
            fingerprint                             TEXT NOT NULL,
            UNIQUE(profile_id, month_key, fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_outlook_snapshots_profile_month
            ON mira_outlook_snapshots(profile_id, month_key, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_outlook_snapshots_valid_until
            ON mira_outlook_snapshots(valid_until);
        """
    )
    _ensure_column(conn, "mira_outlook_snapshots", "safe_to_spend_today", "REAL DEFAULT NULL")
    _ensure_column(conn, "mira_outlook_snapshots", "safe_to_spend_this_week", "REAL DEFAULT NULL")
    _ensure_column(conn, "mira_outlook_snapshots", "buffer_amount", "REAL DEFAULT NULL")
    _ensure_column(conn, "mira_outlook_snapshots", "buffer_status", "TEXT NOT NULL DEFAULT 'unknown'")
    _ensure_column(conn, "mira_outlook_snapshots", "low_point_date", "TEXT DEFAULT NULL")
    _ensure_column(conn, "mira_outlook_snapshots", "low_point_amount", "REAL DEFAULT NULL")
    _ensure_column(conn, "mira_outlook_snapshots", "buffer_breach", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "mira_outlook_snapshots", "low_point_drivers_json", "TEXT NOT NULL DEFAULT '[]'")


def build_money_outlook_snapshot(
    conn: sqlite3.Connection,
    *,
    profile: str | None = None,
    as_of: date | datetime | str | None = None,
) -> dict[str, Any]:
    """Build a deterministic current-month projection from safe measurements."""

    scope = _profile_scope(profile)
    as_of_date = _coerce_date(as_of) or date.today()
    month_start = as_of_date.replace(day=1)
    month_end = _month_end(month_start)
    month_key = month_start.strftime("%Y-%m")
    remaining_days = max((month_end - as_of_date).days, 0)

    metrics = _safe_metric_map(conn, profile=scope, as_of=as_of_date)
    mtd = _current_month_actuals(conn, profile=scope, start=month_start, end=as_of_date)

    money_flow = _summary(metrics, "money_flow_baseline")
    operating = _summary(metrics, "monthly_operating_statement")
    goal_capacity = _summary(metrics, "goal_capacity_statement")
    cash = _summary(metrics, "cash_balance_series")
    income_continuity = _summary(metrics, "income_source_continuity")

    avg_income = _num(money_flow.get("avg_monthly_income"))
    normal_spend = _num(money_flow.get("avg_monthly_spend_after_event_exclusions"))
    flexible_monthly = _num(money_flow.get("flexible_monthly_estimate"))
    reviewable_monthly = _num(money_flow.get("reviewable_monthly_estimate"))
    discretionary_monthly = _round(flexible_monthly + reviewable_monthly)
    baseline_month_count = int(_num(money_flow.get("baseline_month_count")) or 0)
    if not (flexible_monthly or reviewable_monthly) and normal_spend:
        flexible_monthly = normal_spend

    current_flexible_mtd = _current_flexible_mtd(metrics.get("money_flow_baseline", {}))
    flexible_baseline = _round(flexible_monthly + reviewable_monthly)
    discretionary_remaining = _round(max(discretionary_monthly - current_flexible_mtd, 0.0))
    projected_flexible_remaining = _round(max(flexible_baseline - current_flexible_mtd, 0.0))

    projected_income_remaining = _round(max(avg_income - _num(mtd.get("income")), 0.0))
    projected_obligations_remaining = _round(_remaining_obligations(metrics.get("recurring_obligation_calendar", {}), as_of_date, month_end))

    target_savings = _round(_num(goal_capacity.get("required_goal_contribution_monthly")))
    expected_month_income = _round(_num(mtd.get("income")) + projected_income_remaining)
    expected_month_outflow = _round(_num(mtd.get("spend")) + projected_obligations_remaining + projected_flexible_remaining)
    projected_savings_delta = _round(expected_month_income - expected_month_outflow - target_savings)

    cash_like_balance = _num(cash.get("cash_like_balance"))
    projected_end_balance = _round(cash_like_balance + projected_income_remaining - projected_obligations_remaining - projected_flexible_remaining)

    caveats = _build_caveats(
        metrics=metrics,
        avg_income=avg_income,
        baseline_month_count=baseline_month_count,
        target_savings=target_savings,
        cash_like_balance=cash_like_balance,
        income_continuity=income_continuity,
        as_of_date=as_of_date,
    )
    confidence = _confidence(
        avg_income=avg_income,
        baseline_month_count=baseline_month_count,
        cash_like_balance=cash_like_balance,
        caveats=caveats,
        as_of_date=as_of_date,
    )
    low_point = _cash_low_point_projection(
        metrics=metrics,
        cash_like_balance=cash_like_balance,
        normal_spend=normal_spend,
        projected_income_remaining=projected_income_remaining,
        projected_obligations_remaining=projected_obligations_remaining,
        projected_flexible_remaining=discretionary_remaining,
        confidence=confidence,
        as_of_date=as_of_date,
        month_end=month_end,
    )
    safe_to_spend = _safe_to_spend_projection(
        projected_savings_delta=projected_savings_delta,
        projected_flexible_remaining=discretionary_remaining,
        low_point=low_point,
        confidence=confidence,
        caveats=caveats,
        as_of_date=as_of_date,
        month_end=month_end,
    )
    drivers = _build_drivers(
        metrics=metrics,
        projected_savings_delta=projected_savings_delta,
        projected_obligations_remaining=projected_obligations_remaining,
        projected_income_remaining=projected_income_remaining,
        mtd=mtd,
        as_of_date=as_of_date,
        month_end=month_end,
    )
    generated_at = _now_iso()
    valid_until = _valid_until(as_of_date)
    evidence = {
        "version": MONEY_OUTLOOK_VERSION,
        "as_of_date": as_of_date.isoformat(),
        "remaining_days": remaining_days,
        "summary": {
            "cash_like_balance": _round(cash_like_balance),
            "mtd_income": _round(mtd.get("income")),
            "mtd_spend": _round(mtd.get("spend")),
            "expected_month_income": expected_month_income,
            "expected_month_outflow": expected_month_outflow,
            "avg_monthly_income": _round(avg_income),
            "normal_monthly_spend": _round(normal_spend),
            "flexible_baseline": flexible_baseline,
            "current_flexible_mtd": _round(current_flexible_mtd),
            "discretionary_remaining": discretionary_remaining,
            "operating_capacity_before_goals": _round(operating.get("capacity_before_configured_goals")),
            "baseline_month_count": baseline_month_count,
            "safe_to_spend_today": safe_to_spend["safe_to_spend_today"],
            "safe_to_spend_this_week": safe_to_spend["safe_to_spend_this_week"],
            "buffer_amount": low_point["buffer_amount"],
            "buffer_status": low_point["buffer_status"],
            "low_point_date": low_point["low_point_date"],
            "low_point_amount": low_point["low_point_amount"],
            "buffer_breach": low_point["buffer_breach"],
        },
        "metrics": {
            name: {
                "confidence": metric.get("confidence"),
                "summary_numbers": metric.get("summary_numbers") or {},
                "evidence_ids": metric.get("evidence_ids") or [],
            }
            for name, metric in metrics.items()
        },
    }

    fingerprint = _fingerprint(
        {
            "version": MONEY_OUTLOOK_VERSION,
            "profile_id": scope,
            "month_key": month_key,
            "as_of_date": as_of_date.isoformat(),
            "projected_end_balance": projected_end_balance,
            "projected_income_remaining": projected_income_remaining,
            "projected_obligations_remaining": projected_obligations_remaining,
            "projected_flexible_spend_remaining": projected_flexible_remaining,
            "projected_savings_delta": projected_savings_delta,
            "target_savings_amount": target_savings,
            "safe_to_spend": safe_to_spend,
            "low_point": low_point,
            "drivers": drivers,
            "caveats": caveats,
            "evidence_summary": evidence["summary"],
        }
    )

    return {
        "version": MONEY_OUTLOOK_VERSION,
        "profile_id": scope,
        "generated_at": generated_at,
        "valid_until": valid_until,
        "month_key": month_key,
        "projected_end_balance": projected_end_balance,
        "projected_income_remaining": projected_income_remaining,
        "projected_obligations_remaining": projected_obligations_remaining,
        "projected_flexible_spend_remaining": projected_flexible_remaining,
        "projected_savings_delta": projected_savings_delta,
        "target_savings_amount": target_savings,
        "safe_to_spend_today": safe_to_spend["safe_to_spend_today"],
        "safe_to_spend_this_week": safe_to_spend["safe_to_spend_this_week"],
        "safe_to_spend_top_caveat": safe_to_spend["top_caveat"],
        "buffer_amount": low_point["buffer_amount"],
        "buffer_status": low_point["buffer_status"],
        "low_point_date": low_point["low_point_date"],
        "low_point_amount": low_point["low_point_amount"],
        "buffer_breach": low_point["buffer_breach"],
        "low_point_drivers": low_point["low_point_drivers"],
        "confidence": confidence,
        "drivers": drivers,
        "caveats": caveats,
        "evidence": evidence,
        "fingerprint": fingerprint,
    }


def store_money_outlook_snapshot(
    conn: sqlite3.Connection,
    *,
    profile: str | None = None,
    as_of: date | datetime | str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_money_outlook_tables(conn)
    payload = snapshot or build_money_outlook_snapshot(conn, profile=profile, as_of=as_of)
    scope = _profile_scope(payload.get("profile_id") or profile)
    conn.execute(
        """
        INSERT OR IGNORE INTO mira_outlook_snapshots (
            profile_id, generated_at, valid_until, month_key, projected_end_balance,
            projected_income_remaining, projected_obligations_remaining,
            projected_flexible_spend_remaining, projected_savings_delta,
            target_savings_amount, safe_to_spend_today, safe_to_spend_this_week,
            buffer_amount, buffer_status, low_point_date, low_point_amount,
            buffer_breach, low_point_drivers_json, confidence, drivers_json,
            caveats_json, evidence_json, fingerprint
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scope,
            str(payload.get("generated_at") or _now_iso()),
            str(payload.get("valid_until") or _valid_until(_coerce_date(as_of) or date.today())),
            str(payload.get("month_key") or ""),
            _nullable_num(payload.get("projected_end_balance")),
            _round(payload.get("projected_income_remaining")),
            _round(payload.get("projected_obligations_remaining")),
            _round(payload.get("projected_flexible_spend_remaining")),
            _nullable_num(payload.get("projected_savings_delta")),
            _round(payload.get("target_savings_amount")),
            _nullable_num(payload.get("safe_to_spend_today")),
            _nullable_num(payload.get("safe_to_spend_this_week")),
            _nullable_num(payload.get("buffer_amount")),
            _buffer_status_value(payload.get("buffer_status")),
            payload.get("low_point_date"),
            _nullable_num(payload.get("low_point_amount")),
            1 if payload.get("buffer_breach") else 0,
            _json(payload.get("low_point_drivers") or []),
            _confidence_value(payload.get("confidence")),
            _json(payload.get("drivers") or []),
            _json(payload.get("caveats") or []),
            _json(payload.get("evidence") or {}),
            str(payload.get("fingerprint") or _fingerprint(payload)),
        ),
    )
    conn.commit()
    stored = load_money_outlook_snapshot_by_fingerprint(
        conn,
        profile=scope,
        month_key=str(payload.get("month_key") or ""),
        fingerprint=str(payload.get("fingerprint") or _fingerprint(payload)),
    )
    return stored or payload


def load_latest_money_outlook_snapshot(
    conn: sqlite3.Connection,
    *,
    profile: str | None = None,
    month_key: str | None = None,
    as_of: date | datetime | str | None = None,
    include_stale: bool = False,
) -> dict[str, Any] | None:
    ensure_money_outlook_tables(conn)
    scope = _profile_scope(profile)
    today = _coerce_date(as_of) or date.today()
    wanted_month = month_key or today.strftime("%Y-%m")
    where = ["profile_id = ?", "month_key = ?"]
    params: list[Any] = [scope, wanted_month]
    if not include_stale:
        where.append("valid_until >= ?")
        params.append(today.isoformat())
    row = conn.execute(
        f"""
        SELECT *
          FROM mira_outlook_snapshots
         WHERE {' AND '.join(where)}
         ORDER BY generated_at DESC, id DESC
         LIMIT 1
        """,
        params,
    ).fetchone()
    return _public_row(dict(row)) if row else None


def money_outlook_needs_refresh(
    conn: sqlite3.Connection,
    *,
    profile: str | None = None,
    month_key: str | None = None,
    as_of: date | datetime | str | None = None,
) -> bool:
    return load_latest_money_outlook_snapshot(conn, profile=profile, month_key=month_key, as_of=as_of) is None


def mark_money_outlook_snapshots_stale(
    conn: sqlite3.Connection,
    *,
    profile: str | None = None,
    month_key: str | None = None,
    as_of: date | datetime | str | None = None,
) -> int:
    ensure_money_outlook_tables(conn)
    scope = _profile_scope(profile)
    as_of_date = _coerce_date(as_of)
    today = as_of_date or date.today()
    stale_until = (as_of_date - timedelta(days=1)).isoformat() if as_of_date else "0001-01-01"
    where = ["profile_id = ?"]
    params: list[Any] = [scope]
    if month_key:
        where.append("month_key = ?")
        params.append(str(month_key))
    cur = conn.execute(
        f"""
        UPDATE mira_outlook_snapshots
           SET valid_until = ?
         WHERE {' AND '.join(where)}
           AND valid_until >= ?
        """,
        [stale_until, *params, today.isoformat()],
    )
    conn.commit()
    return int(cur.rowcount or 0)


def load_money_outlook_snapshot_by_fingerprint(
    conn: sqlite3.Connection,
    *,
    profile: str | None,
    month_key: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    ensure_money_outlook_tables(conn)
    row = conn.execute(
        """
        SELECT *
          FROM mira_outlook_snapshots
         WHERE profile_id = ?
           AND month_key = ?
           AND fingerprint = ?
         ORDER BY generated_at DESC, id DESC
         LIMIT 1
        """,
        (_profile_scope(profile), month_key, fingerprint),
    ).fetchone()
    return _public_row(dict(row)) if row else None


def _safe_metric_map(conn: sqlite3.Connection, *, profile: str, as_of: date) -> dict[str, dict[str, Any]]:
    queries = [
        {"metric": "money_flow_baseline", "range": "last_6_months", "limit": 24},
        {"metric": "monthly_operating_statement", "range": "last_6_months", "limit": 16},
        {"metric": "goal_capacity_statement", "range": "last_6_months", "limit": 20},
        {"metric": "cash_balance_series", "range": "current_month", "limit": 20},
        {"metric": "recurring_obligation_calendar", "range": "current_month", "limit": 40},
        {"metric": "income_cadence", "range": "last_6_months", "limit": 12},
        {"metric": "income_source_continuity", "range": "last_6_months", "limit": 12},
        {"metric": "avoidable_leakage", "range": "current_month", "limit": 12},
    ]
    result = execute_safe_finance_queries(conn, {"queries": queries}, profile=profile, as_of=as_of.isoformat())
    return {str(metric.get("metric")): metric for metric in result.get("results") or []}


def _current_month_actuals(conn: sqlite3.Connection, *, profile: str, start: date, end: date) -> dict[str, Any]:
    categories = ",".join("?" for _ in NON_SPENDING_CATEGORIES)
    row = conn.execute(
        f"""
        SELECT ROUND(COALESCE(SUM(CASE WHEN category = 'Income' AND amount > 0 THEN amount ELSE 0 END), 0), 2) AS income,
               ROUND(COALESCE(SUM(CASE WHEN amount < 0 AND COALESCE(category, '') NOT IN ({categories}) THEN ABS(amount) ELSE 0 END), 0), 2) AS spend,
               COUNT(*) AS transaction_count,
               GROUP_CONCAT(CASE WHEN amount < 0 AND COALESCE(category, '') NOT IN ({categories}) THEN id ELSE NULL END) AS spend_ids
          FROM transactions_visible
         WHERE profile_id = ?
           AND date >= ?
           AND date <= ?
        """,
        [*NON_SPENDING_CATEGORIES, *NON_SPENDING_CATEGORIES, profile, start.isoformat(), end.isoformat()],
    ).fetchone()
    data = dict(row) if row else {}
    evidence_ids = [f"txn:{value}" for value in str(data.get("spend_ids") or "").split(",")[:12] if value]
    return {
        "income": _round(data.get("income")),
        "spend": _round(data.get("spend")),
        "transaction_count": int(_num(data.get("transaction_count"))),
        "evidence_ids": evidence_ids,
    }


def _current_flexible_mtd(money_flow_metric: dict[str, Any]) -> float:
    total = 0.0
    for row in money_flow_metric.get("rows") or []:
        role = str(row.get("spend_role") or "")
        if role in {"structural_floor", "tax_or_irregular", "event_or_irregular"}:
            continue
        total += _num(row.get("current_month_total"))
    return _round(total)


def _remaining_obligations(metric: dict[str, Any], as_of: date, month_end: date) -> float:
    total = 0.0
    for row in metric.get("rows") or []:
        due = _coerce_date(row.get("next_expected_date"))
        if due and as_of < due <= month_end:
            total += _num(row.get("amount"))
    return _round(total)


def _cash_low_point_projection(
    *,
    metrics: dict[str, dict[str, Any]],
    cash_like_balance: float,
    normal_spend: float,
    projected_income_remaining: float,
    projected_obligations_remaining: float,
    projected_flexible_remaining: float,
    confidence: str,
    as_of_date: date,
    month_end: date,
) -> dict[str, Any]:
    buffer_amount = _round(normal_spend) if normal_spend > 0 else None
    if cash_like_balance <= 0 or confidence == "low":
        return {
            "low_point_date": None,
            "low_point_amount": None,
            "buffer_amount": buffer_amount,
            "buffer_status": "unknown",
            "buffer_breach": False,
            "low_point_drivers": [],
        }

    days_left = max((month_end - as_of_date).days + 1, 1)
    daily_flexible = _round(projected_flexible_remaining / days_left, 4) if projected_flexible_remaining > 0 else 0.0
    events = _cash_projection_events(
        metrics=metrics,
        projected_income_remaining=projected_income_remaining,
        as_of_date=as_of_date,
        month_end=month_end,
    )

    balance = _round(cash_like_balance)
    low_amount = balance
    low_date = as_of_date
    dated_drivers: list[dict[str, Any]] = []
    for offset in range(days_left):
        day = as_of_date + timedelta(days=offset)
        if offset > 0 and daily_flexible > 0:
            balance = _round(balance - daily_flexible)
        for event in events.get(day.isoformat(), []):
            balance = _round(balance + _num(event.get("signed_amount")))
            if _num(event.get("signed_amount")) < 0:
                dated_drivers.append(
                    {
                        "kind": str(event.get("kind") or "cash_out"),
                        "subject": str(event.get("subject") or "scheduled outflow"),
                        "amount": abs(_round(event.get("signed_amount"))),
                        "date": day.isoformat(),
                    }
                )
        if balance < low_amount:
            low_amount = balance
            low_date = day

    status = _cash_buffer_status(low_amount, buffer_amount)
    if daily_flexible > 0 and low_date > as_of_date:
        dated_drivers.append(
            {
                "kind": "projected_flexible_spend",
                "subject": "planned flexible spend",
                "amount": _round(daily_flexible * (low_date - as_of_date).days),
                "date": low_date.isoformat(),
            }
        )
    if projected_income_remaining > 0 and not any(
        event.get("kind") == "expected_income" and _coerce_date(date_key) <= low_date
        for date_key, event_rows in events.items()
        for event in event_rows
    ):
        dated_drivers.append(
            {
                "kind": "income_timing",
                "subject": "expected income arrives after the low point",
                "amount": _round(projected_income_remaining),
                "date": low_date.isoformat(),
            }
        )
    return {
        "low_point_date": low_date.isoformat(),
        "low_point_amount": _round(low_amount),
        "buffer_amount": buffer_amount,
        "buffer_status": status,
        "buffer_breach": status == "below_buffer",
        "low_point_drivers": sorted(dated_drivers, key=lambda row: _num(row.get("amount")), reverse=True)[:5],
    }


def _cash_projection_events(
    *,
    metrics: dict[str, dict[str, Any]],
    projected_income_remaining: float,
    as_of_date: date,
    month_end: date,
) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    for row in (metrics.get("recurring_obligation_calendar") or {}).get("rows") or []:
        due = _coerce_date(row.get("next_expected_date"))
        amount = _num(row.get("amount"))
        subject = _clean_subject(row.get("merchant") or row.get("category")) or "scheduled obligation"
        if due and as_of_date < due <= month_end and amount > 0:
            events.setdefault(due.isoformat(), []).append(
                {"kind": "scheduled_obligation", "subject": subject, "signed_amount": -amount}
            )

    income_date = _projected_income_date(metrics.get("income_cadence") or {}, as_of_date, month_end)
    if projected_income_remaining > 0 and income_date:
        events.setdefault(income_date.isoformat(), []).append(
            {"kind": "expected_income", "subject": "expected income", "signed_amount": projected_income_remaining}
        )
    return events


def _projected_income_date(metric: dict[str, Any], as_of_date: date, month_end: date) -> date | None:
    summary = dict(metric.get("summary_numbers") or {})
    rows = metric.get("rows") or []
    dates = sorted(_coerce_date(row.get("date")) for row in rows if _coerce_date(row.get("date")))
    if not dates:
        return month_end
    if summary.get("late_against_cadence"):
        return month_end
    gap = int(round(_num(summary.get("median_gap_days")))) if _num(summary.get("median_gap_days")) > 0 else 0
    candidate = dates[-1] + timedelta(days=gap or 30)
    if candidate <= as_of_date:
        candidate = as_of_date + timedelta(days=1)
    if candidate > month_end:
        candidate = month_end
    return candidate


def _safe_to_spend_projection(
    *,
    projected_savings_delta: float,
    projected_flexible_remaining: float,
    low_point: dict[str, Any],
    confidence: str,
    caveats: list[str],
    as_of_date: date,
    month_end: date,
) -> dict[str, Any]:
    if confidence == "low":
        return {
            "safe_to_spend_today": 0.0,
            "safe_to_spend_this_week": 0.0,
            "buffer_status": "unknown",
            "next_pressure_date": low_point.get("low_point_date"),
            "top_caveat": caveats[0] if caveats else "Projection confidence is low; safe-to-spend is paused until income and cash data are clearer.",
        }

    days_left = max((month_end - as_of_date).days + 1, 1)
    monthly_room = projected_flexible_remaining + projected_savings_delta
    if projected_savings_delta > 0:
        monthly_room = projected_flexible_remaining + projected_savings_delta
    spend_pool = max(monthly_room, 0.0)
    low_amount = low_point.get("low_point_amount")
    buffer_amount = low_point.get("buffer_amount")
    if low_amount is not None and buffer_amount:
        spend_pool = min(spend_pool, max(_num(low_amount) - _num(buffer_amount), 0.0))

    today = _round(spend_pool / days_left)
    week_days = min(7, days_left)
    week = _round(spend_pool * week_days / days_left)
    buffer_status = _buffer_status_value(low_point.get("buffer_status"))
    if buffer_status in {"below_buffer", "near_buffer"}:
        top_caveat = "Cash is projected close to the buffer before month end; keep extra spending conservative until that date passes."
    elif caveats:
        top_caveat = caveats[0]
    else:
        top_caveat = "Known bills and normal flexible spending only; transfers or uncategorized changes may move this."
    return {
        "safe_to_spend_today": today,
        "safe_to_spend_this_week": week,
        "buffer_status": buffer_status,
        "next_pressure_date": low_point.get("low_point_date"),
        "top_caveat": top_caveat,
    }


def _build_drivers(
    *,
    metrics: dict[str, dict[str, Any]],
    projected_savings_delta: float,
    projected_obligations_remaining: float,
    projected_income_remaining: float,
    mtd: dict[str, Any],
    as_of_date: date,
    month_end: date,
) -> list[dict[str, Any]]:
    drivers: list[dict[str, Any]] = []
    if projected_savings_delta < 0:
        drivers.append(
            _driver(
                "savings_gap",
                "current_month",
                abs(projected_savings_delta),
                ["metric:money_outlook:projection"],
                "Projected month-end savings is below the configured target.",
            )
        )

    money_flow = metrics.get("money_flow_baseline", {})
    for row in money_flow.get("rows") or []:
        role = str(row.get("spend_role") or "")
        if role in {"structural_floor", "tax_or_irregular", "event_or_irregular"}:
            continue
        subject = _clean_subject(row.get("category"))
        delta = _num(row.get("current_delta_vs_baseline"))
        monthly_average = _num(row.get("monthly_average"))
        if not subject or delta <= max(25.0, monthly_average * 0.12):
            continue
        drivers.append(
            _driver(
                "spend_pressure",
                subject,
                delta,
                row.get("sample_evidence_ids") or ["metric:money_flow_baseline:category"],
                f"{subject} is running above its normal monthly baseline.",
            )
        )

    recurring = metrics.get("recurring_obligation_calendar", {})
    for row in recurring.get("rows") or []:
        due = _coerce_date(row.get("next_expected_date"))
        subject = _clean_subject(row.get("merchant") or row.get("category"))
        if not due or not subject or not (as_of_date < due <= month_end):
            continue
        drivers.append(
            _driver(
                "upcoming_obligation",
                subject,
                _num(row.get("amount")),
                ["metric:recurring_obligation_calendar"],
                f"{subject} is expected before month end.",
                due_date=due.isoformat(),
            )
        )

    leakage = metrics.get("avoidable_leakage", {})
    for row in leakage.get("rows") or []:
        subject = _clean_subject(row.get("subject") or row.get("merchant") or row.get("category"))
        amount = _num(row.get("monthly_recovery_estimate") or row.get("measured_amount"))
        if not subject or amount <= 0:
            continue
        drivers.append(
            _driver(
                "avoidable_leakage",
                subject,
                amount,
                row.get("sample_evidence_ids") or ["metric:avoidable_leakage"],
                "This looks reviewable before lifestyle cuts.",
            )
        )

    if projected_obligations_remaining > 0:
        drivers.append(
            _driver(
                "remaining_obligations",
                "remaining recurring stack",
                projected_obligations_remaining,
                ["metric:recurring_obligation_calendar:summary"],
                "Known recurring obligations still remain this month.",
            )
        )
    if projected_income_remaining > 0:
        drivers.append(
            _driver(
                "income_remaining",
                "expected income",
                projected_income_remaining,
                ["metric:money_flow_baseline:summary"],
                "The projection assumes normal remaining income arrives.",
            )
        )
    if _num(mtd.get("spend")) > 0:
        drivers.append(
            _driver(
                "mtd_spend",
                "month-to-date spending",
                _num(mtd.get("spend")),
                mtd.get("evidence_ids") or ["metric:money_outlook:mtd_spend"],
                "Month-to-date spending is already included in the projection.",
            )
        )

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in drivers:
        deduped[(item["kind"], item["subject"])] = item
    return sorted(deduped.values(), key=lambda item: _num(item.get("amount")), reverse=True)[:8]


def _build_caveats(
    *,
    metrics: dict[str, dict[str, Any]],
    avg_income: float,
    baseline_month_count: int,
    target_savings: float,
    cash_like_balance: float,
    income_continuity: dict[str, Any],
    as_of_date: date,
) -> list[str]:
    caveats: list[str] = []
    if baseline_month_count < 3:
        caveats.append("Projection is based on fewer than three reliable baseline months.")
    if avg_income <= 0:
        caveats.append("Income remaining cannot be projected from the imported history yet.")
    if target_savings <= 0:
        caveats.append("No active monthly savings target is configured, so savings delta is against zero target.")
    if cash_like_balance <= 0:
        caveats.append("No active cash-like account balance was available for projected end balance.")
    income_cadence = _summary(metrics, "income_cadence")
    if income_cadence.get("late_against_cadence"):
        caveats.append("Expected income appears late against the observed cadence; treat remaining income as unverified until it lands.")
    status = str(income_continuity.get("status") or "")
    if status in {"unlabeled_or_changed_source", "changed_source", "unlabeled_source"}:
        caveats.append("Recent income source labeling changed or is incomplete; verify income continuity before relying on the projection.")
    if as_of_date.day < 7:
        caveats.append("The month is still early, so pacing can swing quickly.")
    for metric in metrics.values():
        for caveat in metric.get("caveats") or []:
            text = str(caveat or "").strip()
            if text and text not in caveats:
                caveats.append(text)
    return caveats[:8]


def _confidence(
    *,
    avg_income: float,
    baseline_month_count: int,
    cash_like_balance: float,
    caveats: list[str],
    as_of_date: date,
) -> str:
    if avg_income <= 0 or baseline_month_count < 2 or cash_like_balance <= 0:
        return "low"
    if any("income appears late" in caveat.lower() for caveat in caveats):
        return "medium"
    if baseline_month_count >= 3 and as_of_date.day >= 7 and len(caveats) <= 3:
        return "high"
    return "medium"


def _driver(kind: str, subject: str, amount: float, evidence_ids: list[Any], rationale: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "subject": _clean_subject(subject) or "current_month",
        "amount": _round(amount),
        "evidence_ids": [str(item) for item in (evidence_ids or [])[:8] if item],
        "rationale": rationale,
        **{key: value for key, value in extra.items() if value is not None},
    }


def _summary(metrics: dict[str, dict[str, Any]], metric_name: str) -> dict[str, Any]:
    return dict((metrics.get(metric_name) or {}).get("summary_numbers") or {})


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "version": MONEY_OUTLOOK_VERSION,
        "profile_id": row.get("profile_id"),
        "generated_at": row.get("generated_at"),
        "valid_until": row.get("valid_until"),
        "month_key": row.get("month_key"),
        "projected_end_balance": _nullable_num(row.get("projected_end_balance")),
        "projected_income_remaining": _round(row.get("projected_income_remaining")),
        "projected_obligations_remaining": _round(row.get("projected_obligations_remaining")),
        "projected_flexible_spend_remaining": _round(row.get("projected_flexible_spend_remaining")),
        "projected_savings_delta": _nullable_num(row.get("projected_savings_delta")),
        "target_savings_amount": _round(row.get("target_savings_amount")),
        "safe_to_spend_today": _nullable_num(row.get("safe_to_spend_today")),
        "safe_to_spend_this_week": _nullable_num(row.get("safe_to_spend_this_week")),
        "safe_to_spend_top_caveat": _safe_to_spend_top_caveat(row),
        "buffer_amount": _nullable_num(row.get("buffer_amount")),
        "buffer_status": _buffer_status_value(row.get("buffer_status")),
        "low_point_date": row.get("low_point_date"),
        "low_point_amount": _nullable_num(row.get("low_point_amount")),
        "buffer_breach": bool(row.get("buffer_breach")),
        "low_point_drivers": _loads(row.get("low_point_drivers_json"), []),
        "confidence": _confidence_value(row.get("confidence")),
        "drivers": _loads(row.get("drivers_json"), []),
        "caveats": _loads(row.get("caveats_json"), []),
        "evidence": _loads(row.get("evidence_json"), {}),
        "fingerprint": row.get("fingerprint"),
    }


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:24]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _loads(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except Exception:
        return fallback


def _clean_subject(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "uncategorized", "none", "null"}:
        return ""
    return text[:120]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _profile_scope(profile: str | None) -> str:
    return str(profile or "household").strip() or "household"


def _confidence_value(value: Any) -> str:
    text = str(value or "medium").strip().lower()
    return text if text in CONFIDENCE_STATES else "medium"


def _buffer_status_value(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    return text if text in BUFFER_STATES else "unknown"


def _cash_buffer_status(low_amount: float | None, buffer_amount: float | None) -> str:
    if low_amount is None or not buffer_amount:
        return "unknown"
    if low_amount < buffer_amount:
        return "below_buffer"
    if low_amount <= buffer_amount * 1.15:
        return "near_buffer"
    return "above_buffer"


def _safe_to_spend_top_caveat(row: dict[str, Any]) -> str:
    caveats = _loads(row.get("caveats_json"), [])
    buffer_status = _buffer_status_value(row.get("buffer_status"))
    if _confidence_value(row.get("confidence")) == "low":
        return caveats[0] if caveats else "Projection confidence is low; safe-to-spend is unavailable."
    if buffer_status in {"below_buffer", "near_buffer"}:
        return "Cash is projected close to the buffer before month end; keep extra spending conservative until that date passes."
    return caveats[0] if caveats else "Known bills and normal flexible spending only; transfers or uncategorized changes may move this."


def _num(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round(value: Any, digits: int = 2) -> float:
    return round(_num(value), digits)


def _nullable_num(value: Any) -> float | None:
    if value is None:
        return None
    return _round(value)


def _coerce_date(value: date | datetime | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _month_end(start: date) -> date:
    if start.month == 12:
        return date(start.year + 1, 1, 1) - timedelta(days=1)
    return date(start.year, start.month + 1, 1) - timedelta(days=1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_until(as_of_date: date) -> str:
    return (as_of_date + timedelta(days=1)).isoformat()
