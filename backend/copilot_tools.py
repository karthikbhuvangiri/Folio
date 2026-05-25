"""
Copilot agent tools. Each tool wraps an existing analytic or persistence
function so the LLM can reach live data through a narrow, named surface.

Tool schemas follow JSON Schema draft-7 and are emitted in Ollama's
OpenAI-style tool-use format.

Time windows use a `range` token:
  current_month | last_month | prior_month | YYYY-MM
  this_week    | last_week
  last_7d | last_30d | last_90d | last_180d | last_365d | last_N_months
  ytd | last_year | all | YYYY-MM-DD..YYYY-MM-DD
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from statistics import pstdev
from typing import Any

from database import get_db
from range_parser import parse_range
from data_manager import (
    NON_SPENDING_CATEGORIES as DATA_NON_SPENDING_CATEGORIES,
    get_category_analytics_data,
    get_category_budgets,
    get_categories_meta,
    get_dashboard_bundle_data,
    get_merchant_insights_data,
    get_monthly_analytics_data,
    get_net_worth_delta_metrics,
    get_net_worth_series_data,
    get_plan_snapshot_data,
    get_recurring_from_db,
    get_summary_data,
    get_transactions_paginated,
)
from cashflow_classifier import CREDITS_REFUNDS_CATEGORY
from mira import metric_registry
from mira import cashflow_forecast
from mira.agentic.temporal_parser import bounded_range_dates

logger = logging.getLogger(__name__)
INTERNAL_TOOL_NAMES = {"run_sql"}


# ──────────────────────────────────────────────────────────────────────────────
# Range resolution
# ──────────────────────────────────────────────────────────────────────────────

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = datetime(year, month + 1, 1) - timedelta(days=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _resolve_range(token: str | None) -> tuple[str | None, str | None, str]:
    """
    Translate a range token to (start_iso_date, end_iso_date, label).
    Returns (None, None, label) for 'all' (no date filter).
    """
    if not token:
        token = "current_month"
    token = token.strip().lower()
    bounded = bounded_range_dates(token)
    if bounded:
        start, end = bounded
        return start, end, token
    parsed = parse_range(token)
    if parsed.explicit:
        token = parsed.token
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    if token in {"current_month", "this_month", "current"}:
        start, end = _month_bounds(now.year, now.month)
        return start, end, f"{now.year:04d}-{now.month:02d}"

    if token in {"last_month", "prior_month", "prior"}:
        y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        start, end = _month_bounds(y, m)
        return start, end, f"{y:04d}-{m:02d}"

    if _MONTH_RE.match(token):
        y, m = int(token[:4]), int(token[5:7])
        start, end = _month_bounds(y, m)
        return start, end, token

    if token in {"this_week"}:
        start_dt = now - timedelta(days=now.weekday())
        return start_dt.strftime("%Y-%m-%d"), today, "this_week"

    if token == "last_week":
        this_mon = now - timedelta(days=now.weekday())
        last_mon = this_mon - timedelta(days=7)
        last_sun = this_mon - timedelta(days=1)
        return last_mon.strftime("%Y-%m-%d"), last_sun.strftime("%Y-%m-%d"), "last_week"

    m = re.match(r"^last_(\d+)d$", token)
    if m:
        days = int(m.group(1))
        start_dt = now - timedelta(days=days)
        return start_dt.strftime("%Y-%m-%d"), today, f"last_{days}d"

    m = re.match(r"^last_(\d{1,2})_?months?$", token)
    if m:
        months = max(1, min(int(m.group(1)), 36))
        y, mon = _shift_month(now.year, now.month, -(months - 1))
        start, _ = _month_bounds(y, mon)
        return start, today, f"last_{months}_months"

    if token == "ytd":
        return f"{now.year:04d}-01-01", today, "ytd"

    if token == "last_year":
        start, end = _month_bounds(now.year - 1, 1)
        _, end_of_year = _month_bounds(now.year - 1, 12)
        return start, end_of_year, f"{now.year - 1:04d}"

    if token == "all":
        return None, None, "all"

    # Unknown token → fall back to current month
    start, end = _month_bounds(now.year, now.month)
    return start, end, f"{now.year:04d}-{now.month:02d}"


_RANGE_ENUM = [
    "current_month", "last_month", "prior_month",
    "this_week", "last_week",
    "last_7d", "last_30d", "last_90d", "last_180d", "last_365d",
    "ytd", "last_year", "all",
]
_RANGE_DESC = (
    "Time window. One of: current_month (default), last_month, this_week, last_week, "
    "last_7d, last_30d, last_90d, last_180d, last_365d, ytd, last_year, all, "
    "last_N_months such as last_13_months, a specific YYYY-MM, or an internal "
    "bounded date token YYYY-MM-DD..YYYY-MM-DD."
)
_NON_SPENDING_CATEGORIES = tuple(DATA_NON_SPENDING_CATEGORIES)
_NON_SPENDING_CATEGORY_KEYS = {str(name).strip().lower() for name in _NON_SPENDING_CATEGORIES}


def _profile_filter(profile: str | None) -> tuple[str, list]:
    if not profile or profile == "household":
        return "", []
    return " AND profile_id = ?", [profile]


def _date_filter(start: str | None, end: str | None) -> tuple[str, list]:
    if start and end:
        return " AND date >= ? AND date <= ?", [start, end]
    if start:
        return " AND date >= ?", [start]
    return "", []


def _fmt_money(v) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def _transfer_ok_clause(profile: str | None) -> str:
    if not profile or profile == "household":
        return "AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))"
    return "AND (expense_type IS NULL OR expense_type != 'transfer_internal')"


def _personal_transfer_type_clause(profile: str | None) -> str:
    if not profile or profile == "household":
        return "expense_type = 'transfer_external'"
    return "expense_type IN ('transfer_external', 'transfer_household')"


def _category_config(category: str, conn) -> tuple[str, str] | None:
    try:
        row = conn.execute(
            """
            SELECT name, expense_type
            FROM categories
            WHERE LOWER(name) = LOWER(?) AND is_active = 1
            LIMIT 1
            """,
            (category,),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return None
    name = row["name"] if hasattr(row, "keys") else row[0]
    expense_type = row["expense_type"] if hasattr(row, "keys") else row[1]
    return str(name or category), str(expense_type or "")


def _is_non_spending_category(category: str, conn) -> tuple[bool, str, str]:
    configured = _category_config(category, conn)
    display = configured[0] if configured else category
    expense_type = configured[1] if configured else ""
    if expense_type == "non_expense":
        return True, display, expense_type
    if category.strip().lower() in _NON_SPENDING_CATEGORY_KEYS:
        return True, display, "non_expense"
    try:
        for meta in get_categories_meta(conn=conn):
            if not meta.get("is_active", 1):
                continue
            if str(meta.get("expense_type") or "") != "non_expense":
                continue
            if str(meta.get("name") or "").strip().lower() == category.strip().lower():
                return True, str(meta.get("name") or category), "non_expense"
    except Exception:
        pass
    return False, display, expense_type


def _unsupported_category_spend_result(
    *,
    category: str,
    label: str,
    start: str | None,
    end: str | None,
    reason: str,
) -> dict[str, Any]:
    suggested = ["find_transactions"]
    key = category.strip().lower()
    if key == "income":
        suggested.insert(0, "get_period_summary(metric='income')")
    elif key == "savings transfer":
        suggested.insert(0, "get_period_summary(metric='savings')")
    elif key == "credit card payment":
        suggested.insert(0, "get_period_summary(metric='credit_card_payments')")
    elif key == "credits & refunds":
        suggested.insert(0, "get_period_summary(metric='refunds')")
    else:
        suggested.insert(0, "get_period_summary")
    caveat = f"{category} is configured as a non-expense category, so it is not a spending category."
    return {
        "error": "unsupported_category_for_spend",
        "unsupported": True,
        "category": category,
        "range": label,
        "start": start,
        "end": end,
        "semantic_type": "non_expense",
        "reason": reason or caveat,
        "message": caveat,
        "suggested_tools": suggested,
        "metric_id": None,
        "metric_ids": [],
        "metric_definition": None,
        "metric_definition_summary": "",
        "filters": {"category": category},
        "row_count": 0,
        "count": 0,
        "sample_transaction_ids": [],
        "calculation_basis": caveat,
        "data_quality": {"caveats": [caveat]},
        "caveats": [caveat],
        "provenance": {
            "tool": "get_category_spend",
            "args": {"category": category},
            "metric_id": None,
            "metric_ids": [],
            "metric_definition_summary": "",
            "range": label,
            "start": start,
            "end": end,
            "filters": {"category": category},
            "row_count": 0,
            "count": 0,
            "sample_transaction_ids": [],
            "calculation_basis": caveat,
            "data_quality": {"caveats": [caveat]},
            "caveats": [caveat],
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────────────────────────────────────

def _t_get_month_summary(args: dict, profile: str | None, conn) -> Any:
    month_arg = args.get("month") or "current"
    _, _, label = _resolve_range(month_arg)
    rows = get_monthly_analytics_data(profile=profile, conn=conn) or []
    if not rows:
        return {"error": "no monthly data"}
    target = next((r for r in rows if r.get("month") == label), None)
    if target is None:
        target = rows[-1]
    idx = rows.index(target)
    prior = rows[idx - 1] if idx > 0 else None
    return _add_contract(
        {"current": target, "prior": prior},
        tool="get_month_summary",
        args=args,
        label=args.get("range") or label,
        row_count=1,
        filters={"profile": profile or "household", "month": label},
    )


def _period_component_filter(metric: str, profile: str | None) -> tuple[str, list[Any]]:
    non_spending_ph = ",".join("?" * len(_NON_SPENDING_CATEGORIES))
    transfer_ok = _transfer_ok_clause(profile)
    personal_transfer_clause = _personal_transfer_type_clause(profile)
    if metric == "income":
        return f"category = 'Income' AND amount > 0 {transfer_ok}", []
    if metric == "expenses":
        return f"amount < 0 AND category NOT IN ({non_spending_ph}) {transfer_ok}", list(_NON_SPENDING_CATEGORIES)
    if metric == "refunds":
        return (
            f"amount > 0 AND (category = ? OR (category NOT IN ({non_spending_ph}) AND category != 'Income')) {transfer_ok}",
            [CREDITS_REFUNDS_CATEGORY, *list(_NON_SPENDING_CATEGORIES)],
        )
    if metric == "savings":
        return "category = 'Savings Transfer' AND amount < 0", []
    if metric == "credit_card_payments":
        return "category = 'Credit Card Payment' AND amount < 0", []
    if metric == "personal_transfers":
        return f"{personal_transfer_clause}", []
    if metric == "cash_deposits":
        return "category = 'Cash Deposit' AND amount > 0", []
    if metric == "cash_withdrawals":
        return "category = 'Cash Withdrawal' AND amount < 0", []
    if metric == "investment_transfers":
        return "category = 'Investment Transfer'", []
    return "1=1", []


def _period_summary_samples(
    metric: str,
    *,
    profile: str | None,
    start: str | None,
    end: str | None,
    conn,
) -> list[str]:
    where, params = _period_component_filter(metric, profile)
    clauses = [where]
    if profile and profile != "household":
        clauses.append("profile_id = ?")
        params.append(profile)
    if start:
        clauses.append("date >= ?")
        params.append(start)
    if end:
        clauses.append("date <= ?")
        params.append(end)
    try:
        rows = conn.execute(
            f"""
            SELECT id AS original_id
            FROM transactions_visible
            WHERE {" AND ".join(clauses)}
            ORDER BY date DESC
            LIMIT 5
            """,
            params,
        ).fetchall()
    except Exception:
        return []
    return _sample_transaction_ids([dict(row) for row in rows])


def _t_get_period_summary(args: dict, profile: str | None, conn) -> Any:
    """Range-aware income, expense, and cashflow-component summary."""
    range_token = args.get("range") or "current_month"
    start, end, label = _resolve_range(range_token)
    metric = str(args.get("metric") or "summary").strip().lower()
    metric_aliases = {
        "income_total": "income",
        "expense": "expenses",
        "expense_total": "expenses",
        "spending": "expenses",
        "credit_card_payment": "credit_card_payments",
        "cc_payments": "credit_card_payments",
        "refund": "refunds",
        "credits_refunds": "refunds",
        "savings_transfer": "savings",
    }
    metric = metric_aliases.get(metric, metric)

    profile_clauses: list[str] = []
    profile_params: list[Any] = []
    if profile and profile != "household":
        profile_clauses.append("profile_id = ?")
        profile_params.append(profile)
    if start:
        profile_clauses.append("date >= ?")
        profile_params.append(start)
    if end:
        profile_clauses.append("date <= ?")
        profile_params.append(end)
    where_sql = " AND ".join(profile_clauses) if profile_clauses else "1=1"
    non_spending_ph = ",".join("?" * len(_NON_SPENDING_CATEGORIES))
    transfer_ok = _transfer_ok_clause(profile)
    personal_transfer_clause = _personal_transfer_type_clause(profile)
    sql = f"""
        SELECT
            COALESCE(SUM(CASE WHEN category = 'Income' AND amount > 0 {transfer_ok} THEN amount ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE WHEN amount < 0 AND category NOT IN ({non_spending_ph}) {transfer_ok} THEN ABS(amount) ELSE 0 END), 0) AS expenses,
            COALESCE(SUM(CASE WHEN amount > 0 AND (category = ? OR (category NOT IN ({non_spending_ph}) AND category != 'Income')) {transfer_ok} THEN amount ELSE 0 END), 0) AS refunds,
            COALESCE(SUM(CASE WHEN category = 'Savings Transfer' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS savings,
            COALESCE(SUM(CASE WHEN category = 'Credit Card Payment' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS credit_card_payments,
            COALESCE(SUM(CASE WHEN {personal_transfer_clause} AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS personal_transfers_out,
            COALESCE(SUM(CASE WHEN {personal_transfer_clause} AND amount > 0 THEN amount ELSE 0 END), 0) AS personal_transfers_in,
            COALESCE(SUM(CASE WHEN category = 'Cash Deposit' AND amount > 0 THEN amount ELSE 0 END), 0) AS cash_deposits,
            COALESCE(SUM(CASE WHEN category = 'Cash Withdrawal' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS cash_withdrawals,
            COALESCE(SUM(CASE WHEN category = 'Investment Transfer' AND amount > 0 THEN amount ELSE 0 END), 0) AS investment_inflows,
            COALESCE(SUM(CASE WHEN category = 'Investment Transfer' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS investment_outflows,
            COALESCE(SUM(CASE WHEN category = 'Income' AND amount > 0 {transfer_ok} THEN 1 ELSE 0 END), 0) AS income_count,
            COALESCE(SUM(CASE WHEN amount < 0 AND category NOT IN ({non_spending_ph}) {transfer_ok} THEN 1 ELSE 0 END), 0) AS expense_count,
            COALESCE(SUM(CASE WHEN amount > 0 AND (category = ? OR (category NOT IN ({non_spending_ph}) AND category != 'Income')) {transfer_ok} THEN 1 ELSE 0 END), 0) AS refund_count,
            COALESCE(SUM(CASE WHEN category = 'Savings Transfer' AND amount < 0 THEN 1 ELSE 0 END), 0) AS savings_count,
            COALESCE(SUM(CASE WHEN category = 'Credit Card Payment' AND amount < 0 THEN 1 ELSE 0 END), 0) AS credit_card_payment_count,
            COALESCE(SUM(CASE WHEN {personal_transfer_clause} THEN 1 ELSE 0 END), 0) AS personal_transfer_count,
            COUNT(*) AS transaction_count
        FROM transactions_visible
        WHERE {where_sql}
    """
    params = (
        list(_NON_SPENDING_CATEGORIES)
        + [CREDITS_REFUNDS_CATEGORY]
        + list(_NON_SPENDING_CATEGORIES)
        + list(_NON_SPENDING_CATEGORIES)
        + [CREDITS_REFUNDS_CATEGORY]
        + list(_NON_SPENDING_CATEGORIES)
        + profile_params
    )
    row = conn.execute(sql, params).fetchone()
    values = dict(row) if hasattr(row, "keys") else {}
    values = {key: _fmt_money(value) for key, value in values.items() if key not in {"income_count", "expense_count", "refund_count", "savings_count", "credit_card_payment_count", "personal_transfer_count", "transaction_count"}}
    counts = {
        "income": int(row["income_count"] if hasattr(row, "keys") else 0),
        "expenses": int(row["expense_count"] if hasattr(row, "keys") else 0),
        "refunds": int(row["refund_count"] if hasattr(row, "keys") else 0),
        "savings": int(row["savings_count"] if hasattr(row, "keys") else 0),
        "credit_card_payments": int(row["credit_card_payment_count"] if hasattr(row, "keys") else 0),
        "personal_transfers": int(row["personal_transfer_count"] if hasattr(row, "keys") else 0),
        "summary": int(row["transaction_count"] if hasattr(row, "keys") else 0),
    }
    values["net"] = _fmt_money(
        values.get("income", 0)
        + values.get("refunds", 0)
        + values.get("personal_transfers_in", 0)
        + values.get("cash_deposits", 0)
        + values.get("investment_inflows", 0)
        - values.get("expenses", 0)
        - values.get("personal_transfers_out", 0)
        - values.get("cash_withdrawals", 0)
        - values.get("investment_outflows", 0)
    )
    selected_values = {
        "income": values.get("income", 0),
        "expenses": values.get("expenses", 0),
        "refunds": values.get("refunds", 0),
        "savings": values.get("savings", 0),
        "credit_card_payments": values.get("credit_card_payments", 0),
        "personal_transfers": values.get("personal_transfers_out", 0),
        "cash_deposits": values.get("cash_deposits", 0),
        "cash_withdrawals": values.get("cash_withdrawals", 0),
        "investment_transfers": values.get("investment_outflows", 0),
        "summary": values.get("net", 0),
    }
    row_count = counts.get(metric, counts["summary"])
    samples = _period_summary_samples(metric, profile=profile, start=start, end=end, conn=conn)
    return _add_contract(
        {
            "range": label,
            "start": start,
            "end": end,
            "metric": metric,
            "value": _fmt_money(selected_values.get(metric, selected_values["summary"])),
            **values,
            "counts": counts,
        },
        tool="get_period_summary",
        args={**args, "metric": metric, "range": range_token},
        label=label,
        start=start,
        end=end,
        row_count=row_count,
        sample_transaction_ids=samples,
        filters={"profile": profile or "household", "metric": metric},
    )


def _range_to_kwargs(args: dict) -> tuple[str, str | None, str | None, str]:
    """Resolve range token to (month_arg, start_date, end_date, label) for data_manager calls."""
    token = args.get("range") or args.get("month")
    start, end, label = _resolve_range(token)
    # If the label is a YYYY-MM and the range is an exact month, use month= for efficiency
    if _MONTH_RE.match(label or ""):
        return label, None, None, label
    return None, start, end, label


def _t_get_top_categories(args: dict, profile: str | None, conn) -> Any:
    """Delegates to get_category_analytics_data — same aggregation as dashboard."""
    month, start, end, label = _range_to_kwargs(args)
    limit = int(args.get("limit") or 10)
    data = get_category_analytics_data(
        month=month, profile=profile, conn=conn, start_date=start, end_date=end,
    ) or {}
    cats = data.get("categories") or []
    return {
        "range": label,
        "start": start, "end": end,
        "categories": cats[:limit],
    }


def _t_get_top_merchants(args: dict, profile: str | None, conn) -> Any:
    """Delegates to get_merchant_insights_data with include_unenriched=True so partially-enriched merchants are visible."""
    month, start, end, label = _range_to_kwargs(args)
    limit = int(args.get("limit") or 10)
    rows = get_merchant_insights_data(
        month=month, profile=profile, conn=conn,
        start_date=start, end_date=end, include_unenriched=True,
    ) or []
    return {
        "range": label,
        "start": start, "end": end,
        "merchants": rows[:limit],
    }


def _t_get_finance_priorities(args: dict, profile: str | None, conn) -> Any:
    """Rank current-month money items to watch or fix, using scoped semantic tools."""
    focus = str(args.get("focus") or "watch").strip().lower()
    if focus not in {"watch", "fix"}:
        focus = "watch"
    range_token = args.get("range") or "current_month"
    top_categories = _t_get_top_categories({"range": range_token, "limit": 8}, profile, conn) or {}
    budget = _t_get_budget_plan_summary({"range": range_token}, profile, conn) or {}
    recurring = _t_get_recurring_summary({"status": "active", "all": True}, profile, conn) or {}
    categories = top_categories.get("categories") if isinstance(top_categories.get("categories"), list) else []
    variable_categories = [
        row for row in categories
        if str(row.get("expense_type") or "").lower() != "fixed"
    ]

    items: list[dict[str, Any]] = []
    if focus == "fix" and not budget.get("has_budget_plan"):
        items.append({
            "kind": "budget_setup",
            "title": "Build a real budget baseline",
            "why": "I do not see a configured category budget plan for this profile yet.",
            "recommended_action": "Set category budgets first, then use safe-to-spend checks against the plan.",
            "severity": "warning",
            "priority": 10,
            "evidence": {"budget_count": budget.get("budget_count") or 0},
        })
    elif budget.get("has_budget_plan"):
        over_count = int(budget.get("over_count") or 0)
        safe_to_spend = float(budget.get("safe_to_spend") or 0)
        if over_count > 0:
            items.append({
                "kind": "budget_overage",
                "title": "Budget categories are over plan",
                "why": f"{over_count} budget category/categories are already over their configured target.",
                "recommended_action": "Start with the over-budget category before adding new discretionary spend.",
                "severity": "warning",
                "priority": 15,
                "evidence": {"over_count": over_count, "safe_to_spend": _fmt_money(safe_to_spend)},
            })
        elif safe_to_spend <= 0:
            items.append({
                "kind": "safe_to_spend",
                "title": "Safe-to-spend is tight",
                "why": f"Budget safe-to-spend is {_fmt_money(safe_to_spend)} for the current plan snapshot.",
                "recommended_action": "Treat optional purchases as review-only until income or budgets move.",
                "severity": "warning",
                "priority": 18,
                "evidence": {"safe_to_spend": _fmt_money(safe_to_spend)},
            })

    if variable_categories:
        lead = variable_categories[0]
        category = lead.get("category") or lead.get("name") or "variable spending"
        amount = lead.get("total") if lead.get("total") is not None else lead.get("amount")
        percent = lead.get("percent")
        why = f"{category} is the largest current-month variable category at ${_fmt_money(amount):,.2f}"
        if percent is not None:
            why += f" ({_fmt_money(percent)}% of categorized spend)"
        items.append({
            "kind": "variable_spend",
            "title": f"Watch {category}",
            "why": why + ".",
            "recommended_action": "Open this category before changing budgets or adding new discretionary spend.",
            "severity": "info" if focus == "watch" else "warning",
            "priority": 25 if focus == "watch" else 20,
            "evidence": {"category": category, "amount": _fmt_money(amount), "range": top_categories.get("range")},
        })

    recurring_items = recurring.get("items") if isinstance(recurring.get("items"), list) else []
    total_monthly = float(recurring.get("total_monthly") or 0)
    if recurring_items and total_monthly > 0:
        items.append({
            "kind": "recurring_pressure",
            "title": "Review recurring charges",
            "why": f"{len(recurring_items)} active recurring item(s) total about ${_fmt_money(total_monthly):,.2f}/month.",
            "recommended_action": "Look for duplicates, annual renewals, or low-value subscriptions before trimming daily spend.",
            "severity": "info",
            "priority": 35 if focus == "watch" else 30,
            "evidence": {
                "active_count": len(recurring_items),
                "total_monthly": _fmt_money(total_monthly),
                "sample": [
                    item.get("merchant") or item.get("name") or item.get("description")
                    for item in recurring_items[:3]
                ],
            },
        })

    if not items and categories:
        lead = categories[0]
        category = lead.get("category") or lead.get("name") or "spending"
        amount = lead.get("total") if lead.get("total") is not None else lead.get("amount")
        items.append({
            "kind": "spend_overview",
            "title": f"Start with {category}",
            "why": f"{category} is the biggest current-month category at ${_fmt_money(amount):,.2f}.",
            "recommended_action": "Use it as the first drill-down, then compare merchants inside it.",
            "severity": "info",
            "priority": 50,
            "evidence": {"category": category, "amount": _fmt_money(amount), "range": top_categories.get("range")},
        })

    items = sorted(items, key=lambda item: int(item.get("priority") or 99))[: int(args.get("limit") or 3)]
    label = str(top_categories.get("range") or range_token)
    summary = (
        f"Ranked {len(items)} current-month item(s) to {'fix' if focus == 'fix' else 'watch'}."
        if items else "No clear priority items were found for this scope."
    )
    caveats = []
    if not budget.get("has_budget_plan"):
        caveats.append("No configured budget plan was found, so budget-based prioritization is limited.")
    return _add_contract({
        "focus": focus,
        "range": label,
        "summary": summary,
        "items": items,
        "top_categories": categories,
        "budget_plan": {
            "has_budget_plan": bool(budget.get("has_budget_plan")),
            "remaining": budget.get("remaining"),
            "safe_to_spend": budget.get("safe_to_spend"),
            "over_count": budget.get("over_count"),
        },
        "recurring": {
            "active_count": len(recurring_items),
            "total_monthly": _fmt_money(total_monthly),
        },
        "provenance": _semantic_provenance(
            tool="get_finance_priorities",
            args={"range": range_token, "focus": focus},
            label=label,
            row_count=len(items),
            filters={"profile": profile or "household", "focus": focus},
            caveats=caveats,
        ),
    }, tool="get_finance_priorities", args={"range": range_token, "focus": focus}, label=label, row_count=len(items), filters={"profile": profile or "household", "focus": focus}, caveats=caveats)


def _t_get_category_spend(args: dict, profile: str | None, conn) -> Any:
    """Lookup one spending category via the same aggregation as the dashboard, plus recent txns."""
    category = (args.get("category") or "").strip()
    if not category:
        return {"error": "category required"}
    month, start, end, label = _range_to_kwargs(args)
    non_spending, display_category, expense_type = _is_non_spending_category(category, conn)
    if non_spending:
        return _unsupported_category_spend_result(
            category=display_category,
            label=label,
            start=start,
            end=end,
            reason=f"{display_category} has expense_type={expense_type or 'non_expense'} and cannot be used with get_category_spend.",
        )

    data = get_category_analytics_data(
        month=month, profile=profile, conn=conn, start_date=start, end_date=end,
    ) or {}
    match = next(
        (c for c in (data.get("categories") or []) if (c.get("category") or "").lower() == category.lower()),
        None,
    )

    # Recent transactions for this category in the same window
    tx_page = get_transactions_paginated(
        month=month, category=category, profile=profile, conn=conn,
        start_date=start, end_date=end, limit=10, offset=0,
    ) or {}

    recent = tx_page.get("data") or []
    row_count = int(tx_page.get("total_count") or 0)
    return _add_contract({
        "category": category,
        "range": label,
        "start": start, "end": end,
        "total": (match or {}).get("total", 0),
        "gross": (match or {}).get("gross", 0),
        "refunds": (match or {}).get("refunds", 0),
        "percent_of_month": (match or {}).get("percent"),
        "expense_type": (match or {}).get("expense_type"),
        "recent": recent,
        "total_count": row_count,
    }, tool="get_category_spend", args=args, label=label, start=start, end=end, row_count=row_count, sample_transaction_ids=_sample_transaction_ids(recent), filters={"category": category})


def _t_get_merchant_spend(args: dict, profile: str | None, conn) -> Any:
    """Lookup a specific merchant by fragment, using the same include_unenriched path as get_top_merchants."""
    from merchant_identity import canonicalize_merchant_key

    merchant = (args.get("merchant") or "").strip()
    if not merchant:
        return {"error": "merchant required"}
    month, start, end, label = _range_to_kwargs(args)
    merchant_key = canonicalize_merchant_key(merchant)

    all_merchants = get_merchant_insights_data(
        month=month, profile=profile, conn=conn,
        start_date=start, end_date=end, include_unenriched=True,
    ) or []
    needle = merchant.lower()
    matched = [
        m for m in all_merchants
        if needle in (m.get("name") or "").lower()
        or (merchant_key and canonicalize_merchant_key(m.get("name") or "") == merchant_key)
    ]

    search = f"%{merchant.upper()}%"
    params: list[Any] = list(_NON_SPENDING_CATEGORIES)
    where = [
        "amount < 0",
        f"category NOT IN ({','.join('?' for _ in _NON_SPENDING_CATEGORIES)})",
        "(expense_type IS NULL OR expense_type NOT IN ('transfer_internal','transfer_household'))",
        """(
            UPPER(COALESCE(merchant_key, '')) = ?
            OR UPPER(COALESCE(description, '')) LIKE ?
            OR UPPER(COALESCE(raw_description, '')) LIKE ?
            OR UPPER(COALESCE(merchant_key, '')) LIKE ?
            OR UPPER(COALESCE(merchant_name, '')) LIKE ?
        )""",
    ]
    params.extend([merchant_key, search, search, search, search])
    if profile and profile != "household":
        where.append("profile_id = ?")
        params.append(profile)
    if month:
        where.append("date LIKE ?")
        params.append(month + "%")
    else:
        if start:
            where.append("date >= ?")
            params.append(start)
        if end:
            where.append("date <= ?")
            params.append(end)
    where_sql = " AND ".join(where)
    total_row = conn.execute(
        f"""
        SELECT COALESCE(SUM(ABS(amount)), 0) AS total, COUNT(*) AS count
        FROM transactions_visible
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    recent_rows = conn.execute(
        f"""
        SELECT id as original_id, profile_id as profile, date, description, raw_description,
               amount, category, original_category, categorization_source, confidence,
               transaction_type as type, account_name, account_type, merchant_name, merchant_key,
               enriched, is_excluded, expense_type
        FROM transactions_visible
        WHERE {where_sql}
        ORDER BY date DESC
        LIMIT 10
        """,
        params,
    ).fetchall()
    recent = [dict(row) for row in recent_rows]
    for tx in recent:
        tx["enriched"] = bool(tx.get("enriched", 0))
        tx["is_excluded"] = bool(tx.get("is_excluded", 0))

    row_count = int(total_row[1] if total_row else 0)
    return _add_contract({
        "merchant_query": merchant,
        "range": label,
        "start": start, "end": end,
        "matched_merchants": matched[:5],
        "total": _fmt_money(total_row[0] if total_row else 0),
        "txn_count": row_count,
        "recent": recent,
        "total_matching_transactions": row_count,
    }, tool="get_merchant_spend", args=args, label=label, start=start, end=end, row_count=row_count, sample_transaction_ids=_sample_transaction_ids(recent), filters={"merchant": merchant})


def _t_get_transactions(args: dict, profile: str | None, conn) -> Any:
    """General-purpose transaction search — same source as the Transactions page."""
    month, start, end, label = _range_to_kwargs(args) if (args.get("range") or args.get("month")) else (None, None, None, "all")
    search = args.get("search") or args.get("merchant")
    result = get_transactions_paginated(
        month=month,
        category=args.get("category"),
        account=args.get("account"),
        search=search,
        profile=profile,
        limit=int(args.get("limit") or 25),
        offset=int(args.get("offset") or 0),
        conn=conn,
        start_date=start,
        end_date=end,
        sort=args.get("sort"),
    ) or {}
    if isinstance(result, dict):
        rows = _semantic_rows(result)
        row_count = _semantic_count(result, rows)
        return _add_contract(
            result,
            tool="get_transactions",
            args=args,
            label=label,
            start=start,
            end=end,
            row_count=row_count,
            sample_transaction_ids=_sample_transaction_ids(rows),
            filters={k: v for k, v in args.items() if k not in {"range", "month", "offset"} and v not in (None, "", [])},
        )
    return result


def _t_get_category_breakdown(args: dict, profile: str | None, conn) -> Any:
    """Full per-category breakdown (Sankey / cash-waterfall data source)."""
    month, start, end, label = _range_to_kwargs(args)
    result = {
        "range": label,
        "start": start, "end": end,
        **(get_category_analytics_data(
            month=month, profile=profile, conn=conn, start_date=start, end_date=end,
        ) or {}),
    }
    categories = result.get("categories") if isinstance(result.get("categories"), list) else []
    return _add_contract(
        result,
        tool="get_category_breakdown",
        args=args,
        label=label,
        start=start,
        end=end,
        row_count=len(categories),
        filters={"profile": profile or "household"},
    )


def _t_get_dashboard_bundle(args: dict, profile: str | None, conn) -> Any:
    """Aggregated dashboard snapshot — same payload that powers the main dashboard."""
    return get_dashboard_bundle_data(profile=profile, conn=conn) or {}


def _sample_transaction_ids(rows: list[dict[str, Any]] | None, limit: int = 5) -> list[str]:
    samples: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        tx_id = row.get("original_id") or row.get("id") or row.get("transaction_id")
        if tx_id and str(tx_id) not in samples:
            samples.append(str(tx_id))
        if len(samples) >= limit:
            break
    return samples


def _metric_contract(
    *,
    tool: str,
    args: dict[str, Any],
    label: str | None = None,
    start: str | None = None,
    end: str | None = None,
    row_count: int | None = None,
    sample_transaction_ids: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    calculation_basis: str | None = None,
    caveats: list[str] | None = None,
) -> dict[str, Any]:
    metric_ids = metric_registry.metric_ids_for_tool(tool, args)
    metric_id = metric_ids[0] if metric_ids else None
    metric = metric_registry.metric_payload(metric_id)
    payload: dict[str, Any] = {
        "metric_id": metric_id,
        "metric_ids": metric_ids,
        "metric_definition": metric,
        "metric_definition_summary": metric_registry.metric_summary(metric_id),
        "range": label,
        "start": start,
        "end": end,
        "filters": filters or {},
        "row_count": int(row_count or 0),
        "count": int(row_count or 0),
        "sample_transaction_ids": list(sample_transaction_ids or [])[:8],
        "calculation_basis": calculation_basis or ((metric or {}).get("default_provenance_text") if metric else "Computed from Folio tool results."),
        "data_quality": {
            "caveats": list(caveats or []),
        },
        "caveats": list(caveats or []),
    }
    return payload


def _add_contract(
    result: dict[str, Any],
    *,
    tool: str,
    args: dict[str, Any],
    label: str | None = None,
    start: str | None = None,
    end: str | None = None,
    row_count: int | None = None,
    sample_transaction_ids: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    calculation_basis: str | None = None,
    caveats: list[str] | None = None,
) -> dict[str, Any]:
    contract = _metric_contract(
        tool=tool,
        args=args,
        label=label,
        start=start,
        end=end,
        row_count=row_count,
        sample_transaction_ids=sample_transaction_ids,
        filters=filters,
        calculation_basis=calculation_basis,
        caveats=caveats,
    )
    result.update(contract)
    provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    result["provenance"] = {**provenance, **contract, "tool": tool, "args": {k: v for k, v in (args or {}).items() if v not in (None, "", [])}}
    return result


def _semantic_provenance(
    *,
    tool: str,
    args: dict[str, Any],
    label: str,
    start: str | None = None,
    end: str | None = None,
    row_count: int = 0,
    sample_transaction_ids: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    caveats: list[str] | None = None,
) -> dict[str, Any]:
    contract = _metric_contract(
        tool=tool,
        args=args,
        label=label,
        start=start,
        end=end,
        row_count=row_count,
        sample_transaction_ids=sample_transaction_ids,
        filters=filters,
        caveats=caveats,
    )
    return {
        "tool": tool,
        "args": {k: v for k, v in (args or {}).items() if v not in (None, "", [])},
        **contract,
    }


def _semantic_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("transactions", "data", "recent"):
        rows = result.get(key)
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _semantic_count(result: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    for key in ("total_count", "txn_count", "total_matching_transactions", "row_count", "active_count"):
        try:
            if result.get(key) is not None:
                return int(result.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return len(rows)


def _t_get_dashboard_snapshot(args: dict, profile: str | None, conn) -> Any:
    caveats: list[str] = []
    try:
        bundle = get_dashboard_bundle_data(profile=profile, conn=conn) or {}
    except Exception:
        caveats.append("Full dashboard bundle was unavailable; snapshot used fixture-safe summary/category/merchant/recurring fallbacks.")
        bundle = {
            "summary": get_summary_data(profile=profile, conn=conn) or {},
            "categories": (_t_get_top_categories({"range": "current_month", "limit": 5}, profile, conn) or {}).get("categories") or [],
            "merchants": (_t_get_top_merchants({"range": "current_month", "limit": 5}, profile, conn) or {}).get("merchants") or [],
            "recurring": _t_get_recurring_summary({"limit": 5}, profile, conn),
        }
    sections = {
        key: bundle.get(key)
        for key in ("summary", "accounts", "monthly", "categories", "net_worth", "recurring", "budgets")
        if key in bundle
    }
    if not sections:
        sections = bundle
    if not sections:
        caveats.append("Dashboard bundle returned no sections.")
    range_token = args.get("range")
    if range_token:
        ranged_categories = _t_get_top_categories({"range": range_token, "limit": int(args.get("category_limit") or 5)}, profile, conn) or {}
        ranged_merchants = _t_get_top_merchants({"range": range_token, "limit": int(args.get("merchant_limit") or 5)}, profile, conn) or {}
        sections = dict(sections)
        sections["categories"] = ranged_categories.get("categories") or []
        sections["merchants"] = ranged_merchants.get("merchants") or []
        caveats.append(f"Dashboard snapshot category/merchant sections are scoped to {range_token}; other dashboard sections keep their native dashboard scope.")
    return _add_contract({
        "summary": "Dashboard snapshot from Folio's dashboard bundle.",
        "sections": sections,
        "provenance": _semantic_provenance(
            tool="get_dashboard_snapshot",
            args=args,
            label="dashboard",
            filters={"profile": profile or "household"},
            caveats=caveats,
        ),
    }, tool="get_dashboard_snapshot", args=args, label="dashboard", filters={"profile": profile or "household"}, caveats=caveats)


def _t_find_transactions(args: dict, profile: str | None, conn) -> Any:
    merchant = str(args.get("merchant") or "").strip()
    query_args = {
        "range": args.get("range"),
        "category": args.get("category"),
        "account": args.get("account"),
        "search": args.get("search") or merchant or args.get("query"),
        "subtype": args.get("subtype"),
        "mode": args.get("mode"),
        "limit": max(1, min(int(args.get("limit") or 25), 50)),
        "offset": max(0, int(args.get("offset") or 0)),
        "sort": args.get("sort"),
    }
    query_args = {k: v for k, v in query_args.items() if v not in (None, "", [])}
    result = _t_get_transactions(query_args, profile, conn)
    if not isinstance(result, dict):
        result = {}
    rows = _semantic_rows(result)
    row_count = _semantic_count(result, rows)
    if query_args.get("mode") == "total" and row_count > len(rows):
        all_rows = list(rows)
        while len(all_rows) < row_count:
            page_args = {**query_args, "limit": 50, "offset": len(all_rows)}
            page = _t_get_transactions(page_args, profile, conn)
            page_rows = _semantic_rows(page if isinstance(page, dict) else {})
            if not page_rows:
                break
            all_rows.extend(page_rows)
        rows = all_rows
        result["data"] = rows
    if query_args.get("mode") == "total":
        result["total_amount"] = sum(float(row.get("amount") or 0) for row in rows if isinstance(row, dict))
    _, start, end, label = _range_to_kwargs(query_args) if query_args.get("range") else (None, None, None, "all")
    row_count = _semantic_count(result, rows)
    return _add_contract({
        **result,
        "range": label,
        "start": start,
        "end": end,
        "transactions": rows,
        "row_count": row_count,
        "summary": f"Found {row_count} matching transaction{'s' if row_count != 1 else ''}.",
        "provenance": _semantic_provenance(
            tool="find_transactions",
            args=query_args,
            label=label,
            start=start,
            end=end,
            row_count=row_count,
            sample_transaction_ids=_sample_transaction_ids(rows),
            filters={k: v for k, v in query_args.items() if k not in {"range", "offset"}},
        ),
    }, tool="find_transactions", args=query_args, label=label, start=start, end=end, row_count=row_count, sample_transaction_ids=_sample_transaction_ids(rows), filters={k: v for k, v in query_args.items() if k not in {"range", "offset"}})


def _subject_spend_tool(subject_type: str):
    if subject_type == "category":
        return _t_get_category_spend, "category"
    if subject_type == "merchant":
        return _t_get_merchant_spend, "merchant"
    return None, ""


def _t_analyze_subject(args: dict, profile: str | None, conn) -> Any:
    subject_type = str(args.get("subject_type") or "").strip().lower()
    subject = str(args.get("subject") or args.get(subject_type) or "").strip()
    range_token = args.get("range") or "current_month"
    tool, key = _subject_spend_tool(subject_type)
    if not tool or not subject:
        return {"error": "subject_type must be merchant or category and subject is required"}
    spend = tool({key: subject, "range": range_token}, profile, conn) or {}
    rows = _semantic_rows(spend)
    row_count = _semantic_count(spend, rows)
    trend = None
    if subject_type == "category":
        trend = _t_get_monthly_spending_trend({"category": subject, "months": 6}, profile, conn)
    budget = None
    if subject_type == "category":
        budget = _t_get_budget_status({"category": subject, "range": range_token}, profile, conn)
    return _add_contract({
        "subject_type": subject_type,
        "subject": subject,
        "range": spend.get("range") or range_token,
        "start": spend.get("start"),
        "end": spend.get("end"),
        "total": spend.get("total", 0),
        "count": row_count,
        "recent": rows[:10],
        "trend": trend,
        "budget": budget,
        "summary": f"{subject} totals {spend.get('total', 0)} across {row_count} transaction(s).",
        "provenance": _semantic_provenance(
            tool="analyze_subject",
            args={"subject_type": subject_type, "subject": subject, "range": range_token},
            label=str(spend.get("range") or range_token),
            start=spend.get("start"),
            end=spend.get("end"),
            row_count=row_count,
            sample_transaction_ids=_sample_transaction_ids(rows),
            filters={subject_type: subject},
        ),
    }, tool="analyze_subject", args={"subject_type": subject_type, "subject": subject, "range": range_token}, label=str(spend.get("range") or range_token), start=spend.get("start"), end=spend.get("end"), row_count=row_count, sample_transaction_ids=_sample_transaction_ids(rows), filters={subject_type: subject})


def _t_compare_periods(args: dict, profile: str | None, conn) -> Any:
    subject_type = str(args.get("subject_type") or "").strip().lower()
    subject = str(args.get("subject") or args.get(subject_type) or "").strip()
    range_a = args.get("range_a") or args.get("range") or "current_month"
    range_b = args.get("range_b") or "last_month"
    tool, key = _subject_spend_tool(subject_type)
    if not tool or not subject:
        return {"error": "subject_type must be merchant or category and subject is required"}
    left = tool({key: subject, "range": range_a}, profile, conn) or {}
    right = tool({key: subject, "range": range_b}, profile, conn) or {}
    total_a = _fmt_money(left.get("total"))
    total_b = _fmt_money(right.get("total"))
    delta = _fmt_money(total_a - total_b)
    rows = _semantic_rows(left) + _semantic_rows(right)
    row_count = _semantic_count(left, _semantic_rows(left)) + _semantic_count(right, _semantic_rows(right))
    return _add_contract({
        "subject_type": subject_type,
        "subject": subject,
        "range_a": left.get("range") or range_a,
        "range_b": right.get("range") or range_b,
        "start_a": left.get("start"),
        "end_a": left.get("end"),
        "start_b": right.get("start"),
        "end_b": right.get("end"),
        "total_a": total_a,
        "total_b": total_b,
        "delta": delta,
        "left": left,
        "right": right,
        "summary": f"{subject} is {total_a} for {left.get('range') or range_a} versus {total_b} for {right.get('range') or range_b}.",
        "provenance": _semantic_provenance(
            tool="compare_periods",
            args={"subject_type": subject_type, "subject": subject, "range_a": range_a, "range_b": range_b},
            label=f"{left.get('range') or range_a} vs {right.get('range') or range_b}",
            row_count=row_count,
            sample_transaction_ids=_sample_transaction_ids(rows),
            filters={subject_type: subject},
        ),
    }, tool="compare_periods", args={"subject_type": subject_type, "subject": subject, "range_a": range_a, "range_b": range_b}, label=f"{left.get('range') or range_a} vs {right.get('range') or range_b}", row_count=row_count, sample_transaction_ids=_sample_transaction_ids(rows), filters={subject_type: subject})


def _t_get_budget_status(args: dict, profile: str | None, conn) -> Any:
    category = str(args.get("category") or "").strip()
    if not category:
        return {"error": "category required"}
    range_token = args.get("range") or "current_month"
    spend = _t_get_category_spend({"category": category, "range": range_token}, profile, conn) or {}
    budgets = get_category_budgets(profile=profile, conn=conn) or []
    budget = next((item for item in budgets if str(item.get("category") or "").lower() == category.lower()), None)
    amount = _fmt_money((budget or {}).get("amount"))
    actual = _fmt_money(spend.get("total"))
    remaining = _fmt_money(amount - actual)
    rows = _semantic_rows(spend)
    row_count = _semantic_count(spend, rows)
    caveats = [] if budget and amount > 0 else [f"No budget is configured for {category}."]
    return _add_contract({
        "category": category,
        "range": spend.get("range") or range_token,
        "start": spend.get("start"),
        "end": spend.get("end"),
        "budget": amount,
        "actual": actual,
        "remaining": remaining,
        "over_budget": actual > amount if amount > 0 else False,
        "has_budget": bool(budget and amount > 0),
        "recent": rows[:10],
        "row_count": row_count,
        "summary": f"{category} has {remaining} remaining against a {amount} budget." if amount > 0 else f"No budget is set for {category}.",
        "provenance": _semantic_provenance(
            tool="get_budget_status",
            args={"category": category, "range": range_token},
            label=str(spend.get("range") or range_token),
            start=spend.get("start"),
            end=spend.get("end"),
            row_count=row_count,
            sample_transaction_ids=_sample_transaction_ids(rows),
            filters={"category": category},
            caveats=caveats,
        ),
    }, tool="get_budget_status", args={"category": category, "range": range_token}, label=str(spend.get("range") or range_token), start=spend.get("start"), end=spend.get("end"), row_count=row_count, sample_transaction_ids=_sample_transaction_ids(rows), filters={"category": category}, caveats=caveats)


def _t_get_budget_plan_summary(args: dict, profile: str | None, conn) -> Any:
    range_token = args.get("range") or "current_month"
    plan = get_plan_snapshot_data(profile=profile, conn=conn) or {}
    budgets = get_category_budgets(profile=profile, conn=conn) or []
    configured = [item for item in budgets if float(item.get("amount") or 0) > 0]
    total_budget = float(plan.get("total_budget") or 0)
    budgeted_spent = float(plan.get("budgeted_spent") or 0)
    remaining = float(plan.get("remaining") or 0)
    safe_to_spend = float(plan.get("safe_to_spend") or 0)
    caveats = [] if configured else ["No category budget plan is configured yet."]
    return _add_contract({
        "range": range_token,
        "month": plan.get("month"),
        "has_budget_plan": bool(configured and total_budget > 0),
        "budget_count": len(configured),
        "total_budget": round(total_budget, 2),
        "budgeted_spent": round(budgeted_spent, 2),
        "remaining": round(remaining, 2),
        "safe_to_spend": round(safe_to_spend, 2),
        "mandatory_spend": plan.get("mandatory_spend"),
        "mandatory_remaining": plan.get("mandatory_remaining"),
        "variable_spend": plan.get("variable_spend"),
        "over_count": int(plan.get("over_count") or 0),
        "active_goal_count": int(plan.get("active_goal_count") or 0),
        "budgets": configured[:10],
        "summary": (
            f"Budget plan has ${remaining:,.2f} remaining against ${total_budget:,.2f} configured."
            if configured and total_budget > 0
            else "No budget plan is configured yet."
        ),
        "provenance": _semantic_provenance(
            tool="get_budget_plan_summary",
            args={"range": range_token},
            label=str(plan.get("month") or range_token),
            row_count=len(configured),
            filters={"profile": profile or "household"},
            caveats=caveats,
        ),
    }, tool="get_budget_plan_summary", args={"range": range_token}, label=str(plan.get("month") or range_token), row_count=len(configured), filters={"profile": profile or "household"}, caveats=caveats)


def _t_get_savings_capacity(args: dict, profile: str | None, conn) -> Any:
    range_token = args.get("range") or "current_month"
    today = date.today()
    current_month = today.strftime("%Y-%m")
    plan = get_plan_snapshot_data(profile=profile, conn=conn) or {}
    budget_summary = _t_get_budget_plan_summary({"range": range_token}, profile, conn) or {}
    forecast = _t_get_cashflow_forecast({"horizon_days": 30}, profile, conn) or {}
    budgets = get_category_budgets(profile=profile, conn=conn) or []
    configured_budgets = [item for item in budgets if float(item.get("amount") or 0) > 0]
    recurring = _t_get_recurring_summary({"status": "active", "all": True}, profile, conn) or {}
    recurring_items = recurring.get("items") if isinstance(recurring.get("items"), list) else []

    completed_months = _completed_visible_months(conn, profile, current_month)
    income_events, income_sample_ids = _income_events_last_90_days(conn, profile, today)
    monthly_income_values = _completed_month_income_values(profile, conn, current_month)
    average_income = sum(monthly_income_values) / len(monthly_income_values) if monthly_income_values else 0.0
    income_cv = pstdev(monthly_income_values) / average_income if len(monthly_income_values) >= 2 and average_income > 0 else 0.0

    planned_income = float(plan.get("planned_income") or 0)
    mandatory_projected = float(plan.get("mandatory_projected") or 0)
    variable_spend = float(plan.get("variable_spend") or 0)
    save_first_target = float(plan.get("save_first_target") or 0)
    surplus_after_spend = max(planned_income - mandatory_projected - variable_spend, 0)
    point = round(max(0.0, min(save_first_target, surplus_after_spend)), 2)

    caveats: list[str] = []
    if completed_months < 3:
        caveats.append("Fewer than 3 completed months of visible transaction history are available, so Mira will not give a confident point estimate.")
    if income_events < 2:
        caveats.append("Fewer than 2 income events appear in the last 90 days, so Mira will not give a confident point estimate.")
    if income_cv > 0.35:
        caveats.append("Income varies by more than 35%, so Mira is returning a range instead of a point estimate.")
    if not configured_budgets:
        caveats.append("No category budgets are configured, so spending capacity is less constrained than it would be with a budget plan.")
    if not recurring_items:
        caveats.append("No active recurring obligations are available, so fixed monthly commitments may be understated.")
    if int(plan.get("active_goal_count") or 0) <= 0:
        caveats.append("No active goals are configured, so savings guidance is not tied to a target date or goal amount.")
    stale_accounts = _stale_account_count(conn, profile, today)
    if stale_accounts:
        caveats.append("At least one account sync is 7+ days old, so balance-derived guidance may be stale.")
    forecast_caveats = forecast.get("caveats") if isinstance(forecast.get("caveats"), list) else []
    for caveat in forecast_caveats:
        text = str(caveat)
        if text and text not in caveats:
            caveats.append(text)

    hard_insufficient = completed_months < 3 or income_events < 2 or planned_income <= 0
    limited = hard_insufficient or income_cv > 0.35 or not configured_budgets or not recurring_items or int(plan.get("active_goal_count") or 0) <= 0
    status = "insufficient" if hard_insufficient else ("limited" if limited else "ready")

    low_high = _savings_capacity_band(
        point=point,
        planned_income=planned_income,
        mandatory_projected=mandatory_projected,
        variable_spend=variable_spend,
        save_first_target=save_first_target,
        income_cv=income_cv,
        monthly_income_values=monthly_income_values,
    )

    inputs = {
        "completed_history_months": {"value": completed_months, "basis": "Distinct completed months with visible transactions before the current month."},
        "income_events_last_90_days": {"value": income_events, "basis": "Visible Income transactions in the last 90 days."},
        "income_coefficient_of_variation": {"value": round(income_cv, 4), "basis": "Population standard deviation divided by average completed-month income."},
        "planned_income": {"value": round(planned_income, 2), "basis": "data_manager.get_plan_snapshot_data planned_income."},
        "mandatory_projected": {"value": round(mandatory_projected, 2), "basis": "Plan snapshot mandatory spend plus scheduled mandatory remaining."},
        "variable_spend": {"value": round(variable_spend, 2), "basis": "Plan snapshot current-month variable spend."},
        "save_first_target": {"value": round(save_first_target, 2), "basis": "Plan snapshot save-first target."},
        "surplus_after_spend": {"value": round(surplus_after_spend, 2), "basis": "planned_income minus mandatory_projected minus variable_spend."},
        "budget_count": {"value": len(configured_budgets), "basis": "Configured category budgets with amount > 0."},
        "recurring_item_count": {"value": len(recurring_items), "basis": "Active recurring obligations from get_recurring_summary."},
        "active_goal_count": {"value": int(plan.get("active_goal_count") or 0), "basis": "Active goals in the plan snapshot."},
        "stale_account_count": {"value": stale_accounts, "basis": "Active accounts with last_synced_at older than 7 days."},
    }
    basis = [
        "Savings capacity is the lower of the Folio plan snapshot save-first target and remaining surplus after planned income, mandatory projected spend, and variable spend.",
        "A point estimate requires at least 3 completed months of visible history, at least 2 income events in the last 90 days, and income coefficient of variation at or below 35%.",
        "The 30-day cash-flow forecast is included for caveats and provenance; it does not override the plan snapshot arithmetic.",
    ]

    payload: dict[str, Any] = {
        "status": status,
        "range": range_token,
        "basis": basis,
        "inputs": inputs,
        "caveats": caveats,
        "budget_plan": {
            "has_budget_plan": bool(budget_summary.get("has_budget_plan")),
            "remaining": {
                "value": budget_summary.get("remaining"),
                "basis": "get_budget_plan_summary remaining.",
            },
            "safe_to_spend": {
                "value": budget_summary.get("safe_to_spend"),
                "basis": "get_budget_plan_summary safe_to_spend.",
            },
            "provenance": budget_summary.get("provenance"),
        },
        "cashflow_forecast": {
            "confidence": forecast.get("confidence"),
            "projected_low_point": {
                "value": forecast.get("projected_low_point"),
                "basis": "get_cashflow_forecast projected_low_point.",
            },
            "projected_ending_balance": {
                "value": forecast.get("projected_ending_balance"),
                "basis": "get_cashflow_forecast projected_ending_balance.",
            },
            "provenance": forecast.get("provenance"),
        },
    }
    if status == "ready":
        payload["suggested_monthly_savings"] = point
    elif status == "limited" and low_high:
        payload["suggested_monthly_savings_low"] = low_high[0]
        payload["suggested_monthly_savings_high"] = low_high[1]

    row_count = completed_months + income_events + len(configured_budgets) + len(recurring_items)
    return _add_contract(
        payload,
        tool="get_savings_capacity",
        args={"range": range_token},
        label=range_token,
        row_count=row_count,
        sample_transaction_ids=income_sample_ids,
        filters={"profile": profile or "household"},
        caveats=caveats,
        calculation_basis="; ".join(basis),
    )


def _completed_visible_months(conn, profile: str | None, current_month: str) -> int:
    profile_sql, params = _profile_filter(profile)
    rows = conn.execute(
        f"""
        SELECT substr(date, 1, 7) AS month
          FROM transactions_visible
         WHERE date < ?
           {profile_sql}
         GROUP BY substr(date, 1, 7)
        """,
        [current_month + "-01", *params],
    ).fetchall()
    return len(rows)


def _income_events_last_90_days(conn, profile: str | None, today: date) -> tuple[int, list[str]]:
    profile_sql, params = _profile_filter(profile)
    start = (today - timedelta(days=90)).isoformat()
    rows = conn.execute(
        f"""
        SELECT id
          FROM transactions_visible
         WHERE amount > 0
           AND category = 'Income'
           AND date >= ?
           {profile_sql}
         ORDER BY date DESC
        """,
        [start, *params],
    ).fetchall()
    ids = [str(row["id"] if hasattr(row, "keys") else row[0]) for row in rows[:8]]
    return len(rows), ids


def _completed_month_income_values(profile: str | None, conn, current_month: str) -> list[float]:
    monthly = get_monthly_analytics_data(profile=profile, conn=conn) or []
    values = []
    for row in monthly:
        month = str(row.get("month") or "")
        if not month or month >= current_month:
            continue
        income = float(row.get("income") or 0)
        if income > 0:
            values.append(income)
    return values[-6:]


def _stale_account_count(conn, profile: str | None, today: date) -> int:
    profile_sql, params = _profile_filter(profile)
    cutoff = (today - timedelta(days=7)).isoformat()
    rows = conn.execute(
        f"""
        SELECT last_synced_at
          FROM accounts
         WHERE COALESCE(is_active, 1) = 1
           AND COALESCE(last_synced_at, '') != ''
           AND substr(last_synced_at, 1, 10) <= ?
           {profile_sql}
        """,
        [cutoff, *params],
    ).fetchall()
    return len(rows)


def _savings_capacity_band(
    *,
    point: float,
    planned_income: float,
    mandatory_projected: float,
    variable_spend: float,
    save_first_target: float,
    income_cv: float,
    monthly_income_values: list[float],
) -> tuple[float, float] | None:
    if planned_income <= 0:
        return None
    if income_cv > 0.35 and monthly_income_values:
        low_income = min(monthly_income_values)
        high_income = max(monthly_income_values)
        low = max(0.0, min(low_income * 0.20, low_income - mandatory_projected - variable_spend))
        high = max(low, min(high_income * 0.20, high_income - mandatory_projected - variable_spend))
        return round(low, 2), round(high, 2)
    low = round(max(0.0, point * 0.75), 2)
    high = round(max(low, point), 2)
    return low, high


def _t_explain_metric(args: dict, profile: str | None, conn) -> Any:
    metric = str(args.get("metric") or "spending").strip().lower().replace(" ", "_")
    range_token = args.get("range") or "current_month"
    if metric in {"net_worth", "networth", "balance"}:
        result = _t_get_net_worth_delta(args, profile, conn)
        return _add_contract({
            "metric": "net_worth",
            "components": result,
            "summary": "Net worth explanation from Folio's dashboard delta metrics.",
            "provenance": _semantic_provenance(tool="explain_metric", args={"metric": "net_worth"}, label="dashboard"),
        }, tool="explain_metric", args={"metric": "net_worth"}, label="dashboard")
    if metric in {"recurring", "subscriptions", "subscription"}:
        result = _t_get_recurring_changes({"range": range_token, "limit": args.get("limit") or 10}, profile, conn)
        return _add_contract(
            {"metric": "recurring", "components": result, "summary": result.get("summary"), "provenance": result.get("provenance")},
            tool="explain_metric",
            args={"metric": "recurring", "range": range_token},
            label=str(result.get("range") or range_token),
            row_count=int(result.get("row_count") or result.get("active_count") or 0),
            filters={"metric": "recurring"},
            caveats=result.get("caveats") if isinstance(result.get("caveats"), list) else [],
        )
    breakdown = _t_get_category_breakdown({"range": range_token}, profile, conn) or {}
    categories = breakdown.get("categories") if isinstance(breakdown.get("categories"), list) else []
    rows = [row for row in categories if isinstance(row, dict)]
    return _add_contract({
        "metric": "spending",
        "range": breakdown.get("range") or range_token,
        "start": breakdown.get("start"),
        "end": breakdown.get("end"),
        "components": rows[:10],
        "summary": "Spending explanation from category contribution breakdown.",
        "provenance": _semantic_provenance(
            tool="explain_metric",
            args={"metric": metric, "range": range_token},
            label=str(breakdown.get("range") or range_token),
            start=breakdown.get("start"),
            end=breakdown.get("end"),
            row_count=len(rows),
            filters={"metric": metric},
        ),
    }, tool="explain_metric", args={"metric": metric, "range": range_token}, label=str(breakdown.get("range") or range_token), start=breakdown.get("start"), end=breakdown.get("end"), row_count=len(rows), filters={"metric": metric})


def _t_get_recurring_changes(args: dict, profile: str | None, conn) -> Any:
    summary = _t_get_recurring_summary({"limit": args.get("limit") or 25}, profile, conn) or {}
    items = summary.get("items") if isinstance(summary.get("items"), list) else []
    changed = [
        item for item in items
        if str(item.get("subscription_status") or item.get("state") or "").lower() in {"active", "cancelled", "inactive", "candidate"}
    ]
    return _add_contract({
        "range": args.get("range") or "recent",
        "active_count": summary.get("active_count", 0),
        "inactive_count": summary.get("inactive_count", 0),
        "cancelled_count": summary.get("cancelled_count", 0),
        "total_monthly": summary.get("total_monthly", 0),
        "items": changed[: int(args.get("limit") or 10)],
        "summary": f"Found {len(changed)} recurring item(s) with current status data.",
        "provenance": _semantic_provenance(
            tool="get_recurring_changes",
            args=args,
            label=str(args.get("range") or "recent"),
            row_count=len(changed),
            filters={"kind": "recurring"},
        ),
    }, tool="get_recurring_changes", args=args, label=str(args.get("range") or "recent"), row_count=len(changed), filters={"kind": "recurring"}, caveats=[] if changed else ["No recurring status rows were available for this scope."])


def _t_get_cashflow_forecast(args: dict, profile: str | None, conn) -> Any:
    return cashflow_forecast.get_cashflow_forecast(
        conn,
        profile,
        horizon_days=args.get("horizon_days"),
        buffer_amount=args.get("buffer_amount"),
    )


def _t_predict_shortfall(args: dict, profile: str | None, conn) -> Any:
    return cashflow_forecast.predict_shortfall(
        conn,
        profile,
        horizon_days=args.get("horizon_days"),
        buffer_amount=args.get("buffer_amount"),
    )


def _t_check_affordability(args: dict, profile: str | None, conn) -> Any:
    return cashflow_forecast.check_affordability(
        conn,
        profile,
        amount=float(args.get("amount") or 0),
        purpose=str(args.get("purpose") or ""),
        category=str(args.get("category") or ""),
        horizon_days=args.get("horizon_days"),
        buffer_amount=args.get("buffer_amount"),
        question=str(args.get("question") or ""),
    )


def _t_find_low_confidence_transactions(args: dict, profile: str | None, conn) -> Any:
    from transaction_enrichment import find_low_confidence

    try:
        threshold = float(args.get("threshold") or 0.7)
    except (TypeError, ValueError):
        threshold = 0.7
    result = find_low_confidence(
        conn,
        profile,
        threshold=threshold,
        limit=int(args.get("limit") or 25),
    )
    rows = result.get("transactions") if isinstance(result, dict) else []
    if isinstance(result, dict):
        return _add_contract(
            result,
            tool="find_low_confidence_transactions",
            args=args,
            label="current",
            row_count=int(result.get("count") or 0),
            sample_transaction_ids=_sample_transaction_ids(rows if isinstance(rows, list) else []),
            filters={"threshold": threshold, "profile": profile or "household"},
            caveats=[] if rows else ["No low-confidence enrichment rows matched the requested threshold."],
        )
    return result


def _t_explain_transaction_enrichment(args: dict, profile: str | None, conn) -> Any:
    from transaction_enrichment import explain_transaction

    tx_id = str(args.get("transaction_id") or args.get("tx_id") or "").strip()
    if not tx_id:
        return {"error": "transaction_id required"}
    result = explain_transaction(conn, tx_id, profile)
    if isinstance(result, dict):
        return _add_contract(
            result,
            tool="explain_transaction_enrichment",
            args=args,
            label="current",
            row_count=1 if not result.get("error") else 0,
            sample_transaction_ids=[tx_id] if tx_id and not result.get("error") else [],
            filters={"transaction_id": tx_id, "profile": profile or "household"},
            caveats=[str(result.get("error"))] if result.get("error") else [],
        )
    return result


def _t_get_enrichment_quality_summary(args: dict, profile: str | None, conn) -> Any:
    from transaction_enrichment import quality_summary, taxonomy_snapshot

    summary = quality_summary(conn, profile)
    if args.get("include_taxonomy"):
        summary["taxonomy"] = taxonomy_snapshot()
    missing = int(summary.get("transaction_count") or 0) - int(summary.get("persisted_enrichment_count") or 0)
    caveats = []
    if missing > 0:
        caveats.append(f"{missing} transaction(s) do not have persisted enrichment yet.")
    coverage_ratio = float(summary.get("coverage_ratio") or 0)
    coverage_percent = round(coverage_ratio * 100, 1)
    summary["summary"] = (
        f"Transaction enrichment is {coverage_percent}% populated "
        f"({int(summary.get('persisted_enrichment_count') or 0)} of "
        f"{int(summary.get('transaction_count') or 0)} transactions)."
    )
    actionable_low = int(summary.get("actionable_low_confidence_count") or 0)
    strict_low = int(summary.get("low_confidence_count") or 0)
    if strict_low and actionable_low != strict_low:
        summary["summary"] += f" {actionable_low} row(s) have actionable non-recurrence quality flags."
    fields = summary.get("populated_fields") if isinstance(summary.get("populated_fields"), dict) else {}
    essentiality = fields.get("essentiality_known") if isinstance(fields.get("essentiality_known"), dict) else {}
    if essentiality and float(essentiality.get("ratio") or 0) < 0.95:
        caveats.append(
            f"Essentiality is known for {int(essentiality.get('count') or 0)} transaction(s); unknown rows should be treated cautiously."
        )
    if strict_low and actionable_low < strict_low:
        caveats.append(
            "The strict low-confidence count includes recurrence-only uncertainty; actionable quality flags separate taxonomy, counterparty, essentiality, and semantic issues."
        )
    return _add_contract(
        summary,
        tool="get_enrichment_quality_summary",
        args=args,
        label="current",
        row_count=int(summary.get("transaction_count") or 0),
        filters={"profile": profile or "household"},
        caveats=caveats,
    )


def _t_get_net_worth_delta(args: dict, profile: str | None, conn) -> Any:
    """Month-over-month net worth deltas (same numbers the dashboard shows)."""
    result = get_net_worth_delta_metrics(profile=profile, conn=conn) or {}
    return _add_contract(result, tool="get_net_worth_delta", args=args, label="dashboard", filters={"profile": profile or "household"})


def _t_get_recurring_summary(args: dict, profile: str | None, conn) -> Any:
    data = get_recurring_from_db(profile=profile, conn=conn) or {}
    items = data.get("items") or []
    status_filter = args.get("status")
    if status_filter:
        wanted = str(status_filter).lower()
        items = [
            i for i in items
            if str(i.get("status") or i.get("subscription_status") or i.get("state") or "").lower() == wanted
        ]
    if args.get("all"):
        rows = items
    else:
        rows = items[: int(args.get("limit") or 25)]
    return _add_contract({
        "active_count": data.get("active_count", 0),
        "inactive_count": data.get("inactive_count", 0),
        "cancelled_count": data.get("cancelled_count", 0),
        "total_monthly": data.get("total_monthly", 0),
        "total_annual": data.get("total_annual", 0),
        "items": rows,
    }, tool="get_recurring_summary", args=args, label="current", row_count=len(items), filters={"status": status_filter} if status_filter else {}, caveats=[] if items else ["No recurring obligations are configured or detected for this scope."])


def _t_get_net_worth_trend(args: dict, profile: str | None, conn) -> Any:
    interval = args.get("interval") or "monthly"
    series = get_net_worth_series_data(interval=interval, profile=profile, conn=conn) or []

    range_token = args.get("range")
    if range_token and range_token != "all":
        start, end, label = _resolve_range(range_token)
        if start:
            series = [s for s in series if (s.get("date") or "") >= start and (not end or (s.get("date") or "") <= end)]
    else:
        label = "all"

    limit = int(args.get("limit") or 24)
    limited = series[-limit:]
    return _add_contract(
        {"interval": interval, "range": label, "series": limited},
        tool="get_net_worth_trend",
        args=args,
        label=label,
        row_count=len(limited),
        filters={"interval": interval, "profile": profile or "household"},
        caveats=[] if limited else ["No net worth history points were available for this scope."],
    )


def _t_get_monthly_spending_trend(args: dict, profile: str | None, conn) -> Any:
    """
    Monthly spending trend for charting. Uses canonical spending semantics and
    optionally narrows to one category.
    """
    try:
        months = max(1, min(int(args.get("months") or 6), 36))
    except (TypeError, ValueError):
        months = 6
    category = (args.get("category") or "").strip()

    now = datetime.now()
    month_keys: list[str] = []
    bounds: list[tuple[str, str, str]] = []
    for offset in range(-(months - 1), 1):
        y, m = _shift_month(now.year, now.month, offset)
        start, end = _month_bounds(y, m)
        label = f"{y:04d}-{m:02d}"
        month_keys.append(label)
        bounds.append((label, start, end))

    start_date = bounds[0][1]
    end_date = bounds[-1][2]
    pclause, pparams = _profile_filter(profile)
    params: list[Any] = [start_date, end_date]
    category_clause = ""
    if category:
        category_clause = " AND LOWER(category) = LOWER(?)"
        params.append(category)
    params.extend(pparams)

    rows = conn.execute(
        f"""
        SELECT substr(date, 1, 7) AS month,
               SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE -ABS(amount) END) AS total
        FROM transactions_visible
        WHERE date >= ? AND date <= ?
          AND is_excluded = 0
          AND category IS NOT NULL
          AND category != ''
          AND category NOT IN ('Savings Transfer','Personal Transfer','Credit Card Payment','Income','Credits & Refunds')
          AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal','transfer_household'))
          {category_clause}
          {pclause}
        GROUP BY month
        ORDER BY month
        """,
        params,
    ).fetchall()
    by_month = {r["month"]: _fmt_money(r["total"]) for r in rows}
    series = [{"month": m, "total": by_month.get(m, 0.0)} for m in month_keys]
    return _add_contract({
        "category": category or None,
        "months": months,
        "start": start_date,
        "end": end_date,
        "series": series,
        "labels": [s["month"] for s in series],
        "values": [s["total"] for s in series],
    }, tool="get_monthly_spending_trend", args=args, label=f"last_{months}_months", start=start_date, end=end_date, row_count=len(series), filters={"category": category} if category else {}, caveats=[] if any(s["total"] for s in series) else ["No matching spending rows were found for the trend period."])


def _t_get_transactions_for_merchant(args: dict, profile: str | None, conn) -> Any:
    from merchant_identity import canonicalize_merchant_key

    merchant = (args.get("merchant") or "").strip()
    if not merchant:
        return {"error": "merchant required"}
    limit = max(1, min(int(args.get("limit") or 25), 100))
    offset = max(0, int(args.get("offset") or 0))
    month, start, end, label = _range_to_kwargs(args) if (args.get("range") or args.get("month")) else (None, None, None, "all")
    merchant_key = canonicalize_merchant_key(merchant) or merchant.upper()
    search = f"%{merchant.upper()}%"
    where = [
        """(
            UPPER(COALESCE(merchant_key, '')) = ?
            OR UPPER(COALESCE(merchant_name, '')) LIKE ?
            OR UPPER(COALESCE(description, '')) LIKE ?
            OR UPPER(COALESCE(raw_description, '')) LIKE ?
        )""",
    ]
    params: list[Any] = [merchant_key, search, search, search]
    if profile and profile != "household":
        where.append("profile_id = ?")
        params.append(profile)
    if month:
        where.append("date LIKE ?")
        params.append(month + "%")
    else:
        if start:
            where.append("date >= ?")
            params.append(start)
        if end:
            where.append("date <= ?")
            params.append(end)
    where_sql = " AND ".join(where)
    total = int(conn.execute(f"SELECT COUNT(*) FROM transactions_visible WHERE {where_sql}", params).fetchone()[0] or 0)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT id as original_id, profile_id as profile, date, description, raw_description,
                   amount, category, original_category, categorization_source, confidence,
                   transaction_type as type, account_name, account_type, merchant_name, merchant_key,
                   enriched, is_excluded, expense_type
            FROM transactions_visible
            WHERE {where_sql}
            ORDER BY date DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    ]
    for tx in rows:
        tx["enriched"] = bool(tx.get("enriched", 0))
        tx["is_excluded"] = bool(tx.get("is_excluded", 0))
    return _add_contract(
        {
            "merchant": merchant,
            "range": label,
            "start": start,
            "end": end,
            "transactions": rows,
            "row_count": total,
            "total_count": total,
            "limit": limit,
            "offset": offset,
        },
        tool="get_transactions_for_merchant",
        args=args,
        label=label,
        start=start,
        end=end,
        row_count=total,
        sample_transaction_ids=_sample_transaction_ids(rows),
        filters={"merchant": merchant, "profile": profile or "household", "limit": limit, "offset": offset},
    )


def _t_get_summary(args: dict, profile: str | None, conn) -> Any:
    result = get_summary_data(profile=profile, conn=conn) or {}
    return _add_contract(result, tool="get_summary", args=args, label="dashboard", filters={"profile": profile or "household"})


def _t_get_account_balances(args: dict, profile: str | None, conn) -> Any:
    pclause, pparams = _profile_filter(profile)
    rows = conn.execute(
        f"""
        SELECT account_name, account_type, account_subtype, institution_name,
               last_four, current_balance, available_balance, currency,
               profile_id, provider, is_active
        FROM accounts
        WHERE is_active = 1{pclause}
        ORDER BY account_type, institution_name, account_name
        """,
        pparams,
    ).fetchall()
    accounts = [dict(r) for r in rows]

    by_type: dict[str, float] = {}
    for a in accounts:
        t = a.get("account_type") or "other"
        by_type[t] = by_type.get(t, 0.0) + float(a.get("current_balance") or 0.0)

    net = (
        by_type.get("depository", 0.0)
        + by_type.get("investment", 0.0)
        - abs(by_type.get("credit", 0.0))
        - abs(by_type.get("loan", 0.0))
    )

    return {
        "accounts": accounts,
        "totals_by_type": {k: _fmt_money(v) for k, v in by_type.items()},
        "approx_net_worth": _fmt_money(net),
    }


def _t_get_category_rules(args: dict, profile: str | None, conn) -> Any:
    rows = conn.execute(
        "SELECT pattern, category, priority, is_active, source FROM category_rules ORDER BY priority DESC LIMIT ?",
        (int(args.get("limit") or 50),),
    ).fetchall()
    return {"rules": [dict(r) for r in rows]}


def _t_search_saved_insights(args: dict, profile: str | None, conn) -> Any:
    keywords = (args.get("keywords") or "").strip().lower()
    limit = int(args.get("limit") or 10)
    if keywords:
        like = f"%{keywords}%"
        rows = conn.execute(
            """
            SELECT id, question, answer, kind, pinned, created_at
            FROM saved_insights
            WHERE (? IS NULL OR profile_id = ? OR profile_id IS NULL)
              AND (LOWER(question) LIKE ? OR LOWER(answer) LIKE ?)
            ORDER BY pinned DESC, created_at DESC
            LIMIT ?
            """,
            (profile, profile, like, like, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, question, answer, kind, pinned, created_at
            FROM saved_insights
            WHERE (? IS NULL OR profile_id = ? OR profile_id IS NULL)
            ORDER BY pinned DESC, created_at DESC
            LIMIT ?
            """,
            (profile, profile, limit),
        ).fetchall()
    return {"insights": [dict(r) for r in rows]}


def _t_remember_user_context(args: dict, profile: str | None, conn) -> Any:
    from mira import memory_v2

    text = str(args.get("text") or args.get("original_text") or "").strip()
    if not text:
        return {"saved": False, "reason": "text is required"}
    return memory_v2.remember_user_context(
        conn=conn,
        profile=profile,
        text=text,
        memory_type=args.get("memory_type"),
        topic=args.get("topic"),
        source_summary=str(args.get("source_summary") or ""),
        source_conversation_id=args.get("source_conversation_id"),
        source_turn_id=args.get("source_turn_id"),
        pinned=bool(args.get("pinned", False)),
        expires_at=args.get("expires_at"),
    )


def _t_retrieve_relevant_memories(args: dict, profile: str | None, conn) -> Any:
    from mira import memory_v2

    return memory_v2.retrieve_relevant_memories(
        conn=conn,
        profile=profile,
        question=str(args.get("question") or args.get("query") or ""),
        route=args.get("route") if isinstance(args.get("route"), dict) else None,
        limit=int(args.get("limit") or 5),
        include_expired=bool(args.get("include_expired", False)),
        force=bool(args.get("force", False)),
    )


def _t_update_memory(args: dict, profile: str | None, conn) -> Any:
    from mira import memory_v2

    memory_id = args.get("id") or args.get("memory_id")
    if memory_id is None:
        text = str(args.get("text") or args.get("original") or "").strip()
        candidate = memory_v2.extract_memory_candidate(text)
        topic = args.get("topic") or (candidate or {}).get("topic") or text
        matches = memory_v2.retrieve_relevant_memories(
            conn=conn,
            profile=profile,
            question=str(topic),
            limit=1,
            force=True,
        ).get("memories") or []
        if not matches:
            return {"updated": False, "reason": "No matching active memory found."}
        memory_id = matches[0]["id"]
        if candidate and not args.get("normalized_text"):
            args = {
                **args,
                "normalized_text": candidate["normalized_text"],
                "memory_type": candidate["memory_type"],
                "topic": candidate["topic"],
                "sensitivity": candidate["sensitivity"],
                "confidence": candidate["confidence"],
            }
    try:
        updated = memory_v2.update_memory(
            conn=conn,
            profile=profile,
            memory_id=int(memory_id),
            normalized_text=args.get("normalized_text"),
            memory_type=args.get("memory_type"),
            topic=args.get("topic"),
            sensitivity=args.get("sensitivity"),
            confidence=args.get("confidence"),
            pinned=args.get("pinned") if "pinned" in args else None,
            expires_at=args.get("expires_at") if "expires_at" in args else None,
            status=args.get("status"),
            source_turn_id=args.get("source_turn_id"),
        )
    except ValueError as exc:
        return {"updated": False, "reason": str(exc)}
    if not updated:
        return {"updated": False, "reason": "No matching active memory found."}
    return {"updated": True, "memory": updated, "memory_trace": memory_v2.trace_for_memories([updated], allowed=True, reason="memory_update")}


def _t_forget_memory(args: dict, profile: str | None, conn) -> Any:
    from mira import memory_v2

    memory_id = args.get("id") or args.get("memory_id")
    try:
        memory_id = int(memory_id) if memory_id is not None else None
    except (TypeError, ValueError):
        memory_id = None
    return memory_v2.forget_memory(
        conn=conn,
        profile=profile,
        memory_id=memory_id,
        topic=args.get("topic"),
        text=args.get("text"),
        source_turn_id=args.get("source_turn_id"),
    )


def _t_list_mira_memories(args: dict, profile: str | None, conn) -> Any:
    from mira import memory_v2

    items = memory_v2.list_memories(
        conn,
        profile,
        include_inactive=bool(args.get("include_inactive", False)),
        include_expired=bool(args.get("include_expired", False)),
        memory_type=args.get("memory_type"),
        limit=int(args.get("limit") or 100),
    )
    return {"items": items, "count": len(items), "memory_trace": memory_v2.trace_for_memories(items, allowed=True, reason="explicit_memory_request")}


def _t_preview_bulk_recategorize(args: dict, profile: str | None, conn) -> Any:
    """Preview moving all transactions for a merchant to a new category.
    Returns a write-preview payload with confirmation_id; user must confirm."""
    from data_manager import bulk_recategorize_preview
    from pending_operations import store_pending_operation

    merchant = (args.get("merchant") or "").strip()
    category = (args.get("category") or "").strip()
    if not merchant or not category:
        return {"error": "merchant and category are required"}

    data = bulk_recategorize_preview(merchant, category, profile, conn)
    count = data.get("count", 0)
    if count == 0:
        return {
            "operation": "read",
            "count": 0,
            "note": f'No transactions found for "{merchant}" that aren\'t already categorized as "{category}".',
        }
    pending = data["pending_operation"]
    confirmation_id = store_pending_operation(
        pending["operation"],
        pending["params"],
        profile,
        {"rows_affected": count, "samples": data.get("samples", [])},
        conn=conn,
    )
    return {
        "_write_preview": True,
        "operation": "write_preview",
        "summary": f"Move {count} {merchant} transaction(s) to {category}",
        "confirmation_id": confirmation_id,
        "rows_affected": count,
        "samples": data.get("samples", []),
        "preview_changes": [{"column": "category", "raw_value": category, "new_value": category}],
    }


def _t_preview_create_rule(args: dict, profile: str | None, conn) -> Any:
    """Preview creating a category rule (pattern → category)."""
    from data_manager import preview_rule_creation
    from pending_operations import store_pending_operation

    pattern = (args.get("pattern") or "").strip()
    category = (args.get("category") or "").strip()
    if not pattern or not category:
        return {"error": "pattern and category are required"}

    data = preview_rule_creation(pattern, category, profile, conn)
    count = data.get("count", 0)
    pending = data["pending_operation"]
    confirmation_id = store_pending_operation(
        pending["operation"],
        pending["params"],
        profile,
        {"rows_affected": count, "samples": data.get("samples", [])},
        conn=conn,
    )
    existing = data.get("existing_rule")
    return {
        "_write_preview": True,
        "operation": "write_preview",
        "summary": (
            f"Create rule {data.get('pattern') or pattern} → {category} "
            f"(applies to {count} existing + all future matches)"
            + (f". Replaces existing rule → {existing['category']}." if existing else "")
        ),
        "confirmation_id": confirmation_id,
        "rows_affected": count,
        "samples": data.get("samples", []),
        "existing_rule": existing,
        "preview_changes": [{"column": "rule", "raw_value": f"{data.get('pattern') or pattern} → {category}", "new_value": category}],
    }


def _t_preview_rename_merchant(args: dict, profile: str | None, conn) -> Any:
    """Preview renaming a merchant (all its transaction variants)."""
    from data_manager import rename_merchant_variants
    from pending_operations import store_pending_operation

    old_name = (args.get("old_name") or "").strip()
    new_name = (args.get("new_name") or "").strip()
    if not old_name or not new_name:
        return {"error": "old_name and new_name are required"}

    data = rename_merchant_variants(old_name, new_name, profile, conn)
    count = data.get("count", 0)
    if count == 0:
        return {
            "operation": "read",
            "count": 0,
            "note": f'No transactions found matching "{old_name}".',
        }
    pending = data["pending_operation"]
    confirmation_id = store_pending_operation(
        pending["operation"],
        pending["params"],
        profile,
        {"rows_affected": count, "samples": data.get("samples", [])},
        conn=conn,
    )
    return {
        "_write_preview": True,
        "operation": "write_preview",
        "summary": f"Rename {count} transaction(s) from {old_name} to {new_name}",
        "confirmation_id": confirmation_id,
        "rows_affected": count,
        "samples": data.get("samples", []),
        "preview_changes": [{"column": "merchant_name", "raw_value": new_name, "new_value": new_name}],
    }


def _store_structured_preview(operation: str, params: dict, profile: str | None, conn) -> Any:
    from data_manager import preview_general_write_operation
    from pending_operations import store_pending_operation

    data = preview_general_write_operation(operation, params, profile, conn)
    count = int(data.get("count") or 0)
    if count <= 0:
        return {
            "operation": "read",
            "count": 0,
            "note": data.get("summary") or "No matching records found for that change.",
            "needs_confirmation": False,
        }
    pending = data["pending_operation"]
    confirmation_id = store_pending_operation(
        pending["operation"],
        pending["params"],
        profile,
        {"rows_affected": count, "samples": data.get("samples", []), "preview_changes": data.get("preview_changes", [])},
        conn=conn,
    )
    return {
        "_write_preview": True,
        "operation": "write_preview",
        "summary": data.get("summary") or f"Preview {operation}",
        "confirmation_id": confirmation_id,
        "rows_affected": count,
        "samples": data.get("samples", []),
        "preview_changes": data.get("preview_changes", []),
    }


def _t_preview_set_budget(args: dict, profile: str | None, conn) -> Any:
    category = (args.get("category") or "").strip()
    if not category:
        return {"error": "category is required"}
    params = {
        "category": category,
        "amount": args.get("amount"),
        "rollover_mode": args.get("rollover_mode"),
        "rollover_balance": args.get("rollover_balance"),
    }
    return _store_structured_preview("set_budget", params, profile, conn)


def _t_preview_create_goal(args: dict, profile: str | None, conn) -> Any:
    name = (args.get("name") or args.get("goal_name") or "").strip()
    if not name:
        return {"error": "name is required"}
    params = {
        "name": name,
        "goal_type": (args.get("goal_type") or "custom").strip() or "custom",
        "target_amount": args.get("target_amount") or 0,
        "current_amount": args.get("current_amount") or 0,
        "target_date": args.get("target_date"),
        "linked_category": args.get("linked_category"),
        "linked_account_id": args.get("linked_account_id"),
    }
    return _store_structured_preview("create_goal", params, profile, conn)


def _t_preview_update_goal_target(args: dict, profile: str | None, conn) -> Any:
    params = {
        "goal_id": args.get("goal_id"),
        "name": args.get("name") or args.get("goal_name"),
        "target_amount": args.get("target_amount"),
        "current_amount": args.get("current_amount"),
        "target_date": args.get("target_date"),
    }
    return _store_structured_preview("update_goal_target", params, profile, conn)


def _t_preview_mark_goal_funded(args: dict, profile: str | None, conn) -> Any:
    params = {"goal_id": args.get("goal_id"), "name": args.get("name") or args.get("goal_name")}
    return _store_structured_preview("mark_goal_funded", params, profile, conn)


def _t_preview_set_transaction_note(args: dict, profile: str | None, conn) -> Any:
    tx_id = (args.get("tx_id") or args.get("transaction_id") or "").strip()
    if not tx_id:
        return {"error": "transaction_id is required"}
    return _store_structured_preview(
        "set_transaction_note",
        {"tx_id": tx_id, "note": args.get("note") or args.get("notes") or ""},
        profile,
        conn,
    )


def _t_preview_set_transaction_tags(args: dict, profile: str | None, conn) -> Any:
    tx_id = (args.get("tx_id") or args.get("transaction_id") or "").strip()
    if not tx_id:
        return {"error": "transaction_id is required"}
    tags = args.get("tags") if isinstance(args.get("tags"), list) else []
    return _store_structured_preview("set_transaction_tags", {"tx_id": tx_id, "tags": tags}, profile, conn)


def _t_preview_mark_reviewed(args: dict, profile: str | None, conn) -> Any:
    tx_id = (args.get("tx_id") or args.get("transaction_id") or "").strip()
    if not tx_id:
        return {"error": "transaction_id is required"}
    return _store_structured_preview("mark_reviewed", {"tx_id": tx_id, "reviewed": bool(args.get("reviewed", True))}, profile, conn)


def _t_preview_bulk_mark_reviewed(args: dict, profile: str | None, conn) -> Any:
    filters = {
        "month": args.get("month"),
        "category": args.get("category"),
        "account": args.get("account"),
        "search": args.get("search"),
        "reviewed": args.get("current_reviewed"),
        "start_date": args.get("start_date"),
        "end_date": args.get("end_date"),
    }
    filters = {k: v for k, v in filters.items() if v not in (None, "", [])}
    return _store_structured_preview(
        "bulk_mark_reviewed",
        {"filters": filters, "reviewed": bool(args.get("reviewed", True))},
        profile,
        conn,
    )


def _t_preview_update_manual_account_balance(args: dict, profile: str | None, conn) -> Any:
    params = {
        "account_id": args.get("account_id"),
        "account_name": args.get("account_name") or args.get("name"),
        "balance": args.get("balance"),
        "notes": args.get("notes"),
    }
    return _store_structured_preview("update_manual_account_balance", params, profile, conn)


def _t_preview_split_transaction(args: dict, profile: str | None, conn) -> Any:
    tx_id = (args.get("tx_id") or args.get("transaction_id") or "").strip()
    if not tx_id:
        return {"error": "transaction_id is required"}
    splits = args.get("splits") if isinstance(args.get("splits"), list) else []
    return _store_structured_preview("split_transaction", {"tx_id": tx_id, "splits": splits}, profile, conn)


def _t_preview_recurring_action(args: dict, profile: str | None, conn, operation: str) -> Any:
    merchant = (args.get("merchant") or args.get("merchant_name") or "").strip()
    if not merchant:
        return {"error": "merchant is required"}
    params = {
        "merchant": merchant,
        "pattern": args.get("pattern"),
        "frequency": args.get("frequency") or args.get("frequency_hint"),
        "category": args.get("category"),
    }
    return _store_structured_preview(operation, params, profile, conn)


def _t_preview_confirm_recurring(args: dict, profile: str | None, conn) -> Any:
    return _t_preview_recurring_action(args, profile, conn, "confirm_recurring_obligation")


def _t_preview_dismiss_recurring(args: dict, profile: str | None, conn) -> Any:
    return _t_preview_recurring_action(args, profile, conn, "dismiss_recurring_obligation")


def _t_preview_cancel_recurring(args: dict, profile: str | None, conn) -> Any:
    return _t_preview_recurring_action(args, profile, conn, "cancel_recurring")


def _t_preview_restore_recurring(args: dict, profile: str | None, conn) -> Any:
    return _t_preview_recurring_action(args, profile, conn, "restore_recurring")


def _t_plot_chart(args: dict, profile: str | None, conn) -> Any:
    """
    Emit a chart spec for the UI to render inline. Use when a visualization
    clarifies trends, comparisons, or distributions.
    Types: 'line' (trends over time), 'bar' (comparisons, top-N),
           'donut' (category share / composition).
    """
    # Be defensive for LLM fallback calls. The deterministic chart path already
    # sends the canonical shape, but models sometimes use common charting aliases
    # like chart_type + data.labels/data.values.
    if "type" not in args and "chart_type" in args:
        args = {**args, "type": args.get("chart_type")}
    nested_data = args.get("data")
    if isinstance(nested_data, dict):
        args = {**nested_data, **args}

    chart_type = (args.get("type") or "").strip().lower()
    if chart_type not in ("line", "bar", "donut"):
        return {"error": "type must be one of: line, bar, donut"}

    labels = args.get("labels") or []
    values = args.get("values") or []
    raw_series = args.get("series") if isinstance(args.get("series"), list) else []
    if not isinstance(labels, list):
        return {"error": "labels must be an array"}
    if not labels:
        return {"error": "at least one data point required"}

    series: list[dict[str, Any]] = []
    if raw_series:
        for idx, item in enumerate(raw_series[:4]):
            if not isinstance(item, dict):
                return {"error": "series items must be objects"}
            item_values = item.get("values") or []
            if not isinstance(item_values, list):
                return {"error": "series values must be arrays"}
            if len(item_values) != len(labels):
                return {"error": f"series {idx + 1} values must match labels length"}
            try:
                numeric_values = [float(v) for v in item_values]
            except (TypeError, ValueError):
                return {"error": "series values must be numeric"}
            series.append(
                {
                    "name": str(item.get("name") or item.get("series_name") or f"Series {idx + 1}"),
                    "values": numeric_values,
                    "color": item.get("color"),
                }
            )
        values = series[0]["values"] if series else []
    else:
        if not isinstance(values, list):
            return {"error": "values must be an array"}
        if len(labels) != len(values):
            return {"error": f"labels ({len(labels)}) and values ({len(values)}) must have same length"}
        try:
            values = [float(v) for v in values]
        except (TypeError, ValueError):
            return {"error": "values must be numeric"}

    annotations = []
    for item in args.get("annotations") or []:
        if not isinstance(item, dict):
            continue
        try:
            value = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        annotations.append({"label": str(item.get("label") or ""), "value": value, "color": item.get("color")})

    return {
        "_chart": True,
        "type": chart_type,
        "title": args.get("title") or "",
        "series_name": args.get("series_name") or "",
        "labels": [str(l) for l in labels],
        "values": values,
        "series": series,
        "annotations": annotations[:4],
        "unit": args.get("unit") or "currency",  # 'currency' | 'number' | 'percent'
    }


def _table_exists(conn, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _safe_count(conn, table_name: str, profile: str | None = None, profile_column: str = "profile_id") -> int | None:
    if not _table_exists(conn, table_name):
        return None
    where = ""
    params: list[Any] = []
    if profile and profile != "household":
        where = f" WHERE {profile_column} = ?"
        params.append(profile)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}{where}", params).fetchone()[0] or 0)
    except Exception:
        return None


def _safe_max(conn, table_name: str, column: str, profile: str | None = None, profile_column: str = "profile_id") -> str | None:
    if not _table_exists(conn, table_name):
        return None
    where = ""
    params: list[Any] = []
    if profile and profile != "household":
        where = f" WHERE {profile_column} = ?"
        params.append(profile)
    try:
        value = conn.execute(f"SELECT MAX({column}) FROM {table_name}{where}", params).fetchone()[0]
        return str(value) if value else None
    except Exception:
        return None


def _days_old(iso_value: str | None) -> int | None:
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_value)[:19])
    except ValueError:
        try:
            dt = datetime.strptime(str(iso_value)[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return max(0, (datetime.now().date() - dt.date()).days)


def _transaction_anomaly_summary(
    conn,
    *,
    profile: str | None,
    transaction_count: int | None,
    visible_transaction_count: int | None,
) -> dict[str, Any]:
    if not _table_exists(conn, "transactions_visible"):
        return {
            "duplicate_group_count": None,
            "duplicate_transaction_count": None,
            "missing_key_field_count": None,
            "hidden_or_filtered_delta": None,
            "sample_duplicate_groups": [],
            "sample_missing_key_transactions": [],
            "caveats": ["transactions_visible is unavailable; import anomaly checks were skipped."],
        }

    profile_clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        profile_clause = " AND profile_id = ?"
        params.append(profile)

    duplicate_group_count = 0
    duplicate_transaction_count = 0
    sample_duplicate_groups: list[dict[str, Any]] = []
    try:
        duplicate_summary = conn.execute(
            f"""
            SELECT COUNT(*) AS duplicate_group_count,
                   COALESCE(SUM(duplicate_count), 0) AS duplicate_transaction_count
              FROM (
                    SELECT COUNT(*) AS duplicate_count
                      FROM transactions_visible
                     WHERE 1=1{profile_clause}
                     GROUP BY profile_id,
                              date,
                              COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''), NULLIF(description, ''), NULLIF(raw_description, ''), ''),
                              amount,
                              COALESCE(category, '')
                    HAVING COUNT(*) > 1
              ) duplicate_groups
            """,
            params,
        ).fetchone()
        duplicate_group_count = int(duplicate_summary["duplicate_group_count"] or 0)
        duplicate_transaction_count = int(duplicate_summary["duplicate_transaction_count"] or 0)
        duplicate_rows = conn.execute(
            f"""
            SELECT profile_id,
                   date,
                   COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''), NULLIF(description, ''), NULLIF(raw_description, ''), '') AS identity_key,
                   amount,
                   COALESCE(category, '') AS category,
                   COUNT(*) AS duplicate_count,
                   GROUP_CONCAT(id) AS transaction_ids
              FROM transactions_visible
             WHERE 1=1{profile_clause}
             GROUP BY profile_id, date, identity_key, amount, category
            HAVING COUNT(*) > 1
             ORDER BY duplicate_count DESC, date DESC
             LIMIT 10
            """,
            params,
        ).fetchall()
        sample_duplicate_groups = [dict(row) for row in duplicate_rows]
    except Exception:
        sample_duplicate_groups = []

    missing_key_field_count = 0
    sample_missing: list[dict[str, Any]] = []
    try:
        missing_rows = conn.execute(
            f"""
            SELECT id, profile_id, date, description, raw_description, amount, category, merchant_name, merchant_key
              FROM transactions_visible
             WHERE (
                    date IS NULL OR date = ''
                 OR amount IS NULL
                 OR (COALESCE(description, '') = '' AND COALESCE(raw_description, '') = '' AND COALESCE(merchant_name, '') = '' AND COALESCE(merchant_key, '') = '')
                 OR category IS NULL OR category = ''
             ){profile_clause}
             ORDER BY date DESC
             LIMIT 10
            """,
            params,
        ).fetchall()
        sample_missing = [dict(row) for row in missing_rows]
        missing_key_field_count = int(conn.execute(
            f"""
            SELECT COUNT(*)
              FROM transactions_visible
             WHERE (
                    date IS NULL OR date = ''
                 OR amount IS NULL
                 OR (COALESCE(description, '') = '' AND COALESCE(raw_description, '') = '' AND COALESCE(merchant_name, '') = '' AND COALESCE(merchant_key, '') = '')
                 OR category IS NULL OR category = ''
             ){profile_clause}
            """,
            params,
        ).fetchone()[0] or 0)
    except Exception:
        sample_missing = []

    hidden_delta = None
    if transaction_count is not None and visible_transaction_count is not None:
        hidden_delta = max(0, int(transaction_count or 0) - int(visible_transaction_count or 0))

    caveats: list[str] = []
    if duplicate_group_count:
        caveats.append(f"{duplicate_group_count} likely duplicate transaction group(s) were detected.")
    if missing_key_field_count:
        caveats.append(f"{missing_key_field_count} visible transaction(s) are missing key fields.")
    if hidden_delta:
        caveats.append(f"{hidden_delta} transaction(s) are hidden or filtered out of transactions_visible.")

    return {
        "duplicate_group_count": duplicate_group_count,
        "duplicate_transaction_count": duplicate_transaction_count,
        "missing_key_field_count": missing_key_field_count,
        "hidden_or_filtered_delta": hidden_delta,
        "sample_duplicate_groups": sample_duplicate_groups[:5],
        "sample_missing_key_transactions": sample_missing[:5],
        "caveats": caveats,
    }


def _t_get_data_health_summary(args: dict, profile: str | None, conn) -> Any:
    caveats: list[str] = []
    try:
        check_row = conn.execute("PRAGMA quick_check(1)").fetchone()
        integrity_status = str(check_row[0] if check_row else "unknown")
    except Exception as exc:
        integrity_status = f"unavailable: {exc}"
        caveats.append("DB quick_check was unavailable on this connection.")
    if integrity_status.lower() != "ok":
        caveats.append(f"DB quick_check reported: {integrity_status}")

    transaction_count = _safe_count(conn, "transactions", profile)
    visible_transaction_count = _safe_count(conn, "transactions_visible", profile)
    latest_transaction_date = _safe_max(conn, "transactions_visible", "date", profile)
    latest_transaction_update = _safe_max(conn, "transactions_visible", "updated_at", profile)
    latest_days = _days_old(latest_transaction_date)
    if transaction_count is None:
        caveats.append("Transactions table is not available.")
    if visible_transaction_count in (None, 0):
        caveats.append("No visible transactions were available for this scope.")
    if latest_days is not None and latest_days > 14:
        caveats.append(f"Latest visible transaction is {latest_days} day(s) old; sync may be stale.")

    enrichment_total = _safe_count(conn, "transaction_enrichment", profile)
    coverage_ratio = None
    if visible_transaction_count and enrichment_total is not None:
        coverage_ratio = round(enrichment_total / visible_transaction_count, 4)
        if coverage_ratio < 0.8:
            caveats.append("Persisted transaction enrichment coverage is below 80%.")
    elif enrichment_total is None:
        caveats.append("Transaction enrichment table is missing; enrichment coverage is unavailable.")

    budget_count = _safe_count(conn, "category_budgets", profile)
    if budget_count in (None, 0):
        caveats.append("No category budgets are configured for this profile scope.")

    recurring_count = _safe_count(conn, "recurring_obligations", profile)
    if recurring_count in (None, 0):
        caveats.append("No recurring obligation data is configured or detected for this profile scope.")

    anomaly_summary = _transaction_anomaly_summary(
        conn,
        profile=profile,
        transaction_count=transaction_count,
        visible_transaction_count=visible_transaction_count,
    )
    caveats.extend(anomaly_summary.get("caveats") or [])

    try:
        import copilot_cache

        cache_stats = copilot_cache.stats()
    except Exception:
        cache_stats = {"available": False}

    result = {
        "profile_scope": profile or "household",
        "db_integrity": {"check": "quick_check", "status": integrity_status},
        "transaction_count": transaction_count or 0,
        "visible_transaction_count": visible_transaction_count or 0,
        "sync_freshness": {
            "latest_transaction_date": latest_transaction_date,
            "latest_transaction_updated_at": latest_transaction_update,
            "latest_transaction_days_old": latest_days,
        },
        "enrichment_coverage": {
            "persisted_enrichment_count": enrichment_total or 0,
            "coverage_ratio": coverage_ratio,
        },
        "cache_freshness": cache_stats,
        "import_anomalies": anomaly_summary,
        "anomaly_summary": anomaly_summary,
        "known_caveats": caveats,
        "summary": "Data health looks usable." if not caveats else "Data health has caveats that can limit Mira answers.",
    }
    return _add_contract(
        result,
        tool="get_data_health_summary",
        args=args,
        label="current",
        row_count=int(visible_transaction_count or 0),
        filters={"profile": profile or "household"},
        caveats=caveats,
    )


def _t_run_sql(args: dict, profile: str | None, conn) -> Any:
    """Internal read-only SQL escape hatch. Not exposed to normal Mira routing."""
    from copilot import _validate_read_semantics, _validate_read_sql, _rewrite_transaction_read_sources

    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query required"}
    question = (args.get("question") or args.get("_question") or "").strip()
    if not question:
        return {"error": "SQL rejected: internal SQL requires the original user question for semantic validation."}

    rewritten = _rewrite_transaction_read_sources(query)
    ok, err = _validate_read_sql(rewritten)
    if not ok:
        return {"error": f"SQL rejected: {err}"}
    ok, err = _validate_read_semantics(question, rewritten)
    if not ok:
        return {"error": f"SQL rejected: {err}"}

    try:
        rows = conn.execute(rewritten).fetchall()
    except Exception as e:
        return {"error": f"SQL execution error: {e}"}

    data = [dict(r) for r in rows[:200]]
    return {"row_count": len(rows), "rows": data, "truncated": len(rows) > 200}


def _t_review_financial_context(args: dict, profile: str | None, conn) -> Any:
    """Read-only access to Mira's compact financial understanding facts."""

    from mira.financial_understanding import list_financial_facts
    from mira.financial_feedback import feedback_adjustments_for_subjects, financial_feedback_loop_enabled

    view = str(args.get("view") or "relevant").strip().lower()
    if view not in {"relevant", "lifestyle_profile", "friction_map", "operating_plan", "monthly_retrospective", "forecast_month_outlook.snapshot", "review_cashflow.safe_to_spend"}:
        view = "relevant"
    if view == "forecast_month_outlook.snapshot":
        return _financial_outlook_context(args, profile, conn)
    if view == "review_cashflow.safe_to_spend":
        return _financial_safe_to_spend_context(args, profile, conn)
    if view == "monthly_retrospective":
        return _financial_monthly_retrospective_context(args, profile, conn)
    try:
        max_facts = max(1, min(int(args.get("max_facts") or 5), 8))
    except (TypeError, ValueError):
        max_facts = 5
    subject_type = str(args.get("subject_type") or "").strip().lower()
    subject_key = _financial_context_key(args.get("subject_key")) if args.get("subject_key") else ""

    rows = list_financial_facts(conn=conn, profile=profile, limit=max(max_facts * 4, 20))
    feedback_adjustments: dict[tuple[str, str], dict[str, Any]] = {}
    if financial_feedback_loop_enabled() and rows:
        subjects = [
            (str(row.get("subject_type") or ""), str(row.get("subject_key") or ""))
            for row in rows
            if row.get("subject_type") and row.get("subject_key")
        ]
        feedback_adjustments = feedback_adjustments_for_subjects(conn=conn, profile=profile, subjects=subjects)
        if feedback_adjustments:
            rows = sorted(rows, key=lambda row: (_financial_feedback_priority(row, feedback_adjustments), str(row.get("generated_at") or ""), int(row.get("id") or 0)), reverse=True)
    facts: list[dict[str, Any]] = []
    suppressed = 0
    feedback_suppressed = 0
    feedback_reframed = 0
    direct_subject = bool(subject_type and subject_key)
    for row in rows:
        family = str(row.get("fact_family") or "").strip()
        row_subject_type = str(row.get("subject_type") or "").strip().lower()
        row_subject_key = str(row.get("subject_key") or "").strip().lower()
        if view != "relevant" and family != view:
            continue
        if subject_type and row_subject_type != subject_type:
            continue
        if subject_key and row_subject_key != subject_key:
            continue
        if str(row.get("sensitivity") or "low").strip().lower() == "high":
            if not (subject_type and subject_key and row_subject_type == subject_type and row_subject_key == subject_key):
                suppressed += 1
                continue
        adjustment = feedback_adjustments.get((row_subject_type, row_subject_key)) if feedback_adjustments else None
        if adjustment and (adjustment.get("suppress") or adjustment.get("too_sensitive")) and not direct_subject:
            feedback_suppressed += 1
            continue
        if adjustment:
            row = {**row, "_feedback_adjustment": adjustment}
            if adjustment.get("reframe"):
                feedback_reframed += 1
        facts.append(_financial_context_fact(row))
        if len(facts) >= max_facts:
            break
    stated_intent_fact = None
    if direct_subject:
        try:
            from mira.stated_intents import stated_intent_context_for_subject

            stated_intent_fact = stated_intent_context_for_subject(
                conn=conn,
                profile=profile,
                subject_type=subject_type,
                subject_key=subject_key,
            )
        except Exception:
            stated_intent_fact = None
    if stated_intent_fact:
        facts = [stated_intent_fact, *facts[: max(0, max_facts - 1)]]
    habit_streak_fact = None
    if direct_subject:
        try:
            from mira.habit_streaks import habit_streak_context_for_subject

            habit_streak_fact = habit_streak_context_for_subject(
                conn=conn,
                profile=profile,
                subject_type=subject_type,
                subject_key=subject_key,
            )
        except Exception:
            habit_streak_fact = None
    if habit_streak_fact:
        prefix = [stated_intent_fact] if stated_intent_fact else []
        start = 1 if stated_intent_fact else 0
        facts = [*prefix, habit_streak_fact, *facts[start : max(0, max_facts - 1)]][:max_facts]

    caveats = []
    if not rows:
        caveats.append("No active financial-understanding facts are available yet.")
    if suppressed:
        caveats.append(f"{suppressed} high-sensitivity context fact(s) were omitted.")
    if feedback_suppressed:
        caveats.append(f"{feedback_suppressed} feedback-suppressed context fact(s) were omitted.")
    if feedback_reframed:
        caveats.append(f"{feedback_reframed} context fact(s) include user correction framing.")
    generated = [str(row.get("generated_at") or "") for row in rows if row.get("generated_at")]
    result = {
        "status": "ok" if facts else "empty",
        "context_kind": "financial_understanding",
        "view": view,
        "summary": _financial_context_summary(facts, view),
        "facts": facts,
        "caveats": caveats,
        "provenance": {
            "fact_ids": [fact.get("id") for fact in facts if fact.get("id") not in (None, "")],
            "generated_at": max(generated) if generated else "",
        },
    }
    return _add_contract(
        result,
        tool="review_financial_context",
        args=args,
        label=view,
        row_count=len(facts),
        filters={"subject_type": subject_type, "subject_key": subject_key},
        calculation_basis="Read from Mira's compact financial-understanding facts; does not compute scalar totals.",
        caveats=caveats,
    )


def _financial_outlook_context(args: dict, profile: str | None, conn) -> Any:
    from mira.money_outlook import load_latest_money_outlook_snapshot, money_outlook_enabled

    view = "forecast_month_outlook.snapshot"
    caveats: list[str] = []
    if not money_outlook_enabled():
        caveats.append("Money Outlook is disabled.")
        return _add_contract(
            {
                "status": "empty",
                "context_kind": "money_outlook",
                "view": view,
                "summary": "No cached money outlook is available.",
                "facts": [],
                "caveats": caveats,
                "provenance": {},
            },
            tool="review_financial_context",
            args=args,
            label=view,
            row_count=0,
            calculation_basis="Cached deterministic money outlook snapshot; no live projection was computed.",
            caveats=caveats,
        )

    snapshot = load_latest_money_outlook_snapshot(conn, profile=profile, as_of=args.get("as_of"))
    if not snapshot:
        caveats.append("No fresh cached money outlook is available yet.")
        return _add_contract(
            {
                "status": "empty",
                "context_kind": "money_outlook",
                "view": view,
                "summary": "No fresh cached money outlook is available yet.",
                "facts": [],
                "caveats": caveats,
                "provenance": {},
            },
            tool="review_financial_context",
            args=args,
            label=view,
            row_count=0,
            calculation_basis="Cached deterministic money outlook snapshot; no live projection was computed.",
            caveats=caveats,
        )

    intent = str(args.get("intent") or "").strip().lower()
    amount = _fmt_money(args.get("amount"))
    savings_delta = _fmt_money(snapshot.get("projected_savings_delta"))
    if intent == "affordability" and amount > 0:
        remaining_after_purchase = _fmt_money(savings_delta - amount)
        threshold = max(100.0, round(amount * 0.25, 2))
        if remaining_after_purchase > threshold:
            caveats.append("Money Outlook skipped because this purchase is not near the monthly savings threshold.")
            return _add_contract(
                {
                    "status": "empty",
                    "context_kind": "money_outlook",
                    "view": view,
                    "summary": "Cached money outlook was not needed for this affordability answer.",
                    "facts": [],
                    "caveats": caveats,
                    "provenance": {"snapshot_id": snapshot.get("id"), "generated_at": snapshot.get("generated_at")},
                },
                tool="review_financial_context",
                args=args,
                label=view,
                row_count=0,
                calculation_basis="Cached deterministic money outlook snapshot; skipped because projected room was not near threshold.",
                caveats=caveats,
            )
    else:
        remaining_after_purchase = None

    summary = _money_outlook_context_summary(snapshot)
    outlook = {
        "month_key": snapshot.get("month_key"),
        "projected_end_balance": snapshot.get("projected_end_balance"),
        "projected_income_remaining": snapshot.get("projected_income_remaining"),
        "projected_obligations_remaining": snapshot.get("projected_obligations_remaining"),
        "projected_flexible_spend_remaining": snapshot.get("projected_flexible_spend_remaining"),
        "projected_savings_delta": snapshot.get("projected_savings_delta"),
        "target_savings_amount": snapshot.get("target_savings_amount"),
        "confidence": snapshot.get("confidence"),
        "drivers": (snapshot.get("drivers") or [])[:5],
        "caveats": (snapshot.get("caveats") or [])[:5],
        "generated_at": snapshot.get("generated_at"),
        "valid_until": snapshot.get("valid_until"),
        "safe_to_spend": {
            "safe_to_spend_today": snapshot.get("safe_to_spend_today"),
            "safe_to_spend_this_week": snapshot.get("safe_to_spend_this_week"),
            "buffer_status": snapshot.get("buffer_status"),
            "next_pressure_date": snapshot.get("low_point_date"),
            "top_caveat": snapshot.get("safe_to_spend_top_caveat"),
        },
        "low_point": {
            "low_point_date": snapshot.get("low_point_date"),
            "low_point_amount": snapshot.get("low_point_amount"),
            "buffer_amount": snapshot.get("buffer_amount"),
            "buffer_status": snapshot.get("buffer_status"),
            "buffer_breach": bool(snapshot.get("buffer_breach")),
            "drivers": (snapshot.get("low_point_drivers") or [])[:5],
        },
    }
    if remaining_after_purchase is not None:
        outlook["affordability_amount"] = amount
        outlook["projected_savings_delta_after_purchase"] = remaining_after_purchase
    facts = [
        {
            "family": "money_outlook",
            "kind": "forecast_month_outlook",
            "summary": summary,
            "numbers": {
                "projected_savings_delta": snapshot.get("projected_savings_delta"),
                "projected_end_balance": snapshot.get("projected_end_balance"),
                "target_savings_amount": snapshot.get("target_savings_amount"),
                "projected_income_remaining": snapshot.get("projected_income_remaining"),
                "projected_obligations_remaining": snapshot.get("projected_obligations_remaining"),
                "projected_flexible_spend_remaining": snapshot.get("projected_flexible_spend_remaining"),
                "safe_to_spend_today": snapshot.get("safe_to_spend_today"),
                "safe_to_spend_this_week": snapshot.get("safe_to_spend_this_week"),
                "low_point_amount": snapshot.get("low_point_amount"),
                "buffer_amount": snapshot.get("buffer_amount"),
                **({"projected_savings_delta_after_purchase": remaining_after_purchase} if remaining_after_purchase is not None else {}),
            },
            "drivers": outlook["drivers"],
            "confidence": snapshot.get("confidence"),
        }
    ]
    return _add_contract(
        {
            "status": "ok",
            "context_kind": "money_outlook",
            "view": view,
            "summary": summary,
            "outlook": outlook,
            "facts": facts,
            "caveats": outlook["caveats"],
            "provenance": {
                "snapshot_id": snapshot.get("id"),
                "generated_at": snapshot.get("generated_at"),
                "valid_until": snapshot.get("valid_until"),
                "fingerprint": snapshot.get("fingerprint"),
            },
        },
        tool="review_financial_context",
        args=args,
        label=view,
        row_count=1,
        calculation_basis="Cached deterministic money outlook snapshot; does not compute new finance totals on the chat path.",
        caveats=outlook["caveats"],
    )


def _financial_safe_to_spend_context(args: dict, profile: str | None, conn) -> Any:
    from mira.money_outlook import load_latest_money_outlook_snapshot, money_outlook_enabled, safe_to_spend_enabled

    view = "review_cashflow.safe_to_spend"
    caveats: list[str] = []
    if not money_outlook_enabled() or not safe_to_spend_enabled():
        caveats.append("Safe-to-spend is disabled.")
        return _add_contract(
            {
                "status": "empty",
                "context_kind": "safe_to_spend",
                "view": view,
                "summary": "No cached safe-to-spend result is available.",
                "facts": [],
                "caveats": caveats,
                "provenance": {},
            },
            tool="review_financial_context",
            args=args,
            label=view,
            row_count=0,
            calculation_basis="Cached deterministic Money Outlook snapshot; no live finance math was computed in chat.",
            caveats=caveats,
        )

    snapshot = load_latest_money_outlook_snapshot(conn, profile=profile, as_of=args.get("as_of"))
    if not snapshot:
        caveats.append("No fresh cached money outlook is available yet.")
        return _add_contract(
            {
                "status": "empty",
                "context_kind": "safe_to_spend",
                "view": view,
                "summary": "No fresh cached safe-to-spend result is available yet.",
                "facts": [],
                "caveats": caveats,
                "provenance": {},
            },
            tool="review_financial_context",
            args=args,
            label=view,
            row_count=0,
            calculation_basis="Cached deterministic Money Outlook snapshot; no live finance math was computed in chat.",
            caveats=caveats,
        )

    safe_today = snapshot.get("safe_to_spend_today")
    safe_week = snapshot.get("safe_to_spend_this_week")
    buffer_status = str(snapshot.get("buffer_status") or "unknown")
    pressure_date = snapshot.get("low_point_date")
    top_caveat = str(snapshot.get("safe_to_spend_top_caveat") or "")
    if top_caveat:
        caveats.append(top_caveat)
    summary = (
        f"Safe-to-spend today is ${_fmt_money(safe_today):,.2f}; "
        f"this week is ${_fmt_money(safe_week):,.2f}. Buffer status: {buffer_status}."
    )
    facts = [
        {
            "family": "safe_to_spend",
            "kind": "review_cashflow.safe_to_spend",
            "summary": summary,
            "numbers": {
                "safe_to_spend_today": safe_today,
                "safe_to_spend_this_week": safe_week,
                "low_point_amount": snapshot.get("low_point_amount"),
                "buffer_amount": snapshot.get("buffer_amount"),
            },
            "dates": {"next_pressure_date": pressure_date},
            "confidence": snapshot.get("confidence"),
            "buffer_status": buffer_status,
        }
    ]
    return _add_contract(
        {
            "status": "ok",
            "context_kind": "safe_to_spend",
            "view": view,
            "summary": summary,
            "safe_to_spend": {
                "safe_to_spend_today": safe_today,
                "safe_to_spend_this_week": safe_week,
                "buffer_status": buffer_status,
                "next_pressure_date": pressure_date,
                "top_caveat": top_caveat,
            },
            "facts": facts,
            "caveats": caveats[:3],
            "provenance": {
                "snapshot_id": snapshot.get("id"),
                "generated_at": snapshot.get("generated_at"),
                "valid_until": snapshot.get("valid_until"),
                "fingerprint": snapshot.get("fingerprint"),
            },
        },
        tool="review_financial_context",
        args=args,
        label=view,
        row_count=1,
        calculation_basis="Cached deterministic Money Outlook snapshot; the LLM may phrase but not compute safe-to-spend.",
        caveats=caveats[:3],
    )


def _financial_monthly_retrospective_context(args: dict, profile: str | None, conn) -> Any:
    from mira.monthly_retrospectives import monthly_retrospective_context, monthly_retrospective_enabled

    view = "monthly_retrospective"
    caveats: list[str] = []
    if not monthly_retrospective_enabled():
        caveats.append("Monthly retrospective is disabled.")
        return _add_contract(
            {
                "status": "empty",
                "context_kind": "monthly_retrospective",
                "view": view,
                "summary": "No monthly retrospective is available.",
                "facts": [],
                "caveats": caveats,
                "provenance": {},
            },
            tool="review_financial_context",
            args=args,
            label=view,
            row_count=0,
            calculation_basis="Cached deterministic monthly retrospective; no live totals were computed.",
            caveats=caveats,
        )
    month_key = str(args.get("month_key") or args.get("range") or "").strip()[:7] or None
    fact = monthly_retrospective_context(conn=conn, profile=profile, month_key=month_key)
    facts = [fact] if fact else []
    if not facts:
        caveats.append("No stored monthly retrospective matched this scope.")
    return _add_contract(
        {
            "status": "ok" if facts else "empty",
            "context_kind": "monthly_retrospective",
            "view": view,
            "summary": fact.get("summary") if fact else "No monthly retrospective matched.",
            "facts": facts,
            "caveats": caveats,
            "provenance": {
                "fact_ids": [fact.get("id")] if fact and fact.get("id") is not None else [],
                "month_key": fact.get("time_scope") if fact else "",
            },
        },
        tool="review_financial_context",
        args=args,
        label=view,
        row_count=len(facts),
        calculation_basis="Cached deterministic monthly retrospective; the LLM may phrase but not recalculate.",
        caveats=caveats,
    )


def _money_outlook_context_summary(snapshot: dict[str, Any]) -> str:
    month_key = str(snapshot.get("month_key") or "this month")
    delta = _fmt_money(snapshot.get("projected_savings_delta"))
    target = _fmt_money(snapshot.get("target_savings_amount"))
    confidence = str(snapshot.get("confidence") or "medium")
    direction = "above" if delta >= 0 else "below"
    return (
        f"Cached outlook for {month_key}: projected savings delta is "
        f"${abs(delta):,.2f} {direction} the ${target:,.2f} target; confidence is {confidence}."
    )


def _financial_context_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "profile"


def _financial_feedback_priority(row: dict[str, Any], adjustments: dict[tuple[str, str], dict[str, Any]]) -> int:
    key = (str(row.get("subject_type") or "").strip().lower(), str(row.get("subject_key") or "").strip().lower())
    adjustment = adjustments.get(key) or {}
    if adjustment.get("uprank"):
        return 2
    if adjustment.get("downrank") or adjustment.get("suppress") or adjustment.get("too_sensitive"):
        return 0
    return 1


def _financial_context_fact(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    result = {
        "id": row.get("id"),
        "family": row.get("fact_family"),
        "kind": row.get("kind"),
        "subject_type": row.get("subject_type"),
        "subject_key": row.get("subject_key"),
        "summary": row.get("summary"),
        "numbers": row.get("numbers") if isinstance(row.get("numbers"), dict) else {},
        "traits": row.get("traits") if isinstance(row.get("traits"), list) else [],
        "confidence": row.get("confidence"),
        "sensitivity": row.get("sensitivity"),
        "valid_until": row.get("valid_until"),
        "time_scope": evidence.get("time_scope"),
        "improvement_theme": evidence.get("improvement_theme"),
    }
    adjustment = row.get("_feedback_adjustment") if isinstance(row.get("_feedback_adjustment"), dict) else {}
    if adjustment:
        result["feedback"] = {
            "effects": adjustment.get("effects") or {},
            "feedback_types": adjustment.get("feedback_types") or {},
            "safe_summaries": adjustment.get("safe_summaries") or [],
        }
    return result


def _financial_context_summary(facts: list[dict[str, Any]], view: str) -> str:
    if not facts:
        return "No compact financial context matched."
    label = {
        "lifestyle_profile": "lifestyle",
        "friction_map": "friction",
        "operating_plan": "operating-plan",
        "monthly_retrospective": "monthly retrospective",
    }.get(view, "financial context")
    return f"{len(facts)} {label} fact(s) available."


# ──────────────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────────────

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_month_summary": {
        "fn": _t_get_month_summary,
        "description": "Income/expense/net/savings for a month, with prior-month comparison. month='current' | 'prior' | 'YYYY-MM'.",
        "parameters": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "current, prior, or YYYY-MM"},
                "metric": {"type": "string", "enum": ["summary", "income", "expenses"], "description": "Optional component to emphasize in the answer."},
            },
        },
    },
    "get_period_summary": {
        "fn": _t_get_period_summary,
        "description": "Range-aware finance summary for income, expenses, savings transfers, credit card payments, refunds, and other cashflow components. Use this for period income totals and non-spending cashflow totals; it is not a category spend tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "metric": {
                    "type": "string",
                    "enum": [
                        "summary",
                        "income",
                        "expenses",
                        "refunds",
                        "savings",
                        "credit_card_payments",
                        "personal_transfers",
                        "cash_deposits",
                        "cash_withdrawals",
                        "investment_transfers",
                    ],
                    "description": "Which period component to emphasize.",
                },
            },
        },
    },
    "get_top_categories": {
        "fn": _t_get_top_categories,
        "description": "Top spending categories for a time range, sorted by amount. Use this when the user asks about the biggest/top categories.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    "get_top_merchants": {
        "fn": _t_get_top_merchants,
        "description": "Top merchants by spend for a time range. Use this for the biggest/top merchant questions.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    "get_finance_priorities": {
        "fn": _t_get_finance_priorities,
        "description": "Ranked current-month finance priorities for 'what should I watch' or 'what should I fix first' questions. Combines scoped top categories, active recurring charges, and budget-plan state.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "focus": {"type": "string", "enum": ["watch", "fix"], "description": "watch for monitoring guidance, fix for prioritized action guidance"},
                "limit": {"type": "integer", "default": 3},
            },
        },
    },
    "get_category_spend": {
        "fn": _t_get_category_spend,
        "description": "Exact spending total and recent transactions for one SPECIFIC spending category by name (e.g. 'Groceries', 'Food & Dining') over a time range. Do not use for Income, Savings Transfer, Personal Transfer, Credit Card Payment, Credits & Refunds, Cash Withdrawal/Deposit, Investment Transfer, or any category configured as non_expense; use summary or transaction tools instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Spending category name, case-insensitive exact match"},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
            "required": ["category"],
        },
    },
    "get_merchant_spend": {
        "fn": _t_get_merchant_spend,
        "description": "Exact total and recent transactions for a SPECIFIC merchant by name fragment (e.g. 'Costco', 'BILT', 'Amazon') over a time range. Matches substring in merchant name or description.",
        "parameters": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string", "description": "Merchant name or fragment, substring match"},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
            "required": ["merchant"],
        },
    },
    "get_recurring_summary": {
        "fn": _t_get_recurring_summary,
        "description": "Active subscriptions/recurring charges with totals. Optional status: 'active'|'inactive'|'cancelled'.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "all": {"type": "boolean", "description": "When true, return every matching recurring item without applying the default limit."},
                "limit": {"type": "integer", "default": 25},
            },
        },
    },
    "get_net_worth_trend": {
        "fn": _t_get_net_worth_trend,
        "description": "Net worth series over time. interval='monthly' (default) or 'weekly'. Optional range filter.",
        "parameters": {
            "type": "object",
            "properties": {
                "interval": {"type": "string", "enum": ["monthly", "weekly"]},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "limit": {"type": "integer", "default": 24},
            },
        },
    },
    "get_monthly_spending_trend": {
        "fn": _t_get_monthly_spending_trend,
        "description": "Monthly spending trend for line charts. Returns labels and values for total spending or one category over the last N calendar months including the current month.",
        "parameters": {
            "type": "object",
            "properties": {
                "months": {"type": "integer", "default": 6, "description": "Number of calendar months to include, 1-36."},
                "category": {"type": "string", "description": "Optional exact category name, e.g. 'Groceries'."},
            },
        },
    },
    "get_transactions_for_merchant": {
        "fn": _t_get_transactions_for_merchant,
        "description": "Recent transactions for a specific merchant (deep drill-down). Honors range/month, profile, limit, and offset.",
        "parameters": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string"},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "month": {"type": "string", "description": "Optional YYYY-MM month filter."},
                "limit": {"type": "integer", "default": 25},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["merchant"],
        },
    },
    "get_summary": {
        "fn": _t_get_summary,
        "description": "Overall dashboard financial snapshot: income, expenses, net flow, counts, net worth, and savings in the dashboard's native scope.",
        "parameters": {"type": "object", "properties": {}},
    },
    "get_account_balances": {
        "fn": _t_get_account_balances,
        "description": "Current balances across all connected bank accounts (checking, savings, credit, loan, investment). Use this for 'what's my balance' or 'how much cash do I have' questions.",
        "parameters": {"type": "object", "properties": {}},
    },
    "get_transactions": {
        "fn": _t_get_transactions,
        "description": "Search / list transactions with filters — same source as the Transactions page, newest transactions first. Use limit=1 for latest/last/most recent transaction questions.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "category": {"type": "string"},
                "merchant": {"type": "string", "description": "Merchant name or fragment; treated as transaction search text"},
                "account": {"type": "string"},
                "search": {"type": "string", "description": "Substring match on description or merchant name"},
                "limit": {"type": "integer", "default": 25},
                "offset": {"type": "integer", "default": 0},
            },
        },
    },
    "get_category_breakdown": {
        "fn": _t_get_category_breakdown,
        "description": "Aggregate expense total plus per-spending-category breakdown for a time range — the dashboard category analytics source. Includes gross, refunds, net, and percent-of-total per spending category; non-expense categories are excluded from spend.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
        },
    },
    "get_dashboard_bundle": {
        "fn": _t_get_dashboard_bundle,
        "description": "Complete dashboard snapshot (summary + accounts + monthly analytics + category breakdown + net worth series) in one call. Use when you need broad context.",
        "parameters": {"type": "object", "properties": {}},
    },
    "get_dashboard_snapshot": {
        "fn": _t_get_dashboard_snapshot,
        "description": "Semantic dashboard snapshot with compact sections and provenance. Use for broad dashboard-equivalent questions.",
        "parameters": {"type": "object", "properties": {}},
    },
    "analyze_subject": {
        "fn": _t_analyze_subject,
        "description": "Semantic analysis for one grounded merchant or category: total, count, recent rows, trend/budget context where available, and provenance.",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_type": {"type": "string", "enum": ["merchant", "category"]},
                "subject": {"type": "string"},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
            "required": ["subject_type", "subject"],
        },
    },
    "compare_periods": {
        "fn": _t_compare_periods,
        "description": "Semantic period comparison for one grounded merchant or category with deterministic totals, delta, row counts, samples, and provenance.",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_type": {"type": "string", "enum": ["merchant", "category"]},
                "subject": {"type": "string"},
                "range_a": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "range_b": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
            "required": ["subject_type", "subject"],
        },
    },
    "get_budget_status": {
        "fn": _t_get_budget_status,
        "description": "Semantic category budget status: budget, actual spend, remaining amount, recent rows, and provenance.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
            "required": ["category"],
        },
    },
    "get_budget_plan_summary": {
        "fn": _t_get_budget_plan_summary,
        "description": "Budget plan summary from Folio budget settings and current-month planning data. Use for broad budget health, safe-to-spend, and 'can I spend more this week?' questions when no specific purchase amount is given.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
        },
    },
    "get_savings_capacity": {
        "fn": _t_get_savings_capacity,
        "description": "Conservative monthly savings capacity contract using the Folio plan snapshot, income history, budgets, recurring obligations, goals, cash-flow caveats, and provenance. Use for 'how much should I save monthly?' questions.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
            },
        },
    },
    "find_transactions": {
        "fn": _t_find_transactions,
        "description": "Semantic transaction finder with merchant/category/range/account/search filters, normalized rows, count, samples, and provenance. Use for timing/details of income, transfers, payments, refunds, and ordinary spending transactions.",
        "parameters": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string"},
                "category": {"type": "string"},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "account": {"type": "string"},
                "search": {"type": "string"},
                "limit": {"type": "integer", "default": 25},
            },
        },
    },
    "explain_metric": {
        "fn": _t_explain_metric,
        "description": "Semantic explanation for dashboard metrics such as spending, net worth, budget, or recurring changes with contributing components and provenance.",
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {"type": "string"},
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    "get_recurring_changes": {
        "fn": _t_get_recurring_changes,
        "description": "Semantic recurring/subscription status summary with items, totals, and provenance.",
        "parameters": {
            "type": "object",
            "properties": {
                "range": {"type": "string", "description": _RANGE_DESC, "enum": _RANGE_ENUM},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    "get_cashflow_forecast": {
        "fn": _t_get_cashflow_forecast,
        "description": "Deterministic cash-flow forecast through the next paycheck or selected horizon, including starting cash, expected income, upcoming recurring obligations, discretionary spend estimate, confidence, caveats, and provenance.",
        "parameters": {
            "type": "object",
            "properties": {
                "horizon_days": {"type": "integer", "description": "Optional forecast horizon in days, 1-90."},
                "buffer_amount": {"type": "number", "description": "Optional cash buffer threshold."},
            },
        },
    },
    "predict_shortfall": {
        "fn": _t_predict_shortfall,
        "description": "Deterministic shortfall predictor over the cash-flow forecast. Warns when projected cash crosses zero or a buffer and suppresses weak low-confidence buffer warnings.",
        "parameters": {
            "type": "object",
            "properties": {
                "horizon_days": {"type": "integer", "description": "Optional forecast horizon in days, 1-90."},
                "buffer_amount": {"type": "number", "description": "Optional cash buffer threshold."},
            },
        },
    },
    "check_affordability": {
        "fn": _t_check_affordability,
        "description": "Deterministic affordability check for a proposed purchase. Uses cash-flow forecast, upcoming obligations, budget/category pace, Memory V2 goal/constraint context where allowed, buffer, confidence, caveats, and provenance.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "purpose": {"type": "string"},
                "category": {"type": "string"},
                "horizon_days": {"type": "integer"},
                "buffer_amount": {"type": "number"},
                "question": {"type": "string"},
            },
            "required": ["amount"],
        },
    },
    "find_low_confidence_transactions": {
        "fn": _t_find_low_confidence_transactions,
        "description": "Read-only Transaction Intelligence tool: find transactions with missing or low-confidence semantic enrichment, including confidence fields and evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "default": 0.7},
                "limit": {"type": "integer", "default": 25},
            },
        },
    },
    "explain_transaction_enrichment": {
        "fn": _t_explain_transaction_enrichment,
        "description": "Read-only Transaction Intelligence tool: explain the semantic enrichment for one transaction_id, including categories, counterparty, confidence, evidence, and corrections.",
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string"},
            },
            "required": ["transaction_id"],
        },
    },
    "get_enrichment_quality_summary": {
        "fn": _t_get_enrichment_quality_summary,
        "description": "Read-only Transaction Intelligence tool: summarize persisted enrichment coverage, low-confidence counts, review counts, semantic distribution, and taxonomy version.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_taxonomy": {"type": "boolean", "default": False},
            },
        },
    },
    "get_data_health_summary": {
        "fn": _t_get_data_health_summary,
        "description": "Read-only Mira data-health summary: DB quick_check, transaction/visible counts, sync freshness, enrichment coverage, cache freshness, caveats, and profile scope.",
        "parameters": {"type": "object", "properties": {}},
    },
    "review_financial_context": {
        "fn": _t_review_financial_context,
        "description": "Read-only Mira financial-understanding facts for lifestyle, friction, operating-plan, monthly-retrospective, and cached outlook context. This does not compute scalar finance totals.",
        "parameters": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["relevant", "lifestyle_profile", "friction_map", "operating_plan", "monthly_retrospective"]},
                "subject_type": {"type": "string", "enum": ["profile", "category", "merchant", "subscription", "account", "cashflow"]},
                "subject_key": {"type": "string"},
                "question": {"type": "string"},
                "intent": {"type": "string"},
                "max_facts": {"type": "integer", "default": 5},
                "month_key": {"type": "string", "description": "Optional YYYY-MM closed month for monthly_retrospective."},
            },
        },
    },
    "get_net_worth_delta": {
        "fn": _t_get_net_worth_delta,
        "description": "Month-over-month net-worth change metrics — same numbers shown on the dashboard's net worth card.",
        "parameters": {"type": "object", "properties": {}},
    },
    "get_category_rules": {
        "fn": _t_get_category_rules,
        "description": "User/system categorization rules.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50}},
        },
    },
    "search_saved_insights": {
        "fn": _t_search_saved_insights,
        "description": "Search saved insights/decisions the user has pinned previously. Empty keywords lists recent.",
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    "remember_user_context": {
        "fn": _t_remember_user_context,
        "description": "Memory V2: save explicit durable user context such as preferences, goals, constraints, stressors, commitments, identity facts, rejected advice, coaching state, or tone preferences. Never use for transaction facts or live finance totals.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "memory_type": {"type": "string"},
                "topic": {"type": "string"},
                "source_summary": {"type": "string"},
                "source_turn_id": {"type": "string"},
                "pinned": {"type": "boolean", "default": False},
                "expires_at": {"type": ["string", "null"]},
            },
            "required": ["text"],
        },
    },
    "retrieve_relevant_memories": {
        "fn": _t_retrieve_relevant_memories,
        "description": "Memory V2: retrieve profile-scoped memories only when the retrieval gate allows memory for coaching, goals, budgeting guidance, or explicit memory inspection. Exact finance queries must return no memories.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
                "include_expired": {"type": "boolean", "default": False},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["question"],
        },
    },
    "update_memory": {
        "fn": _t_update_memory,
        "description": "Memory V2: update a specific memory by id, or update the best matching memory from replacement text.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "text": {"type": "string"},
                "normalized_text": {"type": "string"},
                "memory_type": {"type": "string"},
                "topic": {"type": "string"},
                "sensitivity": {"type": "string", "enum": ["low", "medium", "high"]},
                "confidence": {"type": "number"},
                "pinned": {"type": "boolean"},
                "expires_at": {"type": ["string", "null"]},
                "status": {"type": "string"},
            },
        },
    },
    "forget_memory": {
        "fn": _t_forget_memory,
        "description": "Memory V2: delete a memory by id or best match. This only affects Mira memory, not transactions or financial data.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "integer"},
                "topic": {"type": "string"},
                "text": {"type": "string"},
            },
        },
    },
    "list_mira_memories": {
        "fn": _t_list_mira_memories,
        "description": "Memory V2: list inspectable Mira memories for the active profile, optionally including inactive or expired rows.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_inactive": {"type": "boolean", "default": False},
                "include_expired": {"type": "boolean", "default": False},
                "memory_type": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    "preview_bulk_recategorize": {
        "fn": _t_preview_bulk_recategorize,
        "description": "Preview moving every transaction for a merchant to a new category. Returns a preview with a confirmation ID — the UI will ask the user to confirm before applying. Use when the user says 'recategorize all X as Y', 'move all X to Y', etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string", "description": "Merchant name (e.g. 'Beverages & More'). Substring match."},
                "category": {"type": "string", "description": "Target category name (e.g. 'Entertainment')."},
            },
            "required": ["merchant", "category"],
        },
    },
    "preview_create_rule": {
        "fn": _t_preview_create_rule,
        "description": "Preview creating a category rule: when a transaction description matches PATTERN, auto-assign CATEGORY. Applies to past AND future matches. Use when the user says 'create a rule for X', 'always categorize X as Y'.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Substring/regex pattern that will match transaction descriptions."},
                "category": {"type": "string", "description": "Target category name."},
            },
            "required": ["pattern", "category"],
        },
    },
    "preview_rename_merchant": {
        "fn": _t_preview_rename_merchant,
        "description": "Preview renaming a merchant (cleans up all transaction variants). Use when the user says 'rename X to Y' or 'clean up the merchant name for X'.",
        "parameters": {
            "type": "object",
            "properties": {
                "old_name": {"type": "string", "description": "Current merchant name / fragment (e.g. 'BILT PAYMENT DES:BILTRENT')."},
                "new_name": {"type": "string", "description": "Cleaned display name (e.g. 'Bilt Rent')."},
            },
            "required": ["old_name", "new_name"],
        },
    },
    "preview_set_budget": {
        "fn": _t_preview_set_budget,
        "description": "Preview setting or removing a category budget. Use when the user asks to set/update a monthly budget/cap for a category.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "amount": {"type": ["number", "null"], "description": "Budget amount. Null or 0 removes the budget."},
                "rollover_mode": {"type": "string", "enum": ["none", "surplus", "deficit", "both"]},
                "rollover_balance": {"type": "number"},
            },
            "required": ["category", "amount"],
        },
    },
    "preview_create_goal": {
        "fn": _t_preview_create_goal,
        "description": "Preview creating a savings/planning goal.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "goal_type": {"type": "string"},
                "target_amount": {"type": "number"},
                "current_amount": {"type": "number"},
                "target_date": {"type": "string"},
                "linked_category": {"type": "string"},
                "linked_account_id": {"type": "string"},
            },
            "required": ["name", "target_amount"],
        },
    },
    "preview_update_goal_target": {
        "fn": _t_preview_update_goal_target,
        "description": "Preview updating an existing goal's target amount, current progress, or target date.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer"},
                "name": {"type": "string"},
                "target_amount": {"type": "number"},
                "current_amount": {"type": "number"},
                "target_date": {"type": "string"},
            },
            "required": ["target_amount"],
        },
    },
    "preview_mark_goal_funded": {
        "fn": _t_preview_mark_goal_funded,
        "description": "Preview marking an existing goal as fully funded.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer"},
                "name": {"type": "string"},
            },
        },
    },
    "preview_set_transaction_note": {
        "fn": _t_preview_set_transaction_note,
        "description": "Preview setting the note on a specific transaction by transaction_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["transaction_id", "note"],
        },
    },
    "preview_set_transaction_tags": {
        "fn": _t_preview_set_transaction_tags,
        "description": "Preview replacing tags on a specific transaction by transaction_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["transaction_id", "tags"],
        },
    },
    "preview_mark_reviewed": {
        "fn": _t_preview_mark_reviewed,
        "description": "Preview marking one transaction reviewed or unreviewed by transaction_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string"},
                "reviewed": {"type": "boolean", "default": True},
            },
            "required": ["transaction_id"],
        },
    },
    "preview_bulk_mark_reviewed": {
        "fn": _t_preview_bulk_mark_reviewed,
        "description": "Preview marking a filtered set of transactions reviewed or unreviewed.",
        "parameters": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "YYYY-MM month filter"},
                "category": {"type": "string"},
                "account": {"type": "string"},
                "search": {"type": "string"},
                "current_reviewed": {"type": "boolean", "description": "Only match transactions currently in this reviewed state."},
                "reviewed": {"type": "boolean", "default": True, "description": "Target reviewed state."},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
    "preview_update_manual_account_balance": {
        "fn": _t_preview_update_manual_account_balance,
        "description": "Preview updating a manual account balance.",
        "parameters": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "account_name": {"type": "string"},
                "balance": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["balance"],
        },
    },
    "preview_split_transaction": {
        "fn": _t_preview_split_transaction,
        "description": "Preview replacing a transaction's category splits. Split amounts must add up to the transaction amount.",
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_id": {"type": "string"},
                "splits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "amount": {"type": "number"},
                            "notes": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["category", "amount"],
                    },
                },
            },
            "required": ["transaction_id", "splits"],
        },
    },
    "preview_confirm_recurring_obligation": {
        "fn": _t_preview_confirm_recurring,
        "description": "Preview confirming a merchant as a recurring charge/subscription.",
        "parameters": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string"},
                "pattern": {"type": "string"},
                "frequency": {"type": "string"},
                "category": {"type": "string"},
            },
            "required": ["merchant"],
        },
    },
    "preview_dismiss_recurring_obligation": {
        "fn": _t_preview_dismiss_recurring,
        "description": "Preview dismissing a recurring-charge detection as not recurring.",
        "parameters": {
            "type": "object",
            "properties": {"merchant": {"type": "string"}, "pattern": {"type": "string"}},
            "required": ["merchant"],
        },
    },
    "preview_cancel_recurring": {
        "fn": _t_preview_cancel_recurring,
        "description": "Preview marking a recurring charge/subscription as cancelled.",
        "parameters": {
            "type": "object",
            "properties": {"merchant": {"type": "string"}},
            "required": ["merchant"],
        },
    },
    "preview_restore_recurring": {
        "fn": _t_preview_restore_recurring,
        "description": "Preview restoring a dismissed or cancelled recurring charge/subscription.",
        "parameters": {
            "type": "object",
            "properties": {"merchant": {"type": "string"}},
            "required": ["merchant"],
        },
    },
    "plot_chart": {
        "fn": _t_plot_chart,
        "description": "Render a chart inline in the chat. Call this AFTER fetching the underlying numbers with another tool. Use 'line' for trends over time (net worth, monthly spending), 'bar' for comparisons (top merchants, category totals), 'donut' for composition (category share of month).",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["line", "bar", "donut"]},
                "title": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "X-axis labels (months, merchants, categories, etc.)"},
                "values": {"type": "array", "items": {"type": "number"}, "description": "Numeric values matching labels by index"},
                "series": {
                    "type": "array",
                    "description": "Optional multi-series data. Each series values array must match labels.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "values": {"type": "array", "items": {"type": "number"}},
                            "color": {"type": "string"},
                        },
                        "required": ["name", "values"],
                    },
                },
                "annotations": {
                    "type": "array",
                    "description": "Optional horizontal threshold/target lines.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "number"},
                            "color": {"type": "string"},
                        },
                        "required": ["value"],
                    },
                },
                "series_name": {"type": "string", "description": "Name of the data series (e.g. 'Spending', 'Net worth')"},
                "unit": {"type": "string", "enum": ["currency", "number", "percent"], "description": "How values should be formatted. Default 'currency'."},
            },
            "required": ["type", "labels"],
        },
    },
    "run_sql": {
        "fn": _t_run_sql,
        "description": "Internal/debug only: run a semantically validated read-only SELECT against SQLite.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "question": {"type": "string"}},
            "required": ["query", "question"],
        },
    },
}


def _selected_tool_items(names: list[str] | tuple[str, ...] | set[str] | None = None, *, include_internal: bool = False):
    if not names:
        return [(name, spec) for name, spec in TOOL_REGISTRY.items() if include_internal or name not in INTERNAL_TOOL_NAMES]
    wanted = set(names)
    return [
        (name, spec)
        for name, spec in TOOL_REGISTRY.items()
        if name in wanted and (include_internal or name not in INTERNAL_TOOL_NAMES)
    ]


def tools_for_ollama(names: list[str] | tuple[str, ...] | set[str] | None = None, *, include_internal: bool = False) -> list[dict]:
    return [
        {"type": "function", "function": {"name": name, "description": spec["description"], "parameters": spec["parameters"]}}
        for name, spec in _selected_tool_items(names, include_internal=include_internal)
    ]


def execute_tool(name: str, args: dict, profile: str | None, cache: dict | None = None) -> Any:
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return {"error": f"unknown tool: {name}"}

    cache_key = (name, json.dumps(args, sort_keys=True, default=str), profile)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    try:
        try:
            import copilot_cache
        except Exception:
            copilot_cache = None

        with get_db() as conn:
            if copilot_cache is not None and name in copilot_cache.HOT_TOOL_NAMES:
                fingerprint = copilot_cache.db_fingerprint(conn, profile)
                result = copilot_cache.get_hot_tool_result(
                    name,
                    args or {},
                    profile,
                    lambda: spec["fn"](args or {}, profile, conn),
                    fingerprint=fingerprint,
                )
            else:
                result = spec["fn"](args or {}, profile, conn)
    except Exception as e:
        logger.exception("tool %s failed", name)
        result = {"error": f"tool execution failed: {e}"}

    if cache is not None:
        cache[cache_key] = result
    return result
