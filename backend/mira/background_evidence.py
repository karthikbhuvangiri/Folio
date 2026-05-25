"""Deterministic evidence bundle for background Mira analysis."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Callable

import data_manager
import proactive_insights
from database import get_db
from mira import cashflow_forecast
from recurring_obligations import get_recurring_bundle, get_scheduled_bundle


BUNDLE_VERSION = "mira_background_evidence_v1"
DEFAULT_MAX_FACTS = 56
DEFAULT_MAX_BYTES = 36_000
MAX_LIST_ITEMS = 6
MAX_STRING_CHARS = 180

_BLOCKED_KEYS = {
    "access_token",
    "account_number",
    "api_key",
    "description",
    "notes",
    "password",
    "prior_transactions",
    "raw_description",
    "recent_transactions",
    "routing_number",
    "secret",
    "token",
}


def build_background_evidence_bundle(
    profile: str | None = None,
    conn=None,
    *,
    as_of: date | datetime | str | None = None,
    max_facts: int = DEFAULT_MAX_FACTS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Build a compact deterministic fact bundle for a future background analyst."""

    def _build(c):
        today = _coerce_date(as_of) or date.today()
        scope = _profile_scope(profile)
        bundle: dict[str, Any] = {
            "version": BUNDLE_VERSION,
            "profile_scope": scope,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "periods": {
                "as_of": today.isoformat(),
                "current_month": today.strftime("%Y-%m"),
                "lookback_months": 4,
            },
            "facts": [],
            "candidate_signals": [],
            "confidence_summary": {},
            "caveats": [],
            "evidence_refs": [],
            "source_versions": {
                "builder": BUNDLE_VERSION,
                "proactive_contract": proactive_insights.DETECTOR_CONTRACT_VERSION,
            },
        }
        used_ids: set[str] = set()

        dashboard = _safe_call(bundle, "dashboard_bundle", lambda: data_manager.get_dashboard_bundle_data(profile=profile, conn=c, as_of=today))
        if isinstance(dashboard, dict):
            _add_dashboard_facts(bundle, dashboard, used_ids, conn=c, profile=profile, today=today, max_facts=max_facts)

        goals = _safe_call(bundle, "goals", lambda: data_manager.get_goals(profile=profile, conn=c))
        if isinstance(goals, list):
            _add_goal_facts(bundle, goals, used_ids)

        recurring = _safe_call(bundle, "recurring_bundle", lambda: get_recurring_bundle(c, profile=profile))
        if isinstance(recurring, dict):
            _add_recurring_facts(bundle, recurring, used_ids)

        scheduled = _safe_call(bundle, "scheduled_bundle", lambda: get_scheduled_bundle(c, profile=profile, today=today))
        if isinstance(scheduled, dict):
            _add_scheduled_facts(bundle, scheduled, used_ids)

        shortfall = _safe_call(bundle, "cashflow_shortfall", lambda: cashflow_forecast.predict_shortfall(c, profile, as_of=today))
        if isinstance(shortfall, dict):
            _add_cashflow_facts(bundle, shortfall, used_ids)

        candidates = _safe_call(bundle, "proactive_candidates", lambda: proactive_insights.build_candidate_insights(profile=profile, conn=c, today=today))
        if isinstance(candidates, list):
            _add_candidate_signals(bundle, candidates, used_ids)

        bundle["confidence_summary"] = _confidence_summary(bundle)
        return _finalize_bundle(bundle, max_facts=max_facts, max_bytes=max_bytes)

    if conn is not None:
        return _build(conn)
    with get_db() as c:
        return _build(c)


def bundle_json_bytes(bundle: dict[str, Any]) -> int:
    return len(json.dumps(bundle, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _add_dashboard_facts(
    bundle: dict[str, Any],
    dashboard: dict[str, Any],
    used_ids: set[str],
    *,
    conn,
    profile: str | None,
    today: date,
    max_facts: int,
) -> None:
    month = today.strftime("%Y-%m")
    monthly = dashboard.get("monthly") if isinstance(dashboard.get("monthly"), list) else []
    current = _row_for_month(monthly, month) or (monthly[-1] if monthly else {})
    previous = _previous_month_row(monthly, str(current.get("month") or month))
    summary = dashboard.get("summary") if isinstance(dashboard.get("summary"), dict) else {}
    plan = dashboard.get("planSnapshot") if isinstance(dashboard.get("planSnapshot"), dict) else {}

    _add_fact(
        bundle,
        used_ids,
        kind="dashboard_month_summary",
        source="data_manager.get_dashboard_bundle_data",
        key=str(current.get("month") or month),
        confidence="high",
        values=_pick(
            {**summary, **current},
            (
                "month",
                "income",
                "expenses",
                "net",
                "savings",
                "savings_rate",
                "transaction_count",
                "cc_repaid",
                "credits_refunds",
            ),
        ),
    )
    if previous:
        current_expenses = _number(current.get("expenses"))
        prior_expenses = _number(previous.get("expenses"))
        _add_fact(
            bundle,
            used_ids,
            kind="month_over_month_spend_delta",
            source="data_manager.get_dashboard_bundle_data",
            key=f"{previous.get('month')}_to_{current.get('month')}",
            confidence="high",
            values={
                "current_month": current.get("month"),
                "prior_month": previous.get("month"),
                "current_expenses": current_expenses,
                "prior_expenses": prior_expenses,
                "delta": round(current_expenses - prior_expenses, 2),
            },
        )

    _add_fact(
        bundle,
        used_ids,
        kind="plan_snapshot",
        source="data_manager.get_plan_snapshot_data",
        key=str(plan.get("month") or month),
        confidence="high" if plan else "absent",
        values=_pick(
            plan,
            (
                "month",
                "total_budget",
                "budgeted_spent",
                "remaining",
                "planned_income",
                "income_actual",
                "income_basis",
                "mandatory_projected",
                "variable_spend",
                "safe_to_spend_limit",
                "safe_to_spend_spent",
                "safe_to_spend",
                "active_goal_count",
                "over_count",
            ),
        ),
        caveats=[] if plan else ["No plan snapshot was available."],
    )

    category_result = _safe_call(
        bundle,
        "category_current_month",
        lambda: data_manager.get_category_analytics_data(month=month, profile=profile, conn=conn),
    )
    categories = category_result.get("categories") if isinstance(category_result, dict) else []
    for category in categories[:8]:
        if not isinstance(category, dict):
            continue
        _add_fact(
            bundle,
            used_ids,
            kind="category_current_spend",
            source="data_manager.get_category_analytics_data",
            key=f"{month}:{category.get('category')}",
            confidence="high",
            values=_pick(category, ("category", "total", "gross", "refunds", "percent", "expense_type")),
        )

    for delta in _category_deltas(dashboard.get("monthlyCategoryBreakdown"), limit=6):
        _add_fact(
            bundle,
            used_ids,
            kind="category_month_over_month_delta",
            source="data_manager.get_monthly_category_breakdown",
            key=f"{delta.get('prior_month')}_to_{delta.get('current_month')}:{delta.get('category')}",
            confidence="medium",
            values=delta,
        )

    merchants = _safe_list(
        _safe_call(bundle, "merchant_insights", lambda: data_manager.get_merchant_insights_data(month=month, profile=profile, conn=conn)),
        default=[],
    )
    for merchant in merchants[:5]:
        if not isinstance(merchant, dict):
            continue
        _add_fact(
            bundle,
            used_ids,
            kind="merchant_current_spend",
            source="data_manager.get_merchant_insights_data",
            key=f"{month}:{merchant.get('name')}",
            confidence="medium",
            values=_pick(merchant, ("name", "industry", "city", "state", "total_spent", "transaction_count")),
        )

    if dashboard.get("netWorthMomDelta") is not None or dashboard.get("netWorthYtdDelta") is not None:
        _add_fact(
            bundle,
            used_ids,
            kind="net_worth_delta",
            source="data_manager.get_net_worth_delta_metrics",
            key=month,
            confidence="medium",
            values={
                "month": month,
                "mom": _compact_value(dashboard.get("netWorthMomDelta")),
                "ytd": _compact_value(dashboard.get("netWorthYtdDelta")),
            },
        )

    if len(bundle["facts"]) > max_facts:
        bundle["facts"] = bundle["facts"][:max_facts]


def _add_goal_facts(bundle: dict[str, Any], goals: list[dict], used_ids: set[str]) -> None:
    for goal in goals[:6]:
        if not isinstance(goal, dict):
            continue
        projection = goal.get("projection") if isinstance(goal.get("projection"), dict) else {}
        _add_fact(
            bundle,
            used_ids,
            kind="goal_progress",
            source="data_manager.get_goals",
            key=str(goal.get("id") or goal.get("name")),
            confidence="user",
            values={
                **_pick(goal, ("id", "name", "goal_type", "target_amount", "current_amount", "target_date", "linked_category")),
                "projection": _pick(
                    projection,
                    ("gap", "progress_percent", "months_remaining", "required_monthly", "average_monthly_progress", "status"),
                ),
            },
        )


def _add_recurring_facts(bundle: dict[str, Any], recurring: dict[str, Any], used_ids: set[str]) -> None:
    _add_fact(
        bundle,
        used_ids,
        kind="recurring_summary",
        source="recurring_obligations.get_recurring_bundle",
        key="summary",
        confidence="medium",
        values=_pick(
            recurring,
            (
                "active_count",
                "candidate_count",
                "cancelled_count",
                "dismissed_count",
                "inactive_count",
                "total_monthly",
                "total_annual",
                "unread_event_count",
            ),
        ),
    )
    for item in (recurring.get("items") or [])[:6]:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in {"active", "confirmed", "candidate"}:
            continue
        _add_fact(
            bundle,
            used_ids,
            kind="recurring_obligation",
            source="recurring_obligations.get_recurring_bundle",
            key=str(item.get("obligation_key") or item.get("merchant_key") or item.get("merchant")),
            confidence=str(item.get("confidence") or "medium"),
            values=_pick(
                item,
                (
                    "merchant",
                    "category",
                    "frequency",
                    "amount",
                    "annual_cost",
                    "status",
                    "confidence",
                    "confidence_score",
                    "confirmed",
                    "last_charge",
                    "next_expected",
                    "charge_count",
                    "merchant_key",
                ),
            ),
        )


def _add_scheduled_facts(bundle: dict[str, Any], scheduled: dict[str, Any], used_ids: set[str]) -> None:
    _add_fact(
        bundle,
        used_ids,
        kind="scheduled_summary",
        source="recurring_obligations.get_scheduled_bundle",
        key="summary",
        confidence="medium",
        values=_pick(
            scheduled,
            (
                "window_days",
                "start_date",
                "end_date",
                "scheduled_count",
                "due_soon_count",
                "confirmed_upcoming_total",
                "inferred_upcoming_total",
                "needs_review_total",
                "monthly_equivalent_total",
            ),
        ),
    )
    for item in (scheduled.get("items") or [])[:6]:
        if not isinstance(item, dict):
            continue
        _add_fact(
            bundle,
            used_ids,
            kind="scheduled_item",
            source="recurring_obligations.get_scheduled_bundle",
            key=f"{item.get('merchant_key') or item.get('merchant')}:{item.get('next_date')}",
            confidence=str(item.get("confidence") or "medium"),
            values=_pick(
                item,
                (
                    "merchant",
                    "category",
                    "group",
                    "amount",
                    "frequency",
                    "next_date",
                    "days_until",
                    "status",
                    "source_label",
                    "confidence",
                    "confidence_score",
                    "confirmed",
                    "merchant_key",
                ),
            ),
        )


def _add_cashflow_facts(bundle: dict[str, Any], shortfall: dict[str, Any], used_ids: set[str]) -> None:
    forecast = shortfall.get("forecast") if isinstance(shortfall.get("forecast"), dict) else {}
    low_point = forecast.get("projected_low_point") if isinstance(forecast.get("projected_low_point"), dict) else {}
    provenance = forecast.get("provenance") if isinstance(forecast.get("provenance"), dict) else {}
    _add_fact(
        bundle,
        used_ids,
        kind="cashflow_shortfall",
        source="mira.cashflow_forecast.predict_shortfall",
        key=str((shortfall.get("warning") or {}).get("when") or low_point.get("date") or "forecast"),
        confidence=str(shortfall.get("confidence") or "medium"),
        values={
            "has_shortfall_risk": bool(shortfall.get("has_shortfall_risk")),
            "suppressed": bool(shortfall.get("suppressed")),
            "suppressed_reason": shortfall.get("suppressed_reason") or "",
            "warning": _compact_value(shortfall.get("warning")),
            "projected_low_point": _compact_value(low_point),
            "forecast_horizon": _compact_value(forecast.get("forecast_horizon")),
            "expected_income": _compact_value(forecast.get("expected_income")),
            "upcoming_obligations": _compact_value(forecast.get("upcoming_obligations")),
            "expected_discretionary_spend": _compact_value(forecast.get("expected_discretionary_spend")),
            "sample_transaction_ids": list(provenance.get("sample_transaction_ids") or [])[:5],
        },
        caveats=shortfall.get("caveats") or [],
    )


def _add_candidate_signals(bundle: dict[str, Any], candidates: list[dict], used_ids: set[str]) -> None:
    for candidate in candidates[:10]:
        if not isinstance(candidate, dict):
            continue
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
        evidence_id = _unique_evidence_id(used_ids, "signal", candidate.get("fingerprint") or candidate.get("kind") or "candidate")
        signal = {
            "evidence_id": evidence_id,
            "kind": candidate.get("kind"),
            "insight_type": candidate.get("insight_type") or candidate.get("kind"),
            "title": candidate.get("title") or "",
            "body": candidate.get("body") or "",
            "severity": candidate.get("severity") or "info",
            "priority": candidate.get("priority") or 100,
            "confidence": candidate.get("confidence") or "medium",
            "recommended_action": candidate.get("recommended_action") or "",
            "values": _compact_value(evidence),
            "source": "proactive_insights.build_candidate_insights",
        }
        bundle["candidate_signals"].append(signal)


def _add_fact(
    bundle: dict[str, Any],
    used_ids: set[str],
    *,
    kind: str,
    source: str,
    key: str,
    confidence: str,
    values: dict[str, Any],
    caveats: list[str] | None = None,
) -> None:
    compact = _compact_value(values)
    if not isinstance(compact, dict):
        compact = {}
    evidence_id = _unique_evidence_id(used_ids, kind, key)
    bundle["facts"].append(
        {
            "evidence_id": evidence_id,
            "kind": kind,
            "source": source,
            "confidence": _confidence_state(confidence),
            "values": compact,
            "caveats": [str(item)[:MAX_STRING_CHARS] for item in (caveats or []) if item],
        }
    )


def _finalize_bundle(bundle: dict[str, Any], *, max_facts: int, max_bytes: int) -> dict[str, Any]:
    if len(bundle["facts"]) > max_facts:
        del bundle["facts"][max_facts:]
        bundle["caveats"].append(f"Fact list was capped at {max_facts} items.")
    _refresh_evidence_refs(bundle)
    while bundle_json_bytes(bundle) > max_bytes and (bundle["candidate_signals"] or bundle["facts"]):
        if bundle["candidate_signals"]:
            bundle["candidate_signals"].pop()
        else:
            bundle["facts"].pop()
        if "Bundle was truncated to fit the background evidence size budget." not in bundle["caveats"]:
            bundle["caveats"].append("Bundle was truncated to fit the background evidence size budget.")
        _refresh_evidence_refs(bundle)
    if not bundle["facts"] and not bundle["candidate_signals"]:
        bundle["caveats"].append("No deterministic evidence facts were available.")
    bundle["confidence_summary"] = _confidence_summary(bundle)
    bundle["meta"] = {
        "fact_count": len(bundle["facts"]),
        "candidate_signal_count": len(bundle["candidate_signals"]),
        "evidence_ref_count": len(bundle["evidence_refs"]),
        "json_bytes": 0,
        "estimated_tokens": 0,
        "max_bytes": max_bytes,
    }
    actual_bytes = bundle_json_bytes(bundle)
    bundle["meta"]["json_bytes"] = actual_bytes
    bundle["meta"]["estimated_tokens"] = max(1, round(actual_bytes / 4))
    actual_bytes = bundle_json_bytes(bundle)
    bundle["meta"]["json_bytes"] = actual_bytes
    bundle["meta"]["estimated_tokens"] = max(1, round(actual_bytes / 4))
    return bundle


def _refresh_evidence_refs(bundle: dict[str, Any]) -> None:
    refs = []
    for item in [*bundle.get("facts", []), *bundle.get("candidate_signals", [])]:
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        refs.append(
            {
                "evidence_id": item.get("evidence_id"),
                "kind": item.get("kind"),
                "source": item.get("source"),
                "confidence": item.get("confidence"),
                "value_keys": sorted(values.keys()),
            }
        )
    bundle["evidence_refs"] = refs


def _confidence_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for item in [*bundle.get("facts", []), *bundle.get("candidate_signals", [])]:
        confidence = _confidence_state(str(item.get("confidence") or "absent"))
        counts[confidence] = counts.get(confidence, 0) + 1
        kind = str(item.get("kind") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"confidence_counts": counts, "kind_counts": kinds}


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _BLOCKED_KEYS:
                continue
            compact = _compact_value(item, depth=depth + 1)
            if compact not in (None, "", [], {}):
                out[key_text] = compact
        return out
    if isinstance(value, (list, tuple)):
        return [_compact_value(item, depth=depth + 1) for item in list(value)[:MAX_LIST_ITEMS]]
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, (int, bool)) or value is None:
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value).strip()
    return text[:MAX_STRING_CHARS]


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: _compact_value(row.get(key)) for key in keys if row.get(key) not in (None, "", [], {})}


def _category_deltas(monthly_breakdown: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(monthly_breakdown, list) or len(monthly_breakdown) < 2:
        return []
    rows = [row for row in monthly_breakdown if isinstance(row, dict) and row.get("month")]
    rows.sort(key=lambda row: str(row.get("month")))
    current = rows[-1]
    prior = rows[-2]
    current_cats = _category_total_map(current.get("categories"))
    prior_cats = _category_total_map(prior.get("categories"))
    deltas = []
    for category, total in current_cats.items():
        prior_total = prior_cats.get(category, 0.0)
        delta = round(total - prior_total, 2)
        if abs(delta) < 25:
            continue
        deltas.append(
            {
                "category": category,
                "current_month": current.get("month"),
                "prior_month": prior.get("month"),
                "current_total": round(total, 2),
                "prior_total": round(prior_total, 2),
                "delta": delta,
            }
        )
    return sorted(deltas, key=lambda item: abs(float(item.get("delta") or 0)), reverse=True)[:limit]


def _category_total_map(categories: Any) -> dict[str, float]:
    out = {}
    if not isinstance(categories, list):
        return out
    for item in categories:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        if category:
            out[category] = _number(item.get("total"))
    return out


def _row_for_month(rows: list[dict], month: str) -> dict | None:
    return next((row for row in rows if isinstance(row, dict) and row.get("month") == month), None)


def _previous_month_row(rows: list[dict], current_month: str) -> dict | None:
    earlier = [row for row in rows if isinstance(row, dict) and str(row.get("month") or "") < current_month]
    if not earlier:
        return None
    return sorted(earlier, key=lambda row: str(row.get("month") or ""))[-1]


def _safe_call(bundle: dict[str, Any], section: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        bundle["caveats"].append(f"{section} unavailable: {type(exc).__name__}")
        return None


def _safe_list(value: Any, *, default: list) -> list:
    return value if isinstance(value, list) else default


def _unique_evidence_id(used_ids: set[str], kind: str, key: Any) -> str:
    base = f"{_slug(kind)}.{_slug(key)}"[:96].strip(".") or "evidence"
    candidate = base
    idx = 2
    while candidate in used_ids:
        candidate = f"{base}.{idx}"
        idx += 1
    used_ids.add(candidate)
    return candidate


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return text or "unknown"


def _profile_scope(profile: str | None) -> str:
    return profile if profile and profile != "household" else "household"


def _coerce_date(value: date | datetime | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _number(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _confidence_state(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"high", "medium", "low", "user", "absent"}:
        return normalized
    return "medium"
