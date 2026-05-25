"""
Deterministic proactive insights for Mira.

The generator intentionally uses closed, explainable checks over local data:
category spend spikes, recurring/subscription changes, and safe-to-spend risk.
"""

from __future__ import annotations

import calendar
import json
import os
from datetime import date, datetime, timedelta
from typing import Any

from database import dicts_from_rows, get_db
from mira import cashflow_forecast


NON_SPENDING_CATEGORIES = {
    "Income",
    "Credits & Refunds",
    "Savings Transfer",
    "Personal Transfer",
    "Credit Card Payment",
}

DETECTOR_CONTRACT_VERSION = "deterministic_insights_v2"
_ALLOWED_CONFIDENCE = {"high", "medium", "user"}
_ALLOWED_SEVERITY = {"critical", "warning", "info"}
_DEFAULT_REFRESH_MINUTES = 60

DETECTOR_CONTRACTS = {
    "cashflow_shortfall": {
        "fact": "Projected cash may cross or approach the configured buffer.",
        "required_evidence": ("warning", "projected_low_point"),
    },
    "recurring_change": {
        "fact": "A recent recurring-event detector emitted a change signal.",
        "required_evidence": ("merchant_key", "event_type", "period_bucket"),
    },
    "budget_pace": {
        "fact": "Current-month spend is pacing above the configured monthly budget.",
        "required_evidence": ("month", "category", "spent", "budget", "projected"),
    },
    "goal_followup": {
        "fact": "Goal progress appears behind the expected pace or below early progress.",
        "required_evidence": ("goal_id", "target_amount", "current_amount", "progress_ratio"),
    },
    "merchant_anomaly": {
        "fact": "A current-month merchant charge is materially above its recent average.",
        "required_evidence": ("transaction_id", "merchant", "amount", "prior_average", "sample_size", "date"),
    },
    "category_spike": {
        "fact": "Current-month category spend is materially above recent monthly average.",
        "required_evidence": ("month", "category", "current_total", "prior_average", "prior_months"),
    },
    "duplicate_subscription": {
        "fact": "Multiple active or candidate recurring obligations appear similar.",
        "required_evidence": ("category", "combined_amount", "items"),
    },
    "recurring_calendar": {
        "fact": "An active recurring obligation has an expected date in the next week.",
        "required_evidence": ("merchant_key", "next_expected_date", "expected_amount"),
    },
    "recurring_stopped": {
        "fact": "An active recurring obligation has passed its expected date without a matching charge.",
        "required_evidence": ("merchant_key", "next_expected_date", "last_seen_date"),
    },
    "safe_to_spend": {
        "fact": "The current monthly plan shows low or negative safe-to-spend.",
        "required_evidence": ("month", "safe_to_spend", "safe_to_spend_limit"),
    },
}


def _scope_profile(profile: str | None) -> str:
    return profile if profile and profile != "household" else "household"


def _insights_v2_enabled() -> bool:
    return os.getenv("MIRA_DETERMINISTIC_INSIGHTS_V2_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _refresh_minutes() -> int:
    try:
        return max(5, int(os.getenv("MIRA_PROACTIVE_INSIGHTS_REFRESH_MINUTES", str(_DEFAULT_REFRESH_MINUTES))))
    except (TypeError, ValueError):
        return _DEFAULT_REFRESH_MINUTES


def _profile_clause(profile: str | None, column: str = "profile_id") -> tuple[str, list[Any]]:
    if profile and profile != "household":
        return f" AND {column} = ?", [profile]
    return "", []


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    zero_based = (year * 12 + (month - 1)) + offset
    return zero_based // 12, zero_based % 12 + 1


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _money(value: float | int | None) -> str:
    return f"${float(value or 0):,.0f}"


def _money_exact(value: float | int | None) -> str:
    return f"${float(value or 0):,.2f}"


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dump(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _with_contract(insight: dict) -> dict | None:
    contract = DETECTOR_CONTRACTS.get(str(insight.get("kind") or ""))
    if not contract:
        return None
    evidence = insight.get("evidence")
    if not isinstance(evidence, dict):
        return None
    missing = [key for key in contract["required_evidence"] if not _has_value(evidence.get(key))]
    if missing:
        return None
    confidence = str(insight.get("confidence") or "medium").lower()
    if confidence not in _ALLOWED_CONFIDENCE:
        return None
    severity = str(insight.get("severity") or "info").lower()
    if severity not in _ALLOWED_SEVERITY:
        return None

    stamped = {**insight, "confidence": confidence, "severity": severity}
    stamped_evidence = dict(evidence)
    stamped_evidence["detector_contract"] = {
        "version": DETECTOR_CONTRACT_VERSION,
        "fact": contract["fact"],
        "required_evidence": list(contract["required_evidence"]),
    }
    stamped["evidence"] = stamped_evidence
    return stamped


def _filter_contract_candidates(candidates: list[dict]) -> list[dict]:
    if not _insights_v2_enabled():
        return [
            item for item in candidates
            if item.get("evidence") and str(item.get("confidence") or "medium").lower() != "low"
        ]
    filtered = []
    for item in candidates:
        stamped = _with_contract(item)
        if stamped:
            filtered.append(stamped)
    return filtered


def _has_fresh_cached_insights(conn, profile: str | None) -> bool:
    if not _insights_v2_enabled():
        return False
    scope = _scope_profile(profile)
    minutes = _refresh_minutes()
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
          FROM proactive_insights
         WHERE profile_id = ?
           AND status = 'active'
           AND (valid_until IS NULL OR date(valid_until) >= date('now'))
           AND datetime(generated_at) >= datetime('now', ?)
        """,
        (scope, f"-{minutes} minutes"),
    ).fetchone()
    try:
        return int(row[0] if row is not None else 0) > 0
    except (TypeError, ValueError):
        return False


def _severity_rank(value: str) -> int:
    return {"critical": 0, "warning": 1, "info": 2}.get(value, 3)


def _priority(insight: dict) -> int:
    try:
        return int(insight.get("priority") or {"critical": 10, "warning": 30, "info": 60}.get(insight.get("severity"), 80))
    except (TypeError, ValueError):
        return 80


def _compact_identity(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _insert_or_refresh(conn, profile: str | None, insight: dict) -> None:
    scope = _scope_profile(profile)
    conn.execute(
        """
        INSERT INTO proactive_insights (
            profile_id, kind, insight_type, title, body, severity, priority, confidence,
            evidence_json, assumptions_json, recommended_action, fingerprint, status,
            generated_at, valid_until
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', datetime('now'), ?)
        ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
            kind = excluded.kind,
            insight_type = excluded.insight_type,
            title = excluded.title,
            body = excluded.body,
            severity = excluded.severity,
            priority = excluded.priority,
            confidence = excluded.confidence,
            evidence_json = excluded.evidence_json,
            assumptions_json = excluded.assumptions_json,
            recommended_action = excluded.recommended_action,
            valid_until = excluded.valid_until,
            status = CASE
                WHEN proactive_insights.status = 'dismissed' THEN proactive_insights.status
                ELSE 'active'
            END,
            suppressed_at = CASE
                WHEN proactive_insights.status = 'dismissed' THEN proactive_insights.suppressed_at
                ELSE NULL
            END,
            generated_at = CASE
                WHEN proactive_insights.status = 'dismissed' THEN proactive_insights.generated_at
                ELSE excluded.generated_at
            END
        """,
        (
            scope,
            insight["kind"],
            insight.get("insight_type") or insight["kind"],
            insight["title"],
            insight["body"],
            insight.get("severity") or "info",
            _priority(insight),
            insight.get("confidence") or "",
            _json_dump(insight.get("evidence")),
            _json_dump(insight.get("assumptions") or []),
            insight.get("recommended_action") or "",
            insight["fingerprint"],
            insight.get("valid_until"),
        ),
    )


def upsert_insight(profile: str | None, insight: dict, conn=None) -> None:
    """Store one validated proactive insight."""
    if conn is not None:
        _insert_or_refresh(conn, profile, insight)
        return
    with get_db() as c:
        _insert_or_refresh(c, profile, insight)


def _list_rows(conn, profile: str | None, include_dismissed: bool = False) -> list[dict]:
    scope = _scope_profile(profile)
    status_clause = "" if include_dismissed else " AND status = 'active'"
    rows = dicts_from_rows(
        conn.execute(
            f"""
            SELECT id, profile_id, kind, COALESCE(insight_type, kind) AS insight_type,
                   title, body, severity, priority, confidence, evidence_json,
                   assumptions_json, recommended_action, fingerprint, status,
                   generated_at, dismissed_at, dismissed_reason, dismissed_type,
                   restored_at, valid_until, suppressed_at
            FROM proactive_insights
            WHERE profile_id = ?{status_clause}
              AND (valid_until IS NULL OR date(valid_until) >= date('now'))
            ORDER BY priority ASC, generated_at DESC, id DESC
            LIMIT {12 if include_dismissed else 3}
            """,
            (scope,),
        ).fetchall()
    )
    for row in rows:
        row["evidence"] = _json_load(row.pop("evidence_json", "{}"))
        assumptions = _json_load(row.pop("assumptions_json", "[]"))
        row["assumptions"] = assumptions if isinstance(assumptions, list) else []
    return sorted(rows, key=lambda row: (_priority(row), _severity_rank(row.get("severity") or "info"), row.get("generated_at") or ""), reverse=False)


def _category_spike_candidates(conn, profile: str | None, today: date) -> list[dict]:
    current = _month_key(today.year, today.month)
    start_year, start_month = _shift_month(today.year, today.month, -3)
    end_year, end_month = _shift_month(today.year, today.month, 1)
    start = f"{_month_key(start_year, start_month)}-01"
    end = f"{_month_key(end_year, end_month)}-01"
    profile_sql, profile_params = _profile_clause(profile)

    rows = dicts_from_rows(
        conn.execute(
            f"""
            SELECT substr(date, 1, 7) AS month,
                   COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category,
                   COALESCE(SUM(ABS(amount)), 0) AS total
            FROM transactions_visible
            WHERE amount < 0
              AND date >= ?
              AND date < ?
              AND COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized')
                  NOT IN ({",".join("?" for _ in NON_SPENDING_CATEGORIES)})
              {profile_sql}
            GROUP BY month, category
            """,
            [start, end, *sorted(NON_SPENDING_CATEGORIES), *profile_params],
        ).fetchall()
    )

    by_category: dict[str, dict[str, float]] = {}
    for row in rows:
        by_category.setdefault(row["category"], {})[row["month"]] = float(row["total"] or 0)

    candidates: list[dict] = []
    prior_months = [_month_key(*_shift_month(today.year, today.month, offset)) for offset in (-3, -2, -1)]
    scope = _scope_profile(profile)
    for category, month_totals in by_category.items():
        current_total = month_totals.get(current, 0.0)
        priors = [month_totals.get(month, 0.0) for month in prior_months]
        nonzero_priors = [value for value in priors if value > 0]
        if len(nonzero_priors) < 2:
            continue
        avg = sum(nonzero_priors) / len(nonzero_priors)
        lift = current_total - avg
        if current_total < 75 or avg < 25 or lift < 50 or current_total < avg * 1.6:
            continue
        severity = "warning" if current_total >= avg * 2 else "info"
        candidates.append(
            {
                "kind": "category_spike",
                "insight_type": "spending_anomaly",
                "title": f"{category} is running higher",
                "body": (
                    f"{category} spending is {_money(current_total)} this month, "
                    f"versus a recent average of {_money(avg)}."
                ),
                "severity": severity,
                "priority": 42,
                "confidence": "medium",
                "fingerprint": f"{scope}:category_spike:{current}:{category.lower()}",
                "recommended_action": f"Open recent {category} transactions and decide whether this is expected.",
                "evidence": {
                    "month": current,
                    "category": category,
                    "current_total": round(current_total, 2),
                    "prior_average": round(avg, 2),
                    "prior_months": prior_months,
                    "prior_sample_months": len(nonzero_priors),
                },
            }
        )

    return sorted(candidates, key=lambda item: item["evidence"]["current_total"] - item["evidence"]["prior_average"], reverse=True)[:2]


def _recurring_candidates(conn, profile: str | None, today: date) -> list[dict]:
    profile_sql, profile_params = _profile_clause(profile, "e.profile_id")
    cutoff = (today - timedelta(days=14)).isoformat()
    rows = dicts_from_rows(
        conn.execute(
            f"""
            SELECT e.merchant_key, e.profile_id, e.event_type, e.period_bucket,
                   e.payload_json, e.created_at,
                   o.display_name, o.amount_cents, o.state, o.next_expected_date,
                   o.confidence_label, o.source, o.confidence_score, o.obligation_key
            FROM recurring_events_v2 e
            LEFT JOIN recurring_obligations o
              ON o.profile_id = e.profile_id
             AND o.merchant_key = e.merchant_key
             AND COALESCE(o.source, '') != 'user'
            WHERE date(e.created_at) >= date(?)
              {profile_sql}
            ORDER BY e.created_at DESC,
                     CASE COALESCE(o.source, '')
                       WHEN 'seed' THEN 0
                       WHEN 'algorithm' THEN 1
                       WHEN 'category' THEN 2
                       ELSE 3
                     END,
                     o.confidence_score DESC
            LIMIT 6
            """,
            [cutoff, *profile_params],
        ).fetchall()
    )
    scope = _scope_profile(profile)
    candidates = []
    seen_events = set()
    for row in rows:
        event_key = (row.get("merchant_key"), row.get("event_type"), row.get("period_bucket"))
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        state = str(row.get("state") or "").lower()
        if state in {"inactive", "stale", "dismissed", "cancelled"}:
            continue
        event_type = row.get("event_type") or "recurring_update"
        event_words = event_type.replace("-", "_").split("_")
        merchant = row.get("display_name") or row.get("merchant_key") or "A recurring charge"
        payload = _json_load(row.get("payload_json"))
        amount = float(row.get("amount_cents") or 0) / 100
        old_amount = _float_or_none(payload.get("old_amount"))
        new_amount = _float_or_none(payload.get("new_amount"))
        if event_type == "price_decrease":
            title = f"{merchant} recurring amount may be lower"
            body = f"A recurring detector found a possible price decrease for {merchant}."
        elif event_type == "price_increase":
            title = f"{merchant} recurring amount may be higher"
            body = f"A recurring detector found a possible price increase for {merchant}."
        elif "amount" in event_words or "changed" in event_words:
            title = f"{merchant} may have changed"
            body = f"A recurring-charge change was found for {merchant}."
        elif "new" in event_words or "created" in event_words:
            title = f"New recurring charge: {merchant}"
            body = f"{merchant} looks like a new recurring charge."
        else:
            title = f"Recurring update: {merchant}"
            body = f"A recent recurring activity signal was found for {merchant}."
        if old_amount is not None and new_amount is not None:
            body = f"{body} Reported amount moved from {_money_exact(old_amount)} to {_money_exact(new_amount)}."
        elif amount > 0:
            body = f"{body} Current expected amount is about {_money(amount)}."
        candidates.append(
            {
                "kind": "recurring_change",
                "insight_type": "recurring_change",
                "title": title,
                "body": body,
                "severity": "info",
                "priority": 25,
                "confidence": str(row.get("confidence_label") or "medium") if row.get("confidence_label") else "medium",
                "fingerprint": f"{scope}:recurring:{row.get('merchant_key')}:{event_type}:{row.get('period_bucket')}",
                "recommended_action": "Review the recurring item and dismiss it if this is expected.",
                "evidence": {
                    "merchant_key": row.get("merchant_key"),
                    "event_type": event_type,
                    "period_bucket": row.get("period_bucket"),
                    "payload": payload,
                    "old_amount": round(old_amount, 2) if old_amount is not None else None,
                    "new_amount": round(new_amount, 2) if new_amount is not None else None,
                    "expected_amount": round(amount, 2),
                    "next_expected_date": row.get("next_expected_date"),
                },
            }
        )
    return candidates[:2]


def _safe_to_spend_candidate(conn, profile: str | None) -> dict | None:
    from data_manager import get_plan_snapshot_data

    plan = get_plan_snapshot_data(profile=profile, conn=conn) or {}
    safe_to_spend = float(plan.get("safe_to_spend") or 0)
    limit = float(plan.get("safe_to_spend_limit") or 0)
    month = plan.get("month") or date.today().strftime("%Y-%m")
    has_plan_evidence = any(
        float(plan.get(key) or 0) > 0
        for key in (
            "planned_income",
            "income_actual",
            "safe_to_spend_spent",
            "mandatory_projected",
            "mandatory_spend",
            "total_budget",
            "active_goal_count",
        )
    )
    if limit <= 0 or not has_plan_evidence:
        return None
    low_threshold = max(50.0, limit * 0.10) if limit > 0 else 50.0
    if safe_to_spend >= 0 and safe_to_spend > low_threshold:
        return None
    severity = "warning" if safe_to_spend < 0 else "info"
    title = "Safe-to-spend needs a check"
    body = (
        f"The plan shows {_money(safe_to_spend)} safe-to-spend remaining for {month}. "
        f"Variable spending is {_money(plan.get('safe_to_spend_spent'))} against a limit of {_money(limit)}."
    )
    return {
        "kind": "safe_to_spend",
        "insight_type": "safe_to_spend",
        "title": title,
        "body": body,
        "severity": severity,
        "priority": 45,
        "confidence": "medium",
        "fingerprint": f"{_scope_profile(profile)}:safe_to_spend:{month}",
        "recommended_action": "Review flexible categories or adjust the plan if this is expected.",
        "evidence": plan,
    }


def _shortfall_candidate(conn, profile: str | None, today: date) -> dict | None:
    prediction = cashflow_forecast.predict_shortfall(conn, profile, as_of=today)
    if prediction.get("suppressed") or not prediction.get("has_shortfall_risk"):
        return None
    warning = prediction.get("warning") if isinstance(prediction.get("warning"), dict) else {}
    forecast = prediction.get("forecast") if isinstance(prediction.get("forecast"), dict) else {}
    when = warning.get("when") or (forecast.get("projected_low_point") or {}).get("date")
    return {
        "kind": "cashflow_shortfall",
        "insight_type": "cashflow_shortfall",
        "title": "Cash-flow buffer may get tight",
        "body": f"Forecasted cash may get close to the buffer around {when}.",
        "severity": "critical" if "zero" in str(warning.get("what") or "").lower() else "warning",
        "priority": 5,
        "confidence": prediction.get("confidence") or forecast.get("confidence") or "medium",
        "fingerprint": f"{_scope_profile(profile)}:cashflow_shortfall:{when}",
        "recommended_action": warning.get("recommended_action") or "Review upcoming obligations and flexible spending for the forecast window.",
        "assumptions": forecast.get("assumptions") or [],
        "valid_until": when,
        "evidence": {
            "warning": warning,
            "forecast_horizon": forecast.get("forecast_horizon"),
            "projected_low_point": forecast.get("projected_low_point"),
            "expected_income": forecast.get("expected_income"),
            "upcoming_obligations": forecast.get("upcoming_obligations"),
            "expected_discretionary_spend": forecast.get("expected_discretionary_spend"),
            "provenance": forecast.get("provenance"),
        },
    }


def _goal_candidates(conn, profile: str | None, today: date) -> list[dict]:
    scope = _scope_profile(profile)
    rows = dicts_from_rows(
        conn.execute(
            """
            SELECT id, name, goal_type, target_amount, current_amount, target_date, linked_category, updated_at
              FROM goals
             WHERE profile_id = ?
               AND is_active = 1
               AND target_amount > 0
             ORDER BY updated_at DESC
             LIMIT 8
            """,
            (scope,),
        ).fetchall()
    )
    candidates = []
    for row in rows:
        target = float(row.get("target_amount") or 0)
        current = float(row.get("current_amount") or 0)
        if target <= 0:
            continue
        progress = current / target
        target_date = str(row.get("target_date") or "")[:10]
        status = "on_track"
        expected = None
        if target_date:
            try:
                end = datetime.strptime(target_date, "%Y-%m-%d").date()
                created = today - timedelta(days=90)
                total_days = max(1, (end - created).days)
                elapsed = max(0, min(total_days, (today - created).days))
                expected = elapsed / total_days
                status = "behind" if progress + 0.05 < expected else "ahead"
            except Exception:
                pass
        if status != "behind" and progress >= 0.20:
            continue
        name = row.get("name") or "Goal"
        recommended = "Set aside a small automatic transfer this week, or lower one flexible category to keep the goal moving."
        candidates.append(
            {
                "kind": "goal_followup",
                "insight_type": "goal_followup",
                "title": f"{name} could use a check-in",
                "body": f"{name} is at {_money(current)} of {_money(target)}.",
                "severity": "info",
                "priority": 38 if status == "behind" else 55,
                "confidence": "medium",
                "fingerprint": f"{scope}:goal_followup:{row.get('id')}:{today.isocalendar().year}-{today.isocalendar().week}",
                "recommended_action": recommended,
                "evidence": {
                    "goal_id": row.get("id"),
                    "name": name,
                    "target_amount": round(target, 2),
                    "current_amount": round(current, 2),
                    "progress_ratio": round(progress, 3),
                    "expected_progress_ratio": round(expected, 3) if expected is not None else None,
                    "target_date": target_date,
                    "linked_category": row.get("linked_category"),
                },
            }
        )
    return candidates[:2]


def _budget_pace_candidates(conn, profile: str | None, today: date) -> list[dict]:
    scope = _scope_profile(profile)
    month = _month_key(today.year, today.month)
    month_start = f"{month}-01"
    month_end = f"{month}-{calendar.monthrange(today.year, today.month)[1]:02d}"
    profile_sql, profile_params = _profile_clause(profile, "t.profile_id")
    rows = dicts_from_rows(
        conn.execute(
            f"""
            SELECT b.category,
                   b.amount AS budget_amount,
                   COALESCE(SUM(CASE
                       WHEN t.amount < 0 THEN ABS(t.amount)
                       WHEN t.amount > 0 THEN -ABS(t.amount)
                       ELSE 0
                   END), 0) AS spent
              FROM category_budgets b
              LEFT JOIN transactions_visible t
                ON t.category = b.category
               AND t.date >= ?
               AND t.date <= ?
               AND COALESCE(t.is_excluded, 0) = 0
               AND (t.expense_type IS NULL OR t.expense_type NOT IN ('transfer_internal','transfer_household'))
               {profile_sql}
             WHERE b.profile_id = ?
               AND b.amount > 0
             GROUP BY b.category, b.amount
            """,
            [month_start, month_end, *profile_params, scope],
        ).fetchall()
    )
    day_ratio = today.day / calendar.monthrange(today.year, today.month)[1]
    candidates = []
    for row in rows:
        budget = float(row.get("budget_amount") or 0)
        spent = float(row.get("spent") or 0)
        if budget <= 0:
            continue
        expected_by_now = budget * day_ratio
        projected = spent / max(day_ratio, 0.05)
        if spent < 50 or projected < budget * 1.15 or spent < expected_by_now + 40:
            continue
        category = row.get("category") or "Budget"
        candidates.append(
            {
                "kind": "budget_pace",
                "insight_type": "budget_pace",
                "title": f"{category} may exceed budget",
                "body": (
                    f"{category} is at {_money(spent)} of a {_money(budget)} monthly budget. "
                    f"At this pace, it projects near {_money(projected)}."
                ),
                "severity": "warning" if projected >= budget * 1.35 else "info",
                "priority": 32,
                "confidence": "medium",
                "fingerprint": f"{scope}:budget_pace:{month}:{category.lower()}",
                "recommended_action": f"Review recent {category} spending or adjust the budget if this pace is expected.",
                "evidence": {
                    "month": month,
                    "category": category,
                    "spent": round(spent, 2),
                    "budget": round(budget, 2),
                    "projected": round(projected, 2),
                    "day_ratio": round(day_ratio, 3),
                },
            }
        )
    return sorted(candidates, key=lambda item: item["evidence"]["projected"] - item["evidence"]["budget"], reverse=True)[:2]


def _merchant_anomaly_candidates(conn, profile: str | None, today: date) -> list[dict]:
    scope = _scope_profile(profile)
    profile_sql, profile_params = _profile_clause(profile, "t.profile_id")
    start = (today - timedelta(days=120)).isoformat()
    rows = dicts_from_rows(
        conn.execute(
            f"""
            SELECT t.id, t.date, t.description, t.merchant_name, t.merchant_key,
                   t.amount, t.category
              FROM transactions_visible t
             WHERE t.date >= ?
               AND t.amount < 0
               AND COALESCE(t.is_excluded, 0) = 0
               AND COALESCE(t.category, '') NOT IN ({",".join("?" for _ in NON_SPENDING_CATEGORIES)})
               AND (t.expense_type IS NULL OR t.expense_type NOT IN ('transfer_internal','transfer_household'))
               {profile_sql}
             ORDER BY t.date DESC
             LIMIT 300
            """,
            [start, *sorted(NON_SPENDING_CATEGORIES), *profile_params],
        ).fetchall()
    )
    by_merchant: dict[str, list[dict]] = {}
    for row in rows:
        key = (row.get("merchant_key") or row.get("merchant_name") or row.get("description") or "").strip().upper()
        if not key:
            continue
        by_merchant.setdefault(key, []).append(row)

    candidates = []
    current_month = _month_key(today.year, today.month)
    for key, items in by_merchant.items():
        current_items = [item for item in items if str(item.get("date") or "").startswith(current_month)]
        prior_amounts = [abs(float(item.get("amount") or 0)) for item in items if not str(item.get("date") or "").startswith(current_month)]
        if len(prior_amounts) < 3:
            continue
        avg = sum(prior_amounts) / len(prior_amounts)
        for item in current_items[:2]:
            amount = abs(float(item.get("amount") or 0))
            if amount < 100 or avg < 10 or amount < max(avg * 3, avg + 75):
                continue
            merchant = item.get("merchant_name") or item.get("description") or key
            candidates.append(
                {
                    "kind": "merchant_anomaly",
                    "insight_type": "spending_anomaly",
                    "title": f"Unusual charge at {merchant}",
                    "body": f"{merchant} posted {_money(amount)}, versus a recent average near {_money(avg)}.",
                    "severity": "warning",
                    "priority": 28,
                    "confidence": "medium",
                    "fingerprint": f"{scope}:merchant_anomaly:{item.get('id')}",
                    "recommended_action": "Review this transaction and mark it reviewed if it is expected.",
                    "evidence": {
                        "transaction_id": item.get("id"),
                        "merchant": merchant,
                        "amount": round(amount, 2),
                        "prior_average": round(avg, 2),
                        "sample_size": len(prior_amounts),
                        "date": item.get("date"),
                    },
                }
            )
    return sorted(candidates, key=lambda item: item["evidence"]["amount"] - item["evidence"]["prior_average"], reverse=True)[:2]


def _recurring_calendar_candidates(conn, profile: str | None, today: date) -> list[dict]:
    scope = _scope_profile(profile)
    profile_sql, profile_params = _profile_clause(profile)
    upcoming_end = (today + timedelta(days=7)).isoformat()
    stopped_cutoff = (today - timedelta(days=7)).isoformat()
    rows = dicts_from_rows(
        conn.execute(
            f"""
            SELECT merchant_key, display_name, amount_cents, frequency, state,
                   next_expected_date, last_seen_date, confidence_label
              FROM recurring_obligations
             WHERE next_expected_date IS NOT NULL
               AND state IN ('active', 'confirmed', 'candidate')
               {profile_sql}
             ORDER BY next_expected_date ASC
             LIMIT 30
            """,
            profile_params,
        ).fetchall()
    )
    candidates = []
    for row in rows:
        due = str(row.get("next_expected_date") or "")[:10]
        merchant = row.get("display_name") or row.get("merchant_key") or "Recurring charge"
        amount = float(row.get("amount_cents") or 0) / 100
        if today.isoformat() <= due <= upcoming_end and amount > 0:
            candidates.append(
                {
                    "kind": "recurring_calendar",
                    "insight_type": "upcoming_bill_pressure",
                    "title": f"{merchant} is coming up",
                    "body": f"{merchant} is expected around {due} for about {_money(amount)}.",
                    "severity": "info",
                    "priority": 50,
                    "confidence": str(row.get("confidence_label") or "medium"),
                    "fingerprint": f"{scope}:recurring_calendar:{row.get('merchant_key')}:{due}",
                    "recommended_action": "Keep enough room in cash flow for this upcoming bill.",
                    "valid_until": due,
                    "evidence": {
                        "merchant_key": row.get("merchant_key"),
                        "next_expected_date": due,
                        "expected_amount": round(amount, 2),
                    },
                }
            )
        elif due < stopped_cutoff and row.get("last_seen_date") and str(row.get("last_seen_date"))[:10] < due:
            candidates.append(
                {
                    "kind": "recurring_stopped",
                    "insight_type": "recurring_stopped",
                    "title": f"{merchant} may have stopped posting",
                    "body": f"{merchant} was expected around {due}, but no matching charge has posted yet.",
                    "severity": "info",
                    "priority": 35,
                    "confidence": str(row.get("confidence_label") or "medium"),
                    "fingerprint": f"{scope}:recurring_stopped:{row.get('merchant_key')}:{due}",
                    "recommended_action": "If you cancelled it, dismiss the recurring item; otherwise check whether the charge moved merchants or dates.",
                    "evidence": {
                        "merchant_key": row.get("merchant_key"),
                        "next_expected_date": due,
                        "last_seen_date": row.get("last_seen_date"),
                    },
                }
            )
    return candidates[:3]


def _subscription_identity(row: dict) -> tuple:
    service = _compact_identity(row.get("display_name") or row.get("merchant_key"))
    frequency = str(row.get("frequency") or "monthly").lower().replace("-", "_")
    return (
        service,
        frequency,
        int(row.get("amount_cents") or 0),
        str(row.get("next_expected_date") or "")[:10],
    )


def _subscription_row_rank(row: dict) -> tuple:
    evidence = _json_load(row.get("evidence_json"))
    backfilled = evidence.get("backfilled_from") == "merchants"
    state_rank = {"confirmed": 4, "active": 3, "candidate": 2}.get(str(row.get("state") or ""), 0)
    source_rank = {"user": 4, "seed": 3, "algorithm": 2, "category": 1}.get(str(row.get("source") or ""), 0)
    try:
        score = int(row.get("confidence_score") or 0)
    except (TypeError, ValueError):
        score = 0
    return (0 if backfilled else 1, state_rank, source_rank, score)


def _duplicate_subscription_candidates(conn, profile: str | None, today: date) -> list[dict]:
    scope = _scope_profile(profile)
    profile_sql, profile_params = _profile_clause(profile)
    rows = dicts_from_rows(
        conn.execute(
            f"""
            SELECT merchant_key, display_name, category, amount_cents, next_expected_date,
                   state, source, confidence_score, confidence_label, evidence_json
              FROM recurring_obligations
             WHERE state IN ('active', 'confirmed', 'candidate')
               AND amount_cents > 0
               {profile_sql}
             ORDER BY category, amount_cents DESC
            """,
            profile_params,
        ).fetchall()
    )
    by_identity: dict[tuple, dict] = {}
    for row in rows:
        identity = _subscription_identity(row)
        current = by_identity.get(identity)
        if current is None or _subscription_row_rank(row) > _subscription_row_rank(current):
            by_identity[identity] = row

    by_category: dict[str, list[dict]] = {}
    for row in by_identity.values():
        category = (row.get("category") or "Subscriptions").strip()
        by_category.setdefault(category, []).append(row)
    candidates = []
    for category, items in by_category.items():
        if len(items) < 2 or category in {"Income", "Savings Transfer"}:
            continue
        similar = []
        for idx, left in enumerate(items):
            left_amount = float(left.get("amount_cents") or 0) / 100
            for right in items[idx + 1:]:
                right_amount = float(right.get("amount_cents") or 0) / 100
                if min(left_amount, right_amount) <= 0:
                    continue
                if abs(left_amount - right_amount) <= max(5.0, max(left_amount, right_amount) * 0.25):
                    similar.extend([left, right])
        deduped = []
        seen_keys = set()
        for item in similar:
            key = item.get("merchant_key") or item.get("obligation_key")
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped.append(item)
        if len(deduped) < 2:
            continue
        names = [item.get("display_name") or item.get("merchant_key") for item in deduped[:4]]
        total = sum(float(item.get("amount_cents") or 0) / 100 for item in deduped)
        if total < 20:
            continue
        fingerprint_key = ",".join(sorted(str(item.get("merchant_key") or "") for item in deduped[:4]))
        candidates.append(
            {
                "kind": "duplicate_subscription",
                "insight_type": "duplicate_subscription",
                "title": f"Multiple recurring {category} charges",
                "body": f"{', '.join(names[:3])} are all active or candidate recurring charges, about {_money(total)} combined.",
                "severity": "info",
                "priority": 34,
                "confidence": "medium",
                "fingerprint": f"{scope}:duplicate_subscription:{category.lower()}:{fingerprint_key}",
                "recommended_action": "Review whether these are all intentional before the next billing cycle.",
                "evidence": {
                    "category": category,
                    "combined_amount": round(total, 2),
                    "items": [
                        {
                            "merchant_key": item.get("merchant_key"),
                            "merchant": item.get("display_name") or item.get("merchant_key"),
                            "amount": round(float(item.get("amount_cents") or 0) / 100, 2),
                            "next_expected_date": item.get("next_expected_date"),
                            "state": item.get("state"),
                            "confidence_label": item.get("confidence_label"),
                            "evidence": _json_load(item.get("evidence_json")),
                        }
                        for item in deduped[:4]
                    ],
                },
            }
        )
    return candidates[:2]


def _dismissal_suppressed_kinds(conn, profile: str | None) -> set[str]:
    scope = _scope_profile(profile)
    rows = dicts_from_rows(
        conn.execute(
            """
            SELECT kind, COUNT(*) AS dismissed_count
              FROM proactive_insights
             WHERE profile_id = ?
               AND status = 'dismissed'
               AND datetime(COALESCE(dismissed_at, generated_at)) >= datetime('now', '-30 days')
             GROUP BY kind
            HAVING dismissed_count >= 3
            """,
            (scope,),
        ).fetchall()
    )
    return {row["kind"] for row in rows if row.get("kind")}


def _candidate_insights(conn, profile: str | None, today: date | None = None) -> list[dict]:
    today = today or date.today()
    candidates = []
    shortfall = _shortfall_candidate(conn, profile, today)
    if shortfall:
        candidates.append(shortfall)
    candidates.extend(_recurring_candidates(conn, profile, today))
    candidates.extend(_budget_pace_candidates(conn, profile, today))
    candidates.extend(_goal_candidates(conn, profile, today))
    candidates.extend(_merchant_anomaly_candidates(conn, profile, today))
    candidates.extend(_category_spike_candidates(conn, profile, today))
    candidates.extend(_duplicate_subscription_candidates(conn, profile, today))
    candidates.extend(_recurring_calendar_candidates(conn, profile, today))
    safe = _safe_to_spend_candidate(conn, profile)
    if safe:
        candidates.append(safe)
    suppressed_kinds = _dismissal_suppressed_kinds(conn, profile)
    candidates = [item for item in candidates if item.get("kind") not in suppressed_kinds]
    candidates = _filter_contract_candidates(candidates)
    return sorted(candidates, key=lambda item: (_priority(item), _severity_rank(item.get("severity") or "info")))


def build_candidate_insights(profile: str | None = None, conn=None, today: date | None = None) -> list[dict]:
    """Return current deterministic insight candidates without writing rows."""
    if conn is not None:
        return _candidate_insights(conn, profile, today=today)
    with get_db() as c:
        return _candidate_insights(c, profile, today=today)


def generate_insights(profile: str | None = None, conn=None) -> list[dict]:
    def _generate(c):
        candidates = _candidate_insights(c, profile)
        active_fingerprints = [insight["fingerprint"] for insight in candidates]
        managed_kinds = (
            "cashflow_shortfall",
            "category_spike",
            "recurring_change",
            "safe_to_spend",
            "budget_pace",
            "goal_followup",
            "merchant_anomaly",
            "recurring_calendar",
            "recurring_stopped",
            "duplicate_subscription",
        )
        scope = _scope_profile(profile)
        for insight in candidates:
            _insert_or_refresh(c, profile, insight)
        if active_fingerprints:
            c.execute(
                f"""
                UPDATE proactive_insights
                   SET status = 'stale'
                 WHERE profile_id = ?
                   AND status = 'active'
                   AND kind IN ({",".join("?" for _ in managed_kinds)})
                   AND fingerprint NOT IN ({",".join("?" for _ in active_fingerprints)})
                """,
                [scope, *managed_kinds, *active_fingerprints],
            )
        else:
            c.execute(
                f"""
                UPDATE proactive_insights
                   SET status = 'stale'
                 WHERE profile_id = ?
                   AND status = 'active'
                   AND kind IN ({",".join("?" for _ in managed_kinds)})
                """,
                [scope, *managed_kinds],
            )
        return _list_rows(c, profile)

    if conn is not None:
        return _generate(conn)
    with get_db() as c:
        return _generate(c)


def list_insights(profile: str | None = None, include_dismissed: bool = False, conn=None, generate: bool = True) -> list[dict]:
    def _list(c):
        if generate and not _has_fresh_cached_insights(c, profile):
            generate_insights(profile=profile, conn=c)
        return _list_rows(c, profile, include_dismissed=include_dismissed)

    if conn is not None:
        return _list(conn)
    with get_db() as c:
        return _list(c)


def dismiss_insight(insight_id: int, profile: str | None = None, conn=None, reason: str | None = None, dismissed_type: str | None = None) -> bool:
    def _dismiss(c):
        result = c.execute(
            """
            UPDATE proactive_insights
               SET status = 'dismissed',
                   dismissed_at = datetime('now'),
                   dismissed_reason = ?,
                   dismissed_type = ?
             WHERE id = ?
               AND profile_id = ?
            """,
            (reason or "", dismissed_type or "", insight_id, _scope_profile(profile)),
        )
        return result.rowcount > 0

    if conn is not None:
        return _dismiss(conn)
    with get_db() as c:
        return _dismiss(c)


def restore_insight(insight_id: int, profile: str | None = None, conn=None) -> bool:
    def _restore(c):
        result = c.execute(
            """
            UPDATE proactive_insights
               SET status = 'active',
                   dismissed_at = NULL,
                   restored_at = datetime('now'),
                   generated_at = datetime('now')
             WHERE id = ?
               AND profile_id = ?
            """,
            (insight_id, _scope_profile(profile)),
        )
        return result.rowcount > 0

    if conn is not None:
        return _restore(conn)
    with get_db() as c:
        return _restore(c)
