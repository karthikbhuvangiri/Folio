"""Safe advisor-grade finance measurements for Mira.

This module is the Phase 27.6 measurement layer. It deliberately exposes a
small, validated metric contract instead of SQL or the private backend tool
surface. The LLM may ask for metric names and safe parameters; Python owns all
query construction, math, evidence IDs, confidence, and caveats.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable


QUERY_LAYER_VERSION = "mira_safe_finance_query_v1"

DEFAULT_LIMIT = 12
MAX_LIMIT = 60
MAX_QUERIES = 12

SUPPORTED_DOMAINS: tuple[str, ...] = (
    "money_in",
    "money_out",
    "timing_pacing",
    "cash_resilience",
    "recurring_obligations",
    "budgets_plans",
    "goals_savings",
    "debt_liabilities",
    "net_worth_accounts",
    "anomalies_noise",
    "lifestyle_behavior",
    "quality_confidence",
)

REQUIRED_METRICS_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "money_in": (
        "income_series",
        "income_cadence",
        "income_volatility",
        "paycheck_gap",
        "income_missing_or_late",
        "income_source_concentration",
        "income_source_continuity",
    ),
    "money_out": (
        "monthly_spend_series",
        "money_flow_baseline",
        "category_advisor_ledger",
        "external_transfer_pressure",
        "category_trend",
        "merchant_trend",
        "category_driver_decomposition",
        "merchant_driver_decomposition",
        "frequency_vs_ticket_size",
        "essential_vs_discretionary",
        "fixed_vs_flexible_pressure",
        "refund_adjusted_spend",
        "transfer_payment_excluded_spend",
    ),
    "timing_pacing": (
        "first_half_second_half_pacing",
        "payday_window_spending",
        "weekend_weekday_split",
        "day_of_month_cluster",
        "weekly_burn_rate",
        "month_to_date_pace_vs_baseline",
        "remaining_month_required_pace",
    ),
    "cash_resilience": (
        "cash_balance_series",
        "cash_runway",
        "cash_low_point",
        "next_income_gap_coverage",
        "upcoming_obligation_coverage",
        "buffer_target_gap",
        "resilience_trend",
    ),
    "recurring_obligations": (
        "recurring_obligation_calendar",
        "subscription_cluster",
        "new_or_changed_recurring",
        "cancelled_or_inactive_recurring",
        "fixed_obligation_ratio",
        "floor_burn",
        "bill_stack_before_income",
    ),
    "budgets_plans": (
        "budget_variance",
        "budget_pace",
        "safe_to_spend_status",
        "safe_to_spend_required_adjustment",
        "category_budget_pressure",
        "plan_gap_to_month_end",
    ),
    "goals_savings": (
        "savings_rate_trend",
        "cash_flow_compression",
        "monthly_operating_statement",
        "goal_capacity_statement",
        "goal_feasibility",
        "goal_velocity",
        "required_contribution_vs_recent_behavior",
        "goal_slip_driver",
        "smallest_goal_rescue_lever",
    ),
    "debt_liabilities": (
        "debt_balance_trend",
        "debt_payment_pressure",
        "credit_utilization",
        "minimum_payment_risk",
        "interest_or_fee_signal",
        "avoidable_leakage",
        "liability_to_cash_ratio",
    ),
    "net_worth_accounts": (
        "net_worth_series",
        "net_worth_driver_split",
        "account_balance_trend",
        "cash_vs_liability_position",
        "idle_cash_signal",
        "account_coverage_caveats",
    ),
    "anomalies_noise": (
        "unusual_transactions",
        "one_off_large_purchase",
        "refund_or_transfer_noise",
        "category_false_alarm",
        "merchant_false_alarm",
        "seasonality_or_sparse_data",
        "materiality_filter",
    ),
    "lifestyle_behavior": (
        "convenience_pattern",
        "merchant_stickiness",
        "small_frequent_leak",
        "payday_drift",
        "weekend_pressure",
        "subscription_creep",
        "category_substitution",
        "habit_stability_or_churn",
        "spending_event_clusters",
        "merchant_lifecycle",
        "savings_scenarios",
        "private_discretionary_patterns",
        "realistic_trim_levers",
        "financial_timeline_events",
    ),
    "quality_confidence": (
        "advisor_period_reliability",
        "advisor_data_quality_profile",
        "data_quality_caveats",
        "enrichment_confidence_summary",
        "low_confidence_driver_rows",
        "missing_account_or_date_coverage",
        "category_mapping_uncertainty",
        "recurrence_confidence",
        "profile_scope_caveat",
    ),
}

NON_SPENDING_CATEGORIES: tuple[str, ...] = (
    "Income",
    "Savings Transfer",
    "Credit Card Payment",
    "Cash Deposit",
    "Cash Withdrawal",
    "Investment Transfer",
    "Transfer",
    "Transfers",
    "Personal Transfer",
)

STRUCTURAL_FLOOR_CATEGORIES: tuple[str, ...] = (
    "Housing",
    "Rent",
    "Mortgage",
)

EVENT_ACTIVITY_CATEGORIES: tuple[str, ...] = (
    "Travel",
    "Groceries",
    "Transportation",
    "Food & Dining",
    "Shopping",
    "Other",
    "Alcohol",
    "Entertainment",
)

PRIVATE_DISCRETIONARY_CATEGORIES: tuple[str, ...] = (
    "Alcohol",
    "Vaping",
    "Gambling",
)

BLOCKED_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "access_token",
    "account_number",
    "api_key",
    "description",
    "manual_notes",
    "notes",
    "password",
    "raw_description",
    "routing_number",
    "secret",
    "token",
)

SAFE_FILTER_KEYS: tuple[str, ...] = (
    "account_type",
    "category",
    "exclude_transfers",
    "merchant",
    "minimum_amount",
    "profile_scope",
)

SAFE_DIMENSIONS: tuple[str, ...] = (
    "account",
    "account_type",
    "category",
    "day_bucket",
    "essentiality",
    "merchant",
    "month",
    "recurrence",
    "week",
)

_SQL_FRAGMENT_RE = re.compile(
    r"(;|--|/\*|\*/|\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\bdrop\b|\bpragma\b|\battach\b|\bfrom\b|\bwhere\b)",
    re.IGNORECASE,
)
_METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class MetricSpec:
    metric: str
    domain: str
    description: str
    dimensions: tuple[str, ...] = ()
    filters: tuple[str, ...] = SAFE_FILTER_KEYS
    max_rows: int = DEFAULT_LIMIT
    implemented: bool = True
    implementation_note: str = ""


@dataclass(frozen=True)
class ResolvedRange:
    token: str
    start: str | None
    end: str | None
    label: str
    all_time: bool = False


def _spec(metric: str, domain: str, description: str, *, dimensions: tuple[str, ...] = (), max_rows: int = DEFAULT_LIMIT) -> MetricSpec:
    return MetricSpec(metric=metric, domain=domain, description=description, dimensions=dimensions, max_rows=max_rows)


METRIC_SPECS: dict[str, MetricSpec] = {
    # Money in
    "income_series": _spec("income_series", "money_in", "Monthly income totals and deposit counts.", dimensions=("month",), max_rows=12),
    "income_cadence": _spec("income_cadence", "money_in", "Income deposit interval and cadence.", dimensions=("merchant",), max_rows=12),
    "income_volatility": _spec("income_volatility", "money_in", "Income stability over the selected range.", dimensions=("month",), max_rows=12),
    "paycheck_gap": _spec("paycheck_gap", "money_in", "Largest observed gap between income deposits.", dimensions=("merchant",), max_rows=12),
    "income_missing_or_late": _spec("income_missing_or_late", "money_in", "Whether income appears late against observed cadence.", dimensions=("merchant",), max_rows=12),
    "income_source_concentration": _spec("income_source_concentration", "money_in", "Income concentration by source.", dimensions=("merchant",), max_rows=12),
    "income_source_continuity": _spec("income_source_continuity", "money_in", "Income source/cadence continuity classification.", dimensions=("month", "merchant"), max_rows=12),
    # Money out
    "monthly_spend_series": _spec("monthly_spend_series", "money_out", "Monthly spending totals excluding transfer/payment categories.", dimensions=("month",), max_rows=12),
    "money_flow_baseline": _spec("money_flow_baseline", "money_out", "Normal monthly money map by category and merchant, with event/trip exclusions.", dimensions=("category", "merchant"), max_rows=16),
    "category_advisor_ledger": _spec("category_advisor_ledger", "money_out", "Advisor category ledger with spend, recent-vs-prior trend, ticket size, and merchant drivers.", dimensions=("category", "merchant"), max_rows=24),
    "external_transfer_pressure": _spec("external_transfer_pressure", "money_out", "External transfer inflow/outflow pressure separated from lifestyle spending.", dimensions=("month",), max_rows=12),
    "category_trend": _spec("category_trend", "money_out", "Category totals compared to recent baseline.", dimensions=("category", "month"), max_rows=20),
    "merchant_trend": _spec("merchant_trend", "money_out", "Merchant totals compared to recent baseline.", dimensions=("merchant", "month"), max_rows=20),
    "category_driver_decomposition": _spec("category_driver_decomposition", "money_out", "Category drivers of spend change vs baseline.", dimensions=("category",), max_rows=20),
    "merchant_driver_decomposition": _spec("merchant_driver_decomposition", "money_out", "Merchant drivers of spend change vs baseline.", dimensions=("merchant",), max_rows=20),
    "frequency_vs_ticket_size": _spec("frequency_vs_ticket_size", "money_out", "Separates increased frequency from larger average purchases.", dimensions=("category", "merchant"), max_rows=20),
    "essential_vs_discretionary": _spec("essential_vs_discretionary", "money_out", "Spend split by enrichment essentiality.", dimensions=("essentiality",), max_rows=8),
    "fixed_vs_flexible_pressure": _spec("fixed_vs_flexible_pressure", "money_out", "Spend pressure by fixed/flexible category metadata.", dimensions=("category",), max_rows=8),
    "refund_adjusted_spend": _spec("refund_adjusted_spend", "money_out", "Gross spend, refunds, and net spend.", dimensions=("month",), max_rows=12),
    "transfer_payment_excluded_spend": _spec("transfer_payment_excluded_spend", "money_out", "Shows impact of transfer/payment exclusions.", dimensions=("month",), max_rows=12),
    # Timing / pacing
    "first_half_second_half_pacing": _spec("first_half_second_half_pacing", "timing_pacing", "Spend split between first and second half of month.", dimensions=("day_bucket",), max_rows=8),
    "payday_window_spending": _spec("payday_window_spending", "timing_pacing", "Spend near income dates vs outside payday windows.", dimensions=("day_bucket",), max_rows=8),
    "weekend_weekday_split": _spec("weekend_weekday_split", "timing_pacing", "Weekend vs weekday spending pressure.", dimensions=("day_bucket",), max_rows=8),
    "day_of_month_cluster": _spec("day_of_month_cluster", "timing_pacing", "Spend clustered by day of month.", dimensions=("day_bucket",), max_rows=8),
    "weekly_burn_rate": _spec("weekly_burn_rate", "timing_pacing", "Weekly spending burn rate.", dimensions=("week",), max_rows=16),
    "month_to_date_pace_vs_baseline": _spec("month_to_date_pace_vs_baseline", "timing_pacing", "MTD pace compared with recent daily baseline.", dimensions=("month",), max_rows=6),
    "remaining_month_required_pace": _spec("remaining_month_required_pace", "timing_pacing", "Remaining daily pace implied by configured budget.", dimensions=("month",), max_rows=6),
    # Cash / resilience
    "cash_balance_series": _spec("cash_balance_series", "cash_resilience", "Current cash-like account balances.", dimensions=("account_type",), max_rows=20),
    "cash_runway": _spec("cash_runway", "cash_resilience", "How long cash covers normal burn.", dimensions=("account_type",), max_rows=8),
    "cash_low_point": _spec("cash_low_point", "cash_resilience", "Simple projected cash low point from cash and recurring obligations.", dimensions=("month",), max_rows=8),
    "next_income_gap_coverage": _spec("next_income_gap_coverage", "cash_resilience", "Coverage until next expected income date.", dimensions=("month",), max_rows=8),
    "upcoming_obligation_coverage": _spec("upcoming_obligation_coverage", "cash_resilience", "Cash coverage for upcoming recurring obligations.", dimensions=("merchant",), max_rows=12),
    "buffer_target_gap": _spec("buffer_target_gap", "cash_resilience", "Gap to a one-month expense buffer.", dimensions=("month",), max_rows=8),
    "resilience_trend": _spec("resilience_trend", "cash_resilience", "Net worth and cash-runway resilience trend.", dimensions=("month",), max_rows=12),
    # Recurring
    "recurring_obligation_calendar": _spec("recurring_obligation_calendar", "recurring_obligations", "Upcoming recurring obligations.", dimensions=("merchant",), max_rows=20),
    "subscription_cluster": _spec("subscription_cluster", "recurring_obligations", "Recurring bills clustered by week of month.", dimensions=("day_bucket",), max_rows=8),
    "new_or_changed_recurring": _spec("new_or_changed_recurring", "recurring_obligations", "New or changed recurring events.", dimensions=("merchant",), max_rows=20),
    "cancelled_or_inactive_recurring": _spec("cancelled_or_inactive_recurring", "recurring_obligations", "Cancelled or inactive recurring obligations.", dimensions=("merchant",), max_rows=20),
    "fixed_obligation_ratio": _spec("fixed_obligation_ratio", "recurring_obligations", "Recurring obligation share of income.", dimensions=("month",), max_rows=8),
    "floor_burn": _spec("floor_burn", "recurring_obligations", "Housing-aware fixed monthly floor before flexible spending.", dimensions=("category",), max_rows=8),
    "bill_stack_before_income": _spec("bill_stack_before_income", "recurring_obligations", "Bills expected before next income.", dimensions=("merchant",), max_rows=20),
    # Budgets / goals / debt / net worth
    "budget_variance": _spec("budget_variance", "budgets_plans", "Budget amount vs current spend.", dimensions=("category",), max_rows=30),
    "budget_pace": _spec("budget_pace", "budgets_plans", "Budget spending pace vs elapsed month.", dimensions=("category",), max_rows=30),
    "safe_to_spend_status": _spec("safe_to_spend_status", "budgets_plans", "Remaining flexible room from configured budgets.", dimensions=("month",), max_rows=8),
    "safe_to_spend_required_adjustment": _spec("safe_to_spend_required_adjustment", "budgets_plans", "Required adjustment to finish month inside budget.", dimensions=("category",), max_rows=12),
    "category_budget_pressure": _spec("category_budget_pressure", "budgets_plans", "Categories over or near budget.", dimensions=("category",), max_rows=20),
    "plan_gap_to_month_end": _spec("plan_gap_to_month_end", "budgets_plans", "Gap between remaining budget and projected month-end pace.", dimensions=("month",), max_rows=8),
    "savings_rate_trend": _spec("savings_rate_trend", "goals_savings", "Savings rate over recent months.", dimensions=("month",), max_rows=12),
    "cash_flow_compression": _spec("cash_flow_compression", "goals_savings", "Cash-flow rate compression across reliable income, trailing 12 complete months, and recent 3 complete months.", dimensions=("month",), max_rows=12),
    "monthly_operating_statement": _spec("monthly_operating_statement", "goals_savings", "Reconciled monthly operating statement: income, normal burn, fixed-floor gap, leakage, debt movement, and capacity.", dimensions=("component",), max_rows=16),
    "goal_capacity_statement": _spec("goal_capacity_statement", "goals_savings", "Goal capacity from the reconciled monthly operating statement and configured goals.", dimensions=("goal", "component"), max_rows=20),
    "goal_feasibility": _spec("goal_feasibility", "goals_savings", "Goal gap and feasibility from current progress.", dimensions=("category",), max_rows=20),
    "goal_velocity": _spec("goal_velocity", "goals_savings", "Goal required monthly pace.", dimensions=("category",), max_rows=20),
    "required_contribution_vs_recent_behavior": _spec("required_contribution_vs_recent_behavior", "goals_savings", "Goal contribution required vs recent savings behavior.", dimensions=("category",), max_rows=20),
    "goal_slip_driver": _spec("goal_slip_driver", "goals_savings", "Goal slip risk and likely pressure category.", dimensions=("category",), max_rows=20),
    "smallest_goal_rescue_lever": _spec("smallest_goal_rescue_lever", "goals_savings", "Smallest plausible spending lever for goal rescue.", dimensions=("category",), max_rows=12),
    "debt_balance_trend": _spec("debt_balance_trend", "debt_liabilities", "Liability account balances.", dimensions=("account_type",), max_rows=20),
    "debt_payment_pressure": _spec("debt_payment_pressure", "debt_liabilities", "Debt/payment category pressure.", dimensions=("month",), max_rows=12),
    "credit_utilization": _spec("credit_utilization", "debt_liabilities", "Approximate card utilization where balance data supports it.", dimensions=("account",), max_rows=20),
    "minimum_payment_risk": _spec("minimum_payment_risk", "debt_liabilities", "Minimum-payment caveat where account terms are unavailable.", dimensions=("account",), max_rows=20),
    "interest_or_fee_signal": _spec("interest_or_fee_signal", "debt_liabilities", "Interest/fee transaction signals.", dimensions=("merchant",), max_rows=20),
    "avoidable_leakage": _spec("avoidable_leakage", "debt_liabilities", "Preventable or reviewable leakage such as fees, interest, and duplicate recurring records.", dimensions=("merchant", "category"), max_rows=20),
    "liability_to_cash_ratio": _spec("liability_to_cash_ratio", "debt_liabilities", "Liabilities compared with cash-like balances.", dimensions=("account_type",), max_rows=8),
    "net_worth_series": _spec("net_worth_series", "net_worth_accounts", "Net worth history.", dimensions=("month",), max_rows=20),
    "net_worth_driver_split": _spec("net_worth_driver_split", "net_worth_accounts", "Asset and owed contribution to net worth change.", dimensions=("month",), max_rows=20),
    "account_balance_trend": _spec("account_balance_trend", "net_worth_accounts", "Current account balance mix.", dimensions=("account_type",), max_rows=20),
    "cash_vs_liability_position": _spec("cash_vs_liability_position", "net_worth_accounts", "Cash-like balances vs liabilities.", dimensions=("account_type",), max_rows=8),
    "idle_cash_signal": _spec("idle_cash_signal", "net_worth_accounts", "Cash buffer above normal burn.", dimensions=("account_type",), max_rows=8),
    "account_coverage_caveats": _spec("account_coverage_caveats", "net_worth_accounts", "Account staleness and coverage caveats.", dimensions=("account_type",), max_rows=20),
    # Advisor diagnostics
    "unusual_transactions": _spec("unusual_transactions", "anomalies_noise", "Large transactions relative to recent average.", dimensions=("merchant", "category"), max_rows=20),
    "one_off_large_purchase": _spec("one_off_large_purchase", "anomalies_noise", "Large one-off purchases.", dimensions=("merchant", "category"), max_rows=20),
    "refund_or_transfer_noise": _spec("refund_or_transfer_noise", "anomalies_noise", "Refunds/transfers that may distort spend reads.", dimensions=("category",), max_rows=20),
    "category_false_alarm": _spec("category_false_alarm", "anomalies_noise", "Categories that look loud but are not material drivers.", dimensions=("category",), max_rows=20),
    "merchant_false_alarm": _spec("merchant_false_alarm", "anomalies_noise", "Merchants that look loud but are not material drivers.", dimensions=("merchant",), max_rows=20),
    "seasonality_or_sparse_data": _spec("seasonality_or_sparse_data", "anomalies_noise", "Sparse data caveats for trend reads.", dimensions=("category",), max_rows=20),
    "materiality_filter": _spec("materiality_filter", "anomalies_noise", "Ranks changes by materiality threshold.", dimensions=("category", "merchant"), max_rows=20),
    "convenience_pattern": _spec("convenience_pattern", "lifestyle_behavior", "Convenience-style repeat merchant/category pattern without motive claims.", dimensions=("merchant", "category"), max_rows=20),
    "merchant_stickiness": _spec("merchant_stickiness", "lifestyle_behavior", "Merchants that quietly dominate by repeat count.", dimensions=("merchant",), max_rows=20),
    "small_frequent_leak": _spec("small_frequent_leak", "lifestyle_behavior", "Small frequent purchases with meaningful total.", dimensions=("merchant", "category"), max_rows=20),
    "payday_drift": _spec("payday_drift", "lifestyle_behavior", "Spending concentration after income arrives.", dimensions=("day_bucket",), max_rows=8),
    "weekend_pressure": _spec("weekend_pressure", "lifestyle_behavior", "Weekend spending pressure.", dimensions=("day_bucket",), max_rows=8),
    "subscription_creep": _spec("subscription_creep", "lifestyle_behavior", "Recurring items that are new, changed, or clustered.", dimensions=("merchant",), max_rows=20),
    "category_substitution": _spec("category_substitution", "lifestyle_behavior", "Categories rising while others fall.", dimensions=("category",), max_rows=20),
    "habit_stability_or_churn": _spec("habit_stability_or_churn", "lifestyle_behavior", "Repeat-merchant stability/churn.", dimensions=("merchant",), max_rows=20),
    "spending_event_clusters": _spec("spending_event_clusters", "lifestyle_behavior", "Travel/event-like spend clusters across booking and activity windows.", dimensions=("event",), max_rows=8),
    "merchant_lifecycle": _spec("merchant_lifecycle", "lifestyle_behavior", "Merchant lifecycle: top, new, dormant, and split-label merchant groups.", dimensions=("merchant", "category"), max_rows=24),
    "savings_scenarios": _spec("savings_scenarios", "lifestyle_behavior", "Quantified what-if savings scenarios from observed trends, leakage, and controllable repeat merchants.", dimensions=("category", "merchant"), max_rows=16),
    "private_discretionary_patterns": _spec("private_discretionary_patterns", "lifestyle_behavior", "Local-only sensitive discretionary spend patterns.", dimensions=("category", "merchant"), max_rows=12),
    "realistic_trim_levers": _spec("realistic_trim_levers", "lifestyle_behavior", "Practical low-friction improvement levers grounded in repeatability.", dimensions=("category", "merchant"), max_rows=12),
    "financial_timeline_events": _spec("financial_timeline_events", "lifestyle_behavior", "Chronological advisor events and transitions from safe measurements.", dimensions=("event", "month"), max_rows=12),
    "advisor_period_reliability": _spec("advisor_period_reliability", "quality_confidence", "Analysis period reliability: data range, first income month, partial current month, and complete-month coverage.", dimensions=("month",), max_rows=8),
    "advisor_data_quality_profile": _spec("advisor_data_quality_profile", "quality_confidence", "Advisor data quality profile: review state, low-confidence spend, splits, recurring duplicates, and net-worth coverage.", dimensions=("category",), max_rows=16),
    "data_quality_caveats": _spec("data_quality_caveats", "quality_confidence", "Data coverage and staleness caveats.", dimensions=("month",), max_rows=20),
    "enrichment_confidence_summary": _spec("enrichment_confidence_summary", "quality_confidence", "Enrichment coverage/confidence summary.", dimensions=("category",), max_rows=20),
    "low_confidence_driver_rows": _spec("low_confidence_driver_rows", "quality_confidence", "Low-confidence enriched rows that can affect drivers.", dimensions=("category", "merchant"), max_rows=20),
    "missing_account_or_date_coverage": _spec("missing_account_or_date_coverage", "quality_confidence", "Missing or stale account/date coverage.", dimensions=("account",), max_rows=20),
    "category_mapping_uncertainty": _spec("category_mapping_uncertainty", "quality_confidence", "Category mapping uncertainty.", dimensions=("category",), max_rows=20),
    "recurrence_confidence": _spec("recurrence_confidence", "quality_confidence", "Recurring obligation confidence distribution.", dimensions=("merchant",), max_rows=20),
    "profile_scope_caveat": _spec("profile_scope_caveat", "quality_confidence", "Profile scoping caveat.", dimensions=("month",), max_rows=8),
}


def build_safe_finance_catalog(conn=None) -> dict[str, Any]:
    """Return the safe read surface and metric registry, without row data."""

    tables = {
        "transactions_visible": {
            "allowed_columns": (
                "id",
                "profile_id",
                "date",
                "amount",
                "category",
                "expense_type",
                "account_name",
                "account_type",
                "merchant_name",
                "merchant_key",
                "confidence",
            ),
            "blocked_columns": ("description", "raw_description", "notes", "tags"),
        },
        "accounts": {
            "allowed_columns": (
                "id",
                "profile_id",
                "institution_name",
                "account_name",
                "account_type",
                "account_subtype",
                "current_balance",
                "available_balance",
                "last_synced_at",
                "is_active",
            ),
            "blocked_columns": ("last_four", "manual_notes"),
        },
        "transaction_enrichment": {
            "allowed_columns": (
                "transaction_id",
                "profile_id",
                "canonical_counterparty",
                "display_counterparty",
                "top_level_category",
                "leaf_category",
                "purpose_category",
                "essentiality",
                "recurrence",
                "semantic_type",
                "confidence_json",
                "source",
                "method",
                "user_reviewed",
            ),
            "blocked_columns": ("evidence_json", "evidence_summary"),
        },
        "category_budgets": {"allowed_columns": ("profile_id", "category", "amount", "rollover_mode", "rollover_balance"), "blocked_columns": ()},
        "goals": {"allowed_columns": ("id", "profile_id", "name", "goal_type", "target_amount", "current_amount", "target_date", "linked_category"), "blocked_columns": ()},
        "recurring_obligations": {
            "allowed_columns": (
                "profile_id",
                "merchant_key",
                "display_name",
                "category",
                "amount_cents",
                "frequency",
                "anchor_day",
                "next_expected_date",
                "state",
                "confidence_score",
                "confidence_label",
            ),
            "blocked_columns": ("evidence_json",),
        },
        "net_worth_history": {"allowed_columns": ("date", "profile_id", "total_assets", "total_owed", "net_worth"), "blocked_columns": ()},
    }
    return {
        "version": QUERY_LAYER_VERSION,
        "tables": tables,
        "blocked_fields": BLOCKED_SNAPSHOT_FIELDS,
        "metric_count": len(METRIC_SPECS),
        "metrics": {name: _spec_public(spec) for name, spec in sorted(METRIC_SPECS.items())},
        "domains": {domain: list(metrics) for domain, metrics in REQUIRED_METRICS_BY_DOMAIN.items()},
        "snapshot_counts": _safe_snapshot_counts(conn) if conn is not None else {},
    }


def validate_query_payload(payload: dict[str, Any] | list[dict[str, Any]], *, strict: bool = True) -> dict[str, Any]:
    """Validate the LLM-facing query contract and return normalized queries."""

    raw_queries = payload.get("queries") if isinstance(payload, dict) else payload
    if not isinstance(raw_queries, list):
        return {"ok": False, "queries": [], "errors": [{"reason": "queries_not_list"}], "missing_metric_proposals": []}
    errors: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_queries[:MAX_QUERIES]):
        if not isinstance(item, dict):
            errors.append({"index": idx, "reason": "query_not_object"})
            continue
        metric = _clean_metric(item.get("metric"))
        if not metric:
            errors.append({"index": idx, "reason": "invalid_metric_name"})
            continue
        if _contains_sql_fragment(metric):
            errors.append({"index": idx, "metric": metric, "reason": "sql_fragment"})
            continue
        spec = METRIC_SPECS.get(metric)
        if spec is None:
            proposals.append(
                {
                    "requested_metric": metric,
                    "why_needed": _clean_text(item.get("purpose"), 180) or "Planner requested an unsupported measurement.",
                    "needed_inputs": [],
                    "suggested_output": "",
                    "priority": "medium",
                }
            )
            if strict:
                errors.append({"index": idx, "metric": metric, "reason": "unsupported_metric"})
            continue
        dimensions = _safe_string_list(item.get("dimensions"))
        unsafe_dimensions = [dim for dim in dimensions if dim not in spec.dimensions or dim not in SAFE_DIMENSIONS]
        if unsafe_dimensions:
            errors.append({"index": idx, "metric": metric, "reason": "unsafe_dimension", "dimensions": unsafe_dimensions})
            continue
        raw_filters = item.get("filters") if isinstance(item.get("filters"), dict) else {}
        filters, filter_errors = _validate_filters(raw_filters)
        if filter_errors:
            errors.extend({"index": idx, "metric": metric, **err} for err in filter_errors)
            continue
        purpose = _clean_text(item.get("purpose"), 220)
        if _contains_sql_fragment(purpose):
            errors.append({"index": idx, "metric": metric, "reason": "sql_fragment_in_purpose"})
            continue
        range_token = _clean_range_token(item.get("range") or "last_6_months")
        if not resolve_time_range(range_token).label:
            errors.append({"index": idx, "metric": metric, "reason": "unsupported_range", "range": range_token})
            continue
        limit = _safe_int(item.get("limit"), spec.max_rows)
        normalized.append(
            {
                "metric": metric,
                "range": range_token,
                "dimensions": dimensions,
                "filters": filters,
                "purpose": purpose,
                "limit": min(max(1, limit), min(MAX_LIMIT, spec.max_rows)),
            }
        )
    if len(raw_queries) > MAX_QUERIES:
        errors.append({"reason": "query_count_capped", "max_queries": MAX_QUERIES})
    return {"ok": not errors, "queries": normalized, "errors": errors, "missing_metric_proposals": proposals}


def execute_safe_finance_queries(
    conn,
    queries: dict[str, Any] | list[dict[str, Any]],
    *,
    profile: str | None = None,
    as_of: date | datetime | str | None = None,
) -> dict[str, Any]:
    validation = validate_query_payload(queries, strict=False)
    results = []
    for query in validation["queries"]:
        results.append(execute_metric(conn, query, profile=profile, as_of=as_of))
    return {
        "version": QUERY_LAYER_VERSION,
        "profile_scope": _profile_scope(profile),
        "query_count": len(results),
        "results": results,
        "errors": validation["errors"],
        "missing_metric_proposals": validation["missing_metric_proposals"],
    }


def execute_metric(conn, query: dict[str, Any], *, profile: str | None = None, as_of: date | datetime | str | None = None) -> dict[str, Any]:
    metric = _clean_metric(query.get("metric"))
    spec = METRIC_SPECS.get(metric)
    if spec is None:
        return _missing_metric_result(metric, query)
    resolved = resolve_time_range(query.get("range") or "last_6_months", as_of=as_of)
    ctx = {
        "profile": _profile_scope(profile),
        "range": resolved,
        "limit": min(max(1, _safe_int(query.get("limit"), spec.max_rows)), spec.max_rows),
        "filters": query.get("filters") if isinstance(query.get("filters"), dict) else {},
    }
    try:
        handler = _HANDLERS.get(metric, _handler_for_metric(metric))
        return handler(conn, metric, ctx)
    except Exception as exc:
        return _result(
            metric=metric,
            domain=spec.domain,
            time_range=resolved,
            basis=f"{spec.description} Query failed before returning data.",
            rows=[],
            summary_numbers={},
            confidence="low",
            caveats=[f"{type(exc).__name__}: {str(exc)[:160]}"],
            evidence_ids=[],
        )


def build_semantic_query_planner_prompt(question: str, *, max_queries: int = 8) -> str:
    allowed = {name: {"domain": spec.domain, "dimensions": list(spec.dimensions), "filters": list(spec.filters)} for name, spec in sorted(METRIC_SPECS.items())}
    return (
        "You are Mira's safe finance measurement planner. Return JSON only.\n"
        "Pick only metric names from the allowed registry. Never write SQL.\n"
        "Use range tokens such as current_month, last_month, last_3_months, last_6_months, last_12_months, ytd, all_time, or YYYY-MM-DD..YYYY-MM-DD.\n"
        f"Return at most {max_queries} queries.\n\n"
        "Schema: {\"queries\":[{\"metric\":\"...\",\"range\":\"last_6_months\",\"dimensions\":[],\"filters\":{\"exclude_transfers\":true,\"profile_scope\":\"active\"},\"purpose\":\"...\",\"limit\":12}]}\n\n"
        f"Allowed registry JSON:\n{json.dumps(allowed, sort_keys=True, separators=(',', ':'))}\n\n"
        f"Question: {str(question or '').strip()}\nJSON:"
    )


def plan_safe_finance_queries(
    question: str,
    *,
    complete_fn: Callable[[str], str] | None = None,
    max_queries: int = 8,
) -> dict[str, Any]:
    """Planner stage for Phase 27.6F.

    If no LLM completion function is provided, return a broad default advisor
    set. The default is for offline dossiers and evals, not chat-time routing.
    """

    if complete_fn is None:
        return {"status": "default", "queries": default_advisor_queries(limit=max_queries), "errors": []}
    prompt = build_semantic_query_planner_prompt(question, max_queries=max_queries)
    raw = complete_fn(prompt)
    try:
        payload = json.loads(_json_object_text(raw))
    except Exception as exc:
        return {"status": "invalid_json", "queries": [], "errors": [f"{type(exc).__name__}: {exc}"], "raw_excerpt": str(raw)[:240]}
    validation = validate_query_payload(payload, strict=False)
    return {"status": "ok" if validation["queries"] else "no_queries", **validation}


def default_advisor_queries(*, limit: int = 10) -> list[dict[str, Any]]:
    metrics = (
        "advisor_period_reliability",
        "monthly_spend_series",
        "money_flow_baseline",
        "cash_flow_compression",
        "category_advisor_ledger",
        "merchant_lifecycle",
        "external_transfer_pressure",
        "income_series",
        "cash_runway",
        "category_driver_decomposition",
        "merchant_driver_decomposition",
        "frequency_vs_ticket_size",
        "category_false_alarm",
        "small_frequent_leak",
        "budget_variance",
        "monthly_operating_statement",
        "goal_capacity_statement",
        "savings_scenarios",
        "avoidable_leakage",
        "advisor_data_quality_profile",
        "recurring_obligation_calendar",
        "data_quality_caveats",
        "enrichment_confidence_summary",
    )
    return [
        {
            "metric": metric,
            "range": "last_6_months",
            "dimensions": [],
            "filters": {"exclude_transfers": True, "profile_scope": "active"},
            "purpose": "Build an advisor dossier.",
            "limit": DEFAULT_LIMIT,
        }
        for metric in metrics[: max(1, min(limit, len(metrics)))]
    ]


FINANCIAL_PORTRAIT_METRICS: tuple[str, ...] = (
    "advisor_period_reliability",
    "income_series",
    "income_cadence",
    "income_volatility",
    "paycheck_gap",
    "income_missing_or_late",
    "income_source_concentration",
    "income_source_continuity",
    "monthly_spend_series",
    "money_flow_baseline",
    "cash_flow_compression",
    "category_advisor_ledger",
    "merchant_lifecycle",
    "external_transfer_pressure",
    "refund_adjusted_spend",
    "transfer_payment_excluded_spend",
    "savings_rate_trend",
    "category_trend",
    "merchant_trend",
    "category_driver_decomposition",
    "merchant_driver_decomposition",
    "frequency_vs_ticket_size",
    "essential_vs_discretionary",
    "fixed_vs_flexible_pressure",
    "first_half_second_half_pacing",
    "payday_window_spending",
    "weekend_weekday_split",
    "day_of_month_cluster",
    "weekly_burn_rate",
    "cash_balance_series",
    "cash_runway",
    "cash_low_point",
    "next_income_gap_coverage",
    "upcoming_obligation_coverage",
    "buffer_target_gap",
    "resilience_trend",
    "recurring_obligation_calendar",
    "subscription_cluster",
    "new_or_changed_recurring",
    "fixed_obligation_ratio",
    "floor_burn",
    "bill_stack_before_income",
    "budget_variance",
    "budget_pace",
    "safe_to_spend_status",
    "category_budget_pressure",
    "monthly_operating_statement",
    "goal_capacity_statement",
    "goal_feasibility",
    "goal_velocity",
    "required_contribution_vs_recent_behavior",
    "goal_slip_driver",
    "smallest_goal_rescue_lever",
    "debt_balance_trend",
    "debt_payment_pressure",
    "credit_utilization",
    "minimum_payment_risk",
    "interest_or_fee_signal",
    "avoidable_leakage",
    "liability_to_cash_ratio",
    "net_worth_series",
    "net_worth_driver_split",
    "cash_vs_liability_position",
    "one_off_large_purchase",
    "refund_or_transfer_noise",
    "category_false_alarm",
    "merchant_false_alarm",
    "seasonality_or_sparse_data",
    "small_frequent_leak",
    "convenience_pattern",
    "savings_scenarios",
    "spending_event_clusters",
    "private_discretionary_patterns",
    "realistic_trim_levers",
    "financial_timeline_events",
    "category_substitution",
    "advisor_data_quality_profile",
    "data_quality_caveats",
    "enrichment_confidence_summary",
    "missing_account_or_date_coverage",
    "category_mapping_uncertainty",
    "recurrence_confidence",
    "profile_scope_caveat",
)


def financial_portrait_queries(*, range_token: str = "last_6_months", limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Return the broad safe metric set for an offline advisor portrait."""

    bounded_limit = min(max(1, _safe_int(limit, DEFAULT_LIMIT)), DEFAULT_LIMIT)
    queries = []
    for metric in FINANCIAL_PORTRAIT_METRICS:
        metric_range = "last_12_months" if metric in {"spending_event_clusters", "private_discretionary_patterns", "financial_timeline_events"} and range_token == "last_6_months" else range_token
        queries.append(
            {
                "metric": metric,
                "range": metric_range,
                "dimensions": [],
                "filters": {"exclude_transfers": True, "profile_scope": "active"},
                "purpose": "Build Mira's offline financial portrait.",
                "limit": bounded_limit,
            }
        )
    return queries


def build_advisor_dossier(
    conn,
    question: str,
    *,
    profile: str | None = None,
    query_plan: dict[str, Any] | list[dict[str, Any]] | None = None,
    as_of: date | datetime | str | None = None,
) -> dict[str, Any]:
    plan = query_plan or {"queries": default_advisor_queries()}
    executed = execute_safe_finance_queries(conn, plan, profile=profile, as_of=as_of)
    return _assemble_advisor_dossier(
        question=question,
        profile=profile,
        measurements=executed["results"],
        errors=executed.get("errors") or [],
        missing_metric_proposals=executed.get("missing_metric_proposals") or [],
    )


def build_financial_portrait_dossier(
    conn,
    question: str,
    *,
    profile: str | None = None,
    query_plan: dict[str, Any] | list[dict[str, Any]] | None = None,
    as_of: date | datetime | str | None = None,
) -> dict[str, Any]:
    """Build a broad offline advisor dossier while preserving query caps.

    The public query validator still caps each LLM-facing payload at
    ``MAX_QUERIES``. This helper is Python-owned infrastructure, so it executes
    the approved portrait metric set in safe chunks and then assembles a richer
    dossier for synthesis.
    """

    if query_plan is None:
        raw_queries = financial_portrait_queries()
    elif isinstance(query_plan, dict):
        raw_queries = query_plan.get("queries") if isinstance(query_plan.get("queries"), list) else []
    else:
        raw_queries = query_plan

    measurements: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    for idx in range(0, len(raw_queries), MAX_QUERIES):
        chunk = {"queries": raw_queries[idx : idx + MAX_QUERIES]}
        executed = execute_safe_finance_queries(conn, chunk, profile=profile, as_of=as_of)
        measurements.extend(executed.get("results") or [])
        errors.extend(executed.get("errors") or [])
        proposals.extend(executed.get("missing_metric_proposals") or [])

    return _assemble_advisor_dossier(
        question=question,
        profile=profile,
        measurements=measurements,
        errors=errors,
        missing_metric_proposals=proposals,
        portrait_sections=_portrait_sections(measurements),
    )


def _assemble_advisor_dossier(
    *,
    question: str,
    profile: str | None,
    measurements: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    missing_metric_proposals: list[dict[str, Any]],
    portrait_sections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidate_drivers = _candidate_drivers(measurements)
    false_alarms = _false_alarms(measurements)
    constraints = _constraints(measurements)
    smallest_levers = _smallest_levers(measurements)
    caveats = _dedupe([c for m in measurements for c in (m.get("caveats") or [])])
    confidence = _combine_confidence([m.get("confidence") for m in measurements])
    return {
        "version": QUERY_LAYER_VERSION,
        "question": str(question or "").strip(),
        "profile_scope": _profile_scope(profile),
        "measurements": measurements,
        "candidate_drivers": candidate_drivers,
        "false_alarms": false_alarms,
        "constraints": constraints,
        "smallest_levers": smallest_levers,
        "confidence": confidence,
        "caveats": caveats,
        "portrait_sections": portrait_sections or [],
        "missing_metric_proposals": missing_metric_proposals,
        "audit": {
            "query_metrics": [m.get("metric") for m in measurements],
            "evidence_ids": _dedupe([eid for m in measurements for eid in (m.get("evidence_ids") or [])])[:80],
            "errors": errors,
        },
    }


def _portrait_sections(measurements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_metric = {str(item.get("metric") or ""): item for item in measurements}
    section_specs = (
        (
            "financial_timeline",
            "Major financial events and transitions",
            (
                "financial_timeline_events",
                "income_source_continuity",
                "spending_event_clusters",
                "private_discretionary_patterns",
                "realistic_trim_levers",
            ),
            _merge_summaries(by_metric, ("financial_timeline_events", "income_source_continuity", "realistic_trim_levers")),
        ),
        (
            "structural_surplus",
            "Structural surplus after refunds and transfer exclusions",
            (
                "income_series",
                "monthly_spend_series",
                "refund_adjusted_spend",
                "transfer_payment_excluded_spend",
                "savings_rate_trend",
                "one_off_large_purchase",
            ),
            _structural_surplus_summary(by_metric),
        ),
        (
            "fixed_obligation_load",
            "Fixed obligation load and floor burn",
            (
                "fixed_obligation_ratio",
                "floor_burn",
                "fixed_vs_flexible_pressure",
                "recurring_obligation_calendar",
                "bill_stack_before_income",
            ),
            _merge_summaries(by_metric, ("fixed_obligation_ratio", "floor_burn", "fixed_vs_flexible_pressure")),
        ),
        (
            "liquidity_runway",
            "Liquid cash runway and low-cash points",
            (
                "cash_balance_series",
                "cash_runway",
                "cash_low_point",
                "buffer_target_gap",
                "upcoming_obligation_coverage",
            ),
            _merge_summaries(by_metric, ("cash_runway", "cash_low_point", "buffer_target_gap")),
        ),
        (
            "income_stability",
            "Income cadence and volatility",
            (
                "income_cadence",
                "income_volatility",
                "paycheck_gap",
                "income_missing_or_late",
                "income_source_concentration",
                "income_source_continuity",
            ),
            _merge_summaries(by_metric, ("income_cadence", "income_volatility", "paycheck_gap", "income_source_concentration", "income_source_continuity")),
        ),
        (
            "timing_stress",
            "Cash-flow timing stress",
            (
                "payday_window_spending",
                "day_of_month_cluster",
                "weekly_burn_rate",
                "first_half_second_half_pacing",
                "bill_stack_before_income",
            ),
            _merge_summaries(by_metric, ("payday_window_spending", "day_of_month_cluster", "bill_stack_before_income")),
        ),
        (
            "debt_pressure",
            "Debt pressure and direction",
            (
                "debt_balance_trend",
                "debt_payment_pressure",
                "credit_utilization",
                "minimum_payment_risk",
                "interest_or_fee_signal",
                "liability_to_cash_ratio",
                "cash_vs_liability_position",
            ),
            _merge_summaries(by_metric, ("liability_to_cash_ratio", "debt_payment_pressure", "cash_vs_liability_position")),
        ),
        (
            "savings_goals",
            "Savings velocity and goal feasibility",
            (
                "savings_rate_trend",
                "goal_feasibility",
                "goal_velocity",
                "required_contribution_vs_recent_behavior",
                "goal_slip_driver",
                "smallest_goal_rescue_lever",
                "budget_variance",
                "safe_to_spend_status",
            ),
            _merge_summaries(by_metric, ("savings_rate_trend", "goal_feasibility", "safe_to_spend_status")),
        ),
        (
            "recurring_commitments",
            "Recurring monthly, irregular, and changed commitments",
            (
                "recurring_obligation_calendar",
                "subscription_cluster",
                "new_or_changed_recurring",
                "recurrence_confidence",
            ),
            _merge_summaries(by_metric, ("recurring_obligation_calendar", "subscription_cluster", "recurrence_confidence")),
        ),
        (
            "spending_drift",
            "Spending drift by category and merchant",
            (
                "category_driver_decomposition",
                "merchant_driver_decomposition",
                "category_trend",
                "merchant_trend",
                "category_substitution",
            ),
            _merge_summaries(by_metric, ("category_driver_decomposition", "merchant_driver_decomposition")),
        ),
        (
            "frequency_ticket",
            "Frequency versus ticket-size changes",
            ("frequency_vs_ticket_size", "small_frequent_leak", "convenience_pattern"),
            _merge_summaries(by_metric, ("frequency_vs_ticket_size", "small_frequent_leak")),
        ),
        (
            "noise_false_alarms",
            "One-off noise versus repeated pressure",
            (
                "one_off_large_purchase",
                "refund_or_transfer_noise",
                "category_false_alarm",
                "merchant_false_alarm",
                "seasonality_or_sparse_data",
            ),
            _merge_summaries(by_metric, ("one_off_large_purchase", "refund_or_transfer_noise")),
        ),
        (
            "confidence_data",
            "Confidence, missing data, and caveats",
            (
                "data_quality_caveats",
                "enrichment_confidence_summary",
                "missing_account_or_date_coverage",
                "category_mapping_uncertainty",
                "profile_scope_caveat",
            ),
            _merge_summaries(by_metric, ("data_quality_caveats", "enrichment_confidence_summary")),
        ),
    )
    sections = []
    for key, label, metrics, summary in section_specs:
        items = [by_metric[metric] for metric in metrics if metric in by_metric]
        if not items:
            continue
        sections.append(_portrait_section(key, label, items, summary))
    return sections


def _portrait_section(key: str, label: str, measurements: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    evidence_ids = _dedupe(
        [
            value
            for item in measurements
            for value in [f"metric:{item.get('metric')}:summary", *(item.get("evidence_ids") or [])]
            if value
        ]
    )
    rows = {
        str(item.get("metric")): (item.get("rows") or [])[:5]
        for item in measurements
        if item.get("rows")
    }
    caveats = _dedupe([caveat for item in measurements for caveat in (item.get("caveats") or []) if caveat])
    empty_count = sum(1 for item in measurements if not item.get("rows"))
    return {
        "key": key,
        "label": label,
        "status": "caveated" if empty_count or caveats else "measured",
        "metrics": [item.get("metric") for item in measurements],
        "summary_numbers": _scalar_summary(summary),
        "rows": rows,
        "confidence": _combine_confidence([item.get("confidence") for item in measurements]),
        "caveats": caveats[:8],
        "evidence_ids": evidence_ids[:40],
    }


def _merge_summaries(by_metric: dict[str, dict[str, Any]], metrics: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric in metrics:
        summary = by_metric.get(metric, {}).get("summary_numbers") or {}
        for key, value in summary.items():
            out[f"{metric}.{key}"] = value
    return out


def _structural_surplus_summary(by_metric: dict[str, dict[str, Any]]) -> dict[str, Any]:
    income_rows = by_metric.get("income_series", {}).get("rows") or []
    spend_rows = by_metric.get("monthly_spend_series", {}).get("rows") or []
    savings_rows = by_metric.get("savings_rate_trend", {}).get("rows") or []
    spend_by_month = {str(row.get("month")): _num(row.get("expenses")) for row in spend_rows}
    income_by_month = {str(row.get("month")): _num(row.get("income")) for row in income_rows}
    shared = [month for month in income_by_month if month in spend_by_month]
    avg_income = _avg([income_by_month[month] for month in shared])
    avg_expenses = _avg([spend_by_month[month] for month in shared])
    summary = {
        "shared_months": len(shared),
        "avg_income": avg_income,
        "avg_expenses": avg_expenses,
        "avg_structural_surplus": _round(avg_income - avg_expenses) if shared else None,
        "avg_savings_rate": _avg([row.get("savings_rate") for row in savings_rows if row.get("savings_rate") is not None]),
    }
    summary.update(_merge_summaries(by_metric, ("refund_adjusted_spend", "transfer_payment_excluded_spend")))
    return summary


def resolve_time_range(token: Any, *, as_of: date | datetime | str | None = None) -> ResolvedRange:
    today = _coerce_date(as_of) or date.today()
    text = _clean_range_token(token or "last_6_months")
    if text == "all_time":
        return ResolvedRange(text, None, None, "all time", all_time=True)
    if text == "current_month":
        start = today.replace(day=1)
        return ResolvedRange(text, start.isoformat(), today.isoformat(), today.strftime("%Y-%m"))
    if text == "last_month":
        y, m = _shift_month(today.year, today.month, -1)
        start = date(y, m, 1)
        return ResolvedRange(text, start.isoformat(), _month_end(start).isoformat(), start.strftime("%Y-%m"))
    if text in {"last_3_months", "last_6_months", "last_12_months"}:
        months = int(text.split("_")[1])
        y, m = _shift_month(today.year, today.month, -(months - 1))
        start = date(y, m, 1)
        return ResolvedRange(text, start.isoformat(), today.isoformat(), f"last {months} months")
    if text in {"ytd", "current_year"}:
        start = date(today.year, 1, 1)
        return ResolvedRange(text, start.isoformat(), today.isoformat(), f"{today.year} YTD")
    if _MONTH_RE.match(text):
        y, m = [int(v) for v in text.split("-")]
        start = date(y, m, 1)
        return ResolvedRange(text, start.isoformat(), _month_end(start).isoformat(), text)
    if ".." in text:
        start_text, end_text = text.split("..", 1)
        if _DATE_RE.match(start_text) and _DATE_RE.match(end_text):
            start = date.fromisoformat(start_text)
            end = min(date.fromisoformat(end_text), today)
            if start <= end:
                return ResolvedRange(text, start.isoformat(), end.isoformat(), f"{start.isoformat()} to {end.isoformat()}")
    return ResolvedRange(text, None, None, "")


def _handler_for_metric(metric: str) -> Callable[[Any, str, dict[str, Any]], dict[str, Any]]:
    if metric in {"income_series", "income_volatility", "savings_rate_trend"}:
        return _monthly_flow_handler
    if metric in {"income_cadence", "paycheck_gap", "income_missing_or_late", "income_source_concentration", "income_source_continuity"}:
        return _income_profile_handler
    if metric in {"monthly_spend_series", "refund_adjusted_spend", "transfer_payment_excluded_spend"}:
        return _monthly_flow_handler
    if metric == "money_flow_baseline":
        return _money_flow_baseline_handler
    if metric in {
        "advisor_period_reliability",
        "cash_flow_compression",
        "category_advisor_ledger",
        "merchant_lifecycle",
        "external_transfer_pressure",
        "savings_scenarios",
        "advisor_data_quality_profile",
    }:
        return _advisor_private_baseline_handler
    if metric in {"category_trend", "category_driver_decomposition", "category_false_alarm", "materiality_filter", "category_substitution"}:
        return _category_driver_handler
    if metric in {"merchant_trend", "merchant_driver_decomposition", "merchant_false_alarm", "merchant_stickiness", "habit_stability_or_churn"}:
        return _merchant_driver_handler
    if metric in {"frequency_vs_ticket_size", "small_frequent_leak", "convenience_pattern"}:
        return _frequency_ticket_handler
    if metric in {"essential_vs_discretionary", "fixed_vs_flexible_pressure"}:
        return _classification_split_handler
    if metric in {"first_half_second_half_pacing", "weekend_weekday_split", "day_of_month_cluster", "weekly_burn_rate", "payday_window_spending", "payday_drift", "weekend_pressure", "month_to_date_pace_vs_baseline", "remaining_month_required_pace"}:
        return _timing_handler
    if metric in {"cash_balance_series", "cash_runway", "cash_low_point", "next_income_gap_coverage", "upcoming_obligation_coverage", "buffer_target_gap", "resilience_trend"}:
        return _cash_resilience_handler
    if metric in {"recurring_obligation_calendar", "subscription_cluster", "new_or_changed_recurring", "cancelled_or_inactive_recurring", "fixed_obligation_ratio", "floor_burn", "bill_stack_before_income", "subscription_creep", "recurrence_confidence"}:
        return _recurring_handler
    if metric in {"budget_variance", "budget_pace", "safe_to_spend_status", "safe_to_spend_required_adjustment", "category_budget_pressure", "plan_gap_to_month_end"}:
        return _budget_handler
    if metric in {"monthly_operating_statement", "goal_capacity_statement"}:
        return _operating_capacity_handler
    if metric in {"goal_feasibility", "goal_velocity", "required_contribution_vs_recent_behavior", "goal_slip_driver", "smallest_goal_rescue_lever"}:
        return _goal_handler
    if metric == "avoidable_leakage":
        return _avoidable_leakage_handler
    if metric in {"debt_balance_trend", "debt_payment_pressure", "credit_utilization", "minimum_payment_risk", "interest_or_fee_signal", "liability_to_cash_ratio"}:
        return _debt_handler
    if metric in {"net_worth_series", "net_worth_driver_split", "account_balance_trend", "cash_vs_liability_position", "idle_cash_signal", "account_coverage_caveats"}:
        return _net_worth_handler
    if metric == "spending_event_clusters":
        return _spending_event_cluster_handler
    if metric == "private_discretionary_patterns":
        return _private_discretionary_handler
    if metric == "realistic_trim_levers":
        return _realistic_trim_levers_handler
    if metric == "financial_timeline_events":
        return _financial_timeline_events_handler
    if metric in {"unusual_transactions", "one_off_large_purchase", "refund_or_transfer_noise", "seasonality_or_sparse_data"}:
        return _anomaly_handler
    if metric in {"data_quality_caveats", "enrichment_confidence_summary", "low_confidence_driver_rows", "missing_account_or_date_coverage", "category_mapping_uncertainty", "profile_scope_caveat"}:
        return _quality_handler
    return _unsupported_metric_handler


_HANDLERS: dict[str, Callable[[Any, str, dict[str, Any]], dict[str, Any]]] = {}


def _monthly_flow_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT substr(date, 1, 7) AS month,
               ROUND(COALESCE(SUM(CASE WHEN category = 'Income' AND amount > 0 THEN amount ELSE 0 END), 0), 2) AS income,
               ROUND(COALESCE(SUM(CASE WHEN {_expense_sql()} THEN ABS(amount) ELSE 0 END), 0), 2) AS expenses,
               ROUND(COALESCE(SUM(CASE WHEN amount > 0 AND category != 'Income' THEN amount ELSE 0 END), 0), 2) AS refunds,
               ROUND(COALESCE(SUM(CASE WHEN category = 'Savings Transfer' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0), 2) AS savings,
               SUM(CASE WHEN category = 'Income' AND amount > 0 THEN 1 ELSE 0 END) AS income_count,
               SUM(CASE WHEN {_expense_sql()} THEN 1 ELSE 0 END) AS expense_count
          FROM transactions_visible
         WHERE profile_id = ?{_range_clause(ctx['range'])}
         GROUP BY substr(date, 1, 7)
         ORDER BY month
         LIMIT ?
        """,
        [_profile_scope(ctx["profile"]), *_range_params(ctx["range"]), ctx["limit"]],
    )
    for row in rows:
        income = _num(row.get("income"))
        expenses = _num(row.get("expenses"))
        row["net"] = _round(income - expenses)
        row["savings_rate"] = _round((income - expenses) / income, 4) if income else None
        row["net_spend_after_refunds"] = _round(expenses - _num(row.get("refunds")))
    if metric == "income_volatility":
        incomes = [_num(r.get("income")) for r in rows if _num(r.get("income")) > 0]
        summary = _series_summary(incomes)
        rows = [{"month": r["month"], "income": r["income"], "income_count": r["income_count"]} for r in rows]
    elif metric == "income_series":
        summary = {"total_income": _round(sum(_num(r.get("income")) for r in rows)), "months": len(rows)}
        rows = [{"month": r["month"], "income": r["income"], "income_count": r["income_count"]} for r in rows]
    elif metric == "savings_rate_trend":
        summary = {"avg_savings_rate": _avg([r.get("savings_rate") for r in rows if r.get("savings_rate") is not None]), "months": len(rows)}
        rows = [{"month": r["month"], "income": r["income"], "expenses": r["expenses"], "net": r["net"], "savings_rate": r["savings_rate"]} for r in rows]
    elif metric == "refund_adjusted_spend":
        summary = {"gross_expenses": _round(sum(_num(r.get("expenses")) for r in rows)), "refunds": _round(sum(_num(r.get("refunds")) for r in rows))}
        rows = [{"month": r["month"], "gross_expenses": r["expenses"], "refunds": r["refunds"], "net_spend_after_refunds": r["net_spend_after_refunds"]} for r in rows]
    elif metric == "transfer_payment_excluded_spend":
        included = _included_vs_excluded(conn, ctx)
        rows = included["rows"]
        summary = included["summary_numbers"]
    else:
        summary = {"total_expenses": _round(sum(_num(r.get("expenses")) for r in rows)), "months": len(rows)}
        rows = [{"month": r["month"], "expenses": r["expenses"], "expense_count": r["expense_count"]} for r in rows]
    return _result_for_rows(metric, ctx, rows, summary, "posted transactions grouped by month; transfers/payment categories excluded from spend")


def _money_flow_baseline_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    expenses = _fetch_all(
        conn,
        f"""
        SELECT id,
               date,
               COALESCE(NULLIF(category, ''), 'Uncategorized') AS category,
               COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, ''), category, 'Merchant') AS merchant,
               ROUND(ABS(amount), 2) AS amount
          FROM transactions_visible
         WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}
         ORDER BY date ASC
        """,
        [ctx["profile"], *_range_params(ctx["range"])],
    )
    event_metric = execute_metric(conn, {"metric": "spending_event_clusters", "range": ctx["range"].token, "limit": 8}, profile=ctx["profile"])
    event_windows = _event_windows(event_metric.get("rows") or [])

    normal_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    skipped_uncategorized = 0
    for row in expenses:
        category = str(row.get("category") or "").strip()
        if not category or category == "Uncategorized":
            skipped_uncategorized += 1
            continue
        parsed = _parse_date(row.get("date"))
        row = {**row, "month": str(row.get("date") or "")[:7]}
        if parsed and _row_in_event_window(parsed, category, event_windows):
            event_rows.append(row)
        else:
            normal_rows.append(row)

    observed_months = sorted({str(row.get("date") or "")[:7] for row in expenses if _MONTH_RE.match(str(row.get("date") or "")[:7])})
    baseline_months = _complete_month_keys(ctx["range"], observed_months)
    baseline_rows = [row for row in normal_rows if row.get("month") in baseline_months] or normal_rows
    baseline_month_count = max(1, len({row.get("month") for row in baseline_rows if row.get("month")}))

    income_rows = _fetch_all(
        conn,
        f"""
        SELECT substr(date, 1, 7) AS month,
               ROUND(SUM(amount), 2) AS income
          FROM transactions_visible
         WHERE profile_id = ? AND category = 'Income' AND amount > 0{_range_clause(ctx['range'])}
         GROUP BY month
         ORDER BY month
        """,
        [ctx["profile"], *_range_params(ctx["range"])],
    )
    income_baseline = [row for row in income_rows if row.get("month") in baseline_months] or income_rows

    by_category: dict[str, dict[str, Any]] = {}
    merchants_by_category: dict[str, dict[str, dict[str, Any]]] = {}
    for row in baseline_rows:
        category = str(row.get("category") or "")
        item = by_category.setdefault(
            category,
            {
                "category": category,
                "normal_total": 0.0,
                "transaction_count": 0,
                "months": set(),
                "sample_evidence_ids": [],
            },
        )
        amount = _num(row.get("amount"))
        item["normal_total"] = _round(_num(item.get("normal_total")) + amount)
        item["transaction_count"] += 1
        item["months"].add(row.get("month"))
        if row.get("id") and len(item["sample_evidence_ids"]) < 8:
            item["sample_evidence_ids"].append(f"txn:{row.get('id')}")

        merchant = str(row.get("merchant") or category)
        merchant_item = merchants_by_category.setdefault(category, {}).setdefault(
            merchant,
            {"merchant": merchant, "total": 0.0, "count": 0},
        )
        merchant_item["total"] = _round(_num(merchant_item.get("total")) + amount)
        merchant_item["count"] += 1

    current_month = _ctx_end_date(ctx).strftime("%Y-%m")
    current_totals: dict[str, float] = {}
    for row in normal_rows:
        if row.get("month") == current_month:
            category = str(row.get("category") or "")
            current_totals[category] = _round(current_totals.get(category, 0.0) + _num(row.get("amount")))

    normal_total = _round(sum(_num(row.get("amount")) for row in baseline_rows))
    raw_baseline_total = _round(
        sum(_num(row.get("amount")) for row in expenses if row.get("date") and str(row.get("date"))[:7] in baseline_months)
    )
    avg_spend_after_events = _round(normal_total / baseline_month_count) if baseline_month_count else 0.0
    avg_spend_before_events = _round(raw_baseline_total / baseline_month_count) if baseline_month_count else 0.0

    rows: list[dict[str, Any]] = []
    for category, item in by_category.items():
        monthly_average = _round(_num(item.get("normal_total")) / baseline_month_count)
        merchants = sorted(merchants_by_category.get(category, {}).values(), key=lambda row: _num(row.get("total")), reverse=True)[:5]
        current_total = current_totals.get(category, 0.0)
        rows.append(
            {
                "category": category,
                "spend_role": _money_map_role(category),
                "controllability": _money_map_controllability(category),
                "monthly_average": monthly_average,
                "normal_total": _round(item.get("normal_total")),
                "share_of_normal_spend": _round(monthly_average / avg_spend_after_events, 4) if avg_spend_after_events else None,
                "transaction_count": item.get("transaction_count"),
                "active_month_count": len(item.get("months") or []),
                "current_month": current_month,
                "current_month_total": current_total,
                "current_delta_vs_baseline": _round(current_total - monthly_average),
                "top_merchants": merchants,
                "sample_evidence_ids": item.get("sample_evidence_ids") or [],
            }
        )

    rows = sorted(rows, key=lambda row: _num(row.get("monthly_average")), reverse=True)[: ctx["limit"]]
    visible_flexible_monthly = _round(
        sum(
            _num(row.get("monthly_average"))
            for row in rows
            if row.get("spend_role") in {"flexible_living", "private_discretionary", "event_or_irregular"}
        )
    )
    reviewable_monthly = _round(
        sum(
            _num(row.get("monthly_average"))
            for row in rows
            if row.get("spend_role") in {"recurring_or_vendor_review", "avoidable_leakage"}
        )
    )
    floor = execute_metric(conn, {"metric": "floor_burn", "range": ctx["range"].token, "limit": 8}, profile=ctx["profile"])
    fixed_floor = _num((floor.get("summary_numbers") or {}).get("floor_burn_monthly"))
    event_excluded_total = _round(sum(_num(row.get("amount")) for row in event_rows))
    current_month_event_excluded_total = _round(
        sum(_num(row.get("amount")) for row in event_rows if row.get("month") == current_month)
    )
    summary = {
        "avg_monthly_income": _avg([row.get("income") for row in income_baseline]),
        "avg_monthly_spend_before_event_exclusions": avg_spend_before_events,
        "avg_monthly_spend_after_event_exclusions": avg_spend_after_events,
        "event_excluded_total": event_excluded_total,
        "event_excluded_total_in_range": event_excluded_total,
        "current_month_event_excluded_total": current_month_event_excluded_total,
        "fixed_floor_monthly": _round(fixed_floor),
        "flexible_monthly_estimate": visible_flexible_monthly,
        "reviewable_monthly_estimate": reviewable_monthly,
        "baseline_month_count": baseline_month_count,
        "category_count": len(rows),
        "top_category": rows[0].get("category") if rows else None,
        "top_category_monthly_average": rows[0].get("monthly_average") if rows else 0.0,
        "current_month": current_month,
        "current_month_spend_after_event_exclusions": _round(sum(_num(v) for v in current_totals.values())),
    }
    caveats = ["Travel/event exclusions are measured estimates until user-confirmed."]
    if _ctx_end_date(ctx) < _month_end(_ctx_end_date(ctx).replace(day=1)):
        caveats.append("Current-month totals may be partial.")
    if skipped_uncategorized:
        caveats.append("Rows without usable category labels were excluded from the category map.")
    return _result_for_rows(
        metric,
        ctx,
        rows,
        summary,
        "posted expenses grouped by category and merchant; transfer/payment categories excluded; travel/event windows removed from the normal baseline",
        caveats=caveats,
    )


def _income_profile_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT date,
               COALESCE(NULLIF(merchant_name,''), NULLIF(counterparty_name,''), 'Income') AS source,
               ROUND(amount, 2) AS amount,
               id
          FROM transactions_visible
         WHERE profile_id = ? AND category = 'Income' AND amount > 0{_range_clause(ctx['range'])}
         ORDER BY date
         LIMIT ?
        """,
        [_profile_scope(ctx["profile"]), *_range_params(ctx["range"]), min(200, ctx["limit"] * 12)],
    )
    dates = [_parse_date(r.get("date")) for r in rows if _parse_date(r.get("date"))]
    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = str(row.get("source") or "Income")
        item = by_source.setdefault(source, {"source": source, "amount": 0.0, "count": 0})
        item["amount"] = _round(item["amount"] + _num(row.get("amount")))
        item["count"] += 1
    source_rows = sorted(by_source.values(), key=lambda r: r["amount"], reverse=True)[: ctx["limit"]]
    total = sum(_num(r.get("amount")) for r in source_rows)
    if metric == "income_source_continuity":
        return _income_source_continuity_result(metric, ctx, rows, dates, gaps, source_rows)
    if metric == "income_source_concentration":
        for row in source_rows:
            row["share"] = _round(_num(row["amount"]) / total, 4) if total else None
        summary = {"total_income": _round(total), "top_source_share": source_rows[0].get("share") if source_rows else None, "source_count": len(source_rows)}
        out_rows = source_rows
    else:
        today = _ctx_end_date(ctx)
        late_days = (today - dates[-1]).days if dates else None
        median_gap = statistics.median(gaps) if gaps else None
        out_rows = [{"date": r["date"], "source": r["source"], "amount": r["amount"]} for r in rows[-ctx["limit"] :]]
        summary = {
            "deposit_count": len(rows),
            "median_gap_days": median_gap,
            "max_gap_days": max(gaps) if gaps else None,
            "days_since_last_income": late_days,
            "late_against_cadence": bool(median_gap and late_days is not None and late_days > median_gap + 4),
        }
    return _result_for_rows(metric, ctx, out_rows, summary, "income transactions only; cadence inferred from posted dates")


def _income_source_continuity_result(metric: str, ctx: dict[str, Any], rows: list[dict[str, Any]], dates: list[date], gaps: list[int], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    material_rows = [row for row in rows if _num(row.get("amount")) >= 100.0]
    monthly: dict[str, dict[str, Any]] = {}
    for row in material_rows:
        month = str(row.get("date") or "")[:7]
        if not month:
            continue
        item = monthly.setdefault(month, {"month": month, "total_income": 0.0, "deposit_count": 0, "sources": {}})
        item["total_income"] = _round(item["total_income"] + _num(row.get("amount")))
        item["deposit_count"] += 1
        source = str(row.get("source") or "Income")
        item["sources"][source] = _round(item["sources"].get(source, 0.0) + _num(row.get("amount")))

    monthly_rows = []
    for item in sorted(monthly.values(), key=lambda r: str(r.get("month") or "")):
        sources = item.get("sources") or {}
        top_source, top_amount = max(sources.items(), key=lambda pair: pair[1]) if sources else ("", 0.0)
        monthly_rows.append(
            {
                "month": item["month"],
                "total_income": item["total_income"],
                "deposit_count": item["deposit_count"],
                "top_source": top_source,
                "top_source_amount": _round(top_amount),
                "source_count": len(sources),
            }
        )

    today = _ctx_end_date(ctx)
    current_month = today.strftime("%Y-%m")
    current_month_income = next((row for row in monthly_rows if row.get("month") == current_month), None)
    completed_rows = [row for row in monthly_rows if row.get("month") != current_month]
    recent_completed = completed_rows[-3:]
    prior_completed = completed_rows[:-3]
    dominant_prior_source = _dominant_source(prior_completed)
    if not dominant_prior_source and len(completed_rows) >= 3:
        dominant_prior_source = _dominant_source(completed_rows[:-1])
    recent_source = _dominant_source(recent_completed)
    latest_complete = completed_rows[-1] if completed_rows else None
    latest_source = latest_complete.get("top_source") if latest_complete else None
    late_days = (today - dates[-1]).days if dates else None
    median_gap = statistics.median(gaps) if gaps else None
    late_against_cadence = bool(median_gap and late_days is not None and late_days > median_gap + 4)

    status = "unknown"
    reason = "Not enough material income history."
    if latest_source and dominant_prior_source and latest_source != dominant_prior_source:
        status = "changed_source"
        reason = "Latest complete month source differs from the earlier dominant source."
    elif recent_source and dominant_prior_source and recent_source != dominant_prior_source:
        status = "changed_source"
        reason = "Recent income sources differ from the earlier dominant source."
    elif current_month_income is None and today.day < 20:
        status = "incomplete_current_month"
        reason = "Current month has no material income yet, but the month is still incomplete."
    elif late_against_cadence:
        status = "late_against_cadence"
        reason = "Days since last income is above the observed median cadence plus buffer."
    elif latest_source:
        status = "stable_or_recovered"
        reason = "Recent material income is present and no source change was detected."

    if latest_source == "Income" and status in {"changed_source", "stable_or_recovered"}:
        status = "unlabeled_or_changed_source"
        reason = "Latest material income source is unlabeled, so continuity is uncertain."

    out_rows = [
        {
            "status": status,
            "reason": reason,
            "dominant_prior_source": dominant_prior_source,
            "recent_source": recent_source,
            "latest_complete_month": latest_complete.get("month") if latest_complete else None,
            "latest_complete_source": latest_source,
            "current_month": current_month,
            "current_month_income": current_month_income.get("total_income") if current_month_income else 0.0,
            "current_month_is_partial": today.day < _month_end(today.replace(day=1)).day,
            "material_income_threshold": 100.0,
        },
        *monthly_rows[-8:],
    ]
    summary = {
        "status": status,
        "material_month_count": len(monthly_rows),
        "dominant_prior_source": dominant_prior_source,
        "recent_source": recent_source,
        "latest_complete_source": latest_source,
        "current_month_income": current_month_income.get("total_income") if current_month_income else 0.0,
        "days_since_last_income": late_days,
        "median_gap_days": median_gap,
        "late_against_cadence": late_against_cadence,
    }
    caveats = []
    if any(row.get("top_source") == "Income" for row in monthly_rows[-3:]):
        caveats.append("Recent material income contains unlabeled source rows.")
    if current_month_income is None:
        caveats.append("Current month has no material income in the selected snapshot.")
    return _result_for_rows(metric, ctx, out_rows, summary, "material income rows grouped by month and source; tiny interest-like rows excluded", caveats=caveats)


def _dominant_source(rows: list[dict[str, Any]]) -> str | None:
    totals: dict[str, float] = {}
    for row in rows:
        source = str(row.get("top_source") or "")
        if not source:
            continue
        totals[source] = _round(totals.get(source, 0.0) + _num(row.get("top_source_amount")))
    if not totals:
        return None
    return max(totals.items(), key=lambda pair: pair[1])[0]


def _category_driver_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _dimension_monthly(conn, ctx, "category")
    driver_rows = _driver_rows(rows, "category")
    if metric in {"category_false_alarm", "materiality_filter"}:
        total_delta = sum(max(0.0, _num(r.get("delta_vs_baseline"))) for r in driver_rows) or 1.0
        out = []
        for row in driver_rows:
            share = max(0.0, _num(row.get("delta_vs_baseline"))) / total_delta
            if metric == "category_false_alarm" and (share >= 0.15 or abs(_num(row.get("delta_vs_baseline"))) >= 100):
                continue
            row = {**row, "driver_share": _round(share, 4), "false_alarm_reason": "low materiality relative to total positive change"}
            out.append(row)
        driver_rows = out[: ctx["limit"]]
    elif metric == "category_substitution":
        ups = [r for r in driver_rows if _num(r.get("delta_vs_baseline")) > 25]
        downs = [r for r in driver_rows if _num(r.get("delta_vs_baseline")) < -25]
        driver_rows = [{"rising_category": u["category"], "falling_category": d["category"], "rise": u["delta_vs_baseline"], "fall": d["delta_vs_baseline"]} for u, d in zip(ups, downs)][: ctx["limit"]]
    else:
        driver_rows = sorted(driver_rows, key=lambda r: (_num(r.get("delta_vs_baseline")) <= 0, -abs(_num(r.get("delta_vs_baseline")))))
    summary = _driver_summary(driver_rows)
    return _result_for_rows(metric, ctx, driver_rows[: ctx["limit"]], summary, "category spend vs prior-month baseline; transfers/payment categories excluded")


def _merchant_driver_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _dimension_monthly(conn, ctx, "merchant")
    driver_rows = _driver_rows(rows, "merchant")
    if metric == "merchant_false_alarm":
        total_delta = sum(max(0.0, _num(r.get("delta_vs_baseline"))) for r in driver_rows) or 1.0
        driver_rows = [
            {**r, "driver_share": _round(max(0.0, _num(r.get("delta_vs_baseline"))) / total_delta, 4), "false_alarm_reason": "merchant change is not a material driver"}
            for r in driver_rows
            if max(0.0, _num(r.get("delta_vs_baseline"))) / total_delta < 0.15
        ][: ctx["limit"]]
    if metric in {"merchant_stickiness", "habit_stability_or_churn"}:
        driver_rows = sorted(driver_rows, key=lambda r: (_num(r.get("current_count")), _num(r.get("current_total"))), reverse=True)[: ctx["limit"]]
    elif metric in {"merchant_driver_decomposition", "merchant_trend"}:
        driver_rows = sorted(driver_rows, key=lambda r: (_num(r.get("delta_vs_baseline")) <= 0, -abs(_num(r.get("delta_vs_baseline")))))
    summary = _driver_summary(driver_rows)
    return _result_for_rows(metric, ctx, driver_rows[: ctx["limit"]], summary, "merchant spend vs prior-month baseline; transfers/payment categories excluded")


def _frequency_ticket_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT COALESCE(NULLIF(merchant_name,''), NULLIF(merchant_key,''), category, 'Unknown') AS merchant,
               COALESCE(category, 'Unknown') AS category,
               COUNT(*) AS count,
               ROUND(SUM(ABS(amount)), 2) AS total,
               ROUND(AVG(ABS(amount)), 2) AS avg_ticket,
               SUM(CASE WHEN ABS(amount) <= 25 THEN 1 ELSE 0 END) AS small_count,
               ROUND(SUM(CASE WHEN ABS(amount) <= 25 THEN ABS(amount) ELSE 0 END), 2) AS small_total,
               GROUP_CONCAT(id) AS ids
          FROM transactions_visible
         WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY merchant, category
         HAVING total > 0
         ORDER BY small_total DESC, count DESC, total DESC
         LIMIT ?
        """,
        [_profile_scope(ctx["profile"]), *_range_params(ctx["range"]), ctx["limit"]],
    )
    if metric == "small_frequent_leak":
        rows = [r for r in rows if _num(r.get("small_count")) >= 3 and _num(r.get("small_total")) >= 25]
    if metric == "convenience_pattern":
        rows = [r for r in rows if _num(r.get("count")) >= 3]
    rows = [_with_sample_ids(r) for r in rows[: ctx["limit"]]]
    summary = {"row_count": len(rows), "top_total": rows[0].get("total") if rows else 0, "top_count": rows[0].get("count") if rows else 0}
    return _result_for_rows(metric, ctx, rows, summary, "transaction frequency and average-ticket analysis from posted expenses")


def _classification_split_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    if metric == "essential_vs_discretionary":
        rows = _fetch_all(
            conn,
            f"""
            SELECT COALESCE(NULLIF(e.essentiality,''), 'unknown') AS essentiality,
                   COUNT(*) AS count,
                   ROUND(SUM(ABS(t.amount)), 2) AS total
              FROM transactions_visible t
              LEFT JOIN transaction_enrichment e ON e.transaction_id = t.id AND e.profile_id = t.profile_id
             WHERE t.profile_id = ? AND {_expense_sql('t')}{_range_clause(ctx['range'], 't')}
             GROUP BY essentiality
             ORDER BY total DESC
            """,
            [_profile_scope(ctx["profile"]), *_range_params(ctx["range"])],
        )
        basis = "transaction_enrichment.essentiality joined to expense transactions"
    else:
        rows = _fetch_all(
            conn,
            f"""
            SELECT COALESCE(c.expense_type, t.expense_type, 'variable') AS pressure_type,
                   COUNT(*) AS count,
                   ROUND(SUM(ABS(t.amount)), 2) AS total
              FROM transactions_visible t
              LEFT JOIN categories c ON c.name = t.category
             WHERE t.profile_id = ? AND {_expense_sql('t')}{_range_clause(ctx['range'], 't')}
             GROUP BY pressure_type
             ORDER BY total DESC
            """,
            [_profile_scope(ctx["profile"]), *_range_params(ctx["range"])],
        )
        basis = "category expense_type metadata joined to posted expenses"
    total = sum(_num(r.get("total")) for r in rows) or 1.0
    for row in rows:
        row["share"] = _round(_num(row.get("total")) / total, 4)
    return _result_for_rows(metric, ctx, rows[: ctx["limit"]], {"total": _round(total)}, basis)


def _timing_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    if metric in {"payday_window_spending", "payday_drift"}:
        return _payday_window(conn, metric, ctx)
    if metric in {"weekend_weekday_split", "weekend_pressure"}:
        expr = "CASE WHEN strftime('%w', date) IN ('0','6') THEN 'weekend' ELSE 'weekday' END"
    elif metric == "day_of_month_cluster":
        expr = "CASE WHEN CAST(strftime('%d', date) AS INTEGER) <= 7 THEN 'days_1_7' WHEN CAST(strftime('%d', date) AS INTEGER) <= 15 THEN 'days_8_15' WHEN CAST(strftime('%d', date) AS INTEGER) <= 23 THEN 'days_16_23' ELSE 'days_24_end' END"
    elif metric == "weekly_burn_rate":
        expr = "strftime('%Y-W%W', date)"
    elif metric == "first_half_second_half_pacing":
        expr = "CASE WHEN CAST(strftime('%d', date) AS INTEGER) <= 15 THEN 'first_half' ELSE 'second_half' END"
    elif metric in {"month_to_date_pace_vs_baseline", "remaining_month_required_pace"}:
        return _mtd_pace(conn, metric, ctx)
    else:
        expr = "substr(date, 1, 7)"
    rows = _fetch_all(
        conn,
        f"""
        SELECT {expr} AS bucket,
               COUNT(*) AS count,
               ROUND(SUM(ABS(amount)), 2) AS total,
               ROUND(AVG(ABS(amount)), 2) AS avg_ticket
          FROM transactions_visible
         WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY bucket
         ORDER BY total DESC
         LIMIT ?
        """,
        [_profile_scope(ctx["profile"]), *_range_params(ctx["range"]), ctx["limit"]],
    )
    total = sum(_num(r.get("total")) for r in rows) or 1.0
    for row in rows:
        row["share"] = _round(_num(row.get("total")) / total, 4)
    return _result_for_rows(metric, ctx, rows, {"total": _round(total), "bucket_count": len(rows)}, "posted expense timing buckets")


def _cash_resilience_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    cash = _cash_balance(conn, ctx["profile"])
    monthly_expenses = _recent_monthly_expenses(conn, ctx)
    recurring_total = _recurring_monthly_total(conn, ctx["profile"])
    runway_days = _round((cash / (monthly_expenses / 30.0)), 1) if monthly_expenses > 0 else None
    if metric == "cash_balance_series":
        rows = _account_rows(conn, ctx, cash_like=True)
        summary = {"cash_like_balance": _round(cash)}
    elif metric == "resilience_trend":
        return _net_worth_handler(conn, metric, ctx)
    else:
        rows = [
            {
                "cash_like_balance": _round(cash),
                "normal_monthly_burn": _round(monthly_expenses),
                "recurring_monthly": _round(recurring_total),
                "cash_runway_days": runway_days,
                "one_month_buffer_gap": _round(max(monthly_expenses - cash, 0)),
                "simple_30_day_low_point": _round(cash - recurring_total - monthly_expenses),
            }
        ]
        summary = rows[0].copy()
    return _result_for_rows(metric, ctx, rows, summary, "active account balances plus recent expense baseline; no live writes")


def _recurring_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    if metric == "new_or_changed_recurring":
        rows = _fetch_all(
            conn,
            """
            SELECT merchant_key, event_type, period_bucket, payload_json, created_at
              FROM recurring_events_v2
             WHERE profile_id = ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            [ctx["profile"], ctx["limit"]],
        )
        return _result_for_rows(metric, ctx, rows, {"event_count": len(rows)}, "recurring_events_v2 event log")
    states = ("active", "confirmed", "candidate") if metric != "cancelled_or_inactive_recurring" else ("cancelled", "dismissed", "inactive")
    placeholders = ",".join("?" for _ in states)
    raw_rows = _fetch_all(
        conn,
        f"""
        SELECT display_name AS merchant,
               merchant_key,
               category,
               ROUND(amount_cents / 100.0, 2) AS amount,
               frequency,
               anchor_day,
               next_expected_date,
               state,
               confidence_label,
               confidence_score
          FROM recurring_obligations
         WHERE profile_id = ? AND state IN ({placeholders})
         ORDER BY next_expected_date IS NULL, next_expected_date ASC, amount_cents DESC
         LIMIT ?
        """,
        [ctx["profile"], *states, ctx["limit"]],
    )
    rows = _dedupe_recurring_rows(raw_rows)
    dedup_row_count = len(rows)
    duplicate_row_count = max(0, len(raw_rows) - dedup_row_count)
    total_face_amount = _round(sum(_num(r.get("amount")) for r in rows if isinstance(r.get("amount"), (int, float))))
    total_monthly = _round(sum(_num(r.get("monthly_equivalent")) for r in rows if isinstance(r.get("monthly_equivalent"), (int, float))))
    if metric == "floor_burn":
        return _floor_burn_result(conn, ctx, rows, raw_row_count=len(raw_rows), duplicate_row_count=duplicate_row_count)
    if metric == "subscription_cluster":
        clusters: dict[str, dict[str, Any]] = {}
        for row in rows:
            day = _safe_int(row.get("anchor_day"), 0)
            bucket = "unknown" if day <= 0 else f"days_{((day - 1) // 7) * 7 + 1}_{min(((day - 1) // 7) * 7 + 7, 31)}"
            item = clusters.setdefault(bucket, {"bucket": bucket, "count": 0, "total": 0.0, "monthly_equivalent": 0.0, "merchants": []})
            item["count"] += 1
            item["total"] = _round(item["total"] + _num(row.get("amount")))
            item["monthly_equivalent"] = _round(item["monthly_equivalent"] + _num(row.get("monthly_equivalent")))
            item["merchants"].append(row.get("merchant"))
        rows = sorted(clusters.values(), key=lambda r: r["total"], reverse=True)
    if metric in {"fixed_obligation_ratio", "bill_stack_before_income"}:
        income = _recent_monthly_income(conn, ctx)
        rows = [{"recurring_monthly": total_monthly, "recent_monthly_income": _round(income), "recurring_to_income_ratio": _round(total_monthly / income, 4) if income else None, "item_count": dedup_row_count}]
    if metric == "subscription_creep":
        rows = [r for r in rows if r.get("state") in {"candidate", "active", "confirmed"}]
    summary = {
        "row_count": len(rows),
        "dedup_item_count": dedup_row_count,
        "raw_row_count": len(raw_rows),
        "duplicate_row_count": duplicate_row_count,
        "total_face_amount": total_face_amount,
        "total_monthly": total_monthly,
    }
    return _result_for_rows(metric, ctx, rows[: ctx["limit"]], summary, "deduped recurring_obligations read model")


def _dedupe_recurring_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        clean = dict(row)
        amount = _round(clean.get("amount"))
        clean["amount"] = amount
        clean["monthly_equivalent"] = _monthly_equivalent_amount(amount, clean.get("frequency"))
        groups.setdefault(_recurring_dedupe_key(clean), []).append(clean)

    out: list[dict[str, Any]] = []
    for group_rows in groups.values():
        ordered = sorted(group_rows, key=_recurring_preference_key, reverse=True)
        keep = dict(ordered[0])
        keep["duplicate_count"] = len(group_rows) - 1
        keep["dedupe_group_size"] = len(group_rows)
        if len(group_rows) > 1:
            keep["dedupe_note"] = "merged duplicate recurring rows with the same name, frequency, amount, and anchor"
        out.append(keep)

    return sorted(out, key=lambda r: (r.get("next_expected_date") is None, str(r.get("next_expected_date") or ""), -_num(r.get("amount"))))


def _floor_burn_result(conn, ctx: dict[str, Any], recurring_rows: list[dict[str, Any]], *, raw_row_count: int, duplicate_row_count: int) -> dict[str, Any]:
    recurring_monthly = _round(sum(_num(row.get("monthly_equivalent")) for row in recurring_rows))
    housing = _housing_floor_component(conn, ctx, recurring_rows)
    housing_monthly = _num(housing.get("monthly_amount"))
    floor_burn = _round(recurring_monthly + housing_monthly)
    rows = [
        {
            "component": "deduped_recurring_commitments",
            "monthly_amount": recurring_monthly,
            "item_count": len(recurring_rows),
            "raw_row_count": raw_row_count,
            "duplicate_row_count": duplicate_row_count,
            "basis": "deduped recurring obligations converted to monthly equivalents",
        },
        housing,
    ]
    caveats = []
    if housing.get("status") == "skipped_recurring_housing_present":
        caveats.append("Housing transactions were not added because a housing-like recurring obligation is already present.")
    elif _safe_int(housing.get("active_month_count"), 0) and _safe_int(housing.get("active_month_count"), 0) < 3:
        caveats.append("Housing floor is based on fewer than three active months.")
    elif not housing_monthly:
        caveats.append("No structural housing spend was found in the selected range.")
    caveats.append("Minimum debt payments are not included unless imported as recurring obligations or institution fields.")
    summary = {
        "floor_burn_monthly": floor_burn,
        "recurring_monthly": recurring_monthly,
        "housing_monthly": _round(housing_monthly),
        "dedup_recurring_count": len(recurring_rows),
        "raw_recurring_row_count": raw_row_count,
        "duplicate_recurring_row_count": duplicate_row_count,
    }
    return _result_for_rows("floor_burn", ctx, rows, summary, "deduped recurring commitments plus measured structural housing floor", caveats=caveats)


def _housing_floor_component(conn, ctx: dict[str, Any], recurring_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if any(str(row.get("category") or "") in STRUCTURAL_FLOOR_CATEGORIES for row in recurring_rows):
        return {
            "component": "structural_housing",
            "monthly_amount": 0.0,
            "active_month_count": 0,
            "basis": "skipped to avoid double-counting a housing-like recurring obligation",
            "status": "skipped_recurring_housing_present",
        }
    rows = _fetch_all(
        conn,
        f"""
        SELECT substr(date, 1, 7) AS month,
               ROUND(SUM(ABS(amount)), 2) AS total,
               COUNT(*) AS count,
               GROUP_CONCAT(id) AS ids
          FROM transactions_visible
         WHERE profile_id = ?
           AND category IN ({_placeholders(STRUCTURAL_FLOOR_CATEGORIES)})
           AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY month
         ORDER BY month DESC
        """,
        [ctx["profile"], *STRUCTURAL_FLOOR_CATEGORIES, *_range_params(ctx["range"])],
    )
    active = [row for row in rows if _num(row.get("total")) > 0]
    monthly = _avg([row.get("total") for row in active])
    evidence_ids: list[str] = []
    for row in active[:3]:
        evidence_ids.extend([f"txn:{value}" for value in str(row.get("ids") or "").split(",")[:5] if value])
    return {
        "component": "structural_housing",
        "monthly_amount": monthly,
        "active_month_count": len(active),
        "months": [row.get("month") for row in active[:6]],
        "basis": "average of active structural housing months in the selected range",
        "status": "measured" if active else "missing",
        "sample_evidence_ids": evidence_ids[:8],
    }


def _recurring_dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    frequency = _normalized_frequency(row.get("frequency"))
    anchor_day = _safe_int(row.get("anchor_day"), 0)
    next_date = str(row.get("next_expected_date") or "")
    anchor_month = _safe_int(next_date[5:7] if len(next_date) >= 7 else None, 0)
    return (
        _recurring_subject_key(row.get("merchant") or row.get("merchant_key")),
        frequency,
        _round(row.get("amount")),
        anchor_day,
        anchor_month,
    )


def _recurring_subject_key(value: Any) -> str:
    text = str(value or "").upper()
    return "".join(ch for ch in text if ch.isalnum()) or "UNKNOWN"


def _recurring_preference_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    confidence = _safe_int(row.get("confidence_score"), 0)
    label = str(row.get("confidence_label") or "").lower()
    state = str(row.get("state") or "").lower()
    confirmed_rank = 1 if state == "confirmed" or label == "user" else 0
    active_rank = 1 if state == "active" else 0
    has_next_date = 1 if row.get("next_expected_date") else 0
    return confirmed_rank, confidence, active_rank, has_next_date


def _monthly_equivalent_amount(amount: Any, frequency: Any) -> float:
    value = _num(amount)
    freq = _normalized_frequency(frequency)
    if freq in {"annual", "yearly"}:
        return _round(value / 12.0)
    if freq in {"semiannual", "semi_annually", "twice_yearly"}:
        return _round(value / 6.0)
    if freq == "quarterly":
        return _round(value / 3.0)
    if freq == "weekly":
        return _round(value * 52.0 / 12.0)
    if freq in {"biweekly", "every_two_weeks"}:
        return _round(value * 26.0 / 12.0)
    return _round(value)


def _normalized_frequency(value: Any) -> str:
    return str(value or "monthly").strip().lower().replace("-", "_").replace(" ", "_") or "monthly"


def _budget_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    month_range = resolve_time_range("current_month")
    spend = _category_spend_map(conn, {**ctx, "range": month_range})
    budgets = _fetch_all(conn, "SELECT category, ROUND(amount, 2) AS budget FROM category_budgets WHERE profile_id = ? ORDER BY amount DESC", [ctx["profile"]])
    rows = []
    for row in budgets:
        amount = _num(row.get("budget"))
        spent = spend.get(str(row.get("category") or ""), 0.0)
        remaining = _round(amount - spent)
        pace = _month_elapsed_ratio()
        rows.append({"category": row.get("category"), "budget": amount, "spent": spent, "remaining": remaining, "pace_ratio": pace, "spent_ratio": _round(spent / amount, 4) if amount else None})
    if metric in {"safe_to_spend_status", "plan_gap_to_month_end", "safe_to_spend_required_adjustment"}:
        total_budget = sum(_num(r.get("budget")) for r in rows)
        total_spent = sum(_num(r.get("spent")) for r in rows)
        rows = [{"total_budget": _round(total_budget), "spent": _round(total_spent), "remaining": _round(total_budget - total_spent), "elapsed_ratio": _month_elapsed_ratio(), "projected_month_end_spend": _round(total_spent / max(_month_elapsed_ratio(), 0.01))}]
    elif metric in {"category_budget_pressure", "budget_pace"}:
        rows = sorted(rows, key=lambda r: (_num(r.get("spent_ratio") or 0), _num(r.get("spent"))), reverse=True)
    summary = {"budget_count": len(budgets), "over_count": sum(1 for r in rows if _num(r.get("remaining")) < 0)}
    caveats = [] if budgets else ["No category budgets are configured for this profile."]
    return _result_for_rows(metric, ctx, rows[: ctx["limit"]], summary, "category_budgets joined to current-month spending", caveats=caveats)


def _operating_capacity_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    range_token = ctx["range"].token
    money_flow = execute_metric(conn, {"metric": "money_flow_baseline", "range": range_token, "limit": 24}, profile=ctx["profile"])
    money_summary = money_flow.get("summary_numbers") or {}
    money_rows = money_flow.get("rows") or []
    baseline_month_count = max(1, int(_num(money_summary.get("baseline_month_count")) or 1))

    avg_income = _num(money_summary.get("avg_monthly_income"))
    normal_spend = _num(money_summary.get("avg_monthly_spend_after_event_exclusions"))
    fixed_floor = _num(money_summary.get("fixed_floor_monthly"))
    flexible_monthly = _num(money_summary.get("flexible_monthly_estimate"))
    reviewable_monthly = _num(money_summary.get("reviewable_monthly_estimate"))
    visible_floor_like = _round(
        sum(
            _num(row.get("monthly_average"))
            for row in money_rows
            if row.get("spend_role") in {"structural_floor", "recurring_or_vendor_review"}
        )
    )
    fixed_floor_gap = _round(max(fixed_floor - visible_floor_like, 0.0))
    operating_burn = _round(normal_spend + fixed_floor_gap)
    capacity_before_goals = _round(avg_income - operating_burn)

    leakage = execute_metric(conn, {"metric": "avoidable_leakage", "range": range_token, "limit": 20}, profile=ctx["profile"])
    leakage_summary = leakage.get("summary_numbers") or {}
    leakage_monthly = _round(_num(leakage_summary.get("fee_or_interest_total")) / baseline_month_count)
    capacity_if_leakage_fixed = _round(capacity_before_goals + leakage_monthly)

    debt = execute_metric(conn, {"metric": "debt_payment_pressure", "range": range_token, "limit": 12}, profile=ctx["profile"])
    debt_rows = debt.get("rows") or []
    debt_payment_movement_monthly = _avg([row.get("payment_total") for row in debt_rows])
    capacity_after_debt_movement = _round(capacity_before_goals - debt_payment_movement_monthly)

    goal_rows = _goal_capacity_rows(conn, ctx, capacity_before_goals, capacity_if_leakage_fixed)
    required_goal_monthly = _round(sum(_num(row.get("required_monthly")) for row in goal_rows if row.get("required_monthly") is not None))
    capacity_after_goals = _round(capacity_before_goals - required_goal_monthly)
    capacity_after_goals_if_leakage_fixed = _round(capacity_if_leakage_fixed - required_goal_monthly)
    goal_status = "configured_goals" if goal_rows else "planning_capacity_without_configured_goals"

    component_rows = [
        {
            "component": "average_monthly_income",
            "role": "money_in",
            "monthly_amount": _round(avg_income),
            "interpretation": "Income available before ordinary monthly outflow.",
            "sample_evidence_ids": ["metric:money_flow_baseline:summary"],
        },
        {
            "component": "event_adjusted_normal_spend",
            "role": "money_out",
            "monthly_amount": _round(normal_spend),
            "interpretation": "Normal spend after separating measured trip/event windows.",
            "sample_evidence_ids": ["metric:money_flow_baseline:summary"],
        },
        {
            "component": "fixed_floor_visible_in_spend",
            "role": "fixed_commitment_check",
            "monthly_amount": visible_floor_like,
            "interpretation": "Fixed or recurring spend already visible inside transaction spending.",
            "sample_evidence_ids": ["metric:money_flow_baseline:summary", "metric:floor_burn:summary"],
        },
        {
            "component": "fixed_floor_gap_not_visible_in_spend",
            "role": "fixed_commitment_gap",
            "monthly_amount": fixed_floor_gap,
            "interpretation": "Recurring/fixed floor not already visible in transaction spending; added to avoid understating burn.",
            "sample_evidence_ids": ["metric:money_flow_baseline:summary", "metric:floor_burn:summary"],
        },
        {
            "component": "reconciled_operating_burn",
            "role": "operating_burn",
            "monthly_amount": operating_burn,
            "interpretation": "Event-adjusted normal spend plus any fixed-floor gap not already visible in spending.",
            "sample_evidence_ids": ["metric:money_flow_baseline:summary", "metric:floor_burn:summary"],
        },
        {
            "component": "capacity_before_configured_goals",
            "role": "goal_capacity",
            "monthly_amount": capacity_before_goals,
            "interpretation": "Measured monthly room before configured goal contributions.",
            "sample_evidence_ids": ["metric:money_flow_baseline:summary", "metric:floor_burn:summary"],
        },
        {
            "component": "avoidable_leakage_monthly",
            "role": "low_pain_capacity_recovery",
            "monthly_amount": leakage_monthly,
            "interpretation": "Fee/interest leakage converted to a monthly recovery estimate before lifestyle cuts.",
            "sample_evidence_ids": ["metric:avoidable_leakage:summary"],
        },
        {
            "component": "capacity_if_leakage_fixed",
            "role": "goal_capacity_after_low_pain_fix",
            "monthly_amount": capacity_if_leakage_fixed,
            "interpretation": "Capacity before goals if fee/interest leakage is fixed.",
            "sample_evidence_ids": ["metric:money_flow_baseline:summary", "metric:avoidable_leakage:summary"],
        },
        {
            "component": "observed_debt_payment_movement",
            "role": "debt_movement_pressure",
            "monthly_amount": _round(debt_payment_movement_monthly),
            "interpretation": "Payment-like debt movement shown separately so it is not silently double-counted as lifestyle spend.",
            "sample_evidence_ids": ["metric:debt_payment_pressure:summary"],
        },
    ]
    if metric == "goal_capacity_statement":
        rows = [
            {
                "component": "total_required_goal_contribution",
                "role": "goal_requirement",
                "monthly_amount": required_goal_monthly,
                "goal_count": len(goal_rows),
                "goal_configuration_status": goal_status,
                "capacity_after_required_goals": capacity_after_goals,
                "capacity_after_required_goals_if_leakage_fixed": capacity_after_goals_if_leakage_fixed,
                "sample_evidence_ids": ["metric:monthly_operating_statement:summary", "metric:goal_feasibility:summary"],
            },
            *goal_rows,
            *component_rows[:6],
        ][: ctx["limit"]]
    else:
        rows = component_rows[: ctx["limit"]]

    summary = {
        "avg_monthly_income": _round(avg_income),
        "event_adjusted_normal_spend": _round(normal_spend),
        "fixed_floor_monthly": _round(fixed_floor),
        "fixed_floor_visible_in_spend": visible_floor_like,
        "fixed_floor_gap_not_visible_in_spend": fixed_floor_gap,
        "flexible_monthly_estimate": _round(flexible_monthly),
        "reviewable_monthly_estimate": _round(reviewable_monthly),
        "reconciled_operating_burn": operating_burn,
        "capacity_before_configured_goals": capacity_before_goals,
        "avoidable_leakage_monthly": leakage_monthly,
        "capacity_if_leakage_fixed": capacity_if_leakage_fixed,
        "observed_debt_payment_movement_monthly": _round(debt_payment_movement_monthly),
        "capacity_after_observed_debt_payment_movement": capacity_after_debt_movement,
        "debt_payment_movement_subtracted_from_primary_capacity": False,
        "active_goal_count": len(goal_rows),
        "required_goal_contribution_monthly": required_goal_monthly,
        "capacity_after_required_goals": capacity_after_goals,
        "capacity_after_required_goals_if_leakage_fixed": capacity_after_goals_if_leakage_fixed,
        "goal_configuration_status": goal_status,
        "baseline_month_count": baseline_month_count,
    }
    caveats = []
    if baseline_month_count < 3:
        caveats.append("Capacity is based on fewer than three baseline months.")
    basis = "money_flow_baseline plus floor_burn reconciliation, avoidable_leakage, debt_payment_pressure, and active goals"
    return _result_for_rows(metric, ctx, rows, summary, basis, caveats=caveats)


def _goal_capacity_rows(conn, ctx: dict[str, Any], capacity_before_goals: float, capacity_if_leakage_fixed: float) -> list[dict[str, Any]]:
    rows = _fetch_all(
        conn,
        """
        SELECT id, name, goal_type, target_amount, current_amount, target_date, linked_category,
               ROUND(MAX(target_amount - current_amount, 0), 2) AS gap
          FROM goals
         WHERE profile_id = ? AND is_active = 1
         ORDER BY target_date IS NULL, target_date ASC
         LIMIT ?
        """,
        [ctx["profile"], ctx["limit"]],
    )
    today = _ctx_end_date(ctx)
    out: list[dict[str, Any]] = []
    for row in rows:
        target = _parse_date(row.get("target_date"))
        months = max(1, round(((target - today).days / 30.44), 1)) if target and target > today else None
        required = _round(_num(row.get("gap")) / months) if months else None
        out.append(
            {
                "goal": row.get("name"),
                "goal_type": row.get("goal_type"),
                "target_amount": _round(row.get("target_amount")),
                "current_amount": _round(row.get("current_amount")),
                "remaining_gap": _round(row.get("gap")),
                "target_date": row.get("target_date"),
                "months_to_target": months,
                "required_monthly": required,
                "capacity_before_configured_goals": _round(capacity_before_goals),
                "capacity_after_this_goal": _round(capacity_before_goals - _num(required)) if required is not None else None,
                "capacity_after_this_goal_if_leakage_fixed": _round(capacity_if_leakage_fixed - _num(required)) if required is not None else None,
                "feasible_from_operating_capacity": bool(required is not None and capacity_before_goals >= _num(required)),
                "sample_evidence_ids": [f"goal:{row.get('id')}", "metric:monthly_operating_statement:summary"],
            }
        )
    return out


def _goal_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        """
        SELECT id, name, goal_type, target_amount, current_amount, target_date, linked_category,
               ROUND(MAX(target_amount - current_amount, 0), 2) AS gap
          FROM goals
         WHERE profile_id = ? AND is_active = 1
         ORDER BY target_date IS NULL, target_date ASC
         LIMIT ?
        """,
        [ctx["profile"], ctx["limit"]],
    )
    savings = _recent_monthly_savings(conn, ctx)
    today = _ctx_end_date(ctx)
    for row in rows:
        target = _parse_date(row.get("target_date"))
        months = max(1, round(((target - today).days / 30.44), 1)) if target and target > today else None
        row["required_monthly"] = _round(_num(row.get("gap")) / months) if months else None
        row["recent_monthly_savings"] = _round(savings)
        row["feasible_from_recent_savings"] = bool(row["required_monthly"] is not None and savings >= row["required_monthly"])
    summary = {"goal_count": len(rows), "recent_monthly_savings": _round(savings)}
    caveats = [] if rows else ["No active goals are configured."]
    return _result_for_rows(metric, ctx, rows, summary, "goals table plus recent savings behavior", caveats=caveats)


def _debt_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    liability_rows = _account_rows(conn, ctx, liability_like=True)
    cash = _cash_balance(conn, ctx["profile"])
    liability_total = sum(abs(_num(r.get("current_balance"))) for r in liability_rows)
    if metric == "debt_payment_pressure":
        rows = _fetch_all(
            conn,
            f"""
            SELECT substr(date, 1, 7) AS month,
                   ROUND(SUM(ABS(amount)), 2) AS payment_total,
                   COUNT(*) AS count
              FROM transactions_visible
             WHERE profile_id = ? AND (category LIKE '%Payment%' OR category LIKE '%Debt%' OR merchant_name LIKE '%CARD%'){_range_clause(ctx['range'])}
             GROUP BY month
             ORDER BY month
             LIMIT ?
            """,
            [ctx["profile"], *_range_params(ctx["range"]), ctx["limit"]],
        )
    elif metric == "interest_or_fee_signal":
        rows = _fetch_all(
            conn,
            f"""
            SELECT date, COALESCE(NULLIF(merchant_name,''), category, 'Fee') AS merchant, category, ROUND(amount, 2) AS amount, id
              FROM transactions_visible
             WHERE profile_id = ? AND amount < 0 AND (LOWER(category) LIKE '%fee%' OR LOWER(category) LIKE '%interest%' OR LOWER(merchant_name) LIKE '%fee%' OR LOWER(merchant_name) LIKE '%interest%'){_range_clause(ctx['range'])}
             ORDER BY ABS(amount) DESC
             LIMIT ?
            """,
            [ctx["profile"], *_range_params(ctx["range"]), ctx["limit"]],
        )
    else:
        rows = liability_rows
    summary = {"liability_total": _round(liability_total), "cash_like_balance": _round(cash), "liability_to_cash_ratio": _round(liability_total / cash, 4) if cash else None}
    caveats = ["APR, credit limit, and minimum-payment fields are unavailable unless imported from an institution."]
    return _result_for_rows(metric, ctx, rows, summary, "account liability balances and payment-like transactions", caveats=caveats)


def _avoidable_leakage_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    cash = _cash_balance(conn, ctx["profile"])
    fee_rows = _fetch_all(
        conn,
        f"""
        SELECT id,
               date,
               COALESCE(NULLIF(merchant_name,''), NULLIF(merchant_key,''), category, 'Fee') AS merchant,
               COALESCE(NULLIF(category,''), 'Fees & Charges') AS category,
               ROUND(ABS(amount), 2) AS measured_amount
          FROM transactions_visible
         WHERE profile_id = ?
           AND amount < 0
           AND (
                LOWER(COALESCE(category, '')) LIKE '%fee%'
             OR LOWER(COALESCE(category, '')) LIKE '%interest%'
             OR LOWER(COALESCE(merchant_name, '')) LIKE '%fee%'
             OR LOWER(COALESCE(merchant_name, '')) LIKE '%interest%'
             OR LOWER(COALESCE(merchant_name, '')) LIKE '%finance charge%'
           ){_range_clause(ctx['range'])}
         ORDER BY measured_amount DESC, date DESC
         LIMIT ?
        """,
        [ctx["profile"], *_range_params(ctx["range"]), ctx["limit"]],
    )

    rows: list[dict[str, Any]] = []
    for row in fee_rows:
        amount = _num(row.get("measured_amount"))
        rows.append(
            {
                "leakage_type": "fee_or_interest",
                "subject": row.get("merchant") or row.get("category") or "Fee",
                "category": row.get("category"),
                "date": row.get("date"),
                "measured_amount": _round(amount),
                "amount_basis": "posted fee, interest, or finance-charge-like transaction",
                "cash_like_balance": _round(cash),
                "cash_context": "cash_available_timing_friction" if cash >= amount and amount > 0 else "cash_context_needs_review",
                "controllability": "high",
                "action": "Fix timing, autopay, reminders, or due-date friction before cutting intentional spending.",
                "tradeoff": "Treat this as preventable friction only after confirming the charge is real and not miscategorized.",
                "caveat": "One fee row should not become a lifestyle verdict.",
                "sample_evidence_ids": [f"txn:{row.get('id')}"] if row.get("id") else [],
            }
        )

    recurring = execute_metric(conn, {"metric": "recurring_obligation_calendar", "range": ctx["range"].token, "limit": 20}, profile=ctx["profile"])
    recurring_rows = recurring.get("rows") or []
    for idx, row in enumerate(recurring_rows, start=1):
        duplicate_count = _safe_int(row.get("duplicate_count"), 0)
        if duplicate_count <= 0:
            continue
        rows.append(
            {
                "leakage_type": "recurring_duplicate_record",
                "subject": row.get("merchant") or row.get("category") or "Recurring obligation",
                "category": row.get("category"),
                "measured_amount": row.get("monthly_equivalent"),
                "amount_basis": "monthly equivalent of a deduped recurring record with duplicate detections",
                "duplicate_count": duplicate_count,
                "cash_like_balance": _round(cash),
                "cash_context": "not_a_cash_problem",
                "controllability": "reviewable",
                "action": "Confirm whether repeated recurring records are historical detections or duplicate active obligations.",
                "tradeoff": "Do not assume this is a duplicate charge until the underlying obligation is confirmed.",
                "caveat": "Recurring duplicate records can reflect repeated annual detections, not extra payments.",
                "sample_evidence_ids": [f"metric:recurring_obligation_calendar:{idx}"],
            }
        )

    rows = sorted(rows, key=lambda row: (_leakage_priority(row), _num(row.get("measured_amount"))), reverse=True)[: ctx["limit"]]
    fee_total = _round(sum(_num(row.get("measured_amount")) for row in rows if row.get("leakage_type") == "fee_or_interest"))
    summary = {
        "cash_like_balance": _round(cash),
        "fee_or_interest_count": sum(1 for row in rows if row.get("leakage_type") == "fee_or_interest"),
        "fee_or_interest_total": fee_total,
        "cash_available_fee_count": sum(1 for row in rows if row.get("cash_context") == "cash_available_timing_friction"),
        "recurring_duplicate_candidate_count": sum(1 for row in rows if row.get("leakage_type") == "recurring_duplicate_record"),
        "top_subject": rows[0].get("subject") if rows else None,
        "top_measured_amount": rows[0].get("measured_amount") if rows else 0,
    }
    caveats = ["Leakage rows are review candidates, not automatic judgment or automatic cancellation."]
    if not rows:
        caveats.append("No fee, interest, or duplicate-recurring review rows were detected in the selected range.")
    return _result_for_rows(metric, ctx, rows, summary, "fee/interest rows plus recurring duplicate-review candidates with cash context", caveats=caveats)


def _net_worth_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    if metric in {"account_balance_trend", "cash_vs_liability_position", "idle_cash_signal", "account_coverage_caveats"}:
        rows = _account_rows(conn, ctx)
        cash = sum(_num(r.get("current_balance")) for r in rows if _cash_like(r))
        liabilities = sum(abs(_num(r.get("current_balance"))) for r in rows if _liability_like(r))
        summary = {"cash_like_balance": _round(cash), "liability_total": _round(liabilities), "cash_minus_liabilities": _round(cash - liabilities)}
        return _result_for_rows(metric, ctx, rows[: ctx["limit"]], summary, "active account balance snapshot")
    rows = _fetch_all(
        conn,
        f"""
        SELECT date, ROUND(total_assets, 2) AS total_assets, ROUND(total_owed, 2) AS total_owed, ROUND(net_worth, 2) AS net_worth
          FROM net_worth_history
         WHERE profile_id = ?{_range_clause(ctx['range'])}
         ORDER BY date
         LIMIT ?
        """,
        [ctx["profile"], *_range_params(ctx["range"]), ctx["limit"]],
    )
    if metric == "net_worth_driver_split" and len(rows) >= 2:
        first, last = rows[0], rows[-1]
        rows = [{"start": first["date"], "end": last["date"], "asset_delta": _round(_num(last.get("total_assets")) - _num(first.get("total_assets"))), "owed_delta": _round(_num(last.get("total_owed")) - _num(first.get("total_owed"))), "net_worth_delta": _round(_num(last.get("net_worth")) - _num(first.get("net_worth")))}]
    summary = {"row_count": len(rows), "latest_net_worth": rows[-1].get("net_worth") if rows else None}
    return _result_for_rows(metric, ctx, rows, summary, "net_worth_history snapshot")


def _anomaly_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    avg_row = _fetch_one(
        conn,
        f"SELECT AVG(ABS(amount)) AS avg_amount FROM transactions_visible WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}",
        [ctx["profile"], *_range_params(ctx["range"])],
    )
    avg_amount = max(_num(avg_row.get("avg_amount")), 1.0)
    if metric == "refund_or_transfer_noise":
        rows = _fetch_all(
            conn,
            f"""
            SELECT date, category, COALESCE(NULLIF(merchant_name,''), category) AS merchant, ROUND(amount, 2) AS amount, id
              FROM transactions_visible
             WHERE profile_id = ? AND (amount > 0 OR category IN ({_placeholders(NON_SPENDING_CATEGORIES)})){_range_clause(ctx['range'])}
             ORDER BY ABS(amount) DESC
             LIMIT ?
            """,
            [ctx["profile"], *NON_SPENDING_CATEGORIES, *_range_params(ctx["range"]), ctx["limit"]],
        )
    else:
        rows = _fetch_all(
            conn,
            f"""
            SELECT date, category, COALESCE(NULLIF(merchant_name,''), NULLIF(merchant_key,''), category) AS merchant,
                   ROUND(amount, 2) AS amount, id
              FROM transactions_visible
             WHERE profile_id = ? AND {_expense_sql()} AND ABS(amount) >= ?{_range_clause(ctx['range'])}
             ORDER BY ABS(amount) DESC
             LIMIT ?
            """,
            [ctx["profile"], avg_amount * 2, *_range_params(ctx["range"]), ctx["limit"]],
        )
    rows = [_with_single_evidence_id(r) for r in rows]
    return _result_for_rows(metric, ctx, rows, {"avg_expense_amount": _round(avg_amount), "row_count": len(rows)}, "posted transactions compared to selected-range average")


def _quality_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    if metric in {"enrichment_confidence_summary", "low_confidence_driver_rows", "category_mapping_uncertainty"}:
        rows = _fetch_all(
            conn,
            f"""
            SELECT COALESCE(e.top_level_category, t.category, 'Unknown') AS category,
                   COALESCE(e.essentiality, 'unknown') AS essentiality,
                   COALESCE(e.recurrence, 'unknown') AS recurrence,
                   COALESCE(e.method, 'missing') AS method,
                   COUNT(*) AS count
              FROM transactions_visible t
              LEFT JOIN transaction_enrichment e ON e.transaction_id = t.id AND e.profile_id = t.profile_id
             WHERE t.profile_id = ?{_range_clause(ctx['range'], 't')}
             GROUP BY category, essentiality, recurrence, method
             ORDER BY count DESC
             LIMIT ?
            """,
            [ctx["profile"], *_range_params(ctx["range"]), ctx["limit"]],
        )
    elif metric == "recurrence_confidence":
        return _recurring_handler(conn, metric, ctx)
    else:
        account_rows = _account_rows(conn, ctx)
        tx_count = _fetch_one(conn, f"SELECT COUNT(*) AS count, MIN(date) AS min_date, MAX(date) AS max_date FROM transactions_visible WHERE profile_id = ?{_range_clause(ctx['range'])}", [ctx["profile"], *_range_params(ctx["range"])])
        rows = [{"profile_scope": ctx["profile"], "transaction_count": tx_count.get("count"), "min_date": tx_count.get("min_date"), "max_date": tx_count.get("max_date"), "active_account_count": len(account_rows), "stale_account_count": sum(1 for r in account_rows if _stale_account(r))}]
    summary = {"row_count": len(rows)}
    return _result_for_rows(metric, ctx, rows, summary, "data coverage and enrichment confidence checks")


def _advisor_private_baseline_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    scope = _advisor_period_scope(conn, ctx)
    start = scope.get("analysis_start") or scope.get("range_start")
    end = scope.get("analysis_end") or scope.get("range_end")
    if not start or not end:
        return _result_for_rows(metric, ctx, [], {}, "advisor private baseline could not find a usable date range")
    if metric == "advisor_period_reliability":
        return _advisor_period_reliability_metric(metric, ctx, scope)
    if metric == "cash_flow_compression":
        return _cash_flow_compression_metric(conn, metric, ctx, scope)
    if metric == "category_advisor_ledger":
        return _category_advisor_ledger_metric(conn, metric, ctx, scope)
    if metric == "merchant_lifecycle":
        return _merchant_lifecycle_metric(conn, metric, ctx, scope)
    if metric == "external_transfer_pressure":
        return _external_transfer_pressure_metric(conn, metric, ctx, scope)
    if metric == "savings_scenarios":
        return _savings_scenarios_metric(conn, metric, ctx, scope)
    if metric == "advisor_data_quality_profile":
        return _advisor_data_quality_profile_metric(conn, metric, ctx, scope)
    return _unsupported_metric_handler(conn, metric, ctx)


def _advisor_period_reliability_metric(metric: str, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "period": "full_visible_data",
            "start_date": scope.get("data_start"),
            "end_date": scope.get("data_end"),
            "month_count": scope.get("visible_month_count"),
            "transaction_count": scope.get("visible_transaction_count"),
        },
        {
            "period": "reliable_income_period",
            "start_date": scope.get("analysis_start"),
            "end_date": scope.get("analysis_end"),
            "month_count": scope.get("analysis_month_count"),
            "transaction_count": scope.get("analysis_transaction_count"),
        },
        {
            "period": "complete_months",
            "start_date": scope.get("first_complete_month"),
            "end_date": scope.get("last_complete_month"),
            "month_count": scope.get("complete_month_count"),
            "transaction_count": None,
        },
    ]
    summary = {
        "visible_transaction_count": scope.get("visible_transaction_count"),
        "visible_month_count": scope.get("visible_month_count"),
        "first_visible_date": scope.get("data_start"),
        "last_visible_date": scope.get("data_end"),
        "first_income_date": scope.get("first_income_date"),
        "analysis_start": scope.get("analysis_start"),
        "analysis_end": scope.get("analysis_end"),
        "analysis_month_count": scope.get("analysis_month_count"),
        "complete_month_count": scope.get("complete_month_count"),
        "current_month_partial": scope.get("current_month_partial"),
    }
    caveats = []
    if scope.get("first_income_date") and scope.get("data_start") and scope.get("first_income_date") > scope.get("data_start"):
        caveats.append("Spending exists before the first observed income row, so cash-flow rates should use the reliable income period.")
    if scope.get("current_month_partial"):
        caveats.append("The latest visible month is partial and should not be annualized directly.")
    return _result_for_rows(
        metric,
        ctx,
        rows,
        summary,
        "visible transaction dates plus first observed income month and complete-month coverage",
        caveats=caveats,
    )


def _cash_flow_compression_metric(conn, metric: str, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    monthly = _advisor_monthly_cash_flow(conn, ctx, scope)
    complete_months = [row for row in monthly if row.get("month") in set(scope.get("complete_months") or [])]
    income_positive = [row for row in monthly if _num(row.get("income")) > 0]
    trailing_12 = complete_months[-12:]
    recent_3 = complete_months[-3:]
    rows = [
        _cash_flow_period_row("reliable_income_period", monthly),
        _cash_flow_period_row("income_positive_months", income_positive),
        _cash_flow_period_row("trailing_12_complete_months", trailing_12),
        _cash_flow_period_row("recent_3_complete_months", recent_3),
    ]
    negative_months = [row for row in monthly if _num(row.get("net_cash_flow")) < 0 and _num(row.get("income")) > 0]
    rows.extend(
        {
            "period": "negative_income_month",
            "month": row.get("month"),
            "income": row.get("income"),
            "gross_spending": row.get("gross_spending"),
            "credits_refunds": row.get("credits_refunds"),
            "outgoing_external_transfers": row.get("outgoing_external_transfers"),
            "net_cash_flow": row.get("net_cash_flow"),
            "top_pressure": row.get("top_pressure"),
        }
        for row in negative_months[: max(0, ctx["limit"] - 4)]
    )
    trailing_rate = _num(rows[2].get("cash_flow_rate")) if len(rows) > 2 else 0.0
    recent_rate = _num(rows[3].get("cash_flow_rate")) if len(rows) > 3 else 0.0
    summary = {
        "reliable_income_period_net_cash_flow": rows[0].get("net_cash_flow"),
        "reliable_income_period_cash_flow_rate": rows[0].get("cash_flow_rate"),
        "complete_month_count": len(complete_months),
        "trailing_12_cash_flow_rate": rows[2].get("cash_flow_rate"),
        "recent_3_cash_flow_rate": rows[3].get("cash_flow_rate"),
        "cash_flow_rate_delta_recent_vs_trailing": _round(recent_rate - trailing_rate, 4),
        "negative_income_month_count": len(negative_months),
    }
    return _result_for_rows(
        metric,
        ctx,
        rows[: ctx["limit"]],
        summary,
        "monthly income, gross spending, refunds, and external transfers grouped into reliable/trailing/recent cash-flow periods",
        caveats=["Cash-flow compression is based on complete months; the latest partial month is excluded from recent complete-month rates."] if scope.get("current_month_partial") else [],
    )


def _category_advisor_ledger_metric(conn, metric: str, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    spend_rows = _advisor_spending_rows(conn, ctx, scope)
    month_count = max(1, int(scope.get("analysis_month_count") or 1))
    recent_months = set(scope.get("recent_3_complete_months") or [])
    prior_months = set(scope.get("prior_9_complete_months") or [])
    total_spend = _round(sum(_num(row.get("amount")) for row in spend_rows))
    by_category: dict[str, dict[str, Any]] = {}
    merchants: dict[str, dict[str, dict[str, Any]]] = {}
    for row in spend_rows:
        category = str(row.get("category") or "Uncategorized")
        item = by_category.setdefault(
            category,
            {"category": category, "total_spend": 0.0, "transaction_count": 0, "months": set(), "recent_total": 0.0, "prior_total": 0.0, "sample_evidence_ids": []},
        )
        amount = _num(row.get("amount"))
        month = str(row.get("month") or "")
        item["total_spend"] = _round(_num(item.get("total_spend")) + amount)
        item["transaction_count"] += 1
        item["months"].add(month)
        if month in recent_months:
            item["recent_total"] = _round(_num(item.get("recent_total")) + amount)
        if month in prior_months:
            item["prior_total"] = _round(_num(item.get("prior_total")) + amount)
        if row.get("id") and len(item["sample_evidence_ids"]) < 8:
            item["sample_evidence_ids"].append(f"txn:{row.get('id')}")
        merchant = str(row.get("merchant") or category)
        merchant_item = merchants.setdefault(category, {}).setdefault(merchant, {"merchant": merchant, "total": 0.0, "count": 0})
        merchant_item["total"] = _round(_num(merchant_item.get("total")) + amount)
        merchant_item["count"] += 1

    rows = []
    for category, item in by_category.items():
        recent_avg = _round(_num(item.get("recent_total")) / max(1, len(recent_months))) if recent_months else 0.0
        prior_avg = _round(_num(item.get("prior_total")) / max(1, len(prior_months))) if prior_months else 0.0
        tx_count = int(item.get("transaction_count") or 0)
        category_total = _num(item.get("total_spend"))
        top_merchants = sorted(merchants.get(category, {}).values(), key=lambda value: _num(value.get("total")), reverse=True)[:5]
        rows.append(
            {
                "category": category,
                "spend_role": _money_map_role(category),
                "controllability": _money_map_controllability(category),
                "total_spend": _round(category_total),
                "monthly_average": _round(category_total / month_count),
                "share_of_total_spend": _round(category_total / total_spend, 4) if total_spend else None,
                "transaction_count": tx_count,
                "active_month_count": len(item.get("months") or []),
                "avg_ticket_size": _round(category_total / tx_count) if tx_count else 0.0,
                "recent_3mo_average": recent_avg,
                "prior_9mo_average": prior_avg,
                "recent_vs_prior_monthly_delta": _round(recent_avg - prior_avg),
                "top_merchants": top_merchants,
                "sample_evidence_ids": item.get("sample_evidence_ids") or [],
            }
        )
    rows = sorted(rows, key=lambda row: _num(row.get("total_spend")), reverse=True)[: ctx["limit"]]
    summary = {
        "total_spend": total_spend,
        "category_count": len(by_category),
        "analysis_month_count": month_count,
        "top_category": rows[0].get("category") if rows else None,
        "top_category_total": rows[0].get("total_spend") if rows else 0.0,
        "recent_3_complete_month_count": len(recent_months),
        "prior_9_complete_month_count": len(prior_months),
    }
    return _result_for_rows(
        metric,
        ctx,
        rows,
        summary,
        "advisor category ledger from safe spending rows: transfers/payments excluded, recent complete months compared with prior complete months",
    )


def _merchant_lifecycle_metric(conn, metric: str, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    spend_rows = _advisor_spending_rows(conn, ctx, scope)
    recent_months = set(scope.get("recent_3_complete_months") or [])
    recent_start_month = min(recent_months) if recent_months else None
    by_merchant: dict[str, dict[str, Any]] = {}
    label_groups: dict[str, set[str]] = {}
    for row in spend_rows:
        merchant = str(row.get("merchant") or "Merchant")
        group = _merchant_lifecycle_group(merchant)
        label_groups.setdefault(group, set()).add(merchant)
        item = by_merchant.setdefault(
            merchant,
            {"merchant": merchant, "category": row.get("category"), "total_spend": 0.0, "transaction_count": 0, "first_seen": row.get("date"), "last_seen": row.get("date"), "sample_evidence_ids": []},
        )
        amount = _num(row.get("amount"))
        item["total_spend"] = _round(_num(item.get("total_spend")) + amount)
        item["transaction_count"] += 1
        item["first_seen"] = min(str(item.get("first_seen") or row.get("date")), str(row.get("date") or ""))
        item["last_seen"] = max(str(item.get("last_seen") or row.get("date")), str(row.get("date") or ""))
        if row.get("id") and len(item["sample_evidence_ids"]) < 5:
            item["sample_evidence_ids"].append(f"txn:{row.get('id')}")
    top = sorted(by_merchant.values(), key=lambda row: _num(row.get("total_spend")), reverse=True)[:8]
    rows = [{**row, "lifecycle_type": "top_merchant"} for row in top]
    if recent_start_month:
        recent_start = f"{recent_start_month}-01"
        new_rows = [
            {**row, "lifecycle_type": "new_since_recent_window"}
            for row in by_merchant.values()
            if str(row.get("first_seen") or "") >= recent_start
        ]
        dormant_rows = [
            {**row, "lifecycle_type": "dormant_since_recent_window"}
            for row in by_merchant.values()
            if str(row.get("last_seen") or "") < recent_start and _num(row.get("total_spend")) >= 300
        ]
        rows.extend(sorted(new_rows, key=lambda row: _num(row.get("total_spend")), reverse=True)[:5])
        rows.extend(sorted(dormant_rows, key=lambda row: _num(row.get("total_spend")), reverse=True)[:5])
    for group, labels in sorted(label_groups.items()):
        if len(labels) < 2:
            continue
        group_rows = [row for row in by_merchant.values() if row["merchant"] in labels]
        total = _round(sum(_num(row.get("total_spend")) for row in group_rows))
        if total < 250:
            continue
        first_seen_values = [str(row.get("first_seen") or "") for row in group_rows if row.get("first_seen")]
        last_seen_values = [str(row.get("last_seen") or "") for row in group_rows if row.get("last_seen")]
        rows.append(
            {
                "lifecycle_type": "split_label_group",
                "merchant": group,
                "category": "mixed",
                "total_spend": total,
                "transaction_count": sum(int(row.get("transaction_count") or 0) for row in group_rows),
                "first_seen": min(first_seen_values) if first_seen_values else None,
                "last_seen": max(last_seen_values) if last_seen_values else None,
                "labels": sorted(labels)[:8],
                "sample_evidence_ids": [eid for row in group_rows for eid in row.get("sample_evidence_ids", [])][:8],
            }
        )
    rows = rows[: ctx["limit"]]
    summary = {
        "merchant_count": len(by_merchant),
        "top_merchant": top[0].get("merchant") if top else None,
        "top_merchant_total": top[0].get("total_spend") if top else 0.0,
        "split_label_group_count": sum(1 for row in rows if row.get("lifecycle_type") == "split_label_group"),
        "recent_window_start_month": recent_start_month,
    }
    return _result_for_rows(
        metric,
        ctx,
        rows,
        summary,
        "merchant lifecycle from safe spending rows: top, new, dormant, and likely split-label groups",
    )


def _external_transfer_pressure_metric(conn, metric: str, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    rows_raw = _fetch_all(
        conn,
        """
        SELECT id, date, substr(date, 1, 7) AS month, ROUND(amount, 2) AS amount
          FROM transactions_visible
         WHERE profile_id = ?
           AND COALESCE(expense_type, '') = 'transfer_external'
           AND date >= ? AND date <= ?
         ORDER BY date
        """,
        [ctx["profile"], scope["analysis_start"], scope["analysis_end"]],
    )
    by_month: dict[str, dict[str, Any]] = {}
    for row in rows_raw:
        month = str(row.get("month") or "")
        item = by_month.setdefault(month, {"month": month, "incoming_external_transfers": 0.0, "outgoing_external_transfers": 0.0, "transaction_count": 0, "sample_evidence_ids": []})
        amount = _num(row.get("amount"))
        if amount > 0:
            item["incoming_external_transfers"] = _round(_num(item.get("incoming_external_transfers")) + amount)
        elif amount < 0:
            item["outgoing_external_transfers"] = _round(_num(item.get("outgoing_external_transfers")) + abs(amount))
        item["transaction_count"] += 1
        if row.get("id") and len(item["sample_evidence_ids"]) < 8:
            item["sample_evidence_ids"].append(f"txn:{row.get('id')}")
    rows = sorted(by_month.values(), key=lambda row: row.get("month") or "")[: ctx["limit"]]
    total_outgoing = _round(sum(_num(row.get("outgoing_external_transfers")) for row in by_month.values()))
    total_incoming = _round(sum(_num(row.get("incoming_external_transfers")) for row in by_month.values()))
    max_month = max(by_month.values(), key=lambda row: _num(row.get("outgoing_external_transfers")), default={})
    summary = {
        "incoming_external_transfer_total": total_incoming,
        "outgoing_external_transfer_total": total_outgoing,
        "net_external_transfer_outflow": _round(total_outgoing - total_incoming),
        "month_count_with_external_transfers": len(by_month),
        "max_outgoing_month": max_month.get("month"),
        "max_outgoing_month_amount": max_month.get("outgoing_external_transfers") or 0.0,
    }
    return _result_for_rows(
        metric,
        ctx,
        rows,
        summary,
        "external transfer rows separated from lifestyle spending and grouped by month",
        caveats=["External transfers reduce cash but need user labels before Mira treats them as obligations, investments, support, or discretionary outflows."] if rows else [],
    )


def _savings_scenarios_metric(conn, metric: str, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    ledger = _category_advisor_ledger_metric(conn, "category_advisor_ledger", {**ctx, "limit": 24}, scope)
    rows: list[dict[str, Any]] = []
    for item in ledger.get("rows") or []:
        prior = _num(item.get("prior_9mo_average"))
        recent = _num(item.get("recent_3mo_average"))
        delta = _round(prior - recent)
        if delta >= 25:
            rows.append(
                {
                    "scenario_type": "preserve_recent_improvement",
                    "subject": item.get("category"),
                    "monthly_effect": delta,
                    "annual_effect": _round(delta * 12),
                    "basis": "prior 9 complete months minus recent 3 complete months",
                    "tradeoff": "This is a preservation scenario, not a demand to cut further.",
                    "sample_evidence_ids": item.get("sample_evidence_ids") or [],
                }
            )
    leakage = execute_metric(conn, {"metric": "avoidable_leakage", "range": ctx["range"].token, "limit": 12}, profile=ctx["profile"])
    leakage_total = _num((leakage.get("summary_numbers") or {}).get("fee_or_interest_total"))
    if leakage_total:
        monthly = _round(leakage_total / max(1, int(scope.get("analysis_month_count") or 1)))
        rows.append(
            {
                "scenario_type": "recover_fee_interest_leakage",
                "subject": "Fees and interest",
                "monthly_effect": monthly,
                "annual_effect": _round(monthly * 12),
                "basis": "fee and interest review rows spread across the reliable analysis period",
                "tradeoff": "Recover leakage before painful lifestyle cuts.",
                "sample_evidence_ids": leakage.get("evidence_ids") or [],
            }
        )
    for item in ledger.get("rows") or []:
        role = item.get("spend_role")
        if role not in {"flexible_living", "private_discretionary"}:
            continue
        monthly_avg = _num(item.get("monthly_average"))
        if monthly_avg < 100:
            continue
        rows.append(
            {
                "scenario_type": "ten_percent_sensitivity",
                "subject": item.get("category"),
                "monthly_effect": _round(monthly_avg * 0.10),
                "annual_effect": _round(monthly_avg * 1.2),
                "basis": "10 percent sensitivity on the observed category monthly average",
                "tradeoff": "Use only as a planning sensitivity; it is not a moral judgment or automatic recommendation.",
                "sample_evidence_ids": item.get("sample_evidence_ids") or [],
            }
        )
    rows = sorted(rows, key=lambda row: _num(row.get("monthly_effect")), reverse=True)[: ctx["limit"]]
    summary = {
        "scenario_count": len(rows),
        "top_scenario": rows[0].get("subject") if rows else None,
        "top_monthly_effect": rows[0].get("monthly_effect") if rows else 0.0,
        "total_monthly_effect_if_all_taken": _round(sum(_num(row.get("monthly_effect")) for row in rows)),
    }
    return _result_for_rows(
        metric,
        ctx,
        rows,
        summary,
        "what-if savings scenarios computed from category trends and fee/interest leakage; scenarios are planning sensitivities, not commands",
    )


def _advisor_data_quality_profile_metric(conn, metric: str, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    tx_cols = _table_columns(conn, "transactions")
    reviewed_expr = "SUM(CASE WHEN COALESCE(reviewed,0)=0 THEN 1 ELSE 0 END)" if "reviewed" in tx_cols else "NULL"
    blank_category = _fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM transactions_visible WHERE profile_id = ? AND COALESCE(TRIM(category),'') = ''",
        [ctx["profile"]],
    )
    low_conf = _fetch_one(
        conn,
        f"""
        SELECT COUNT(*) AS count, ROUND(COALESCE(SUM(ABS(amount)), 0), 2) AS amount
          FROM transactions_visible
         WHERE profile_id = ?
           AND {_advisor_spend_sql()}
           AND LOWER(COALESCE(confidence, '')) NOT IN ('high', 'reviewed', 'user', 'manual')
        """,
        [ctx["profile"]],
    )
    tx_counts = _fetch_one(
        conn,
        f"SELECT COUNT(*) AS transaction_count, {reviewed_expr} AS unreviewed_count FROM transactions_visible WHERE profile_id = ?",
        [ctx["profile"]],
    )
    recurring_dupes = _fetch_one(
        conn,
        """
        SELECT COALESCE(SUM(extra_rows), 0) AS duplicate_row_count
          FROM (
            SELECT MAX(COUNT(*) - 1, 0) AS extra_rows
              FROM recurring_obligations
             WHERE profile_id = ? AND COALESCE(state, '') IN ('active', 'confirmed')
             GROUP BY UPPER(TRIM(COALESCE(display_name, merchant_key, ''))), amount_cents, frequency, next_expected_date
          )
        """,
        [ctx["profile"]],
    )
    nw = _fetch_one(
        conn,
        "SELECT COUNT(*) AS count, MIN(date) AS min_date, MAX(date) AS max_date FROM net_worth_history WHERE profile_id = ?",
        [ctx["profile"]],
    )
    rows = [
        {"quality_check": "visible_transactions", "count": tx_counts.get("transaction_count"), "amount": None},
        {"quality_check": "unreviewed_transactions", "count": tx_counts.get("unreviewed_count"), "amount": None},
        {"quality_check": "blank_categories", "count": blank_category.get("count"), "amount": None},
        {"quality_check": "low_confidence_spending", "count": low_conf.get("count"), "amount": low_conf.get("amount")},
        {"quality_check": "recurring_duplicate_rows", "count": recurring_dupes.get("duplicate_row_count"), "amount": None},
        {"quality_check": "transaction_splits", "count": _safe_table_count(conn, "transaction_splits"), "amount": None},
        {"quality_check": "investment_holdings", "count": _safe_table_count(conn, "investment_holdings"), "amount": None},
        {"quality_check": "net_worth_snapshots", "count": nw.get("count"), "amount": None, "min_date": nw.get("min_date"), "max_date": nw.get("max_date")},
    ]
    summary = {
        "visible_transaction_count": tx_counts.get("transaction_count"),
        "unreviewed_transaction_count": tx_counts.get("unreviewed_count"),
        "blank_category_count": blank_category.get("count"),
        "low_confidence_spend_count": low_conf.get("count"),
        "low_confidence_spend_amount": low_conf.get("amount"),
        "recurring_duplicate_row_count": recurring_dupes.get("duplicate_row_count"),
        "transaction_split_count": _safe_table_count(conn, "transaction_splits"),
        "investment_holding_count": _safe_table_count(conn, "investment_holdings"),
        "net_worth_snapshot_count": nw.get("count"),
    }
    caveats = []
    if tx_counts.get("unreviewed_count"):
        caveats.append("Some or all transactions are not marked reviewed, so fine-grained category decisions should be checked before acting.")
    if _num(low_conf.get("amount")):
        caveats.append("Low-confidence spending rows can affect smaller category or merchant conclusions.")
    if _safe_table_count(conn, "investment_holdings") == 0:
        caveats.append("Investment holdings are not available in this database, so this is not a full life-plan review.")
    return _result_for_rows(metric, ctx, rows[: ctx["limit"]], summary, "safe data-quality profile from review flags, confidence, splits, recurring duplicates, and net-worth coverage", caveats=caveats)


def _unsupported_metric_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    return _missing_metric_result(metric, {"range": ctx["range"].token})


def _payday_window(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    income_rows = _fetch_all(conn, f"SELECT date FROM transactions_visible WHERE profile_id = ? AND category = 'Income' AND amount > 0{_range_clause(ctx['range'])} ORDER BY date", [ctx["profile"], *_range_params(ctx["range"])])
    income_dates = [_parse_date(r.get("date")) for r in income_rows if _parse_date(r.get("date"))]
    expense_rows = _fetch_all(conn, f"SELECT date, ABS(amount) AS amount FROM transactions_visible WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}", [ctx["profile"], *_range_params(ctx["range"])])
    buckets = {"within_2_days_after_income": {"bucket": "within_2_days_after_income", "total": 0.0, "count": 0}, "outside_payday_window": {"bucket": "outside_payday_window", "total": 0.0, "count": 0}}
    for row in expense_rows:
        d = _parse_date(row.get("date"))
        near = bool(d and any(0 <= (d - inc).days <= 2 for inc in income_dates))
        bucket = buckets["within_2_days_after_income" if near else "outside_payday_window"]
        bucket["total"] = _round(bucket["total"] + _num(row.get("amount")))
        bucket["count"] += 1
    rows = list(buckets.values())
    total = sum(_num(r.get("total")) for r in rows) or 1.0
    for row in rows:
        row["share"] = _round(_num(row.get("total")) / total, 4)
    return _result_for_rows(metric, ctx, rows, {"income_date_count": len(income_dates), "total": _round(total)}, "expenses bucketed around observed income dates")


def _mtd_pace(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    today = _ctx_end_date(ctx)
    current = execute_metric(conn, {"metric": "monthly_spend_series", "range": "current_month", "limit": 1}, profile=ctx["profile"])
    baseline = execute_metric(conn, {"metric": "monthly_spend_series", "range": "last_3_months", "limit": 3}, profile=ctx["profile"])
    current_spend = _num((current.get("rows") or [{}])[-1].get("expenses"))
    elapsed = max(today.day, 1)
    current_daily = current_spend / elapsed
    baseline_monthly = _avg([r.get("expenses") for r in baseline.get("rows") or []])
    baseline_daily = baseline_monthly / 30.44 if baseline_monthly else 0
    rows = [{"current_month_spend": _round(current_spend), "elapsed_days": elapsed, "current_daily_pace": _round(current_daily), "baseline_daily_pace": _round(baseline_daily), "pace_delta": _round(current_daily - baseline_daily)}]
    return _result_for_rows(metric, ctx, rows, rows[0], "current-month spend pace vs three-month baseline")


def _spending_event_cluster_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    expenses = _fetch_all(
        conn,
        f"""
        SELECT id,
               date,
               category,
               COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, ''), category, 'Unknown') AS merchant,
               ROUND(ABS(amount), 2) AS amount
          FROM transactions_visible
         WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}
         ORDER BY date ASC, amount DESC
        """,
        [ctx["profile"], *_range_params(ctx["range"])],
    )
    travel_rows = [row for row in expenses if row.get("category") == "Travel" and _parse_date(row.get("date"))]
    clusters = _cluster_travel_rows(travel_rows)
    out = []
    for idx, cluster in enumerate(clusters, start=1):
        if len(cluster) < 2 and sum(_num(row.get("amount")) for row in cluster) < 500:
            continue
        dates = [_parse_date(row.get("date")) for row in cluster if _parse_date(row.get("date"))]
        if not dates:
            continue
        travel_start = min(dates)
        travel_end = max(dates)
        activity_start = max(travel_start, travel_end - timedelta(days=2))
        activity_end = travel_end + timedelta(days=5)
        activity_rows = [
            row
            for row in expenses
            if (parsed := _parse_date(row.get("date"))) and activity_start <= parsed <= activity_end
            and row.get("category") in EVENT_ACTIVITY_CATEGORIES
        ]
        pre_activity_travel = [
            row
            for row in cluster
            if (parsed := _parse_date(row.get("date"))) and parsed < activity_start
        ]
        travel_total = _round(sum(_num(row.get("amount")) for row in cluster))
        activity_total = _round(sum(_num(row.get("amount")) for row in activity_rows))
        pre_activity_travel_total = _round(sum(_num(row.get("amount")) for row in pre_activity_travel))
        estimated_total = _round(pre_activity_travel_total + activity_total)
        out.append(
            {
                "event_id": f"travel_event_{idx}",
                "event_type": "travel",
                "travel_window_start": travel_start.isoformat(),
                "travel_window_end": travel_end.isoformat(),
                "activity_window_start": activity_start.isoformat(),
                "activity_window_end": activity_end.isoformat(),
                "travel_transaction_count": len(cluster),
                "activity_transaction_count": len(activity_rows),
                "travel_total": travel_total,
                "activity_window_total": activity_total,
                "pre_activity_travel_total": pre_activity_travel_total,
                "estimated_event_total": estimated_total,
                "category_breakdown": _event_breakdown(activity_rows, "category"),
                "merchant_examples": _event_top_values([*cluster, *activity_rows], "merchant", limit=8),
                "confidence": "high" if len(cluster) >= 3 and activity_total > 0 else "medium",
                "sample_evidence_ids": [f"txn:{row.get('id')}" for row in [*cluster[:5], *activity_rows[:5]] if row.get("id")][:10],
            }
        )
    out = sorted(out, key=lambda row: _num(row.get("estimated_event_total")), reverse=True)[: ctx["limit"]]
    summary = {
        "event_count": len(out),
        "top_event_total": out[0].get("estimated_event_total") if out else 0,
        "top_event_type": out[0].get("event_type") if out else None,
    }
    caveats = [] if out else ["No travel/event-like clusters were detected in the selected range."]
    caveats.append("Event clusters are read-only estimates from posted transactions; they are not user-confirmed trips.")
    return _result_for_rows(metric, ctx, out, summary, "travel category clusters with nearby activity-window spend", caveats=caveats)


def _cluster_travel_rows(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous: date | None = None
    for row in rows:
        current_date = _parse_date(row.get("date"))
        if current_date is None:
            continue
        if previous is not None and (current_date - previous).days > 14:
            if current:
                clusters.append(current)
            current = []
        current.append(row)
        previous = current_date
    if current:
        clusters.append(current)
    return clusters


def _event_breakdown(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row.get(key) or "Unknown")
        item = totals.setdefault(label, {key: label, "count": 0, "total": 0.0})
        item["count"] += 1
        item["total"] = _round(item["total"] + _num(row.get("amount")))
    return sorted(totals.values(), key=lambda item: _num(item.get("total")), reverse=True)[:8]


def _event_top_values(rows: list[dict[str, Any]], key: str, *, limit: int) -> list[str]:
    totals: dict[str, float] = {}
    for row in rows:
        label = str(row.get(key) or "Unknown")
        totals[label] = _round(totals.get(label, 0.0) + _num(row.get("amount")))
    return [label for label, _ in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:limit]]


def _private_discretionary_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT category,
               COUNT(*) AS transaction_count,
               ROUND(SUM(ABS(amount)), 2) AS total,
               ROUND(AVG(ABS(amount)), 2) AS avg_ticket,
               MIN(date) AS first_date,
               MAX(date) AS last_date,
               GROUP_CONCAT(id) AS ids
          FROM transactions_visible
         WHERE profile_id = ?
           AND category IN ({_placeholders(PRIVATE_DISCRETIONARY_CATEGORIES)})
           AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY category
         ORDER BY total DESC
         LIMIT ?
        """,
        [ctx["profile"], *PRIVATE_DISCRETIONARY_CATEGORIES, *_range_params(ctx["range"]), ctx["limit"]],
    )
    monthly = _fetch_all(
        conn,
        f"""
        SELECT category,
               substr(date, 1, 7) AS month,
               COUNT(*) AS count,
               ROUND(SUM(ABS(amount)), 2) AS total
          FROM transactions_visible
         WHERE profile_id = ?
           AND category IN ({_placeholders(PRIVATE_DISCRETIONARY_CATEGORIES)})
           AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY category, month
         ORDER BY category, month
        """,
        [ctx["profile"], *PRIVATE_DISCRETIONARY_CATEGORIES, *_range_params(ctx["range"])],
    )
    merchants = _fetch_all(
        conn,
        f"""
        SELECT category,
               COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, ''), category, 'Unknown') AS merchant,
               COUNT(*) AS count,
               ROUND(SUM(ABS(amount)), 2) AS total
          FROM transactions_visible
         WHERE profile_id = ?
           AND category IN ({_placeholders(PRIVATE_DISCRETIONARY_CATEGORIES)})
           AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY category, merchant
         ORDER BY category, total DESC
        """,
        [ctx["profile"], *PRIVATE_DISCRETIONARY_CATEGORIES, *_range_params(ctx["range"])],
    )
    monthly_by_category: dict[str, list[dict[str, Any]]] = {}
    for row in monthly:
        monthly_by_category.setdefault(str(row.get("category") or ""), []).append({"month": row.get("month"), "count": row.get("count"), "total": row.get("total")})
    merchants_by_category: dict[str, list[dict[str, Any]]] = {}
    for row in merchants:
        merchants_by_category.setdefault(str(row.get("category") or ""), []).append({"merchant": row.get("merchant"), "count": row.get("count"), "total": row.get("total")})

    out = []
    for row in rows:
        category = str(row.get("category") or "")
        item = _with_sample_ids(dict(row))
        item["monthly_series"] = list(reversed(monthly_by_category.get(category, [])[-12:]))
        item["top_merchants"] = merchants_by_category.get(category, [])[:5]
        item["sensitivity"] = "private"
        item["basis"] = "local-only private discretionary category totals; no motive inference"
        out.append(item)
    summary = {
        "category_count": len(out),
        "total": _round(sum(_num(row.get("total")) for row in out)),
        "top_category": out[0].get("category") if out else None,
        "top_total": out[0].get("total") if out else 0,
    }
    caveats = ["Private discretionary patterns are evidence for local analysis, not a judgment or motive inference."]
    if not out:
        caveats.append("No private discretionary category rows were found in the selected range.")
    return _result_for_rows(metric, ctx, out, summary, "local-only private discretionary spend pattern", caveats=caveats)


def _realistic_trim_levers_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    levers: list[dict[str, Any]] = []
    private = execute_metric(conn, {"metric": "private_discretionary_patterns", "range": ctx["range"].token, "limit": 12}, profile=ctx["profile"])
    private_rows = private.get("rows") or []
    for private_row in private_rows:
        category = str(private_row.get("category") or "Private discretionary")
        months = private_row.get("monthly_series") or []
        active_avg = _avg([row.get("total") for row in months if _num(row.get("total")) > 0])
        last_seen = _parse_date(private_row.get("last_date"))
        days_since = (_ctx_end_date(ctx) - last_seen).days if last_seen else None
        if days_since is not None and days_since >= 45 and active_avg > 0:
            levers.append(
                {
                    "lever_type": "protect_pause",
                    "subject": category,
                    "measured_amount": active_avg,
                    "amount_basis": "average active monthly spend before the visible pause",
                    "friction": "medium",
                    "action": "Protect the apparent pause instead of re-optimizing smaller categories first.",
                    "tradeoff": "Do not keep relitigating old spend if the pause is real.",
                    "caveat": "Only treat this as a pause if no newer transactions are missing from sync.",
                    "sample_evidence_ids": private_row.get("sample_evidence_ids") or [],
                }
            )
        months = sorted([row for row in private_row.get("monthly_series") or [] if _num(row.get("total")) > 0], key=lambda row: str(row.get("month") or ""))
        recent = months[-4:]
        latest = recent[-1] if recent else None
        baseline = _avg([row.get("total") for row in recent[:-1][-3:]])
        latest_total = _num(latest.get("total")) if latest else 0.0
        soft_ceiling_delta = _round(max(0.0, latest_total - baseline)) if baseline else 0.0
        if soft_ceiling_delta >= 50:
            levers.append(
                {
                    "lever_type": "soft_ceiling",
                    "subject": category,
                    "measured_amount": soft_ceiling_delta,
                    "amount_basis": "latest month above recent three-month baseline",
                    "friction": "low",
                    "action": "Use a soft ceiling near the earlier monthly rhythm rather than a zero-spend rule.",
                    "tradeoff": "Do not moralize the category; tune the monthly rhythm.",
                    "caveat": "If the latest month was event-related, do not treat it as the new baseline.",
                    "sample_evidence_ids": private_row.get("sample_evidence_ids") or [],
                }
            )

    leaks = execute_metric(conn, {"metric": "small_frequent_leak", "range": ctx["range"].token, "limit": 12}, profile=ctx["profile"])
    small_rows = [row for row in leaks.get("rows") or [] if _num(row.get("small_total")) >= 25]
    if small_rows:
        selected_rows = small_rows[:3]
        small_total = _round(sum(_num(row.get("small_total")) for row in selected_rows))
        top = selected_rows[0]
        subject = str(top.get("merchant") or top.get("category") or "Repeated small purchases")
        if len(selected_rows) > 1:
            subject = f"{subject} and similar small purchases"
        levers.append(
            {
                "lever_type": "purchase_consolidation",
                "subject": subject,
                "measured_amount": small_total,
                "amount_basis": "small purchases under the metric threshold in the selected range",
                "friction": "low",
                "action": "Consolidate small orders into a weekly cart or add a 48-hour pause.",
                "tradeoff": "This is a tune-up, not the main thesis.",
                "caveat": "Some small purchases may be household essentials.",
                "sample_evidence_ids": [eid for row in selected_rows for eid in (row.get("sample_evidence_ids") or [])][:8],
            }
        )

    category_drivers = execute_metric(conn, {"metric": "category_driver_decomposition", "range": ctx["range"].token, "limit": 20}, profile=ctx["profile"])
    fee = next((row for row in category_drivers.get("rows") or [] if row.get("category") == "Fees & Charges" and _num(row.get("delta_vs_baseline")) > 0), None)
    if fee:
        levers.append(
            {
                "lever_type": "inspect_fee",
                "subject": "Fees & Charges",
                "measured_amount": _round(fee.get("delta_vs_baseline")),
                "amount_basis": "category pressure above recent baseline",
                "friction": "low",
                "action": "Inspect the fee before cutting broad categories.",
                "tradeoff": "If it is one-off or miscategorized, do not turn it into a lifestyle rule.",
                "caveat": "Needs transaction review to decide whether it is avoidable.",
                "sample_evidence_ids": fee.get("sample_evidence_ids") or [],
            }
        )

    recurring = execute_metric(conn, {"metric": "recurring_obligation_calendar", "range": ctx["range"].token, "limit": 20}, profile=ctx["profile"])
    recurring_rows = recurring.get("rows") or []
    reviewable_recurring = max(
        (row for row in recurring_rows if _num(row.get("monthly_equivalent")) > 100 and row.get("category") not in {"Rent", "Mortgage"}),
        key=lambda row: _num(row.get("monthly_equivalent")),
        default=None,
    )
    if reviewable_recurring:
        levers.append(
            {
                "lever_type": "comparison_shop",
                "subject": reviewable_recurring.get("merchant") or reviewable_recurring.get("category") or "Recurring service",
                "measured_amount": _round(reviewable_recurring.get("monthly_equivalent")),
                "amount_basis": "monthly recurring obligation",
                "friction": "medium",
                "action": "Comparison-shop or renegotiate periodically; do not treat this as daily overspending.",
                "tradeoff": "The right move is vendor review, not broad restraint.",
                "caveat": "Coverage or service-level changes can create real risk, so compare like-for-like.",
                "sample_evidence_ids": [f"metric:recurring_obligation_calendar:{idx}" for idx, row in enumerate(recurring_rows, start=1) if row is reviewable_recurring][:1],
            }
        )

    levers = sorted(levers, key=lambda row: (_lever_priority(row), _num(row.get("measured_amount"))), reverse=True)[: ctx["limit"]]
    summary = {
        "lever_count": len(levers),
        "top_subject": levers[0].get("subject") if levers else None,
        "top_measured_amount": levers[0].get("measured_amount") if levers else 0,
    }
    caveats = ["Levers are ranked for practical review, not automatic spending cuts."]
    return _result_for_rows(metric, ctx, levers, summary, "safe candidate levers from private patterns, small leaks, drivers, and recurring obligations", caveats=caveats)


def _financial_timeline_events_handler(conn, metric: str, ctx: dict[str, Any]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    range_token = ctx["range"].token

    income = execute_metric(conn, {"metric": "income_source_continuity", "range": range_token, "limit": 12}, profile=ctx["profile"])
    income_rows = income.get("rows") or []
    income_control = income_rows[0] if income_rows else {}
    income_status = str(income.get("summary_numbers", {}).get("status") or "")
    if income_status in {"changed_source", "unlabeled_or_changed_source", "late_against_cadence", "incomplete_current_month"}:
        latest_month = income_control.get("latest_complete_month")
        latest_month_row = next((row for row in income_rows[1:] if row.get("month") == latest_month), {})
        event_date = f"{latest_month}-01" if latest_month else ctx["range"].end
        income_ids: list[str] = []
        if latest_month and _MONTH_RE.match(str(latest_month)):
            month_start = date.fromisoformat(f"{latest_month}-01")
            month_end = _month_end(month_start)
            income_ids = [
                f"txn:{row.get('id')}"
                for row in _fetch_all(
                    conn,
                    """
                    SELECT id
                      FROM transactions_visible
                     WHERE profile_id = ?
                       AND category = 'Income'
                       AND amount >= 100
                       AND date >= ?
                       AND date <= ?
                     ORDER BY amount DESC
                     LIMIT 6
                    """,
                    [ctx["profile"], month_start.isoformat(), month_end.isoformat()],
                )
                if row.get("id")
            ]
        events.append(
            {
                "event_type": "income_continuity",
                "event_date": event_date,
                "period": latest_month,
                "subject": "Income source continuity",
                "measured_amount": latest_month_row.get("total_income") or income_control.get("current_month_income"),
                "amount_basis": "material income in the latest complete/in-scope month",
                "importance_score": 95,
                "interpretation_hint": income_control.get("reason"),
                "action_hint": "Confirm whether the source change or unlabeled income row reflects the current income setup.",
                "caveat": "; ".join(income.get("caveats") or [])[:160],
                "sample_evidence_ids": income_ids or (income.get("evidence_ids") or [])[:6],
            }
        )

    floor = execute_metric(conn, {"metric": "floor_burn", "range": range_token, "limit": 8}, profile=ctx["profile"])
    floor_summary = floor.get("summary_numbers") or {}
    housing_rows = _fetch_all(
        conn,
        f"""
        SELECT substr(date, 1, 7) AS month,
               MIN(date) AS first_date,
               MAX(date) AS last_date,
               ROUND(SUM(ABS(amount)), 2) AS total,
               COUNT(*) AS count,
               GROUP_CONCAT(id) AS ids
          FROM transactions_visible
         WHERE profile_id = ?
           AND category IN ({_placeholders(STRUCTURAL_FLOOR_CATEGORIES)})
           AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY month
         ORDER BY month
        """,
        [ctx["profile"], *STRUCTURAL_FLOOR_CATEGORIES, *_range_params(ctx["range"])],
    )
    if _num(floor_summary.get("floor_burn_monthly")) > 0 and housing_rows:
        first_housing = housing_rows[0]
        housing_ids = [f"txn:{txid}" for row in housing_rows for txid in str(row.get("ids") or "").split(",") if txid][:8]
        events.append(
            {
                "event_type": "fixed_floor",
                "event_date": first_housing.get("first_date"),
                "period": first_housing.get("month"),
                "subject": "Fixed monthly floor",
                "measured_amount": floor_summary.get("floor_burn_monthly"),
                "amount_basis": "housing-like spend plus deduped recurring commitments",
                "housing_monthly": floor_summary.get("housing_monthly"),
                "recurring_monthly": floor_summary.get("recurring_monthly"),
                "importance_score": 90,
                "interpretation_hint": "This is the monthly floor before flexible spending decisions.",
                "action_hint": "Use this as the first constraint when judging surplus, runway, and goals.",
                "caveat": "; ".join(floor.get("caveats") or [])[:160],
                "sample_evidence_ids": housing_ids or (floor.get("evidence_ids") or [])[:6],
            }
        )

    travel = execute_metric(conn, {"metric": "spending_event_clusters", "range": range_token, "limit": 5}, profile=ctx["profile"])
    for row in travel.get("rows") or []:
        events.append(
            {
                "event_type": "travel_or_event_cluster",
                "event_date": row.get("travel_window_start"),
                "window_start": row.get("travel_window_start"),
                "window_end": row.get("activity_window_end") or row.get("travel_window_end"),
                "subject": "Travel/event cluster",
                "measured_amount": row.get("estimated_event_total"),
                "amount_basis": "travel bookings plus nearby activity-window spend",
                "importance_score": 80,
                "interpretation_hint": "Treat this as a likely event/trip, not ordinary recurring lifestyle drift.",
                "action_hint": "Separate it from the baseline before deciding what actually changed.",
                "caveat": "Event clusters are estimates until user-confirmed.",
                "sample_evidence_ids": row.get("sample_evidence_ids") or [],
            }
        )

    private = execute_metric(conn, {"metric": "private_discretionary_patterns", "range": range_token, "limit": 12}, profile=ctx["profile"])
    for private_row in private.get("rows") or []:
        category = str(private_row.get("category") or "Private discretionary")
        months = private_row.get("monthly_series") or []
        active_avg = _avg([row.get("total") for row in months if _num(row.get("total")) > 0])
        last_seen = _parse_date(private_row.get("last_date"))
        days_since = (_ctx_end_date(ctx) - last_seen).days if last_seen else None
        if days_since is not None and days_since >= 45 and active_avg > 0:
            events.append(
                {
                    "event_type": "private_spend_pause",
                    "event_date": private_row.get("last_date"),
                    "period": str(private_row.get("last_date") or "")[:7],
                    "subject": category,
                    "measured_amount": active_avg,
                    "amount_basis": "average active monthly spend before the visible pause",
                    "importance_score": 78,
                    "sensitivity": "private",
                    "interpretation_hint": "The useful read is to protect the pause if it is real, not keep relitigating old spend.",
                    "action_hint": "Confirm sync completeness, then preserve the behavior change.",
                    "caveat": "Local-only sensitive category; no motive inference.",
                    "sample_evidence_ids": private_row.get("sample_evidence_ids") or [],
                }
            )

        months = sorted([row for row in private_row.get("monthly_series") or [] if _num(row.get("total")) > 0], key=lambda row: str(row.get("month") or ""))
        latest = months[-1] if months else None
        baseline = _avg([row.get("total") for row in months[:-1][-3:]])
        latest_total = _num(latest.get("total")) if latest else 0.0
        delta = _round(max(0.0, latest_total - baseline)) if baseline else 0.0
        if latest and delta >= 50:
            events.append(
                {
                    "event_type": "private_spend_rhythm_change",
                    "event_date": f"{latest.get('month')}-01",
                    "period": latest.get("month"),
                    "subject": category,
                    "measured_amount": delta,
                    "amount_basis": "latest active month above recent three-month rhythm",
                    "importance_score": 58,
                    "sensitivity": "private",
                    "interpretation_hint": "This is a soft-ceiling candidate, not a morality read.",
                    "action_hint": "Reset the monthly rhythm if it was not event-related.",
                    "caveat": "Do not treat event-related months as the new baseline.",
                    "sample_evidence_ids": private_row.get("sample_evidence_ids") or [],
                }
            )

    category_drivers = execute_metric(conn, {"metric": "category_driver_decomposition", "range": range_token, "limit": 20}, profile=ctx["profile"])
    fee = next((row for row in category_drivers.get("rows") or [] if row.get("category") == "Fees & Charges" and _num(row.get("delta_vs_baseline")) > 0), None)
    if fee:
        current_month = fee.get("current_month")
        events.append(
            {
                "event_type": "fee_pressure",
                "event_date": f"{current_month}-01" if current_month else ctx["range"].end,
                "period": current_month,
                "subject": "Fees & Charges",
                "measured_amount": fee.get("delta_vs_baseline"),
                "amount_basis": "category pressure above recent baseline",
                "importance_score": 70,
                "interpretation_hint": "This is a better first inspection point than broad category trimming.",
                "action_hint": "Review whether the fee is avoidable, one-off, or miscategorized.",
                "caveat": "Do not convert a one-off fee into a lifestyle rule.",
                "sample_evidence_ids": fee.get("sample_evidence_ids") or [],
            }
        )

    recurring = execute_metric(conn, {"metric": "recurring_obligation_calendar", "range": range_token, "limit": 20}, profile=ctx["profile"])
    recurring_rows = recurring.get("rows") or []
    large_recurring = max(
        (row for row in recurring_rows if _num(row.get("monthly_equivalent")) >= 100 and row.get("category") != "Rent"),
        key=lambda row: _num(row.get("monthly_equivalent")),
        default=None,
    )
    if large_recurring:
        idx = recurring_rows.index(large_recurring) + 1
        events.append(
            {
                "event_type": "upcoming_recurring_constraint",
                "event_date": large_recurring.get("next_expected_date"),
                "subject": large_recurring.get("merchant") or large_recurring.get("category") or "Recurring obligation",
                "measured_amount": large_recurring.get("monthly_equivalent"),
                "amount_basis": "monthly equivalent of a deduped recurring obligation",
                "importance_score": 62,
                "interpretation_hint": "This is a vendor/commitment review lever, not day-to-day overspending.",
                "action_hint": "Compare, renegotiate, or monitor before cutting flexible categories.",
                "caveat": "Compare like-for-like before changing coverage or service level.",
                "sample_evidence_ids": [f"metric:recurring_obligation_calendar:{idx}"],
            }
        )

    liability = execute_metric(conn, {"metric": "liability_to_cash_ratio", "range": range_token, "limit": 8}, profile=ctx["profile"])
    liability_summary = liability.get("summary_numbers") or {}
    if _num(liability_summary.get("liability_total")) > 0:
        events.append(
            {
                "event_type": "liability_position",
                "event_date": ctx["range"].end,
                "subject": "Debt/liability position",
                "measured_amount": liability_summary.get("liability_total"),
                "amount_basis": "active liability balances compared with cash-like balances",
                "cash_like_balance": liability_summary.get("cash_like_balance"),
                "liability_to_cash_ratio": liability_summary.get("liability_to_cash_ratio"),
                "importance_score": 50,
                "interpretation_hint": "Debt pressure should be judged against cash coverage, not in isolation.",
                "action_hint": "Monitor terms and payment rhythm; prioritize only if pressure rises.",
                "caveat": "; ".join(liability.get("caveats") or [])[:160],
                "sample_evidence_ids": ["metric:liability_to_cash_ratio:summary"],
            }
        )

    events = [event for event in events if event.get("subject") and str(event.get("subject")) != "Unknown"]
    events = _dedupe(events)
    events = sorted(events, key=lambda row: (_timeline_date_key(row), -_num(row.get("importance_score"))))[: ctx["limit"]]
    most_important = max(events, key=lambda row: _num(row.get("importance_score")), default={})
    summary = {
        "event_count": len(events),
        "top_event_type": most_important.get("event_type"),
        "top_subject": most_important.get("subject"),
        "top_measured_amount": most_important.get("measured_amount"),
        "span_start": events[0].get("event_date") if events else None,
        "span_end": events[-1].get("event_date") if events else None,
    }
    caveats = ["Timeline events are measured advisor packets, not user-confirmed narrative labels."]
    if not events:
        caveats.append("No material timeline events were detected in the selected range.")
    return _result_for_rows(metric, ctx, events, summary, "chronological synthesis from safe measurements and constrained transaction aggregates", caveats=caveats)


def _lever_priority(row: dict[str, Any]) -> int:
    return {
        "protect_pause": 5,
        "inspect_fee": 4,
        "soft_ceiling": 3,
        "purchase_consolidation": 2,
        "comparison_shop": 1,
    }.get(str(row.get("lever_type") or ""), 0)


def _leakage_priority(row: dict[str, Any]) -> int:
    return {
        "fee_or_interest": 2,
        "recurring_duplicate_record": 1,
    }.get(str(row.get("leakage_type") or ""), 0)


def _timeline_date_key(row: dict[str, Any]) -> str:
    for key in ("event_date", "window_start", "period"):
        value = row.get(key)
        if not value:
            continue
        text = str(value)
        if _DATE_RE.match(text[:10]):
            return text[:10]
        if _MONTH_RE.match(text[:7]):
            return f"{text[:7]}-01"
    return "9999-12-31"


def _included_vs_excluded(conn, ctx: dict[str, Any]) -> dict[str, Any]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT substr(date, 1, 7) AS month,
               ROUND(SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END), 2) AS all_outflows,
               ROUND(SUM(CASE WHEN {_expense_sql()} THEN ABS(amount) ELSE 0 END), 2) AS spending_outflows
          FROM transactions_visible
         WHERE profile_id = ?{_range_clause(ctx['range'])}
         GROUP BY month
         ORDER BY month
         LIMIT ?
        """,
        [ctx["profile"], *_range_params(ctx["range"]), ctx["limit"]],
    )
    for row in rows:
        row["excluded_outflows"] = _round(_num(row.get("all_outflows")) - _num(row.get("spending_outflows")))
    return {"rows": rows, "summary_numbers": {"excluded_outflows": _round(sum(_num(r.get("excluded_outflows")) for r in rows))}}


def _dimension_monthly(conn, ctx: dict[str, Any], dimension: str) -> list[dict[str, Any]]:
    if dimension == "merchant":
        dim_expr = "COALESCE(NULLIF(merchant_name,''), NULLIF(merchant_key,''), category, 'Unknown')"
    else:
        dim_expr = "COALESCE(category, 'Unknown')"
    rows = _fetch_all(
        conn,
        f"""
        SELECT {dim_expr} AS dim,
               substr(date, 1, 7) AS month,
               COUNT(*) AS count,
               ROUND(SUM(ABS(amount)), 2) AS total,
               GROUP_CONCAT(id) AS ids
          FROM transactions_visible
         WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY dim, month
         ORDER BY month, total DESC
        """,
        [ctx["profile"], *_range_params(ctx["range"])],
    )
    return rows


def _driver_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    by_dim: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_dim.setdefault(str(row.get("dim") or "Unknown"), []).append(row)
    out = []
    for dim, items in by_dim.items():
        items = sorted(items, key=lambda r: str(r.get("month") or ""))
        current = items[-1] if items else {}
        prior = items[:-1][-3:]
        baseline = _avg([r.get("total") for r in prior]) if prior else 0.0
        current_total = _num(current.get("total"))
        current_count = _num(current.get("count"))
        prior_count = _avg([r.get("count") for r in prior]) if prior else 0.0
        out.append(
            {
                key: dim,
                "current_month": current.get("month"),
                "current_total": _round(current_total),
                "baseline_total": _round(baseline),
                "delta_vs_baseline": _round(current_total - baseline),
                "current_count": current_count,
                "baseline_count": _round(prior_count),
                "sample_evidence_ids": [f"txn:{v}" for v in str(current.get("ids") or "").split(",")[:5] if v],
            }
        )
    return sorted(out, key=lambda r: abs(_num(r.get("delta_vs_baseline"))), reverse=True)


def _driver_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    top = next((row for row in rows if _num(row.get("delta_vs_baseline")) > 0), rows[0] if rows else {})
    return {
        "driver_count": len(rows),
        "top_driver": top.get("category") or top.get("merchant") if top else None,
        "top_delta": top.get("delta_vs_baseline") if top else 0,
    }


def _event_windows(rows: list[dict[str, Any]]) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    for row in rows:
        start = _parse_date(row.get("activity_window_start") or row.get("travel_window_start") or row.get("window_start"))
        end = _parse_date(row.get("activity_window_end") or row.get("travel_window_end") or row.get("window_end"))
        if start and end and start <= end:
            windows.append((start, end))
    return windows


def _row_in_event_window(row_date: date, category: str, windows: list[tuple[date, date]]) -> bool:
    if category not in EVENT_ACTIVITY_CATEGORIES:
        return False
    return any(start <= row_date <= end for start, end in windows)


def _complete_month_keys(rng: ResolvedRange, observed_months: list[str]) -> set[str]:
    end_date = _parse_date(rng.end) or date.today()
    complete: set[str] = set()
    for month in observed_months:
        if not _MONTH_RE.match(month):
            continue
        month_start = date.fromisoformat(f"{month}-01")
        if _month_end(month_start) <= end_date:
            complete.add(month)
    return complete or {month for month in observed_months if _MONTH_RE.match(month)}


def _ctx_end_date(ctx: dict[str, Any]) -> date:
    return _parse_date(getattr(ctx.get("range"), "end", None)) or date.today()


def _money_map_role(category: Any) -> str:
    text = str(category or "").strip()
    lowered = text.lower()
    if text in STRUCTURAL_FLOOR_CATEGORIES:
        return "structural_floor"
    if text in PRIVATE_DISCRETIONARY_CATEGORIES:
        return "private_discretionary"
    if "fee" in lowered or "interest" in lowered:
        return "avoidable_leakage"
    if "tax" in lowered:
        return "tax_or_irregular"
    if text in {"Travel", "Entertainment"}:
        return "event_or_irregular"
    if text in {"Insurance", "Subscriptions", "Membership"}:
        return "recurring_or_vendor_review"
    return "flexible_living"


def _money_map_controllability(category: Any) -> str:
    text = str(category or "").strip()
    lowered = text.lower()
    if text in STRUCTURAL_FLOOR_CATEGORIES:
        return "low"
    if "fee" in lowered or "interest" in lowered:
        return "high"
    if "tax" in lowered:
        return "low"
    if text in {"Shopping", "Food & Dining", "Dining", "Alcohol", "Vaping", "Entertainment"}:
        return "medium"
    if text in {"Insurance", "Subscriptions", "Membership"}:
        return "reviewable"
    return "medium"


def _category_spend_map(conn, ctx: dict[str, Any]) -> dict[str, float]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT COALESCE(category, 'Unknown') AS category, ROUND(SUM(ABS(amount)), 2) AS total
          FROM transactions_visible
         WHERE profile_id = ? AND {_expense_sql()}{_range_clause(ctx['range'])}
         GROUP BY category
        """,
        [ctx["profile"], *_range_params(ctx["range"])],
    )
    return {str(r.get("category")): _num(r.get("total")) for r in rows}


def _account_rows(conn, ctx: dict[str, Any], *, cash_like: bool = False, liability_like: bool = False) -> list[dict[str, Any]]:
    rows = _fetch_all(
        conn,
        """
        SELECT id, institution_name, account_name, account_type, account_subtype,
               ROUND(current_balance, 2) AS current_balance,
               ROUND(available_balance, 2) AS available_balance,
               last_synced_at, is_active
          FROM accounts
         WHERE profile_id = ? AND is_active = 1
         ORDER BY account_type, institution_name, account_name
        """,
        [ctx["profile"]],
    )
    if cash_like:
        rows = [r for r in rows if _cash_like(r)]
    if liability_like:
        rows = [r for r in rows if _liability_like(r)]
    return rows[: ctx.get("limit", DEFAULT_LIMIT)]


def _cash_balance(conn, profile: str) -> float:
    rows = _account_rows(conn, {"profile": profile, "limit": 200}, cash_like=True)
    return _round(sum(max(_num(r.get("available_balance")) or _num(r.get("current_balance")), 0.0) for r in rows))


def _recent_monthly_expenses(conn, ctx: dict[str, Any]) -> float:
    rows = execute_metric(conn, {"metric": "monthly_spend_series", "range": "last_3_months", "limit": 3}, profile=ctx["profile"]).get("rows") or []
    return _avg([r.get("expenses") for r in rows])


def _recent_monthly_income(conn, ctx: dict[str, Any]) -> float:
    rows = execute_metric(conn, {"metric": "income_series", "range": "last_3_months", "limit": 3}, profile=ctx["profile"]).get("rows") or []
    return _avg([r.get("income") for r in rows])


def _recent_monthly_savings(conn, ctx: dict[str, Any]) -> float:
    rows = execute_metric(conn, {"metric": "savings_rate_trend", "range": "last_3_months", "limit": 3}, profile=ctx["profile"]).get("rows") or []
    return _avg([r.get("net") for r in rows])


def _recurring_monthly_total(conn, profile: str) -> float:
    rows = _fetch_all(
        conn,
        """
        SELECT display_name AS merchant,
               merchant_key,
               ROUND(amount_cents / 100.0, 2) AS amount,
               frequency,
               anchor_day,
               next_expected_date,
               state,
               confidence_label,
               confidence_score
          FROM recurring_obligations
         WHERE profile_id = ? AND state IN ('active','confirmed','candidate')
        """,
        [profile],
    )
    return _round(sum(_num(r.get("monthly_equivalent")) for r in _dedupe_recurring_rows(rows)))


def _fetch_all(conn, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    columns = [desc[0] for desc in cur.description or []]
    out = []
    for row in cur.fetchall():
        if hasattr(row, "keys"):
            out.append(dict(row))
        else:
            out.append({columns[idx]: row[idx] for idx in range(len(columns))})
    return out


def _fetch_one(conn, sql: str, params: list[Any]) -> dict[str, Any]:
    rows = _fetch_all(conn, sql, params)
    return rows[0] if rows else {}


def _result_for_rows(metric: str, ctx: dict[str, Any], rows: list[dict[str, Any]], summary: dict[str, Any], basis: str, *, caveats: list[str] | None = None) -> dict[str, Any]:
    spec = METRIC_SPECS[metric]
    evidence_ids = _evidence_ids_from_rows(metric, rows)
    return _result(
        metric=metric,
        domain=spec.domain,
        time_range=ctx["range"],
        basis=basis,
        rows=[_compact_row(r) for r in rows],
        summary_numbers=_scalar_summary(summary),
        confidence="high" if rows else "low",
        caveats=caveats or ([] if rows else ["No matching rows were available for this metric."]),
        evidence_ids=evidence_ids,
    )


def _result(
    *,
    metric: str,
    domain: str,
    time_range: ResolvedRange,
    basis: str,
    rows: list[dict[str, Any]],
    summary_numbers: dict[str, Any],
    confidence: str,
    caveats: list[str],
    evidence_ids: list[str],
) -> dict[str, Any]:
    return {
        "metric": metric,
        "domain": domain,
        "time_range": {"token": time_range.token, "start": time_range.start, "end": time_range.end, "label": time_range.label},
        "basis": basis,
        "rows": rows,
        "summary_numbers": summary_numbers,
        "confidence": _confidence(confidence),
        "caveats": _dedupe([str(c) for c in caveats if c]),
        "evidence_ids": _dedupe(evidence_ids)[:80],
    }


def _missing_metric_result(metric: str, query: dict[str, Any]) -> dict[str, Any]:
    rng = resolve_time_range(query.get("range") or "last_6_months")
    return _result(
        metric=metric or "unknown",
        domain="unsupported",
        time_range=rng,
        basis="Unsupported metric was not executed.",
        rows=[],
        summary_numbers={},
        confidence="low",
        caveats=["Unsupported metric. A missing_metric_proposal should be created instead of unsafe SQL."],
        evidence_ids=[],
    )


def _candidate_drivers(measurements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for result in measurements:
        if "driver" not in result.get("metric", "") and result.get("metric") not in {"small_frequent_leak", "subscription_cluster", "budget_variance"}:
            continue
        for row in result.get("rows") or []:
            delta = _num(row.get("delta_vs_baseline") or row.get("total") or row.get("remaining"))
            if delta <= 0 and result.get("metric") not in {"budget_variance", "subscription_cluster"}:
                continue
            rows.append({"metric": result.get("metric"), "subject": row.get("category") or row.get("merchant") or row.get("bucket") or row.get("category"), "amount": _round(delta), "basis": result.get("basis"), "evidence_ids": row.get("sample_evidence_ids") or result.get("evidence_ids", [])[:5]})
    return sorted(rows, key=lambda r: abs(_num(r.get("amount"))), reverse=True)[:8]


def _false_alarms(measurements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for result in measurements:
        if "false_alarm" not in result.get("metric", "") and result.get("metric") not in {"refund_or_transfer_noise", "seasonality_or_sparse_data"}:
            continue
        for row in result.get("rows") or []:
            out.append({"metric": result.get("metric"), "subject": row.get("category") or row.get("merchant") or row.get("month") or "profile", "reason": row.get("false_alarm_reason") or "noise or low materiality", "basis": result.get("basis")})
    return out[:8]


def _constraints(measurements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for result in measurements:
        if result.get("domain") in {"cash_resilience", "budgets_plans", "debt_liabilities"}:
            out.append({"metric": result.get("metric"), "summary_numbers": result.get("summary_numbers") or {}, "confidence": result.get("confidence"), "caveats": result.get("caveats") or []})
    return out[:8]


def _smallest_levers(measurements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for result in measurements:
        if result.get("metric") in {"small_frequent_leak", "subscription_cluster", "category_budget_pressure", "merchant_driver_decomposition", "smallest_goal_rescue_lever"}:
            for row in result.get("rows") or []:
                subject = row.get("merchant") or row.get("category") or row.get("bucket") or row.get("name")
                amount = row.get("small_total") or row.get("total") or row.get("delta_vs_baseline") or row.get("remaining")
                out.append({"metric": result.get("metric"), "subject": subject, "amount": amount, "move": "review_or_reduce", "evidence_ids": row.get("sample_evidence_ids") or result.get("evidence_ids", [])[:5]})
    return sorted(out, key=lambda r: abs(_num(r.get("amount"))), reverse=True)[:8]


def _safe_snapshot_counts(conn) -> dict[str, Any]:
    counts = {}
    for table in ("transactions_visible", "accounts", "category_budgets", "goals", "recurring_obligations", "net_worth_history", "transaction_enrichment"):
        try:
            counts[table] = int(_fetch_one(conn, f"SELECT COUNT(*) AS count FROM {table}", []).get("count") or 0)
        except Exception:
            counts[table] = None
    return counts


def _advisor_period_scope(conn, ctx: dict[str, Any]) -> dict[str, Any]:
    profile = ctx["profile"]
    data = _fetch_one(
        conn,
        "SELECT COUNT(*) AS count, MIN(date) AS min_date, MAX(date) AS max_date, COUNT(DISTINCT substr(date, 1, 7)) AS months FROM transactions_visible WHERE profile_id = ?",
        [profile],
    )
    first_income = _fetch_one(
        conn,
        """
        SELECT MIN(date) AS first_income_date
          FROM transactions_visible
         WHERE profile_id = ? AND category = 'Income' AND amount > 0
           AND COALESCE(expense_type, '') NOT IN ('transfer_internal', 'transfer_household')
        """,
        [profile],
    ).get("first_income_date")
    min_date = _parse_date(data.get("min_date"))
    max_date = _parse_date(data.get("max_date"))
    range_start = _parse_date(ctx["range"].start) or min_date
    range_end = _parse_date(ctx["range"].end) or max_date or date.today()
    first_income_date = _parse_date(first_income)
    analysis_start_date = range_start or min_date
    if first_income_date and analysis_start_date and first_income_date > analysis_start_date:
        analysis_start_date = first_income_date.replace(day=1)
    analysis_end_date = min([d for d in (range_end, max_date) if d] or [date.today()])
    analysis_start = analysis_start_date.isoformat() if analysis_start_date else None
    analysis_end = analysis_end_date.isoformat() if analysis_end_date else None
    observed_months = []
    if analysis_start and analysis_end:
        observed_months = [
            str(row.get("month"))
            for row in _fetch_all(
                conn,
                """
                SELECT DISTINCT substr(date, 1, 7) AS month
                  FROM transactions_visible
                 WHERE profile_id = ? AND date >= ? AND date <= ?
                 ORDER BY month
                """,
                [profile, analysis_start, analysis_end],
            )
            if _MONTH_RE.match(str(row.get("month") or ""))
        ]
    scoped_range = ResolvedRange(ctx["range"].token, analysis_start, analysis_end, ctx["range"].label or "advisor analysis period")
    complete_months = sorted(_complete_month_keys(scoped_range, observed_months))
    if analysis_end_date and observed_months:
        last_month = observed_months[-1]
        latest_month_start = date.fromisoformat(f"{last_month}-01")
        current_month_partial = _month_end(latest_month_start) > analysis_end_date
    else:
        current_month_partial = False
    recent_3 = complete_months[-3:]
    prior_9 = complete_months[-12:-3]
    tx_count = _fetch_one(
        conn,
        "SELECT COUNT(*) AS count FROM transactions_visible WHERE profile_id = ? AND date >= ? AND date <= ?",
        [profile, analysis_start or "0000-01-01", analysis_end or "9999-12-31"],
    ).get("count")
    return {
        "data_start": data.get("min_date"),
        "data_end": data.get("max_date"),
        "visible_transaction_count": data.get("count"),
        "visible_month_count": data.get("months"),
        "first_income_date": first_income,
        "range_start": range_start.isoformat() if range_start else None,
        "range_end": range_end.isoformat() if range_end else None,
        "analysis_start": analysis_start,
        "analysis_end": analysis_end,
        "analysis_transaction_count": tx_count,
        "analysis_month_count": len(observed_months),
        "complete_months": complete_months,
        "complete_month_count": len(complete_months),
        "first_complete_month": complete_months[0] if complete_months else None,
        "last_complete_month": complete_months[-1] if complete_months else None,
        "recent_3_complete_months": recent_3,
        "prior_9_complete_months": prior_9,
        "current_month_partial": current_month_partial,
    }


def _advisor_monthly_cash_flow(conn, ctx: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    raw_rows = _fetch_all(
        conn,
        """
        SELECT id, date, substr(date, 1, 7) AS month, ROUND(amount, 2) AS amount,
               COALESCE(category, '') AS category,
               COALESCE(expense_type, '') AS expense_type
          FROM transactions_visible
         WHERE profile_id = ? AND date >= ? AND date <= ?
         ORDER BY date
        """,
        [ctx["profile"], scope["analysis_start"], scope["analysis_end"]],
    )
    months: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        month = str(row.get("month") or "")
        item = months.setdefault(
            month,
            {
                "month": month,
                "income": 0.0,
                "gross_spending": 0.0,
                "credits_refunds": 0.0,
                "incoming_external_transfers": 0.0,
                "outgoing_external_transfers": 0.0,
                "expense_count": 0,
            },
        )
        amount = _num(row.get("amount"))
        category = str(row.get("category") or "")
        expense_type = str(row.get("expense_type") or "")
        if expense_type == "transfer_external":
            if amount > 0:
                item["incoming_external_transfers"] = _round(_num(item.get("incoming_external_transfers")) + amount)
            elif amount < 0:
                item["outgoing_external_transfers"] = _round(_num(item.get("outgoing_external_transfers")) + abs(amount))
            continue
        if amount > 0 and category == "Income" and expense_type not in {"transfer_internal", "transfer_household"}:
            item["income"] = _round(_num(item.get("income")) + amount)
        elif amount > 0 and category != "Income" and expense_type not in {"transfer_internal", "transfer_household"}:
            item["credits_refunds"] = _round(_num(item.get("credits_refunds")) + amount)
        elif amount < 0 and _advisor_row_is_spend(category, expense_type):
            item["gross_spending"] = _round(_num(item.get("gross_spending")) + abs(amount))
            item["expense_count"] += 1
    pressure_by_month = _top_spend_pressure_by_month(conn, ctx, scope)
    rows = []
    for month, item in sorted(months.items()):
        net = _round(
            _num(item.get("income"))
            + _num(item.get("credits_refunds"))
            + _num(item.get("incoming_external_transfers"))
            - _num(item.get("gross_spending"))
            - _num(item.get("outgoing_external_transfers"))
        )
        income = _num(item.get("income"))
        rows.append(
            {
                **item,
                "net_spending_after_credits": _round(_num(item.get("gross_spending")) - _num(item.get("credits_refunds"))),
                "net_cash_flow": net,
                "cash_flow_rate": _round(net / income, 4) if income else None,
                "top_pressure": pressure_by_month.get(month),
            }
        )
    return rows


def _cash_flow_period_row(period: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    income = _round(sum(_num(row.get("income")) for row in rows))
    gross = _round(sum(_num(row.get("gross_spending")) for row in rows))
    credits = _round(sum(_num(row.get("credits_refunds")) for row in rows))
    outgoing = _round(sum(_num(row.get("outgoing_external_transfers")) for row in rows))
    incoming = _round(sum(_num(row.get("incoming_external_transfers")) for row in rows))
    net = _round(income + credits + incoming - gross - outgoing)
    return {
        "period": period,
        "month_count": len(rows),
        "income": income,
        "gross_spending": gross,
        "credits_refunds": credits,
        "outgoing_external_transfers": outgoing,
        "incoming_external_transfers": incoming,
        "net_cash_flow": net,
        "avg_monthly_income": _round(income / len(rows)) if rows else 0.0,
        "avg_monthly_gross_spending": _round(gross / len(rows)) if rows else 0.0,
        "avg_monthly_net_cash_flow": _round(net / len(rows)) if rows else 0.0,
        "cash_flow_rate": _round(net / income, 4) if income else None,
        "negative_month_count": sum(1 for row in rows if _num(row.get("net_cash_flow")) < 0),
    }


def _advisor_spending_rows(conn, ctx: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    return _fetch_all(
        conn,
        f"""
        SELECT id,
               date,
               substr(date, 1, 7) AS month,
               COALESCE(NULLIF(category, ''), 'Uncategorized') AS category,
               COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, ''), NULLIF(counterparty_name, ''), category, 'Merchant') AS merchant,
               ROUND(ABS(amount), 2) AS amount,
               COALESCE(confidence, '') AS confidence,
               COALESCE(expense_type, '') AS expense_type
          FROM transactions_visible
         WHERE profile_id = ?
           AND date >= ? AND date <= ?
           AND {_advisor_spend_sql()}
         ORDER BY date
        """,
        [ctx["profile"], scope["analysis_start"], scope["analysis_end"]],
    )


def _top_spend_pressure_by_month(conn, ctx: dict[str, Any], scope: dict[str, Any]) -> dict[str, str]:
    rows = _fetch_all(
        conn,
        f"""
        SELECT month, category, total
          FROM (
            SELECT substr(date, 1, 7) AS month,
                   COALESCE(NULLIF(category, ''), 'Uncategorized') AS category,
                   ROUND(SUM(ABS(amount)), 2) AS total,
                   ROW_NUMBER() OVER (PARTITION BY substr(date, 1, 7) ORDER BY SUM(ABS(amount)) DESC) AS rn
              FROM transactions_visible
             WHERE profile_id = ? AND date >= ? AND date <= ? AND {_advisor_spend_sql()}
             GROUP BY month, category
          )
         WHERE rn = 1
        """,
        [ctx["profile"], scope["analysis_start"], scope["analysis_end"]],
    )
    return {str(row.get("month")): f"{row.get('category')} {_round(row.get('total'))}" for row in rows}


def _advisor_row_is_spend(category: str, expense_type: str) -> bool:
    return bool(category not in (*NON_SPENDING_CATEGORIES, "Credits & Refunds") and expense_type not in {"transfer_internal", "transfer_household", "transfer_external"})


def _advisor_spend_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    categories = ",".join("'" + value.replace("'", "''") + "'" for value in (*NON_SPENDING_CATEGORIES, "Credits & Refunds"))
    return (
        f"{prefix}amount < 0 "
        f"AND COALESCE({prefix}category, '') NOT IN ({categories}) "
        f"AND COALESCE({prefix}expense_type, '') NOT IN ('transfer_internal', 'transfer_household', 'transfer_external')"
    )


def _merchant_lifecycle_group(merchant: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", str(merchant or "").upper())
    parts = [part for part in text.split() if part not in {"THE", "INC", "LLC", "STORE", "MARKETPLACE"}]
    if not parts:
        return "MERCHANT"
    if parts[0] == "AMAZON":
        return "AMAZON"
    return parts[0]


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {str(row.get("name")) for row in _fetch_all(conn, f"PRAGMA table_info('{table}')", [])}
    except Exception:
        return set()


def _safe_table_count(conn, table: str) -> int | None:
    try:
        return int(_fetch_one(conn, f"SELECT COUNT(*) AS count FROM {table}", []).get("count") or 0)
    except Exception:
        return None


def _spec_public(spec: MetricSpec) -> dict[str, Any]:
    return {
        "domain": spec.domain,
        "description": spec.description,
        "dimensions": list(spec.dimensions),
        "filters": list(spec.filters),
        "max_rows": spec.max_rows,
        "implemented": spec.implemented,
        "implementation_note": spec.implementation_note,
    }


def _validate_filters(filters: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    out: dict[str, Any] = {}
    errors = []
    for key, value in filters.items():
        clean_key = _clean_metric(key)
        if clean_key not in SAFE_FILTER_KEYS:
            errors.append({"reason": "unsafe_filter", "filter": str(key)})
            continue
        if clean_key == "profile_scope":
            if str(value or "active").strip().lower() not in {"active", "current"}:
                errors.append({"reason": "cross_profile_filter"})
            else:
                out[clean_key] = "active"
            continue
        if isinstance(value, str):
            if _contains_sql_fragment(value):
                errors.append({"reason": "sql_fragment_in_filter", "filter": clean_key})
                continue
            out[clean_key] = _clean_text(value, 80)
        elif isinstance(value, bool) or isinstance(value, (int, float)):
            out[clean_key] = value
        elif isinstance(value, list):
            clean_values = []
            for item in value[:12]:
                text = _clean_text(item, 80)
                if _contains_sql_fragment(text):
                    errors.append({"reason": "sql_fragment_in_filter", "filter": clean_key})
                    continue
                clean_values.append(text)
            out[clean_key] = clean_values
        elif value is None:
            continue
        else:
            errors.append({"reason": "unsafe_filter_value", "filter": clean_key})
    return out, errors


def _expense_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    # Constant allowlist owned by Python. Query callers never provide SQL here.
    literal_categories = ",".join("'" + value.replace("'", "''") + "'" for value in NON_SPENDING_CATEGORIES)
    return f"{prefix}amount < 0 AND COALESCE({prefix}category, '') NOT IN ({literal_categories})"


def _range_clause(rng: ResolvedRange, alias: str = "") -> str:
    if rng.all_time or not (rng.start and rng.end):
        return ""
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}date >= ? AND {prefix}date <= ?"


def _range_params(rng: ResolvedRange) -> list[Any]:
    return [] if rng.all_time or not (rng.start and rng.end) else [rng.start, rng.end]


def _placeholders(values: tuple[Any, ...] | list[Any]) -> str:
    return ",".join("?" for _ in values)


def _profile_scope(profile: str | None) -> str:
    return str(profile or "household").strip() or "household"


def _clean_metric(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if _METRIC_RE.match(text) else ""


def _clean_range_token(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")[:80]


def _contains_sql_fragment(value: Any) -> bool:
    return bool(_SQL_FRAGMENT_RE.search(str(value or "")))


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:12]:
        text = _clean_metric(item)
        if text:
            out.append(text)
    return out


def _clean_text(value: Any, max_len: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:max_len]


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _num(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _round(value: Any, digits: int = 2) -> float:
    return round(_num(value), digits)


def _avg(values: list[Any]) -> float:
    nums = [_num(v) for v in values if v is not None]
    return _round(sum(nums) / len(nums)) if nums else 0.0


def _series_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "avg": _avg(values),
        "min": _round(min(values)),
        "max": _round(max(values)),
        "stdev": _round(statistics.pstdev(values)) if len(values) > 1 else 0.0,
    }


def _coerce_date(value: date | datetime | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except Exception:
            return None
    return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _month_end(start: date) -> date:
    y, m = _shift_month(start.year, start.month, 1)
    return date(y, m, 1) - timedelta(days=1)


def _month_elapsed_ratio() -> float:
    today = date.today()
    return min(1.0, max(today.day / _month_end(today.replace(day=1)).day, 0.01))


def _cash_like(row: dict[str, Any]) -> bool:
    text = f"{row.get('account_type')} {row.get('account_subtype')}".lower()
    return any(term in text for term in ("depository", "checking", "savings", "cash"))


def _liability_like(row: dict[str, Any]) -> bool:
    text = f"{row.get('account_type')} {row.get('account_subtype')}".lower()
    return any(term in text for term in ("credit", "loan", "liability", "mortgage"))


def _stale_account(row: dict[str, Any]) -> bool:
    synced = _parse_date(row.get("last_synced_at"))
    return bool(synced and (date.today() - synced).days > 7)


def _with_sample_ids(row: dict[str, Any]) -> dict[str, Any]:
    ids = [v for v in str(row.pop("ids", "") or "").split(",") if v][:5]
    return {**row, "sample_evidence_ids": [f"txn:{v}" for v in ids]}


def _with_single_evidence_id(row: dict[str, Any]) -> dict[str, Any]:
    tx_id = row.get("id")
    out = dict(row)
    if tx_id:
        out["sample_evidence_ids"] = [f"txn:{tx_id}"]
    return out


def _evidence_ids_from_rows(metric: str, rows: list[dict[str, Any]]) -> list[str]:
    ids = []
    for row in rows:
        ids.extend([str(v) for v in row.get("sample_evidence_ids") or []])
        if row.get("id"):
            ids.append(f"txn:{row.get('id')}")
    if not ids and rows:
        ids = [f"metric:{metric}:{idx}" for idx, _ in enumerate(rows[:12], start=1)]
    return ids


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in row.items():
        if str(key).lower() in BLOCKED_SNAPSHOT_FIELDS:
            continue
        if isinstance(value, float):
            out[key] = round(value, 4)
        elif isinstance(value, (int, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = value[:8]
        else:
            out[key] = str(value)[:160]
    return out


def _scalar_summary(summary: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in summary.items():
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = round(float(value), 4)
        elif value is None:
            out[key] = None
        elif isinstance(value, str):
            out[key] = value[:120]
    return out


def _confidence(value: Any) -> str:
    text = str(value or "medium").strip().lower()
    return text if text in {"high", "medium", "low"} else "medium"


def _combine_confidence(values: list[Any]) -> str:
    clean = [_confidence(v) for v in values if v]
    if not clean:
        return "low"
    if "low" in clean:
        return "medium" if clean.count("low") <= max(1, len(clean) // 3) else "low"
    if "medium" in clean:
        return "medium"
    return "high"


def _dedupe(values: list[Any]) -> list[Any]:
    out = []
    seen = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _json_object_text(raw: str) -> str:
    text = str(raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start >= 0 and end >= start else text


__all__ = [
    "FINANCIAL_PORTRAIT_METRICS",
    "METRIC_SPECS",
    "QUERY_LAYER_VERSION",
    "REQUIRED_METRICS_BY_DOMAIN",
    "build_advisor_dossier",
    "build_financial_portrait_dossier",
    "build_safe_finance_catalog",
    "build_semantic_query_planner_prompt",
    "default_advisor_queries",
    "execute_metric",
    "execute_safe_finance_queries",
    "financial_portrait_queries",
    "plan_safe_finance_queries",
    "resolve_time_range",
    "validate_query_payload",
]
