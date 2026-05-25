from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from mira.agentic.semantic_catalog import canonical_semantic_tool_name, is_semantic_tool
from mira.agentic.semantic_frames import (
    FILTER_KEYS_BY_TOOL,
    PAYLOAD_KEYS_BY_TOOL,
    PREVIEW_CHANGE_TO_TOOL,
    PREVIEW_PAYLOAD_SCHEMAS,
    SEMANTIC_FRAME_SCHEMAS,
    TOP_LEVEL_KEYS_BY_TOOL,
    complete_semantic_frame,
    normalize_semantic_frame_args,
    validate_semantic_frame,
)


@dataclass(frozen=True)
class SemanticExecutionCall:
    semantic_tool: str
    semantic_args: dict[str, Any]
    registry_tool: str
    registry_args: dict[str, Any]
    answer_hints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SemanticValidationIssue:
    status: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return not self.status


MEMORY_ACTION_TO_TOOL: dict[str, str] = {
    "remember": "remember_user_context",
    "retrieve": "retrieve_relevant_memories",
    "list": "list_mira_memories",
    "update": "update_memory",
    "forget": "forget_memory",
}

WRITE_APPLY_KEYS = {"apply", "applied", "commit", "committed", "confirm", "confirmed", "execute", "executed"}
SORT_VALUES = {"date_desc", "date_asc", "amount_desc", "amount_asc"}
CHART_DATA_KEYS = {"labels", "values", "series"}
_INTERNAL_VALIDATION_KEYS = {"_invalid_filters_type", "_invalid_payload_type"}


def normalize_semantic_selector_args(tool_name: str, call: dict[str, Any]) -> dict[str, Any]:
    source = {
        key: copy.deepcopy(value)
        for key, value in (call or {}).items()
        if key not in {"args", "tool", "name", "id", "depends_on", "context_action", "range_source"}
        and value not in (None, "", [], {})
    }
    source = _canonicalize_selector_keys(source)
    raw_filters = source.get("filters")
    raw_payload = source.get("payload")
    invalid_filters = raw_filters not in (None, "", [], {}) and not isinstance(raw_filters, dict)
    invalid_payload = raw_payload not in (None, "", [], {}) and not isinstance(raw_payload, dict)

    canonical_tool = canonical_semantic_tool_name(tool_name)
    _tool, args = normalize_semantic_frame_args(canonical_tool, source)
    if invalid_filters:
        args["_invalid_filters_type"] = type(raw_filters).__name__
    if invalid_payload:
        args["_invalid_payload_type"] = type(raw_payload).__name__
    return _clean(args)


def semantic_validation_issue(tool_name: str, args: dict[str, Any], prior_steps: dict[str, str] | None = None) -> SemanticValidationIssue:
    canonical_tool = canonical_semantic_tool_name(tool_name)
    if canonical_tool not in SEMANTIC_FRAME_SCHEMAS:
        return SemanticValidationIssue("blocked", f"unknown semantic tool: {tool_name or '<empty>'}")
    if contains_apply_key(args):
        return SemanticValidationIssue("blocked", "write requests cannot apply, confirm, commit, or execute changes in the selector path")

    shape_error = semantic_selector_shape_error(canonical_tool, args)
    if shape_error:
        return SemanticValidationIssue("clarify", shape_error)

    frame_issue = validate_semantic_frame(canonical_tool, args)
    if frame_issue.status:
        return SemanticValidationIssue(frame_issue.status, frame_issue.message)

    if canonical_tool == "query_transactions":
        return _validate_query_transactions(args)
    if canonical_tool == "summarize_spending":
        return _validate_summarize_spending(args)
    if canonical_tool == "finance_overview":
        return _validate_finance_overview(args)
    if canonical_tool == "review_budget":
        return _validate_budget(args)
    if canonical_tool == "review_cashflow":
        return _validate_cashflow(args)
    if canonical_tool == "check_affordability":
        return _validate_affordability(args)
    if canonical_tool == "review_recurring":
        return _validate_recurring(args)
    if canonical_tool == "review_net_worth":
        return _validate_net_worth(args)
    if canonical_tool == "review_data_quality":
        return _validate_data_quality(args)
    if canonical_tool == "review_financial_context":
        return _validate_financial_context(args)
    if canonical_tool == "manage_memory":
        return _validate_memory(args)
    if canonical_tool == "preview_finance_change":
        return _validate_preview(args)
    if canonical_tool == "make_chart":
        return _validate_chart(args, prior_steps or {})
    return SemanticValidationIssue()


def adapt_semantic_execution(tool_name: str, args: dict[str, Any]) -> SemanticExecutionCall:
    canonical_tool = canonical_semantic_tool_name(tool_name)
    frame_result = complete_semantic_frame(canonical_tool, args or {})
    final_args = frame_result.args if frame_result.args else copy.deepcopy(args or {})

    if canonical_tool == "query_transactions":
        return SemanticExecutionCall(canonical_tool, final_args, "find_transactions", _transaction_args(final_args))
    if canonical_tool == "summarize_spending":
        return _summarize_spending_call(final_args)
    if canonical_tool == "finance_overview":
        return _finance_overview_call(final_args)
    if canonical_tool == "review_budget":
        return _budget_call(final_args)
    if canonical_tool == "review_cashflow":
        return _cashflow_call(final_args)
    if canonical_tool == "check_affordability":
        return _affordability_call(final_args)
    if canonical_tool == "review_recurring":
        return _recurring_call(final_args)
    if canonical_tool == "review_net_worth":
        return _net_worth_call(final_args)
    if canonical_tool == "review_data_quality":
        return _data_quality_call(final_args)
    if canonical_tool == "review_financial_context":
        return _financial_context_call(final_args)
    if canonical_tool == "manage_memory":
        return _memory_call(final_args)
    if canonical_tool == "preview_finance_change":
        return _preview_call(final_args)
    if canonical_tool == "make_chart":
        return _chart_call(final_args)
    raise ValueError(f"unknown semantic tool: {tool_name}")


def contains_apply_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in WRITE_APPLY_KEYS:
                if isinstance(item, str):
                    if item.strip().lower() in {"1", "true", "yes", "on"}:
                        return True
                elif bool(item):
                    return True
            if contains_apply_key(item):
                return True
    if isinstance(value, list):
        return any(contains_apply_key(item) for item in value)
    return False


def strip_apply_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_apply_keys(item)
            for key, item in value.items()
            if str(key).strip().lower() not in WRITE_APPLY_KEYS
        }
    if isinstance(value, list):
        return [strip_apply_keys(item) for item in value]
    return value


def preview_execution_tool_for_change(change_type: str) -> str:
    return PREVIEW_CHANGE_TO_TOOL.get(str(change_type or "").strip().lower(), "")


def is_memory_semantic_tool(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    _ = args
    return canonical_semantic_tool_name(tool_name) == "manage_memory"


def is_preview_semantic_tool(tool_name: str) -> bool:
    return canonical_semantic_tool_name(tool_name) == "preview_finance_change"


def semantic_selector_shape_error(tool_name: str, args: dict[str, Any]) -> str:
    _ = tool_name
    if "_invalid_filters_type" in args:
        return "filters must be an object."
    if "_invalid_payload_type" in args:
        return "payload must be an object."
    if "filters" in args and args.get("filters") not in (None, "", [], {}) and not isinstance(args.get("filters"), dict):
        return "filters must be an object."
    if "payload" in args and args.get("payload") not in (None, "", [], {}) and not isinstance(args.get("payload"), dict):
        return "payload must be an object."
    return ""


def _transaction_args(args: dict[str, Any]) -> dict[str, Any]:
    filters = _dict(args.get("filters"))
    view = str(args.get("view") or "").strip().lower()
    limit = 1 if view in {"latest", "detail"} else _limit(args, 50)
    search = filters.get("search")
    if view == "detail" and filters.get("transaction_id") and not search:
        search = filters.get("transaction_id")
    return _clean({
        "range": args.get("range"),
        "merchant": filters.get("merchant"),
        "category": filters.get("category"),
        "account": filters.get("account"),
        "search": search,
        "reviewed": filters.get("reviewed"),
        "limit": limit,
        "offset": args.get("offset"),
        "sort": args.get("sort"),
    })


def _summarize_spending_call(args: dict[str, Any]) -> SemanticExecutionCall:
    view = str(args.get("view") or "period_total").strip().lower()
    filters = _dict(args.get("filters"))
    payload = _dict(args.get("payload"))
    if view == "entity_total":
        if filters.get("merchant"):
            return SemanticExecutionCall("summarize_spending", args, "get_merchant_spend", _clean({
                "merchant": filters.get("merchant"),
                "range": args.get("range") or "current_month",
            }))
        return SemanticExecutionCall("summarize_spending", args, "get_category_spend", _clean({
            "category": filters.get("category"),
            "range": args.get("range") or "current_month",
        }))
    if view == "compare":
        subject_type = "merchant" if filters.get("merchant") else "category"
        subject = filters.get("merchant") or filters.get("category")
        return SemanticExecutionCall("summarize_spending", args, "compare_periods", _clean({
            "subject_type": subject_type,
            "subject": subject,
            "range_a": args.get("range_a") or "current_month",
            "range_b": args.get("range_b") or "last_month",
        }))
    if view == "top":
        group_by = str(payload.get("group_by") or "category").strip().lower()
        registry_tool = "get_top_merchants" if group_by == "merchant" else "get_top_categories"
        return SemanticExecutionCall("summarize_spending", args, registry_tool, _clean({
            "range": args.get("range") or "current_month",
            "limit": _limit(args, 10),
        }))
    if view == "breakdown":
        return SemanticExecutionCall("summarize_spending", args, "get_category_breakdown", _clean({
            "range": args.get("range") or "current_month",
        }))
    if view == "trend":
        return SemanticExecutionCall("summarize_spending", args, "get_monthly_spending_trend", _clean({
            "months": _months_from_args(args),
            "category": filters.get("category"),
        }))
    return SemanticExecutionCall("summarize_spending", args, "get_period_summary", _clean({
        "range": args.get("range") or "current_month",
        "metric": _metric_alias(str(payload.get("metric") or "summary")),
    }))


def _finance_overview_call(args: dict[str, Any]) -> SemanticExecutionCall:
    view = str(args.get("view") or "snapshot").strip().lower()
    payload = _dict(args.get("payload"))
    if view == "explain_metric":
        return SemanticExecutionCall("finance_overview", args, "explain_metric", _clean({
            "metric": payload.get("metric"),
            "range": args.get("range") or "current_month",
            "limit": _limit(args, 10),
        }))
    if view == "priorities":
        return SemanticExecutionCall("finance_overview", args, "get_finance_priorities", _clean({
            "range": args.get("range") or "current_month",
            "focus": payload.get("focus") or "watch",
            "limit": _limit(args, 5),
        }))
    return SemanticExecutionCall("finance_overview", args, "get_dashboard_snapshot", _clean({"range": args.get("range")}))


def _budget_call(args: dict[str, Any]) -> SemanticExecutionCall:
    view = str(args.get("view") or "plan").strip().lower()
    filters = _dict(args.get("filters"))
    if view == "savings_capacity":
        return SemanticExecutionCall("review_budget", args, "get_savings_capacity", _clean({"range": args.get("range") or "current_month"}))
    if view == "category_status":
        return SemanticExecutionCall("review_budget", args, "get_budget_status", _clean({
            "category": filters.get("category"),
            "range": args.get("range") or "current_month",
        }))
    return SemanticExecutionCall("review_budget", args, "get_budget_plan_summary", _clean({"range": args.get("range") or "current_month"}))


def _cashflow_call(args: dict[str, Any]) -> SemanticExecutionCall:
    payload = _dict(args.get("payload"))
    view = str(args.get("view") or "forecast").strip().lower()
    registry_tool = "predict_shortfall" if view == "shortfall" else "get_cashflow_forecast"
    return SemanticExecutionCall("review_cashflow", args, registry_tool, _clean({
        "horizon_days": payload.get("horizon_days"),
        "buffer_amount": payload.get("buffer_amount"),
    }))


def _affordability_call(args: dict[str, Any]) -> SemanticExecutionCall:
    filters = _dict(args.get("filters"))
    payload = _dict(args.get("payload"))
    return SemanticExecutionCall("check_affordability", args, "check_affordability", _clean({
        "amount": payload.get("amount"),
        "purpose": payload.get("purpose"),
        "category": filters.get("category"),
        "horizon_days": payload.get("horizon_days"),
        "buffer_amount": payload.get("buffer_amount"),
        "question": payload.get("question"),
    }))


def _recurring_call(args: dict[str, Any]) -> SemanticExecutionCall:
    view = str(args.get("view") or "summary").strip().lower()
    filters = _dict(args.get("filters"))
    payload = _dict(args.get("payload"))
    if view == "changes":
        return SemanticExecutionCall("review_recurring", args, "get_recurring_changes", _clean({
            "range": args.get("range"),
            "limit": _limit(args, 10),
        }))
    status = str(filters.get("status") or "").strip().lower()
    return SemanticExecutionCall("review_recurring", args, "get_recurring_summary", _clean({
        "status": status if status and status != "all" else None,
        "all": True if status == "all" else payload.get("all"),
        "limit": _limit(args, 25),
    }))


def _net_worth_call(args: dict[str, Any]) -> SemanticExecutionCall:
    view = str(args.get("view") or "trend").strip().lower()
    payload = _dict(args.get("payload"))
    if view == "balances":
        return SemanticExecutionCall("review_net_worth", args, "get_account_balances", {})
    if view == "delta":
        return SemanticExecutionCall("review_net_worth", args, "get_net_worth_delta", {})
    return SemanticExecutionCall("review_net_worth", args, "get_net_worth_trend", _clean({
        "range": args.get("range") or "last_6_months",
        "interval": payload.get("interval") or "monthly",
        "limit": _limit(args, 24),
    }))


def _data_quality_call(args: dict[str, Any]) -> SemanticExecutionCall:
    view = str(args.get("view") or "health").strip().lower()
    filters = _dict(args.get("filters"))
    payload = _dict(args.get("payload"))
    if view == "enrichment_summary":
        return SemanticExecutionCall("review_data_quality", args, "get_enrichment_quality_summary", _clean({"include_taxonomy": payload.get("include_taxonomy")}))
    if view == "low_confidence":
        return SemanticExecutionCall("review_data_quality", args, "find_low_confidence_transactions", _clean({
            "threshold": payload.get("threshold"),
            "limit": _limit(args, 25),
        }))
    if view == "explain_transaction":
        return SemanticExecutionCall("review_data_quality", args, "explain_transaction_enrichment", _clean({"transaction_id": filters.get("transaction_id")}))
    return SemanticExecutionCall("review_data_quality", args, "get_data_health_summary", {})


def _financial_context_call(args: dict[str, Any]) -> SemanticExecutionCall:
    filters = _dict(args.get("filters"))
    payload = _dict(args.get("payload"))
    return SemanticExecutionCall("review_financial_context", args, "review_financial_context", _clean({
        "view": args.get("view") or "relevant",
        "subject_type": filters.get("subject_type"),
        "subject_key": filters.get("subject_key"),
        "question": payload.get("question"),
        "intent": payload.get("intent"),
        "max_facts": payload.get("max_facts") or args.get("limit") or 5,
        "amount": payload.get("amount"),
        "month_key": payload.get("month_key"),
    }))


def _memory_call(args: dict[str, Any]) -> SemanticExecutionCall:
    view = str(args.get("view") or "").strip().lower()
    payload = _dict(args.get("payload"))
    registry_tool = MEMORY_ACTION_TO_TOOL.get(view, "")
    if view == "remember":
        registry_args = _clean({key: payload.get(key) for key in ("text", "memory_type", "topic", "source_summary", "source_turn_id", "pinned", "expires_at")})
    elif view == "retrieve":
        registry_args = _clean({
            "question": payload.get("question") or payload.get("text") or payload.get("topic"),
            "limit": _limit(args, 5),
            "include_expired": payload.get("include_expired"),
            "force": payload.get("force"),
        })
    elif view == "list":
        registry_args = _clean({
            "include_inactive": payload.get("include_inactive"),
            "include_expired": payload.get("include_expired"),
            "memory_type": payload.get("memory_type"),
            "limit": _limit(args, 100),
        })
    elif view == "update":
        registry_args = _clean({key: payload.get(key) for key in ("memory_id", "text", "normalized_text", "memory_type", "topic", "sensitivity", "confidence", "pinned", "expires_at", "status", "source_turn_id")})
    elif view == "forget":
        registry_args = _clean({key: payload.get(key) for key in ("memory_id", "topic", "text", "source_turn_id")})
    else:
        registry_args = {}
    return SemanticExecutionCall("manage_memory", args, registry_tool, registry_args)


def _preview_call(args: dict[str, Any]) -> SemanticExecutionCall:
    payload = _dict(args.get("payload"))
    change_type = str(payload.pop("change_type", "") or "").strip().lower()
    registry_tool = PREVIEW_CHANGE_TO_TOOL.get(change_type, "")
    return SemanticExecutionCall("preview_finance_change", args, registry_tool, _clean(payload))


def _chart_call(args: dict[str, Any]) -> SemanticExecutionCall:
    payload = _dict(args.get("payload"))
    chart_type = str(args.get("view") or "line").strip().lower()
    return SemanticExecutionCall("make_chart", args, "plot_chart", _clean({
        "source_step_id": payload.get("source_step_id"),
        "chart_type": chart_type,
        "type": chart_type,
        "title": payload.get("title"),
        "series_name": payload.get("series_name"),
        "unit": payload.get("unit"),
    }))


def _validate_query_transactions(args: dict[str, Any]) -> SemanticValidationIssue:
    return _validate_sort(args, allowed={"", "date_desc", "date_asc", "amount_desc", "amount_asc"}, label="transaction sort")


def _validate_summarize_spending(args: dict[str, Any]) -> SemanticValidationIssue:
    view = str(args.get("view") or "").strip().lower()
    if view in {"period_total", "entity_total"}:
        return _validate_sort(args, allowed={"", "date_desc"}, label="summary sort")
    if view in {"top", "breakdown"}:
        return _validate_sort(args, allowed={"", "amount_desc"}, label="top-list sort")
    if view == "trend":
        sort_issue = _validate_sort(args, allowed={"", "date_asc"}, label="trend sort")
        if not sort_issue.ok:
            return sort_issue
        payload = _dict(args.get("payload"))
        return _validate_number_bounds(payload, "months", 1, 36, required=False)
    return SemanticValidationIssue()


def _validate_finance_overview(args: dict[str, Any]) -> SemanticValidationIssue:
    _ = args
    return SemanticValidationIssue()


def _validate_budget(args: dict[str, Any]) -> SemanticValidationIssue:
    _ = args
    return SemanticValidationIssue()


def _validate_cashflow(args: dict[str, Any]) -> SemanticValidationIssue:
    payload = _dict(args.get("payload"))
    issue = _validate_number_bounds(payload, "horizon_days", 1, 90, required=False)
    if not issue.ok:
        return issue
    return _validate_number_bounds(payload, "buffer_amount", 0, None, required=False)


def _validate_affordability(args: dict[str, Any]) -> SemanticValidationIssue:
    payload = _dict(args.get("payload"))
    issue = _validate_number_bounds(payload, "amount", 0.01, None, required=True)
    if not issue.ok:
        return issue
    issue = _validate_number_bounds(payload, "horizon_days", 1, 90, required=False)
    if not issue.ok:
        return issue
    return _validate_number_bounds(payload, "buffer_amount", 0, None, required=False)


def _validate_recurring(args: dict[str, Any]) -> SemanticValidationIssue:
    filters = _dict(args.get("filters"))
    status = str(filters.get("status") or "").strip().lower()
    if status and status not in {"active", "inactive", "cancelled", "candidate", "all"}:
        return SemanticValidationIssue("clarify", "I need a supported recurring status.")
    return SemanticValidationIssue()


def _validate_net_worth(args: dict[str, Any]) -> SemanticValidationIssue:
    payload = _dict(args.get("payload"))
    interval = str(payload.get("interval") or "").strip().lower()
    if interval and interval not in {"monthly", "weekly"}:
        return SemanticValidationIssue("clarify", "I need a supported net worth interval.")
    return SemanticValidationIssue()


def _validate_data_quality(args: dict[str, Any]) -> SemanticValidationIssue:
    _ = args
    return SemanticValidationIssue()


def _validate_financial_context(args: dict[str, Any]) -> SemanticValidationIssue:
    filters = _dict(args.get("filters"))
    subject_type = str(filters.get("subject_type") or "").strip().lower()
    if subject_type and subject_type not in {"profile", "category", "merchant", "subscription", "account", "cashflow"}:
        return SemanticValidationIssue("clarify", "I need a supported financial context subject type.")
    payload = _dict(args.get("payload"))
    return _validate_number_bounds(payload, "max_facts", 1, 8, required=False)


def _validate_memory(args: dict[str, Any]) -> SemanticValidationIssue:
    _ = args
    return SemanticValidationIssue()


def _validate_preview(args: dict[str, Any]) -> SemanticValidationIssue:
    payload = _dict(args.get("payload"))
    change_type = str(payload.get("change_type") or "").strip().lower()
    schema = PREVIEW_PAYLOAD_SCHEMAS.get(change_type)
    if schema is None:
        return SemanticValidationIssue("clarify", "I need a supported finance change_type to preview.")
    missing = [key for key in schema.required if payload.get(key) in (None, "", [], {})]
    if missing:
        return SemanticValidationIssue("clarify", f"preview_finance_change missing required payload field(s): {', '.join(sorted(missing))}")
    return SemanticValidationIssue()


def _validate_chart(args: dict[str, Any], prior_steps: dict[str, str]) -> SemanticValidationIssue:
    payload = _dict(args.get("payload"))
    if any(key in payload for key in CHART_DATA_KEYS):
        return SemanticValidationIssue("blocked", "make_chart must use prior tool evidence instead of selector-provided labels or values")
    source_step_id = str(payload.get("source_step_id") or "").strip()
    if not source_step_id:
        return SemanticValidationIssue("blocked", "make_chart requires payload.source_step_id from an earlier evidence-producing tool")
    if source_step_id not in prior_steps:
        return SemanticValidationIssue("blocked", "make_chart source_step_id must reference an existing earlier tool step")
    return SemanticValidationIssue()


def _validate_sort(args: dict[str, Any], *, allowed: set[str] | None = None, label: str = "sort") -> SemanticValidationIssue:
    sort = str(args.get("sort") or "").strip().lower()
    if sort and sort not in SORT_VALUES:
        return SemanticValidationIssue("clarify", "I need a supported sort: date_desc, date_asc, amount_desc, or amount_asc.")
    if allowed is not None and sort not in allowed:
        supported = ", ".join(value for value in sorted(allowed) if value) or "default"
        return SemanticValidationIssue("clarify", f"I need a supported {label}: {supported}.")
    return SemanticValidationIssue()


def _validate_number_bounds(args: dict[str, Any], key: str, minimum: float | None, maximum: float | None, *, required: bool) -> SemanticValidationIssue:
    value = args.get(key)
    if value in (None, ""):
        if required:
            return SemanticValidationIssue("clarify", f"I need {key} before I can answer that.")
        return SemanticValidationIssue()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return SemanticValidationIssue("clarify", f"{key} must be a number.")
    if minimum is not None and numeric < minimum:
        return SemanticValidationIssue("clarify", f"{key} must be at least {minimum}.")
    if maximum is not None and numeric > maximum:
        return SemanticValidationIssue("clarify", f"{key} must be no more than {maximum}.")
    return SemanticValidationIssue()


def _canonicalize_selector_keys(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        canonical = _canonical_key(key)
        if canonical in {"payload", "filters"} and isinstance(value, dict):
            value = _canonicalize_selector_keys(value)
        if canonical not in out or out.get(canonical) in (None, "", [], {}):
            out[canonical] = value
    return out


def _canonical_key(key: Any) -> str:
    lowered = str(key or "").strip().lower()
    aliases = {
        "time_period": "range",
        "period": "range",
        "date_range": "range",
        "filters_payload": "filters",
        "filter_payload": "filters",
        "filter": "filters",
        "top_n": "limit",
        "breakdown_type": "group_by",
        "breakdown": "group_by",
        "period_1": "range_a",
        "period1": "range_a",
        "range_1": "range_a",
        "range_a_period": "range_a",
        "period_2": "range_b",
        "period2": "range_b",
        "range_2": "range_b",
        "range_b_period": "range_b",
        "mode": "view",
        "action": "view",
        "view_type": "view",
        "horizon": "horizon_days",
        "item_description": "purpose",
        "item": "purpose",
        "merchant_name": "merchant",
        "category_name": "category",
        "target_category": "category",
        "source_entity": "merchant",
    }
    return aliases.get(lowered, str(key or "").strip())


def _metric_alias(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "spending": "expenses",
        "spend": "expenses",
        "expense": "expenses",
        "total_spending": "expenses",
        "spending_total": "expenses",
        "total_expenses": "expenses",
        "expenses_total": "expenses",
        "charges": "expenses",
        "income_total": "income",
        "expense_total": "expenses",
        "refund": "refunds",
        "credits": "refunds",
        "credit_card_payment": "credit_card_payments",
        "cc_payments": "credit_card_payments",
        "savings_transfer": "savings",
    }
    return aliases.get(text, text or "summary")


def _months_from_args(args: dict[str, Any]) -> int:
    payload = _dict(args.get("payload"))
    if payload.get("months") not in (None, ""):
        try:
            return max(1, min(int(payload.get("months") or 6), 36))
        except (TypeError, ValueError):
            return 6
    range_token = str(args.get("range") or "").strip().lower()
    if range_token == "last_6_months":
        return 6
    if range_token == "last_90d":
        return 3
    if range_token in {"last_year", "last_365d"}:
        return 12
    if range_token.startswith("last_") and range_token.endswith("_months"):
        try:
            return max(1, min(int(range_token.split("_")[1]), 36))
        except (IndexError, ValueError):
            return 6
    return 6


def _limit(args: dict[str, Any], default: int) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), 50))
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _clean(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


__all__ = [
    "MEMORY_ACTION_TO_TOOL",
    "PREVIEW_CHANGE_TO_TOOL",
    "SemanticExecutionCall",
    "SemanticValidationIssue",
    "adapt_semantic_execution",
    "contains_apply_key",
    "is_memory_semantic_tool",
    "is_preview_semantic_tool",
    "normalize_semantic_selector_args",
    "preview_execution_tool_for_change",
    "semantic_validation_issue",
    "semantic_selector_shape_error",
    "strip_apply_keys",
]
