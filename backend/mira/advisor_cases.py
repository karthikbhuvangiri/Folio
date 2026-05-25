"""Advisor-style Mira cards built from the safe finance query layer.

These cards are intentionally not chat-time LLM output. Python builds a small
case record from deterministic measurements, stores the evidence, and the UI
can use the card as a starting point for a deeper Mira conversation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from hashlib import sha1
from typing import Any

from mira.safe_finance_query import build_advisor_dossier


STORE_VERSION = "mira_advisor_cases_v1"
_FALSE_VALUES = {"0", "false", "no", "off"}
_HIGH_SENSITIVITY_TERMS = (
    "abortion",
    "adult",
    "alcohol",
    "bail",
    "casino",
    "debt",
    "fertility",
    "firearm",
    "gambling",
    "loan",
    "medical",
    "payday",
    "therapy",
    "tobacco",
    "vaping",
    "juul",
)
_MOTIVE_TERMS = (
    "because you are",
    "because you're",
    "you are stressed",
    "you're stressed",
    "you are anxious",
    "you're anxious",
    "you lack discipline",
    "impulsive",
)
_INTERNAL_TERMS = (
    "backend",
    "bundle",
    "deterministic",
    "evidence_id",
    "run_sql",
    "sql",
    "tool registry",
)


def advisor_cases_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_CASES_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_cards_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_CARDS_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_card_max_count() -> int:
    try:
        return max(1, min(int(os.getenv("MIRA_ADVISOR_CARD_MAX_COUNT", "4")), 8))
    except Exception:
        return 4


def advisor_cases_need_refresh(*, conn, profile: str | None = None) -> bool:
    if not advisor_cases_enabled():
        return False
    _ensure_tables(conn)
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
          FROM mira_advisor_cases
         WHERE profile_id = ?
           AND status = 'active'
           AND (valid_until IS NULL OR valid_until > ?)
        """,
        (_scope_profile(profile), _now()),
    ).fetchone()
    return int(row["count"] if hasattr(row, "keys") else row[0]) == 0


def refresh_advisor_cases(*, conn, profile: str | None = None, force: bool = False) -> dict[str, Any]:
    """Refresh stored advisor cases from deterministic safe measurements."""

    if not advisor_cases_enabled():
        return {"status": "disabled", "stored_count": 0, "case_count": 0}
    _ensure_tables(conn)
    if not force and not advisor_cases_need_refresh(conn=conn, profile=profile):
        return {"status": "fresh_cache", "stored_count": 0, "case_count": len(list_advisor_cases(conn=conn, profile=profile))}

    dossier = build_advisor_dossier(
        conn,
        "What should I notice or do differently next month?",
        profile=profile,
        query_plan={"queries": _advisor_case_queries()},
    )
    candidates = _build_cases_from_dossier(dossier)
    stored = 0
    for case in candidates:
        if _store_case(conn, profile=profile, case=case):
            stored += 1
    return {
        "status": "ok",
        "stored_count": stored,
        "case_count": len(candidates),
        "confidence": dossier.get("confidence") or "medium",
        "dossier_measurement_count": len(dossier.get("measurements") or []),
    }


def list_advisor_cases(
    *,
    conn,
    profile: str | None = None,
    include_dismissed: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not advisor_cards_enabled():
        return []
    _ensure_tables(conn)
    max_count = limit or advisor_card_max_count()
    status_clause = "" if include_dismissed else "AND status = 'active'"
    rows = conn.execute(
        f"""
        SELECT *
          FROM mira_advisor_cases
         WHERE profile_id = ?
           {status_clause}
           AND (valid_until IS NULL OR valid_until > ? OR status = 'dismissed')
         ORDER BY
           CASE case_type
             WHEN 'cash_resilience' THEN 0
             WHEN 'spend_driver' THEN 1
             WHEN 'smallest_lever' THEN 2
             WHEN 'false_alarm' THEN 3
             ELSE 4
           END,
           CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
           generated_at DESC,
           id DESC
         LIMIT ?
        """,
        (_scope_profile(profile), _now(), max_count),
    ).fetchall()
    return [_row_to_case(row) for row in rows]


def list_or_refresh_advisor_cases(*, conn, profile: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    if not advisor_cases_enabled() or not advisor_cards_enabled():
        return []
    _ensure_tables(conn)
    items = list_advisor_cases(conn=conn, profile=profile, limit=limit)
    if items:
        return items
    refresh_advisor_cases(conn=conn, profile=profile, force=True)
    return list_advisor_cases(conn=conn, profile=profile, limit=limit)


def dismiss_advisor_case(*, conn, case_id: int, profile: str | None = None) -> bool:
    _ensure_tables(conn)
    cursor = conn.execute(
        """
        UPDATE mira_advisor_cases
           SET status = 'dismissed'
         WHERE id = ? AND profile_id = ?
        """,
        (case_id, _scope_profile(profile)),
    )
    return cursor.rowcount > 0


def _advisor_case_queries() -> list[dict[str, Any]]:
    metrics = (
        "cash_runway",
        "cash_low_point",
        "category_driver_decomposition",
        "merchant_driver_decomposition",
        "small_frequent_leak",
        "frequency_vs_ticket_size",
        "subscription_cluster",
        "fixed_vs_flexible_pressure",
        "weekend_weekday_split",
        "payday_window_spending",
        "category_false_alarm",
        "data_quality_caveats",
    )
    return [
        {
            "metric": metric,
            "range": "last_6_months",
            "dimensions": [],
            "filters": {"exclude_transfers": True, "profile_scope": "active"},
            "purpose": "Build Mira advisor cards.",
            "limit": 12,
        }
        for metric in metrics
    ]


def _build_cases_from_dossier(dossier: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    constraints = dossier.get("constraints") or []
    drivers = [d for d in (dossier.get("candidate_drivers") or []) if _sensitivity_for(d.get("subject")) != "high"]
    levers = [l for l in (dossier.get("smallest_levers") or []) if _sensitivity_for(l.get("subject")) != "high"]
    false_alarms = [
        f
        for f in (dossier.get("false_alarms") or [])
        if _sensitivity_for(f.get("subject")) != "high" and _subject(f).lower() not in {"other", "misc", "miscellaneous"}
    ]
    confidence = str(dossier.get("confidence") or "medium").lower()
    caveat = _first_caveat(dossier)

    runway = _constraint_for(constraints, "cash_runway")
    if runway:
        numbers = runway.get("summary_numbers") or {}
        runway_days = _number(numbers.get("cash_runway_days"))
        buffer_gap = _number(numbers.get("one_month_buffer_gap"))
        if buffer_gap > 0:
            body = f"Your cash buffer is short by {_money(buffer_gap)}. The useful move is not a pep talk; it is protecting the next month of fixed obligations first."
            lever = "Protect the next month of fixed obligations before trimming smaller categories."
        elif runway_days >= 60:
            body = "Your cash runway looks sturdy. That makes the next move optimization, not emergency mode: tune the repeatable leaks before they become furniture."
            lever = "Use the breathing room to review repeat expenses instead of chasing tiny one-offs."
        else:
            body = f"Runway is about {int(runway_days)} days. That is close enough to deserve a calm plan before the calendar gets bossy."
            lever = "Prioritize cash timing before discretionary clean-up."
        cases.append(
            _case(
                case_type="cash_resilience",
                title="Runway Sets The Mood",
                body=body,
                constraint="cash_runway",
                driver=(drivers[0].get("subject") if drivers else ""),
                pattern="cash runway versus normal burn",
                false_alarms=[],
                lever=lever,
                evidence=runway.get("metric"),
                evidence_ids=_evidence_from_constraint(runway),
                confidence=runway.get("confidence") or confidence,
                sensitivity="low",
                caveat=caveat,
                seed="What is Mira's read on my cash runway and spending pressure?",
            )
        )

    if drivers:
        driver = drivers[0]
        subject = _subject(driver)
        amount = _number(driver.get("amount"))
        cases.append(
            _case(
                case_type="spend_driver",
                title=f"{_title_subject(subject)} Is The Loud Part",
                body=f"{subject} is the biggest recent pressure point at about {_money(amount)}. If it was a one-off, do not overcorrect elsewhere; if it repeats, it is the fix-first line.",
                constraint=(constraints[0].get("metric") if constraints else ""),
                driver=subject,
                pattern=str(driver.get("basis") or "recent driver versus baseline"),
                false_alarms=false_alarms[:2],
                lever=f"Review whether {subject} is one-off noise or a repeatable habit.",
                evidence=driver.get("metric"),
                evidence_ids=driver.get("evidence_ids") or [],
                confidence=confidence,
                sensitivity="low",
                caveat=caveat,
                seed=f"Why is {subject} showing up as the biggest pressure point?",
            )
        )

    lever = _first_distinct_lever(levers, drivers)
    if lever:
        subject = _subject(lever)
        amount = _number(lever.get("amount"))
        cases.append(
            _case(
                case_type="smallest_lever",
                title=f"{_title_subject(subject)} Is A Small Lever",
                body=f"{subject} shows up as a repeatable lever around {_money(amount)}. Not a crisis, just the kind of background hum that gets expensive when nobody looks at it.",
                constraint=(constraints[0].get("metric") if constraints else ""),
                driver=subject,
                pattern=str(lever.get("metric") or "repeatable spending lever"),
                false_alarms=false_alarms[:2],
                lever=f"Review the repeat pattern for {subject} before cutting broad categories.",
                evidence=lever.get("metric"),
                evidence_ids=lever.get("evidence_ids") or [],
                confidence=confidence,
                sensitivity="low",
                caveat=caveat,
                seed=f"What should I do about my {subject} pattern?",
            )
        )

    if false_alarms:
        alarm = false_alarms[0]
        subject = _subject(alarm)
        cases.append(
            _case(
                case_type="false_alarm",
                title=f"Do Not Chase { _title_subject(subject) } Yet",
                body=f"{subject} looks noisy, but the evidence marks it as low materiality versus the bigger moves. Tiny steering wheel, wrong road.",
                constraint=(constraints[0].get("metric") if constraints else ""),
                driver=(drivers[0].get("subject") if drivers else ""),
                pattern=str(alarm.get("reason") or "low materiality"),
                false_alarms=[alarm],
                lever=f"Focus on the larger driver before optimizing {subject}.",
                evidence=alarm.get("metric"),
                evidence_ids=alarm.get("evidence_ids") or [f"metric:{alarm.get('metric') or 'false_alarm'}:summary"],
                confidence=confidence,
                sensitivity="low",
                caveat=caveat,
                seed=f"What should I ignore versus fix first in my spending?",
            )
        )

    cleaned = []
    seen_types = set()
    for case in cases:
        if case["case_type"] in seen_types:
            continue
        rejection = _case_rejection_reason(case)
        if rejection:
            continue
        seen_types.add(case["case_type"])
        cleaned.append(case)
    return cleaned[: advisor_card_max_count()]


def _case(
    *,
    case_type: str,
    title: str,
    body: str,
    constraint: str,
    driver: str,
    pattern: str,
    false_alarms: list[dict[str, Any]],
    lever: str,
    evidence: Any,
    evidence_ids: list[Any],
    confidence: str,
    sensitivity: str,
    caveat: str,
    seed: str,
) -> dict[str, Any]:
    payload = {
        "case_type": case_type,
        "constraint": _clean(constraint, 160),
        "driver": _clean(driver, 160),
        "pattern": _clean(pattern, 240),
        "false_alarms": false_alarms[:3],
        "smallest_lever": _clean(lever, 260),
        "evidence": {"metric": evidence, "evidence_ids": [str(v) for v in (evidence_ids or [])[:8]]},
        "confidence": confidence if confidence in {"high", "medium", "low"} else "medium",
        "sensitivity": sensitivity if sensitivity in {"low", "medium", "high"} else "low",
        "card_title": _clean(title, 72),
        "card_body": _clean(body, 260),
        "caveat": _clean(caveat, 160),
        "chat_seed_question": _clean(seed, 180),
    }
    payload["fingerprint"] = _fingerprint(payload)
    return payload


def _store_case(conn, *, profile: str | None, case: dict[str, Any]) -> bool:
    generated_at = _now()
    valid_until = (datetime.utcnow() + timedelta(days=7)).isoformat(timespec="seconds") + "Z"
    params = (
        _scope_profile(profile),
        case["case_type"],
        case["constraint"],
        case["driver"],
        case["pattern"],
        json.dumps(case["false_alarms"], sort_keys=True),
        case["smallest_lever"],
        json.dumps(case["evidence"], sort_keys=True),
        case["confidence"],
        case["sensitivity"],
        case["card_title"],
        case["card_body"],
        case["caveat"],
        case["chat_seed_question"],
        generated_at,
        valid_until,
        "active",
        case["fingerprint"],
        STORE_VERSION,
    )
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO mira_advisor_cases (
            profile_id, case_type, constraint_key, driver_key, pattern,
            false_alarms_json, smallest_lever, evidence_json, confidence,
            sensitivity, card_title, card_body, caveat, chat_seed_question,
            generated_at, valid_until, status, fingerprint, version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
            case_type = excluded.case_type,
            constraint_key = excluded.constraint_key,
            driver_key = excluded.driver_key,
            pattern = excluded.pattern,
            false_alarms_json = excluded.false_alarms_json,
            smallest_lever = excluded.smallest_lever,
            evidence_json = excluded.evidence_json,
            confidence = excluded.confidence,
            sensitivity = excluded.sensitivity,
            card_title = excluded.card_title,
            card_body = excluded.card_body,
            caveat = excluded.caveat,
            chat_seed_question = excluded.chat_seed_question,
            generated_at = excluded.generated_at,
            valid_until = excluded.valid_until,
            version = excluded.version,
            status = CASE
                WHEN mira_advisor_cases.status = 'dismissed' THEN mira_advisor_cases.status
                ELSE excluded.status
            END
        """,
        params,
    )
    return conn.total_changes > before


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mira_advisor_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            case_type TEXT NOT NULL,
            constraint_key TEXT DEFAULT '',
            driver_key TEXT DEFAULT '',
            pattern TEXT DEFAULT '',
            false_alarms_json TEXT DEFAULT '[]',
            smallest_lever TEXT DEFAULT '',
            evidence_json TEXT DEFAULT '{}',
            confidence TEXT DEFAULT 'medium',
            sensitivity TEXT DEFAULT 'low',
            card_title TEXT NOT NULL,
            card_body TEXT NOT NULL,
            caveat TEXT DEFAULT '',
            chat_seed_question TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            valid_until TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            fingerprint TEXT NOT NULL,
            version TEXT DEFAULT '',
            UNIQUE(profile_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mira_advisor_cases_profile_status
            ON mira_advisor_cases(profile_id, status, generated_at DESC)
        """
    )


def _row_to_case(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "case_type": data["case_type"],
        "constraint": data.get("constraint_key") or "",
        "driver": data.get("driver_key") or "",
        "pattern": data.get("pattern") or "",
        "false_alarms": _json_load(data.get("false_alarms_json"), []),
        "smallest_lever": data.get("smallest_lever") or "",
        "evidence": _json_load(data.get("evidence_json"), {}),
        "confidence": data.get("confidence") or "medium",
        "sensitivity": data.get("sensitivity") or "low",
        "title": data.get("card_title") or "",
        "body": data.get("card_body") or "",
        "caveat": data.get("caveat") or "",
        "chat_seed_question": data.get("chat_seed_question") or "",
        "generated_at": data.get("generated_at"),
        "valid_until": data.get("valid_until"),
        "status": data.get("status") or "active",
        "version": data.get("version") or "",
    }


def _constraint_for(constraints: list[dict[str, Any]], metric: str) -> dict[str, Any] | None:
    for item in constraints:
        if item.get("metric") == metric:
            return item
    return None


def _evidence_from_constraint(item: dict[str, Any]) -> list[str]:
    metric = item.get("metric") or "constraint"
    return [f"metric:{metric}:summary"]


def _first_distinct_lever(levers: list[dict[str, Any]], drivers: list[dict[str, Any]]) -> dict[str, Any] | None:
    driver_subjects = {_subject(item).lower() for item in drivers[:2]}
    for lever in levers:
        if _subject(lever).lower() not in driver_subjects:
            return lever
    return levers[0] if levers else None


def _first_caveat(dossier: dict[str, Any]) -> str:
    caveats = [str(v).strip() for v in (dossier.get("caveats") or []) if str(v).strip()]
    return caveats[0] if caveats else ""


def _case_rejection_reason(case: dict[str, Any]) -> str:
    visible = f"{case.get('card_title')} {case.get('card_body')} {case.get('smallest_lever')}".lower()
    if case.get("sensitivity") == "high":
        return "sensitive"
    if any(term in visible for term in _INTERNAL_TERMS):
        return "internal_term"
    if any(term in visible for term in _MOTIVE_TERMS):
        return "motive_attribution"
    if len(str(case.get("card_title") or "")) > 72 or len(str(case.get("card_body") or "")) > 260:
        return "too_long"
    if not case.get("evidence", {}).get("metric"):
        return "missing_evidence_metric"
    if not case.get("smallest_lever"):
        return "missing_lever"
    return ""


def _fingerprint(case: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "version": STORE_VERSION,
            "case_type": case.get("case_type"),
            "constraint": case.get("constraint"),
            "driver": case.get("driver"),
            "pattern": case.get("pattern"),
            "evidence": case.get("evidence"),
        },
        sort_keys=True,
        default=str,
    )
    return sha1(seed.encode("utf-8")).hexdigest()[:24]


def _scope_profile(profile: str | None) -> str:
    return profile if profile and profile != "household" else "household"


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _json_load(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
        return value if value is not None else default
    except Exception:
        return default


def _subject(item: dict[str, Any]) -> str:
    return _clean(item.get("subject") or item.get("driver") or item.get("metric") or "this pattern", 80)


def _title_subject(value: str) -> str:
    text = _clean(value, 40)
    return text[:1].upper() + text[1:] if text else "This"


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())[:limit]


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _money(value: Any) -> str:
    amount = abs(_number(value))
    if amount >= 1000:
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def _sensitivity_for(value: Any) -> str:
    text = str(value or "").lower()
    return "high" if any(term in text for term in _HIGH_SENSITIVITY_TERMS) else "low"


__all__ = [
    "advisor_card_max_count",
    "advisor_cards_enabled",
    "advisor_cases_enabled",
    "advisor_cases_need_refresh",
    "dismiss_advisor_case",
    "list_advisor_cases",
    "list_or_refresh_advisor_cases",
    "refresh_advisor_cases",
]
