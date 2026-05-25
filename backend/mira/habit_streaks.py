"""Deterministic positive habit streaks for Mira.

Phase 32 keeps this off the chat hot path. The engine stores compact,
profile-scoped streak facts that Mira may retrieve only when the user asks
about the matching subject or a later UI deliberately surfaces them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import pstdev
from typing import Any

from merchant_identity import canonicalize_merchant_key, display_from_key
from mira.safe_finance_query import NON_SPENDING_CATEGORIES


HABIT_STREAK_VERSION = "mira_habit_streak_v1"
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
SUBJECT_TYPES = {"category", "merchant", "cashflow", "habit"}
STREAK_KINDS = {"under_envelope", "lower_frequency", "higher_savings", "on_time_bill", "lower_variance"}
CONFIDENCE_STATES = {"high", "medium", "low"}
SENSITIVITY_STATES = {"low", "medium", "high"}
STATUS_STATES = {"active", "stale", "dismissed"}
TRANSFER_EXPENSE_TYPES = {"transfer_internal", "transfer_household", "transfer_external"}
SHAMING_TERMS = (
    "bad habit",
    "failed",
    "failure",
    "irresponsible",
    "lazy",
    "reckless",
    "shame",
    "you blew",
    "you failed",
)
SENSITIVE_HINTS = (
    "alcohol",
    "bar",
    "beer",
    "cigar",
    "liquor",
    "smoke",
    "tobacco",
    "vape",
    "vaping",
    "weed",
    "wine",
)


def habit_streaks_enabled() -> bool:
    return os.getenv("MIRA_HABIT_STREAKS_ENABLED", "0").strip().lower() not in FALSE_ENV_VALUES


def ensure_habit_streak_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_habit_streaks (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id          TEXT DEFAULT NULL,
            subject_type        TEXT NOT NULL CHECK(subject_type IN ('category', 'merchant', 'cashflow', 'habit')),
            subject_key         TEXT NOT NULL DEFAULT '',
            subject_label       TEXT NOT NULL DEFAULT '',
            streak_kind         TEXT NOT NULL CHECK(streak_kind IN ('under_envelope', 'lower_frequency', 'higher_savings', 'on_time_bill', 'lower_variance')),
            streak_length       INTEGER NOT NULL DEFAULT 0,
            current_value_json  TEXT NOT NULL DEFAULT '{}',
            baseline_json       TEXT NOT NULL DEFAULT '{}',
            summary             TEXT NOT NULL DEFAULT '',
            confidence          TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
            sensitivity         TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
            generated_at        TEXT NOT NULL,
            valid_until         TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'stale', 'dismissed')),
            fingerprint         TEXT NOT NULL,
            UNIQUE(profile_id, fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_profile_status
            ON mira_habit_streaks(profile_id, status, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_subject
            ON mira_habit_streaks(profile_id, subject_type, subject_key, status);
        CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_valid_until
            ON mira_habit_streaks(valid_until);
        """
    )


def generate_habit_streaks(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    as_of: str | date | datetime | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Build and store the current positive streak set."""

    if not habit_streaks_enabled():
        return {"status": "disabled", "stored_count": 0, "items": []}
    ensure_habit_streak_tables(conn)
    as_of_date = _parse_date(as_of)
    candidates = build_habit_streak_candidates(conn=conn, profile=profile, as_of=as_of_date, limit=limit)
    validation = validate_habit_streaks(candidates)
    stored = store_habit_streaks(conn=conn, profile=profile, streaks=validation["accepted"], as_of=as_of_date)
    return {
        "status": "stored" if stored else "no_streaks",
        "stored_count": len(stored),
        "rejected_count": len(validation["rejected"]),
        "items": stored,
        "as_of": as_of_date.isoformat(),
    }


def build_habit_streak_candidates(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    as_of: str | date | datetime | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    as_of_date = _parse_date(as_of)
    windows = _complete_week_windows(as_of_date, count=10)
    if len(windows) < 9:
        return []
    rows = _visible_transaction_rows(conn, profile=profile, start=windows[-1]["start"], end=windows[0]["end"])
    budgets = _category_budget_map(conn, profile=profile)

    candidates: list[dict[str, Any]] = []
    candidates.extend(_category_under_envelope_candidates(rows=rows, windows=windows, budgets=budgets, profile=profile, as_of=as_of_date))
    candidates.extend(_merchant_lower_frequency_candidates(rows=rows, windows=windows, profile=profile, as_of=as_of_date))
    candidates.extend(_cashflow_higher_savings_candidates(rows=rows, windows=windows, profile=profile, as_of=as_of_date))
    candidates.extend(_category_lower_variance_candidates(rows=rows, windows=windows, profile=profile, as_of=as_of_date))

    ranked = sorted(
        candidates,
        key=lambda item: (
            _confidence_rank(item.get("confidence")),
            int(item.get("streak_length") or 0),
            float((item.get("baseline") or {}).get("materiality") or 0),
        ),
        reverse=True,
    )
    return ranked[: max(1, min(int(limit or 8), 20))]


def validate_habit_streaks(streaks: list[dict[str, Any]]) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, streak in enumerate(streaks):
        reason = _streak_rejection_reason(streak)
        key = (
            str(streak.get("subject_type") or ""),
            str(streak.get("subject_key") or ""),
            str(streak.get("streak_kind") or ""),
        )
        if not reason and key in seen:
            reason = "duplicate_subject_kind"
        if reason:
            rejected.append({"index": index, "reason": reason, "summary": str(streak.get("summary") or "")[:160]})
            continue
        seen.add(key)
        accepted.append(_normalize_streak(streak))
    return {"accepted": accepted, "rejected": rejected}


def store_habit_streaks(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    streaks: list[dict[str, Any]],
    as_of: str | date | datetime | None = None,
) -> list[dict[str, Any]]:
    ensure_habit_streak_tables(conn)
    scope = _profile_scope(profile)
    as_of_date = _parse_date(as_of)
    stored: list[dict[str, Any]] = []
    fingerprints: list[str] = []
    for streak in streaks:
        normalized = _normalize_streak(streak)
        fingerprint = _fingerprint(scope, normalized)
        fingerprints.append(fingerprint)
        conn.execute(
            """
            INSERT INTO mira_habit_streaks (
                profile_id, subject_type, subject_key, subject_label, streak_kind,
                streak_length, current_value_json, baseline_json, summary,
                confidence, sensitivity, generated_at, valid_until, status, fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
                streak_length = excluded.streak_length,
                current_value_json = excluded.current_value_json,
                baseline_json = excluded.baseline_json,
                summary = excluded.summary,
                confidence = excluded.confidence,
                sensitivity = excluded.sensitivity,
                generated_at = excluded.generated_at,
                valid_until = excluded.valid_until,
                status = 'active'
            """,
            (
                scope,
                normalized["subject_type"],
                normalized["subject_key"],
                normalized["subject_label"],
                normalized["streak_kind"],
                int(normalized["streak_length"]),
                json.dumps(normalized.get("current_value") or {}, ensure_ascii=True, sort_keys=True),
                json.dumps(normalized.get("baseline") or {}, ensure_ascii=True, sort_keys=True),
                normalized["summary"],
                normalized["confidence"],
                normalized["sensitivity"],
                _now_iso(),
                normalized.get("valid_until") or _valid_until(as_of_date),
                fingerprint,
            ),
        )
    if fingerprints:
        placeholders = ",".join("?" for _ in fingerprints)
        conn.execute(
            f"""
            UPDATE mira_habit_streaks
               SET status = 'stale'
             WHERE profile_id = ?
               AND status = 'active'
               AND fingerprint NOT IN ({placeholders})
            """,
            [scope, *fingerprints],
        )
    else:
        conn.execute(
            """
            UPDATE mira_habit_streaks
               SET status = 'stale'
             WHERE profile_id = ?
               AND status = 'active'
            """,
            (scope,),
        )
    return list_habit_streaks(conn=conn, profile=profile, include_inactive=False, limit=len(fingerprints) or 20)


def list_habit_streaks(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    include_inactive: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_habit_streak_tables(conn)
    where = ["profile_id = ?"]
    params: list[Any] = [_profile_scope(profile)]
    if not include_inactive:
        where.append("status = 'active'")
    clean_subject_type = _enum(subject_type, SUBJECT_TYPES, "") if subject_type else ""
    if clean_subject_type:
        where.append("subject_type = ?")
        params.append(clean_subject_type)
    if subject_key:
        where.append("subject_key = ?")
        params.append(normalize_subject_key(clean_subject_type or subject_type or "category", subject_key))
    rows = conn.execute(
        f"""
        SELECT *
          FROM mira_habit_streaks
         WHERE {' AND '.join(where)}
         ORDER BY generated_at DESC, streak_length DESC, id DESC
         LIMIT ?
        """,
        [*params, max(1, min(int(limit or 50), 200))],
    ).fetchall()
    return [_public_streak(dict(row)) for row in rows]


def dismiss_habit_streak(*, conn: sqlite3.Connection, profile: str | None, streak_id: int) -> dict[str, Any] | None:
    ensure_habit_streak_tables(conn)
    conn.execute(
        """
        UPDATE mira_habit_streaks
           SET status = 'dismissed'
         WHERE id = ?
           AND profile_id = ?
        """,
        (int(streak_id), _profile_scope(profile)),
    )
    row = conn.execute(
        "SELECT * FROM mira_habit_streaks WHERE id = ? AND profile_id = ?",
        (int(streak_id), _profile_scope(profile)),
    ).fetchone()
    return _public_streak(dict(row)) if row else None


def habit_streak_context_for_subject(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    subject_type: str | None,
    subject_key: str | None,
) -> dict[str, Any] | None:
    if not habit_streaks_enabled() or not subject_type or not subject_key:
        return None
    rows = list_habit_streaks(
        conn=conn,
        profile=profile,
        subject_type=subject_type,
        subject_key=subject_key,
        include_inactive=False,
        limit=3,
    )
    for row in rows:
        if row.get("confidence") not in {"high", "medium"}:
            continue
        current = row.get("current_value") if isinstance(row.get("current_value"), dict) else {}
        baseline = row.get("baseline") if isinstance(row.get("baseline"), dict) else {}
        return {
            "family": "habit_streak",
            "kind": row.get("streak_kind"),
            "subject_type": row.get("subject_type"),
            "subject_key": row.get("subject_key"),
            "summary": row.get("summary"),
            "numbers": {
                "streak_length": row.get("streak_length"),
                **_context_numbers(current, prefix="current"),
                **_context_numbers(baseline, prefix="baseline"),
            },
            "traits": [row.get("streak_kind"), "positive_reinforcement"],
            "confidence": row.get("confidence"),
            "sensitivity": row.get("sensitivity"),
            "valid_until": row.get("valid_until"),
            "time_scope": "recent_complete_weeks",
        }
    return None


def normalize_subject_key(subject_type: str | None, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if str(subject_type or "").strip().lower() == "merchant":
        raw = display_from_key(canonicalize_merchant_key(raw)) or raw
    return _key(raw)


def _category_under_envelope_candidates(
    *,
    rows: list[dict[str, Any]],
    windows: list[dict[str, date]],
    budgets: dict[str, float],
    profile: str | None,
    as_of: date,
) -> list[dict[str, Any]]:
    spend = _weekly_subject_stats(rows, windows, subject_type="category")
    candidates: list[dict[str, Any]] = []
    recent = windows[:3]
    baseline_windows = windows[3:9]
    for subject_key, weekly in spend.items():
        label = weekly.get("label") or _subject_label("category", subject_key)
        if _is_non_spending_label(label):
            continue
        recent_amounts = [weekly["by_week"].get(_week_id(window), {}).get("amount", 0.0) for window in recent]
        baseline_amounts = [weekly["by_week"].get(_week_id(window), {}).get("amount", 0.0) for window in baseline_windows]
        baseline_weeks_with_spend = sum(1 for amount in baseline_amounts if amount > 0)
        baseline_avg = _average(baseline_amounts)
        if baseline_weeks_with_spend < 3 or baseline_avg < 40:
            continue
        monthly_budget = float(budgets.get(subject_key) or 0.0)
        if monthly_budget > 0:
            threshold = round(monthly_budget * 12 / 52, 2)
            envelope_source = "category_budget"
        else:
            threshold = round(baseline_avg * 0.85, 2)
            envelope_source = "prior_6_week_average"
        if threshold <= 0:
            continue
        streak_length = _consecutive_count(recent_amounts, lambda amount: amount <= threshold)
        if streak_length <= 0:
            continue
        current_amount = round(recent_amounts[0], 2)
        summary = _summary_under_envelope(label, streak_length, envelope_source)
        candidates.append(
            _candidate(
                profile=profile,
                subject_type="category",
                subject_key=subject_key,
                subject_label=label,
                streak_kind="under_envelope",
                streak_length=streak_length,
                summary=summary,
                current_value={
                    "period_start": recent[0]["start"].isoformat(),
                    "period_end": recent[0]["end"].isoformat(),
                    "amount": current_amount,
                    "recent_week_amounts": [round(amount, 2) for amount in recent_amounts[:streak_length]],
                },
                baseline={
                    "baseline_start": baseline_windows[-1]["start"].isoformat(),
                    "baseline_end": baseline_windows[0]["end"].isoformat(),
                    "baseline_week_count": len(baseline_windows),
                    "baseline_weeks_with_spend": baseline_weeks_with_spend,
                    "average_weekly_amount": round(baseline_avg, 2),
                    "threshold_amount": threshold,
                    "configured_monthly_budget": round(monthly_budget, 2),
                    "envelope_source": envelope_source,
                    "materiality": round(max(baseline_avg - current_amount, 0.0), 2),
                },
                confidence="high" if streak_length >= 3 else "medium",
                sensitivity=_sensitivity_for_subject(label),
                as_of=as_of,
            )
        )
    return candidates


def _merchant_lower_frequency_candidates(
    *,
    rows: list[dict[str, Any]],
    windows: list[dict[str, date]],
    profile: str | None,
    as_of: date,
) -> list[dict[str, Any]]:
    spend = _weekly_subject_stats(rows, windows, subject_type="merchant")
    candidates: list[dict[str, Any]] = []
    recent = windows[:3]
    baseline_windows = windows[3:9]
    for subject_key, weekly in spend.items():
        label = weekly.get("label") or _subject_label("merchant", subject_key)
        recent_stats = [weekly["by_week"].get(_week_id(window), {}) for window in recent]
        baseline_stats = [weekly["by_week"].get(_week_id(window), {}) for window in baseline_windows]
        baseline_counts = [float(item.get("count") or 0) for item in baseline_stats]
        baseline_amounts = [float(item.get("amount") or 0) for item in baseline_stats]
        baseline_weeks_with_spend = sum(1 for count in baseline_counts if count > 0)
        baseline_avg_count = _average(baseline_counts)
        baseline_avg_amount = _average(baseline_amounts)
        if baseline_weeks_with_spend < 3 or baseline_avg_count < 2 or baseline_avg_amount < 20:
            continue
        threshold_count = round(baseline_avg_count * 0.7, 2)
        threshold_amount = round(baseline_avg_amount * 0.9, 2)
        recent_counts = [float(item.get("count") or 0) for item in recent_stats]
        recent_amounts = [float(item.get("amount") or 0) for item in recent_stats]
        streak_length = _consecutive_count(
            list(zip(recent_counts, recent_amounts)),
            lambda item: item[0] <= threshold_count and item[1] <= threshold_amount,
        )
        if streak_length <= 0:
            continue
        summary = f"{_safe_subject(label)} has shown a lower visit rhythm for {streak_length} complete week{'s' if streak_length != 1 else ''}."
        candidates.append(
            _candidate(
                profile=profile,
                subject_type="merchant",
                subject_key=subject_key,
                subject_label=label,
                streak_kind="lower_frequency",
                streak_length=streak_length,
                summary=summary,
                current_value={
                    "period_start": recent[0]["start"].isoformat(),
                    "period_end": recent[0]["end"].isoformat(),
                    "count": int(recent_counts[0]),
                    "amount": round(recent_amounts[0], 2),
                    "recent_week_counts": [int(count) for count in recent_counts[:streak_length]],
                },
                baseline={
                    "baseline_start": baseline_windows[-1]["start"].isoformat(),
                    "baseline_end": baseline_windows[0]["end"].isoformat(),
                    "baseline_week_count": len(baseline_windows),
                    "baseline_weeks_with_spend": baseline_weeks_with_spend,
                    "average_weekly_count": round(baseline_avg_count, 2),
                    "average_weekly_amount": round(baseline_avg_amount, 2),
                    "threshold_count": threshold_count,
                    "threshold_amount": threshold_amount,
                    "materiality": round(max(baseline_avg_amount - recent_amounts[0], 0.0), 2),
                },
                confidence="high" if streak_length >= 3 else "medium",
                sensitivity=_sensitivity_for_subject(label),
                as_of=as_of,
            )
        )
    return candidates


def _cashflow_higher_savings_candidates(
    *,
    rows: list[dict[str, Any]],
    windows: list[dict[str, date]],
    profile: str | None,
    as_of: date,
) -> list[dict[str, Any]]:
    recent = windows[:3]
    baseline_windows = windows[3:9]
    weekly = _weekly_cashflow_stats(rows, windows)
    baseline_stats = [weekly.get(_week_id(window), {}) for window in baseline_windows]
    usable_baseline = [
        item
        for item in baseline_stats
        if float(item.get("income") or 0) >= 500
        and item.get("savings_rate") is not None
        and -1.5 <= float(item.get("savings_rate") or 0) <= 1.0
    ]
    if len(usable_baseline) < 3:
        return []
    baseline_rates = [float(item.get("savings_rate") or 0) for item in usable_baseline]
    baseline_avg_rate = _average(baseline_rates)
    recent_stats = [weekly.get(_week_id(window), {}) for window in recent]
    streak_length = _consecutive_count(
        recent_stats,
        lambda item: float(item.get("income") or 0) >= 500
        and item.get("savings_rate") is not None
        and float(item.get("savings_rate") or 0) >= baseline_avg_rate + 0.1
        and float(item.get("savings_rate") or 0) > 0,
    )
    if streak_length <= 0:
        return []
    current = recent_stats[0]
    summary = f"Weekly cash flow has beaten its recent savings-rate baseline for {streak_length} complete week{'s' if streak_length != 1 else ''}."
    return [
        _candidate(
            profile=profile,
            subject_type="cashflow",
            subject_key="weekly_savings_rate",
            subject_label="Weekly cash flow",
            streak_kind="higher_savings",
            streak_length=streak_length,
            summary=summary,
            current_value={
                "period_start": recent[0]["start"].isoformat(),
                "period_end": recent[0]["end"].isoformat(),
                "income": round(float(current.get("income") or 0), 2),
                "spend": round(float(current.get("spend") or 0), 2),
                "net": round(float(current.get("net") or 0), 2),
                "savings_rate": round(float(current.get("savings_rate") or 0), 4),
            },
            baseline={
                "baseline_start": baseline_windows[-1]["start"].isoformat(),
                "baseline_end": baseline_windows[0]["end"].isoformat(),
                "baseline_week_count": len(baseline_windows),
                "average_savings_rate": round(baseline_avg_rate, 4),
                "threshold_savings_rate": round(baseline_avg_rate + 0.1, 4),
                "materiality": round(max(float(current.get("net") or 0), 0.0), 2),
            },
            confidence="high" if streak_length >= 2 else "medium",
            sensitivity="low",
            as_of=as_of,
        )
    ]


def _category_lower_variance_candidates(
    *,
    rows: list[dict[str, Any]],
    windows: list[dict[str, date]],
    profile: str | None,
    as_of: date,
) -> list[dict[str, Any]]:
    spend = _weekly_subject_stats(rows, windows, subject_type="category")
    recent = windows[:3]
    baseline_windows = windows[3:9]
    candidates: list[dict[str, Any]] = []
    for subject_key, weekly in spend.items():
        label = weekly.get("label") or _subject_label("category", subject_key)
        recent_amounts = [weekly["by_week"].get(_week_id(window), {}).get("amount", 0.0) for window in recent]
        baseline_amounts = [weekly["by_week"].get(_week_id(window), {}).get("amount", 0.0) for window in baseline_windows]
        baseline_weeks_with_spend = sum(1 for amount in baseline_amounts if amount > 0)
        baseline_avg = _average(baseline_amounts)
        if baseline_weeks_with_spend < 4 or baseline_avg < 40 or len(recent_amounts) < 3:
            continue
        baseline_std = pstdev(baseline_amounts)
        recent_std = pstdev(recent_amounts)
        recent_avg = _average(recent_amounts)
        if baseline_std < 25 or recent_std > baseline_std * 0.5 or recent_avg > baseline_avg:
            continue
        summary = f"{_safe_subject(label)} has been steadier for 3 complete weeks, with less week-to-week swing than its recent baseline."
        candidates.append(
            _candidate(
                profile=profile,
                subject_type="category",
                subject_key=subject_key,
                subject_label=label,
                streak_kind="lower_variance",
                streak_length=3,
                summary=summary,
                current_value={
                    "period_start": recent[-1]["start"].isoformat(),
                    "period_end": recent[0]["end"].isoformat(),
                    "average_weekly_amount": round(recent_avg, 2),
                    "weekly_stddev": round(recent_std, 2),
                },
                baseline={
                    "baseline_start": baseline_windows[-1]["start"].isoformat(),
                    "baseline_end": baseline_windows[0]["end"].isoformat(),
                    "baseline_week_count": len(baseline_windows),
                    "average_weekly_amount": round(baseline_avg, 2),
                    "weekly_stddev": round(baseline_std, 2),
                    "materiality": round(max(baseline_std - recent_std, 0.0), 2),
                },
                confidence="medium",
                sensitivity=_sensitivity_for_subject(label),
                as_of=as_of,
            )
        )
    return candidates[:2]


def _candidate(
    *,
    profile: str | None,
    subject_type: str,
    subject_key: str,
    subject_label: str,
    streak_kind: str,
    streak_length: int,
    summary: str,
    current_value: dict[str, Any],
    baseline: dict[str, Any],
    confidence: str,
    sensitivity: str,
    as_of: date,
) -> dict[str, Any]:
    return {
        "profile_id": _profile_scope(profile),
        "subject_type": _enum(subject_type, SUBJECT_TYPES, "habit"),
        "subject_key": normalize_subject_key(subject_type, subject_key),
        "subject_label": _clean_text(subject_label, 120),
        "streak_kind": _enum(streak_kind, STREAK_KINDS, "under_envelope"),
        "streak_length": max(0, int(streak_length or 0)),
        "current_value": current_value,
        "baseline": baseline,
        "summary": _clean_text(summary, 240),
        "confidence": _enum(confidence, CONFIDENCE_STATES, "medium"),
        "sensitivity": _enum(sensitivity, SENSITIVITY_STATES, "low"),
        "valid_until": _valid_until(as_of),
    }


def _visible_transaction_rows(
    conn: sqlite3.Connection,
    *,
    profile: str | None,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    profile_where, params = _profile_where(profile, "profile_id")
    try:
        rows = conn.execute(
            f"""
            SELECT id, profile_id, date, amount, category, expense_type, merchant_key, merchant_name, description
              FROM transactions_visible
             WHERE date >= ?
               AND date <= ?
               {profile_where}
            """,
            [start.isoformat(), end.isoformat(), *params],
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _category_budget_map(conn: sqlite3.Connection, *, profile: str | None) -> dict[str, float]:
    profile_where, params = _profile_where(profile, "profile_id")
    try:
        rows = conn.execute(
            f"""
            SELECT category, amount
              FROM category_budgets
             WHERE amount > 0
               {profile_where}
            """,
            params,
        ).fetchall()
    except Exception:
        return {}
    return {normalize_subject_key("category", row["category"]): float(row["amount"] or 0) for row in rows}


def _weekly_subject_stats(rows: list[dict[str, Any]], windows: list[dict[str, date]], *, subject_type: str) -> dict[str, Any]:
    by_subject: dict[str, Any] = {}
    week_by_day: dict[str, dict[str, date]] = {}
    for window in windows:
        current = window["start"]
        while current <= window["end"]:
            week_by_day[current.isoformat()] = window
            current += timedelta(days=1)
    for row in rows:
        if not _is_spend_row(row):
            continue
        window = week_by_day.get(str(row.get("date") or "")[:10])
        if not window:
            continue
        if subject_type == "category":
            label = str(row.get("category") or "").strip()
            if not label or _is_non_spending_label(label):
                continue
            key = normalize_subject_key("category", label)
        elif subject_type == "merchant":
            raw_key = row.get("merchant_key") or row.get("merchant_name") or row.get("description")
            key = normalize_subject_key("merchant", canonicalize_merchant_key(raw_key))
            label = str(row.get("merchant_name") or display_from_key(canonicalize_merchant_key(raw_key)) or raw_key or "").strip()
            if not key:
                continue
        else:
            continue
        subject = by_subject.setdefault(key, {"label": label, "by_week": {}})
        if len(label) > len(str(subject.get("label") or "")):
            subject["label"] = label
        week = subject["by_week"].setdefault(_week_id(window), {"amount": 0.0, "count": 0, "sample_ids": []})
        week["amount"] = round(float(week["amount"]) + abs(float(row.get("amount") or 0)), 2)
        week["count"] = int(week["count"]) + 1
        if len(week["sample_ids"]) < 5 and row.get("id"):
            week["sample_ids"].append(str(row.get("id")))
    return by_subject


def _weekly_cashflow_stats(rows: list[dict[str, Any]], windows: list[dict[str, date]]) -> dict[str, dict[str, Any]]:
    stats = {_week_id(window): {"income": 0.0, "spend": 0.0, "net": 0.0, "savings_rate": None} for window in windows}
    week_by_day: dict[str, dict[str, date]] = {}
    for window in windows:
        current = window["start"]
        while current <= window["end"]:
            week_by_day[current.isoformat()] = window
            current += timedelta(days=1)
    for row in rows:
        window = week_by_day.get(str(row.get("date") or "")[:10])
        if not window:
            continue
        week = stats[_week_id(window)]
        amount = float(row.get("amount") or 0)
        category = str(row.get("category") or "").strip()
        if amount > 0 and normalize_subject_key("category", category) == "income":
            week["income"] = round(float(week["income"]) + amount, 2)
        elif amount < 0 and _is_spend_row(row):
            week["spend"] = round(float(week["spend"]) + abs(amount), 2)
    for week in stats.values():
        week["net"] = round(float(week["income"]) - float(week["spend"]), 2)
        if float(week["income"]) > 0:
            week["savings_rate"] = round(float(week["net"]) / float(week["income"]), 4)
    return stats


def _complete_week_windows(as_of: date, *, count: int) -> list[dict[str, date]]:
    current_week_start = as_of - timedelta(days=as_of.weekday())
    latest_end = current_week_start - timedelta(days=1)
    windows: list[dict[str, date]] = []
    for offset in range(max(0, count)):
        end = latest_end - timedelta(days=7 * offset)
        start = end - timedelta(days=6)
        windows.append({"start": start, "end": end})
    return windows


def _public_streak(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "current_value": _json_load(row.get("current_value_json"), {}),
        "baseline": _json_load(row.get("baseline_json"), {}),
    }


def _normalize_streak(streak: dict[str, Any]) -> dict[str, Any]:
    subject_type = _enum(streak.get("subject_type"), SUBJECT_TYPES, "habit")
    subject_key = normalize_subject_key(subject_type, streak.get("subject_key"))
    return {
        "subject_type": subject_type,
        "subject_key": subject_key,
        "subject_label": _clean_text(streak.get("subject_label") or _subject_label(subject_type, subject_key), 120),
        "streak_kind": _enum(streak.get("streak_kind"), STREAK_KINDS, "under_envelope"),
        "streak_length": max(1, int(streak.get("streak_length") or 0)),
        "current_value": streak.get("current_value") if isinstance(streak.get("current_value"), dict) else {},
        "baseline": streak.get("baseline") if isinstance(streak.get("baseline"), dict) else {},
        "summary": _clean_text(streak.get("summary"), 240),
        "confidence": _enum(streak.get("confidence"), CONFIDENCE_STATES, "medium"),
        "sensitivity": _enum(streak.get("sensitivity"), SENSITIVITY_STATES, "low"),
        "valid_until": _clean_text(streak.get("valid_until"), 20),
    }


def _streak_rejection_reason(streak: dict[str, Any]) -> str:
    if _enum(streak.get("subject_type"), SUBJECT_TYPES, "") == "":
        return "invalid_subject_type"
    if not normalize_subject_key(streak.get("subject_type"), streak.get("subject_key")):
        return "missing_subject_key"
    if _enum(streak.get("streak_kind"), STREAK_KINDS, "") == "":
        return "invalid_streak_kind"
    try:
        length = int(streak.get("streak_length") or 0)
    except (TypeError, ValueError):
        return "invalid_streak_length"
    if length < 1:
        return "empty_streak"
    summary = str(streak.get("summary") or "")
    if not summary.strip():
        return "missing_summary"
    lowered = summary.lower()
    if any(term in lowered for term in SHAMING_TERMS):
        return "shaming_language"
    current = streak.get("current_value")
    baseline = streak.get("baseline")
    if not isinstance(current, dict) or not isinstance(baseline, dict):
        return "missing_current_or_baseline"
    if not baseline:
        return "missing_baseline"
    return ""


def _context_numbers(values: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[f"{prefix}_{key}"] = value
    return out


def _is_spend_row(row: dict[str, Any]) -> bool:
    if float(row.get("amount") or 0) >= 0:
        return False
    category = str(row.get("category") or "").strip()
    expense_type = str(row.get("expense_type") or "").strip()
    return not (_is_non_spending_label(category) or expense_type in TRANSFER_EXPENSE_TYPES)


def _is_non_spending_label(label: Any) -> bool:
    return str(label or "").strip() in NON_SPENDING_CATEGORIES


def _summary_under_envelope(label: str, streak_length: int, envelope_source: str) -> str:
    subject = _safe_subject(label)
    weeks = f"{streak_length} complete week{'s' if streak_length != 1 else ''}"
    if envelope_source == "category_budget":
        return f"{subject} has stayed under its weekly budget pace for {weeks}."
    return f"{subject} has stayed below its usual weekly pace for {weeks}."


def _safe_subject(value: Any) -> str:
    text = _clean_text(value, 80)
    return text or "This area"


def _sensitivity_for_subject(value: Any) -> str:
    lowered = str(value or "").lower()
    if any(hint in lowered for hint in SENSITIVE_HINTS):
        return "high"
    return "low"


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


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _subject_label(subject_type: str | None, subject_key: Any) -> str:
    if str(subject_type or "").strip().lower() == "merchant":
        return display_from_key(canonicalize_merchant_key(str(subject_key or ""))) or str(subject_key or "")
    if str(subject_type or "").strip().lower() == "cashflow":
        return "Weekly cash flow"
    return " ".join(str(subject_key or "").replace("_", " ").split()).title()


def _parse_date(value: str | date | datetime | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            pass
    return date.today()


def _valid_until(as_of: date) -> str:
    return (as_of + timedelta(days=10)).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _week_id(window: dict[str, date]) -> str:
    return window["start"].isoformat()


def _average(values: list[float]) -> float:
    return sum(float(value or 0) for value in values) / len(values) if values else 0.0


def _consecutive_count(values: list[Any], predicate) -> int:
    count = 0
    for value in values:
        if not predicate(value):
            break
        count += 1
    return count


def _confidence_rank(value: Any) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(str(value or "").strip().lower(), 0)


def _fingerprint(profile: str, streak: dict[str, Any]) -> str:
    payload = {
        "version": HABIT_STREAK_VERSION,
        "profile_id": profile,
        "subject_type": streak.get("subject_type"),
        "subject_key": streak.get("subject_key"),
        "streak_kind": streak.get("streak_kind"),
        "period_end": (streak.get("current_value") or {}).get("period_end"),
        "streak_length": streak.get("streak_length"),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _json_load(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return default


__all__ = [
    "build_habit_streak_candidates",
    "dismiss_habit_streak",
    "ensure_habit_streak_tables",
    "generate_habit_streaks",
    "habit_streak_context_for_subject",
    "habit_streaks_enabled",
    "list_habit_streaks",
    "normalize_subject_key",
    "store_habit_streaks",
    "validate_habit_streaks",
]
