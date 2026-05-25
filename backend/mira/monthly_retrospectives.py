"""Closed-month retrospective diary entries for Mira.

Phase 33 is off the chat hot path. It builds one inspectable monthly diary from
deterministic evidence, validates the visible claims, and stores the result for
later retrieval by explicit retrospective questions.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from mira.safe_finance_query import NON_SPENDING_CATEGORIES


RETROSPECTIVE_VERSION = "mira_monthly_retrospective_v1"
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
CONFIDENCE_STATES = {"high", "medium", "low"}
STATUS_STATES = {"active", "dismissed", "stale"}
TRANSFER_EXPENSE_TYPES = {"transfer_internal", "transfer_household", "transfer_external"}
PRIVATE_TERMS = (
    "run_sql",
    "sql",
    "backend",
    "tool",
    "query id",
    "evidence id",
    "implementation",
    "deterministic",
)
SHAMING_TERMS = (
    "addict",
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
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
DOLLAR_RE = re.compile(r"\$([0-9][0-9,]*(?:\.[0-9]{2})?)")


def monthly_retrospective_enabled() -> bool:
    return os.getenv("MIRA_MONTHLY_RETROSPECTIVE_ENABLED", "0").strip().lower() not in FALSE_ENV_VALUES


def ensure_monthly_retrospective_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_monthly_retrospectives (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id                  TEXT DEFAULT NULL,
            month_key                   TEXT NOT NULL,
            summary                     TEXT NOT NULL DEFAULT '',
            wins_json                   TEXT NOT NULL DEFAULT '[]',
            friction_json               TEXT NOT NULL DEFAULT '[]',
            improvement_themes_json     TEXT NOT NULL DEFAULT '[]',
            evidence_json               TEXT NOT NULL DEFAULT '{}',
            confidence                  TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
            generated_at                TEXT NOT NULL,
            status                      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'dismissed', 'stale')),
            fingerprint                 TEXT NOT NULL,
            UNIQUE(profile_id, month_key)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_monthly_retrospectives_profile_month
            ON mira_monthly_retrospectives(profile_id, month_key DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_monthly_retrospectives_status
            ON mira_monthly_retrospectives(profile_id, status, generated_at DESC);
        """
    )


def generate_monthly_retrospective(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    month_key: str | None = None,
    as_of: str | date | datetime | None = None,
) -> dict[str, Any]:
    if not monthly_retrospective_enabled():
        return {"status": "disabled", "stored": False, "item": None}
    ensure_monthly_retrospective_tables(conn)
    diary = build_monthly_retrospective(conn=conn, profile=profile, month_key=month_key, as_of=as_of)
    if diary.get("status") != "ok":
        return {"status": diary.get("status") or "no_diary", "stored": False, "item": None, "caveats": diary.get("caveats") or []}
    validation = validate_monthly_retrospective(diary)
    if validation["rejected"]:
        return {"status": "rejected", "stored": False, "item": None, "rejected": validation["rejected"]}
    stored = store_monthly_retrospective(conn=conn, profile=profile, diary=validation["accepted"])
    return {"status": "stored", "stored": True, "item": stored}


def build_monthly_retrospective(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    month_key: str | None = None,
    as_of: str | date | datetime | None = None,
) -> dict[str, Any]:
    target_month = _resolve_month_key(month_key=month_key, as_of=as_of)
    prior_month = _shift_month_key(target_month, -1)
    next_month = _shift_month_key(target_month, 1)
    target_stats = _month_stats(conn, profile=profile, month_key=target_month)
    if target_stats["transaction_count"] < 5 or (target_stats["income"] <= 0 and target_stats["expenses"] <= 0):
        return {
            "status": "insufficient_data",
            "month_key": target_month,
            "caveats": ["Closed month has too little visible data for a useful retrospective."],
        }

    prior_stats = _month_stats(conn, profile=profile, month_key=prior_month)
    categories = _category_deltas(conn, profile=profile, month_key=target_month, prior_month_key=prior_month)
    merchants = _top_merchants(conn, profile=profile, month_key=target_month, limit=5)
    budgets = _budget_status(conn, profile=profile, month_key=target_month)
    recurring = _recurring_context(conn, profile=profile, month_key=target_month)
    stored_streaks = _stored_habit_streaks(conn, profile=profile, month_key=target_month)

    wins = _build_wins(categories=categories, stored_streaks=stored_streaks, prior_stats=prior_stats)
    friction = _build_friction(categories=categories, budgets=budgets)
    themes = _build_improvement_themes(wins=wins, friction=friction, merchants=merchants)
    confidence = _confidence(target_stats=target_stats, prior_stats=prior_stats, wins=wins, friction=friction)
    caveats = _caveats(target_stats=target_stats, prior_stats=prior_stats, recurring=recurring)
    summary = _compose_summary(
        month_key=target_month,
        next_month_key=next_month,
        target_stats=target_stats,
        wins=wins,
        friction=friction,
        themes=themes,
        confidence=confidence,
    )
    evidence = {
        "version": RETROSPECTIVE_VERSION,
        "profile_id": _profile_scope(profile),
        "month_key": target_month,
        "prior_month_key": prior_month,
        "summary_numbers": target_stats,
        "prior_summary_numbers": prior_stats,
        "top_categories": categories[:8],
        "top_merchants": merchants[:5],
        "budget_status": budgets[:8],
        "recurring_context": recurring,
        "stored_habit_streaks": stored_streaks[:5],
        "caveats": caveats,
    }
    return {
        "status": "ok",
        "profile_id": _profile_scope(profile),
        "month_key": target_month,
        "summary": summary,
        "wins": wins[:3],
        "friction": friction[:3],
        "improvement_themes": themes[:3],
        "evidence": evidence,
        "confidence": confidence,
        "generated_at": _now_iso(),
    }


def validate_monthly_retrospective(diary: dict[str, Any]) -> dict[str, Any]:
    accepted = _normalize_diary(diary)
    rejected: list[str] = []
    summary = str(accepted.get("summary") or "")
    lowered = summary.lower()
    if not MONTH_RE.match(str(accepted.get("month_key") or "")):
        rejected.append("invalid_month_key")
    if not summary.strip():
        rejected.append("missing_summary")
    if any(term in lowered for term in PRIVATE_TERMS):
        rejected.append("internal_language")
    if any(term in lowered for term in SHAMING_TERMS):
        rejected.append("shaming_language")
    if _sensitive_terms_visible(summary, accepted):
        rejected.append("sensitive_subject_visible")
    unsupported = _unsupported_dollar_claims(summary, accepted)
    if unsupported:
        rejected.append(f"unsupported_dollar_claims:{','.join(unsupported)}")
    if not isinstance(accepted.get("evidence"), dict) or not accepted["evidence"].get("summary_numbers"):
        rejected.append("missing_evidence")
    return {"accepted": accepted, "rejected": rejected}


def store_monthly_retrospective(*, conn: sqlite3.Connection, profile: str | None, diary: dict[str, Any]) -> dict[str, Any]:
    ensure_monthly_retrospective_tables(conn)
    normalized = _normalize_diary(diary)
    scope = _profile_scope(profile)
    fingerprint = _fingerprint(scope, normalized)
    conn.execute(
        """
        INSERT INTO mira_monthly_retrospectives (
            profile_id, month_key, summary, wins_json, friction_json,
            improvement_themes_json, evidence_json, confidence, generated_at,
            status, fingerprint
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        ON CONFLICT(profile_id, month_key) DO UPDATE SET
            summary = excluded.summary,
            wins_json = excluded.wins_json,
            friction_json = excluded.friction_json,
            improvement_themes_json = excluded.improvement_themes_json,
            evidence_json = excluded.evidence_json,
            confidence = excluded.confidence,
            generated_at = excluded.generated_at,
            status = 'active',
            fingerprint = excluded.fingerprint
        """,
        (
            scope,
            normalized["month_key"],
            normalized["summary"],
            json.dumps(normalized.get("wins") or [], ensure_ascii=True, sort_keys=True),
            json.dumps(normalized.get("friction") or [], ensure_ascii=True, sort_keys=True),
            json.dumps(normalized.get("improvement_themes") or [], ensure_ascii=True, sort_keys=True),
            json.dumps(normalized.get("evidence") or {}, ensure_ascii=True, sort_keys=True),
            normalized["confidence"],
            normalized.get("generated_at") or _now_iso(),
            fingerprint,
        ),
    )
    return get_monthly_retrospective(conn=conn, profile=profile, month_key=normalized["month_key"]) or {}


def list_monthly_retrospectives(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    include_inactive: bool = False,
    limit: int = 24,
) -> list[dict[str, Any]]:
    ensure_monthly_retrospective_tables(conn)
    where = ["profile_id = ?"]
    params: list[Any] = [_profile_scope(profile)]
    if not include_inactive:
        where.append("status = 'active'")
    rows = conn.execute(
        f"""
        SELECT *
          FROM mira_monthly_retrospectives
         WHERE {' AND '.join(where)}
         ORDER BY month_key DESC, generated_at DESC
         LIMIT ?
        """,
        [*params, max(1, min(int(limit or 24), 120))],
    ).fetchall()
    return [_public_retrospective(dict(row)) for row in rows]


def get_monthly_retrospective(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    month_key: str | None = None,
) -> dict[str, Any] | None:
    ensure_monthly_retrospective_tables(conn)
    target = month_key or _resolve_month_key(month_key=None)
    row = conn.execute(
        """
        SELECT *
          FROM mira_monthly_retrospectives
         WHERE profile_id = ?
           AND month_key = ?
         LIMIT 1
        """,
        (_profile_scope(profile), target),
    ).fetchone()
    return _public_retrospective(dict(row)) if row else None


def dismiss_monthly_retrospective(*, conn: sqlite3.Connection, profile: str | None, retrospective_id: int) -> dict[str, Any] | None:
    ensure_monthly_retrospective_tables(conn)
    conn.execute(
        """
        UPDATE mira_monthly_retrospectives
           SET status = 'dismissed'
         WHERE id = ?
           AND profile_id = ?
        """,
        (int(retrospective_id), _profile_scope(profile)),
    )
    row = conn.execute(
        "SELECT * FROM mira_monthly_retrospectives WHERE id = ? AND profile_id = ?",
        (int(retrospective_id), _profile_scope(profile)),
    ).fetchone()
    return _public_retrospective(dict(row)) if row else None


def monthly_retrospective_context(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    month_key: str | None = None,
) -> dict[str, Any] | None:
    if not monthly_retrospective_enabled():
        return None
    target = month_key or _resolve_month_key(month_key=None)
    item = get_monthly_retrospective(conn=conn, profile=profile, month_key=target)
    if not item or item.get("status") != "active":
        return None
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    return {
        "id": item.get("id"),
        "family": "monthly_retrospective",
        "kind": "monthly_diary",
        "subject_type": "profile",
        "subject_key": "profile",
        "summary": item.get("summary"),
        "numbers": evidence.get("summary_numbers") if isinstance(evidence.get("summary_numbers"), dict) else {},
        "traits": ["retrospective", item.get("confidence")],
        "confidence": item.get("confidence"),
        "sensitivity": "low",
        "time_scope": item.get("month_key"),
    }


def _month_stats(conn: sqlite3.Connection, *, profile: str | None, month_key: str) -> dict[str, Any]:
    start, end = _month_bounds(month_key)
    profile_where, params = _profile_where(profile, "profile_id")
    non_spending = ",".join("?" for _ in NON_SPENDING_CATEGORIES)
    transfer_ok = _transfer_ok_clause(profile)
    try:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS transaction_count,
                COALESCE(SUM(CASE WHEN category = 'Income' AND amount > 0 {transfer_ok} THEN amount ELSE 0 END), 0) AS income,
                COALESCE(SUM(CASE WHEN amount < 0 AND category NOT IN ({non_spending}) {transfer_ok} THEN ABS(amount) ELSE 0 END), 0) AS expenses,
                COALESCE(SUM(CASE WHEN amount > 0 AND category != 'Income' THEN amount ELSE 0 END), 0) AS refunds,
                COALESCE(SUM(CASE WHEN category = 'Savings Transfer' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS savings_transfer,
                COALESCE(SUM(CASE WHEN category = 'Credit Card Payment' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS credit_card_payments
              FROM transactions_visible
             WHERE date >= ?
               AND date <= ?
               {profile_where}
            """,
            [*NON_SPENDING_CATEGORIES, start, end, *params],
        ).fetchone()
    except Exception:
        row = None
    values = dict(row) if row else {}
    income = _round(values.get("income"))
    expenses = _round(values.get("expenses"))
    refunds = _round(values.get("refunds"))
    return {
        "month_key": month_key,
        "transaction_count": int(values.get("transaction_count") or 0),
        "income": income,
        "expenses": expenses,
        "refunds": refunds,
        "net": _round(income - expenses + refunds),
        "savings_transfer": _round(values.get("savings_transfer")),
        "credit_card_payments": _round(values.get("credit_card_payments")),
    }


def _category_deltas(conn: sqlite3.Connection, *, profile: str | None, month_key: str, prior_month_key: str) -> list[dict[str, Any]]:
    current = _category_stats(conn, profile=profile, month_key=month_key)
    prior = {row["subject_key"]: row for row in _category_stats(conn, profile=profile, month_key=prior_month_key)}
    out: list[dict[str, Any]] = []
    for row in current:
        previous = prior.get(row["subject_key"], {})
        prior_amount = _round(previous.get("amount"))
        amount = _round(row.get("amount"))
        out.append(
            {
                **row,
                "prior_amount": prior_amount,
                "delta_amount": _round(amount - prior_amount),
                "delta_percent": round((amount - prior_amount) / prior_amount, 4) if prior_amount > 0 else None,
            }
        )
    return sorted(out, key=lambda item: item["amount"], reverse=True)


def _category_stats(conn: sqlite3.Connection, *, profile: str | None, month_key: str) -> list[dict[str, Any]]:
    start, end = _month_bounds(month_key)
    profile_where, params = _profile_where(profile, "profile_id")
    non_spending = ",".join("?" for _ in NON_SPENDING_CATEGORIES)
    transfer_ok = _transfer_ok_clause(profile)
    try:
        rows = conn.execute(
            f"""
            SELECT category,
                   COALESCE(SUM(ABS(amount)), 0) AS amount,
                   COUNT(*) AS count
              FROM transactions_visible
             WHERE date >= ?
               AND date <= ?
               AND amount < 0
               AND category NOT IN ({non_spending})
               {transfer_ok}
               {profile_where}
             GROUP BY category
             ORDER BY amount DESC
            """,
            [start, end, *NON_SPENDING_CATEGORIES, *params],
        ).fetchall()
    except Exception:
        rows = []
    out = []
    for row in rows:
        label = str(row["category"] or "Uncategorized").strip() or "Uncategorized"
        sensitive = _is_sensitive(label)
        out.append(
            {
                "subject_type": "category",
                "subject_key": _key(label),
                "subject_label": _display_label(label, sensitive=sensitive),
                "amount": _round(row["amount"]),
                "count": int(row["count"] or 0),
                "sensitive": sensitive,
            }
        )
    return out


def _top_merchants(conn: sqlite3.Connection, *, profile: str | None, month_key: str, limit: int) -> list[dict[str, Any]]:
    start, end = _month_bounds(month_key)
    profile_where, params = _profile_where(profile, "profile_id")
    non_spending = ",".join("?" for _ in NON_SPENDING_CATEGORIES)
    transfer_ok = _transfer_ok_clause(profile)
    try:
        rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, ''), description) AS merchant,
                   GROUP_CONCAT(DISTINCT category) AS categories,
                   COALESCE(SUM(ABS(amount)), 0) AS amount,
                   COUNT(*) AS count
              FROM transactions_visible
             WHERE date >= ?
               AND date <= ?
               AND amount < 0
               AND category NOT IN ({non_spending})
               {transfer_ok}
               {profile_where}
             GROUP BY merchant
             ORDER BY amount DESC
             LIMIT ?
            """,
            [start, end, *NON_SPENDING_CATEGORIES, *params, max(1, min(int(limit or 5), 20))],
        ).fetchall()
    except Exception:
        rows = []
    out = []
    for row in rows:
        label = str(row["merchant"] or "Unknown merchant").strip()
        sensitive = _is_sensitive(label) or _is_sensitive(row["categories"])
        out.append(
            {
                "subject_type": "merchant",
                "subject_key": _key(label),
                "subject_label": _display_label(label, sensitive=sensitive),
                "amount": _round(row["amount"]),
                "count": int(row["count"] or 0),
                "sensitive": sensitive,
            }
        )
    return out


def _budget_status(conn: sqlite3.Connection, *, profile: str | None, month_key: str) -> list[dict[str, Any]]:
    categories = {row["subject_key"]: row for row in _category_stats(conn, profile=profile, month_key=month_key)}
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
        rows = []
    out: list[dict[str, Any]] = []
    for row in rows:
        label = str(row["category"] or "").strip()
        key = _key(label)
        category = categories.get(key, {})
        spent = _round(category.get("amount"))
        budget = _round(row["amount"])
        out.append(
            {
                "subject_type": "category",
                "subject_key": key,
                "subject_label": _display_label(label, sensitive=_is_sensitive(label)),
                "budget_amount": budget,
                "spent_amount": spent,
                "delta_amount": _round(spent - budget),
                "status": "over" if spent > budget else "under",
            }
        )
    return sorted(out, key=lambda item: item["delta_amount"], reverse=True)


def _recurring_context(conn: sqlite3.Connection, *, profile: str | None, month_key: str) -> dict[str, Any]:
    start, end = _month_bounds(month_key)
    profile_where, params = _profile_where(profile, "profile_id")
    try:
        rows = conn.execute(
            f"""
            SELECT display_name, amount_cents, frequency, state, last_seen_date
              FROM recurring_obligations
             WHERE state IN ('active', 'candidate')
               AND COALESCE(last_seen_date, '') >= ?
               AND COALESCE(last_seen_date, '') <= ?
               {profile_where}
             ORDER BY amount_cents DESC
             LIMIT 10
            """,
            [start, end, *params],
        ).fetchall()
    except Exception:
        rows = []
    items = [
        {
            "subject_label": _display_label(row["display_name"], sensitive=_is_sensitive(row["display_name"])),
            "amount": _round(float(row["amount_cents"] or 0) / 100.0),
            "frequency": row["frequency"],
            "state": row["state"],
            "last_seen_date": row["last_seen_date"],
        }
        for row in rows
    ]
    return {"count": len(items), "items": items}


def _stored_habit_streaks(conn: sqlite3.Connection, *, profile: str | None, month_key: str) -> list[dict[str, Any]]:
    try:
        from mira.habit_streaks import list_habit_streaks

        rows = list_habit_streaks(conn=conn, profile=profile, include_inactive=True, limit=20)
    except Exception:
        return []
    return [
        {
            "subject_type": row.get("subject_type"),
            "subject_key": row.get("subject_key"),
            "subject_label": _display_label(row.get("subject_label"), sensitive=str(row.get("sensitivity")) == "high"),
            "streak_kind": row.get("streak_kind"),
            "streak_length": row.get("streak_length"),
            "summary": row.get("summary"),
            "confidence": row.get("confidence"),
        }
        for row in rows
        if str(row.get("status") or "") == "active" and str(row.get("valid_until") or "") >= f"{month_key}-01"
    ]


def _build_wins(*, categories: list[dict[str, Any]], stored_streaks: list[dict[str, Any]], prior_stats: dict[str, Any]) -> list[dict[str, Any]]:
    wins: list[dict[str, Any]] = []
    if prior_stats.get("transaction_count"):
        for item in sorted(categories, key=lambda row: row.get("delta_amount") or 0):
            delta = _round(item.get("delta_amount"))
            prior = _round(item.get("prior_amount"))
            current = _round(item.get("amount"))
            if prior >= 40 and delta <= -20:
                wins.append(
                    {
                        "kind": "category_cooled",
                        "subject_type": "category",
                        "subject_key": item["subject_key"],
                        "subject_label": item["subject_label"],
                        "amount": current,
                        "prior_amount": prior,
                        "delta_amount": abs(delta),
                        "summary": f"{item['subject_label']} cooled by {_money(abs(delta))} versus the prior month.",
                        "sensitive": bool(item.get("sensitive")),
                    }
                )
                break
    if not wins and stored_streaks:
        streak = stored_streaks[0]
        wins.append(
            {
                "kind": "habit_streak",
                "subject_type": streak.get("subject_type"),
                "subject_key": streak.get("subject_key"),
                "subject_label": streak.get("subject_label"),
                "streak_length": streak.get("streak_length"),
                "summary": streak.get("summary"),
                "sensitive": False,
            }
        )
    return wins


def _build_friction(*, categories: list[dict[str, Any]], budgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    friction: list[dict[str, Any]] = []
    fee = next((row for row in categories if row["subject_key"] in {"fees_charges", "fees_and_charges", "fees"} and row["amount"] > 0), None)
    if fee:
        friction.append(
            {
                "kind": "avoidable_fee_pressure",
                "subject_type": "category",
                "subject_key": fee["subject_key"],
                "subject_label": fee["subject_label"],
                "amount": fee["amount"],
                "summary": f"{fee['subject_label']} took {_money(fee['amount'])}; check this before trimming normal life spending.",
                "sensitive": False,
            }
        )
    over_budget = next((row for row in budgets if row["delta_amount"] > 20), None)
    if over_budget:
        friction.append(
            {
                "kind": "budget_pressure",
                "subject_type": "category",
                "subject_key": over_budget["subject_key"],
                "subject_label": over_budget["subject_label"],
                "amount": over_budget["spent_amount"],
                "budget_amount": over_budget["budget_amount"],
                "delta_amount": over_budget["delta_amount"],
                "summary": f"{over_budget['subject_label']} finished {_money(over_budget['delta_amount'])} over its monthly budget.",
                "sensitive": False,
            }
        )
    increase = next(
        (
            row
            for row in sorted(categories, key=lambda item: item.get("delta_amount") or 0, reverse=True)
            if _round(row.get("delta_amount")) >= 50 and not row.get("sensitive")
        ),
        None,
    )
    if increase:
        friction.append(
            {
                "kind": "category_warmed",
                "subject_type": "category",
                "subject_key": increase["subject_key"],
                "subject_label": increase["subject_label"],
                "amount": increase["amount"],
                "prior_amount": increase["prior_amount"],
                "delta_amount": increase["delta_amount"],
                "summary": f"{increase['subject_label']} rose by {_money(increase['delta_amount'])} versus the prior month.",
                "sensitive": False,
            }
        )
    return _dedupe_subjects(friction)[:3]


def _build_improvement_themes(*, wins: list[dict[str, Any]], friction: list[dict[str, Any]], merchants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    themes: list[dict[str, Any]] = []
    if friction:
        item = friction[0]
        themes.append(
            {
                "kind": "first_review",
                "subject_type": item.get("subject_type"),
                "subject_key": item.get("subject_key"),
                "subject_label": item.get("subject_label"),
                "summary": f"Start next month by checking {item.get('subject_label')} before changing the whole budget.",
            }
        )
    elif wins:
        item = wins[0]
        themes.append(
            {
                "kind": "protect_win",
                "subject_type": item.get("subject_type"),
                "subject_key": item.get("subject_key"),
                "subject_label": item.get("subject_label"),
                "summary": f"Protect the calmer {item.get('subject_label')} pattern before adding new commitments.",
            }
        )
    elif merchants:
        item = merchants[0]
        themes.append(
            {
                "kind": "merchant_review",
                "subject_type": "merchant",
                "subject_key": item.get("subject_key"),
                "subject_label": item.get("subject_label"),
                "summary": f"Use {item.get('subject_label')} as the first merchant to review next month.",
            }
        )
    return themes


def _compose_summary(
    *,
    month_key: str,
    next_month_key: str,
    target_stats: dict[str, Any],
    wins: list[dict[str, Any]],
    friction: list[dict[str, Any]],
    themes: list[dict[str, Any]],
    confidence: str,
) -> str:
    month_name = _month_name(month_key)
    next_month = _month_name(next_month_key)
    if _round(target_stats.get("refunds")) > 0:
        first = (
            f"{month_name}'s read: income was {_money(target_stats['income'])}, "
            f"spending was {_money(target_stats['expenses'])}, credits/refunds added {_money(target_stats['refunds'])}, "
            f"and net cash flow landed at {_money(target_stats['net'])}."
        )
    else:
        first = (
            f"{month_name}'s read: income was {_money(target_stats['income'])}, "
            f"spending was {_money(target_stats['expenses'])}, and net cash flow landed at {_money(target_stats['net'])}."
        )
    second = wins[0]["summary"] if wins else "There was not enough clean prior-month contrast to call a real win."
    third = friction[0]["summary"] if friction else "No single friction point stood out enough to deserve a loud warning."
    fourth = themes[0]["summary"] if themes else f"For {next_month}, keep watching the baseline before making a bigger plan."
    if confidence == "low":
        fourth += " Treat this read as directional because the comparable history is thin."
    return " ".join([first, second, third, fourth])


def _confidence(*, target_stats: dict[str, Any], prior_stats: dict[str, Any], wins: list[dict[str, Any]], friction: list[dict[str, Any]]) -> str:
    if target_stats["transaction_count"] >= 20 and prior_stats.get("transaction_count") and (wins or friction):
        return "high"
    if target_stats["transaction_count"] >= 8:
        return "medium"
    return "low"


def _caveats(*, target_stats: dict[str, Any], prior_stats: dict[str, Any], recurring: dict[str, Any]) -> list[str]:
    caveats: list[str] = []
    if not prior_stats.get("transaction_count"):
        caveats.append("No prior-month comparison was available.")
    if target_stats["transaction_count"] < 20:
        caveats.append("The month has a modest visible transaction count.")
    if recurring.get("count", 0) <= 0:
        caveats.append("No active recurring-obligation records were seen in the closed month.")
    return caveats[:3]


def _public_retrospective(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "wins": _json_load(row.get("wins_json"), []),
        "friction": _json_load(row.get("friction_json"), []),
        "improvement_themes": _json_load(row.get("improvement_themes_json"), []),
        "evidence": _json_load(row.get("evidence_json"), {}),
    }


def _normalize_diary(diary: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile_id": _profile_scope(diary.get("profile_id")),
        "month_key": str(diary.get("month_key") or "").strip(),
        "summary": _clean_text(diary.get("summary"), 900),
        "wins": diary.get("wins") if isinstance(diary.get("wins"), list) else [],
        "friction": diary.get("friction") if isinstance(diary.get("friction"), list) else [],
        "improvement_themes": diary.get("improvement_themes") if isinstance(diary.get("improvement_themes"), list) else [],
        "evidence": diary.get("evidence") if isinstance(diary.get("evidence"), dict) else {},
        "confidence": _enum(diary.get("confidence"), CONFIDENCE_STATES, "medium"),
        "generated_at": str(diary.get("generated_at") or _now_iso()),
    }


def _unsupported_dollar_claims(summary: str, diary: dict[str, Any]) -> list[str]:
    allowed = _collect_numbers(diary)
    unsupported: list[str] = []
    for match in DOLLAR_RE.findall(summary):
        value = round(float(match.replace(",", "")), 2)
        if not any(abs(value - candidate) <= 0.01 for candidate in allowed):
            unsupported.append(match)
    return unsupported


def _collect_numbers(value: Any) -> set[float]:
    numbers: set[float] = set()
    if isinstance(value, dict):
        for child in value.values():
            numbers.update(_collect_numbers(child))
    elif isinstance(value, list):
        for child in value:
            numbers.update(_collect_numbers(child))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        numbers.add(round(abs(float(value)), 2))
    return numbers


def _sensitive_terms_visible(summary: str, diary: dict[str, Any]) -> bool:
    lowered = summary.lower()
    has_sensitive_item = any(
        bool(item.get("sensitive"))
        for item in [*(diary.get("wins") or []), *(diary.get("friction") or [])]
        if isinstance(item, dict)
    )
    return has_sensitive_item and any(term in lowered for term in SENSITIVE_HINTS)


def _dedupe_subjects(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (str(item.get("kind") or ""), str(item.get("subject_type") or ""), str(item.get("subject_key") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _resolve_month_key(*, month_key: str | None, as_of: str | date | datetime | None = None) -> str:
    if month_key:
        clean = str(month_key).strip()[:7]
        if not MONTH_RE.match(clean):
            raise ValueError("month_key must be YYYY-MM.")
        return clean
    as_of_date = _parse_date(as_of)
    current = as_of_date.replace(day=1)
    prior = current - timedelta(days=1)
    return prior.strftime("%Y-%m")


def _shift_month_key(month_key: str, delta: int) -> str:
    year, month = int(month_key[:4]), int(month_key[5:7])
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _month_bounds(month_key: str) -> tuple[str, str]:
    year, month = int(month_key[:4]), int(month_key[5:7])
    end_day = calendar.monthrange(year, month)[1]
    return f"{month_key}-01", f"{month_key}-{end_day:02d}"


def _month_name(month_key: str) -> str:
    year, month = int(month_key[:4]), int(month_key[5:7])
    return f"{calendar.month_name[month]} {year}"


def _transfer_ok_clause(profile: str | None) -> str:
    if not profile or profile == "household":
        return "AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))"
    return "AND (expense_type IS NULL OR expense_type != 'transfer_internal')"


def _profile_where(profile: str | None, column: str) -> tuple[str, list[Any]]:
    scope = _profile_scope(profile)
    if not scope or scope == "household":
        return "", []
    return f"AND {column} = ?", [scope]


def _profile_scope(profile: str | None) -> str:
    return str(profile or "household").strip() or "household"


def _key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:100] or "unknown"


def _enum(value: Any, allowed: set[str], default: str) -> str:
    text = _key(value)
    return text if text in allowed else default


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _is_sensitive(value: Any) -> bool:
    lowered = str(value or "").lower()
    return any(term in lowered for term in SENSITIVE_HINTS)


def _display_label(value: Any, *, sensitive: bool) -> str:
    if sensitive:
        return "Private category"
    return _clean_text(value, 120) or "Unknown"


def _money(value: Any) -> str:
    return f"${_round(value):,.2f}"


def _round(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(profile: str, diary: dict[str, Any]) -> str:
    payload = {
        "version": RETROSPECTIVE_VERSION,
        "profile_id": profile,
        "month_key": diary.get("month_key"),
        "summary": diary.get("summary"),
        "wins": diary.get("wins"),
        "friction": diary.get("friction"),
        "themes": diary.get("improvement_themes"),
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
    "build_monthly_retrospective",
    "dismiss_monthly_retrospective",
    "ensure_monthly_retrospective_tables",
    "generate_monthly_retrospective",
    "get_monthly_retrospective",
    "list_monthly_retrospectives",
    "monthly_retrospective_context",
    "monthly_retrospective_enabled",
    "store_monthly_retrospective",
    "validate_monthly_retrospective",
]
