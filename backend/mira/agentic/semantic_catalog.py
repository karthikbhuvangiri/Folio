from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


READ = "read"
WRITE_PREVIEW = "write_preview"
MEMORY = "memory"
APP = "app"
CHART = "chart"


SELECTOR_SEMANTIC_TOOL_NAMES: tuple[str, ...] = (
    "query_transactions",
    "summarize_spending",
    "finance_overview",
    "review_budget",
    "review_cashflow",
    "check_affordability",
    "review_recurring",
    "review_net_worth",
    "review_data_quality",
    "manage_memory",
    "preview_finance_change",
    "make_chart",
)


LEGACY_SEMANTIC_TOOL_ALIASES: dict[str, str] = {
    "analyze_entity": "summarize_spending",
    "compare_entity_periods": "summarize_spending",
    "review_savings_capacity": "review_budget",
    "write_preview": "preview_finance_change",
}


@dataclass(frozen=True)
class SemanticToolSpec:
    name: str
    description: str
    risk_level: str = READ
    aliases: tuple[str, ...] = field(default_factory=tuple)
    can_parallel: bool = True
    selector_visible: bool = True

    def for_selector(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(SEMANTIC_ARG_SCHEMA),
            },
        }


SEMANTIC_ARG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "view": {"type": "string"},
        "range": {"type": "string"},
        "range_a": {"type": "string"},
        "range_b": {"type": "string"},
        "filters": {"type": "object"},
        "payload": {"type": "object"},
        "limit": {"type": "integer"},
        "offset": {"type": "integer"},
        "sort": {"type": "string"},
        "context_action": {"type": "string"},
        "range_source": {"type": "string"},
    },
    "required": ["view"],
    "additionalProperties": False,
}


SEMANTIC_TOOL_CATALOG: dict[str, SemanticToolSpec] = {
    "query_transactions": SemanticToolSpec(
        name="query_transactions",
        description="Transaction rows and timing. Views: latest, list, search, detail.",
    ),
    "summarize_spending": SemanticToolSpec(
        name="summarize_spending",
        description="Spending/income totals, entity totals, top lists, breakdowns, trends, and comparisons. Views: period_total, entity_total, top, breakdown, trend, compare.",
    ),
    "finance_overview": SemanticToolSpec(
        name="finance_overview",
        description="Dashboard snapshot, finance priorities, or metric explanation. Views: snapshot, priorities, explain_metric.",
    ),
    "review_budget": SemanticToolSpec(
        name="review_budget",
        description="Budget plan, category budget status, or savings capacity. Views: plan, category_status, savings_capacity.",
    ),
    "review_cashflow": SemanticToolSpec(
        name="review_cashflow",
        description="Cash forecast and shortfall risk. Views: forecast, shortfall.",
    ),
    "check_affordability": SemanticToolSpec(
        name="check_affordability",
        description="Proposed purchase affordability. View: purchase.",
    ),
    "review_recurring": SemanticToolSpec(
        name="review_recurring",
        description="Recurring charges, subscriptions, status, and changes. Views: summary, changes.",
    ),
    "review_net_worth": SemanticToolSpec(
        name="review_net_worth",
        description="Balances, net worth trend, or net worth delta. Views: balances, trend, delta.",
    ),
    "review_data_quality": SemanticToolSpec(
        name="review_data_quality",
        description="Data health and enrichment quality. Views: health, enrichment_summary, low_confidence, explain_transaction.",
        risk_level=APP,
    ),
    "review_financial_context": SemanticToolSpec(
        name="review_financial_context",
        description="Read-only Mira financial understanding facts. Views: relevant, lifestyle_profile, friction_map, operating_plan.",
        selector_visible=False,
    ),
    "manage_memory": SemanticToolSpec(
        name="manage_memory",
        description="Mira memory. Views: remember, retrieve, list, update, forget.",
        risk_level=MEMORY,
        can_parallel=False,
    ),
    "preview_finance_change": SemanticToolSpec(
        name="preview_finance_change",
        description="Preview-only finance changes. View: preview. payload.change_type chooses the private preview tool.",
        risk_level=WRITE_PREVIEW,
        can_parallel=False,
    ),
    "make_chart": SemanticToolSpec(
        name="make_chart",
        description="Render a chart from prior evidence only. Views: line, bar, donut.",
        risk_level=CHART,
        can_parallel=False,
    ),
}


SEMANTIC_TOOL_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("transactions", "transaction rows and search", ("query_transactions",)),
    ("spending", "totals, top-N, trends, entity analysis, comparisons", ("summarize_spending",)),
    ("overview", "dashboard, priorities, metric explanations", ("finance_overview",)),
    ("budget_cashflow", "budgets, savings, cashflow, affordability", ("review_budget", "review_cashflow", "check_affordability")),
    ("recurring_net_worth", "recurring charges, balances, net worth", ("review_recurring", "review_net_worth")),
    ("quality_memory", "data quality, financial context, and memory", ("review_data_quality", "review_financial_context", "manage_memory")),
    ("writes_charts", "write previews and charts", ("preview_finance_change", "make_chart")),
)


def canonical_semantic_tool_name(name: str) -> str:
    text = str(name or "").strip()
    return LEGACY_SEMANTIC_TOOL_ALIASES.get(text, text)


def semantic_tools_for_selector(names: list[str] | tuple[str, ...] | set[str] | None = None) -> list[dict[str, Any]]:
    selected = set(names or SELECTOR_SEMANTIC_TOOL_NAMES)
    canonical_selected = {canonical_semantic_tool_name(name) for name in selected}
    return [
        SEMANTIC_TOOL_CATALOG[name].for_selector()
        for name in SELECTOR_SEMANTIC_TOOL_NAMES
        if name in canonical_selected
    ]


def semantic_tool_names(*, include_aliases: bool = True) -> set[str]:
    names = set(SEMANTIC_TOOL_CATALOG)
    if include_aliases:
        names.update(LEGACY_SEMANTIC_TOOL_ALIASES)
    return names


def is_selector_semantic_tool(name: str) -> bool:
    return str(name or "").strip() in SELECTOR_SEMANTIC_TOOL_NAMES


def is_semantic_tool(name: str) -> bool:
    text = str(name or "").strip()
    return text in SEMANTIC_TOOL_CATALOG or text in LEGACY_SEMANTIC_TOOL_ALIASES


__all__ = [
    "APP",
    "CHART",
    "LEGACY_SEMANTIC_TOOL_ALIASES",
    "MEMORY",
    "READ",
    "SELECTOR_SEMANTIC_TOOL_NAMES",
    "SEMANTIC_ARG_SCHEMA",
    "SEMANTIC_TOOL_CATALOG",
    "SEMANTIC_TOOL_FAMILIES",
    "SemanticToolSpec",
    "WRITE_PREVIEW",
    "canonical_semantic_tool_name",
    "is_selector_semantic_tool",
    "is_semantic_tool",
    "semantic_tool_names",
    "semantic_tools_for_selector",
]
