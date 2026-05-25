from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any

from mira.agentic.temporal_parser import bounded_range_token, is_bounded_range_token

from mira.agentic.financial_context_policy import maybe_append_financial_context_call
from mira.agentic.intent_frame import ConversationFrame, MiraSubject


MISSING_VALUES = (None, "", [], {})


@dataclass(frozen=True)
class IntentCompilerResult:
    calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = "empty"
    issue: str = ""
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "compiled" and bool(self.calls)


@dataclass(frozen=True)
class _CompilerHints:
    intent: str = ""
    tool_name: str = ""
    view: str = ""
    range: str = ""
    range_a: str = ""
    range_b: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    limit: Any = None
    offset: Any = None
    sort: str = ""


def compile_selector_decision(
    decision: dict[str, Any],
    *,
    frame: ConversationFrame | None = None,
    selector_calls: list[dict[str, Any]] | None = None,
) -> IntentCompilerResult:
    if not isinstance(decision, dict):
        decision = {}
    if frame is None:
        return IntentCompilerResult(status="empty", issue="missing conversation frame")

    hints = _hints_from_decision(decision, selector_calls=selector_calls)
    calls = _compile_frame(frame, hints)
    if not calls:
        return IntentCompilerResult(
            status="empty",
            issue=f"no compiler mapping for route={frame.route} intent={frame.intent}",
            trace=_trace(frame, hints, []),
        )
    return IntentCompilerResult(status="compiled", calls=calls, trace=_trace(frame, hints, calls))


def _compile_frame(frame: ConversationFrame, hints: _CompilerHints) -> list[dict[str, Any]]:
    route = str(frame.route or "").strip().lower()
    if route == "finance":
        time_issue = _time_range_issue(frame)
        if time_issue:
            return [_validation_error_call(time_issue)]
    if route == "finance":
        calls = _compile_finance_frame(frame, hints)
    elif route == "write_preview":
        calls = _compile_write_preview_frame(frame, hints)
    elif route == "memory":
        calls = _compile_memory_frame(frame, hints)
    else:
        return []

    if calls and calls[0].get("validation_error"):
        return calls
    calls = maybe_append_financial_context_call(calls, frame=frame)
    if calls and frame.output == "chart" and not any(call.get("name") == "make_chart" for call in calls):
        calls = _append_chart_call(calls, frame)
    return calls


def _compile_finance_frame(frame: ConversationFrame, hints: _CompilerHints) -> list[dict[str, Any]]:
    intent = _effective_intent(frame, hints)
    if frame.output == "chart":
        return [_chart_evidence_call(intent, frame, hints)]
    if intent == "spending_total":
        return [_spending_total_call(frame, hints)]
    if intent == "spending_top":
        return [_spending_top_call(frame, hints)]
    if intent == "spending_breakdown":
        return [_call("selector_call_1", "summarize_spending", {"view": "breakdown", "range": _range(frame, hints)})]
    if intent == "spending_trend":
        return [_spending_trend_call(frame, hints)]
    if intent == "spending_compare":
        return [_spending_compare_call(frame, hints)]
    if intent in {"spending_explain", "transaction_lookup"}:
        return [_transaction_lookup_call(frame, hints)]
    if intent in {"budget_status", "budget_plan", "savings_capacity"}:
        return [_budget_call(frame, hints)]
    if intent in {"cashflow_forecast", "cashflow_shortfall"}:
        return [_cashflow_call(intent, hints)]
    if intent == "affordability":
        return [_affordability_call(frame, hints)]
    if intent in {"recurring_summary", "recurring_changes"}:
        return [_recurring_call(intent, hints)]
    if intent in {"net_worth_balance", "net_worth_trend", "net_worth_delta"}:
        return [_net_worth_call(intent, frame, hints)]
    if intent in {"data_health", "enrichment_quality", "low_confidence_transactions", "explain_transaction"}:
        return [_data_quality_call(intent, frame, hints)]
    if intent in {"finance_snapshot", "finance_priorities", "explain_metric"}:
        return [_finance_overview_call(intent, frame, hints)]
    return []


def _chart_evidence_call(intent: str, frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    if intent in {"spending_total", "spending_trend"}:
        subject = _subject(frame)
        if subject.kind == "merchant" and subject.text:
            return _unsupported_chart_call("merchant spending trends")
        return _spending_trend_call(frame, hints)
    if intent == "spending_top":
        if _subject_filters(_subject(frame)):
            return _unsupported_chart_call("filtered top spending charts")
        return _spending_top_call(frame, hints)
    if intent == "spending_breakdown":
        return _call("selector_call_1", "summarize_spending", {"view": "breakdown", "range": _range(frame, hints)})
    if intent == "spending_compare":
        return _spending_compare_call(frame, hints)
    if intent in {"net_worth_balance", "net_worth_trend", "net_worth_delta"}:
        return _net_worth_call("net_worth_trend", frame, hints)
    if intent in {"cashflow_forecast", "cashflow_shortfall"}:
        return _cashflow_call("cashflow_forecast", hints)
    if intent == "savings_capacity":
        return _unsupported_chart_call("savings capacity")
    if intent in {"budget_status", "budget_plan"}:
        return _budget_call(frame, hints)
    if intent in {"recurring_summary", "recurring_changes"}:
        return _recurring_call("recurring_summary", hints)
    return _unsupported_chart_call(intent or "this request")


def _spending_total_call(frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    subject = _subject(frame)
    filters = _subject_filters(subject)
    payload = {"metric": _spending_metric(frame, hints, entity_total=bool(filters))}
    args: dict[str, Any] = {"view": "entity_total" if filters else "period_total", "range": _range(frame, hints), "payload": payload}
    if filters:
        args["filters"] = filters
    return _call("selector_call_1", "summarize_spending", args)


def _spending_top_call(frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    subject = _subject(frame)
    subject_filters = _subject_filters(subject)
    group_by = _group_by(hints, default="category")
    if not subject_filters and subject.kind == "merchant" and group_by == "category":
        group_by = "merchant"
    if subject_filters:
        if frame.output in {"list", "table"}:
            return _transaction_lookup_call(frame, hints)
        return _spending_total_call(frame, hints)
    args: dict[str, Any] = {
        "view": "top",
        "range": _range(frame, hints),
        "payload": {"group_by": group_by, "metric": _spending_metric(frame, hints, entity_total=False)},
    }
    if subject_filters:
        args["filters"] = subject_filters
    if hints.limit not in MISSING_VALUES:
        args["limit"] = hints.limit
    if hints.sort:
        args["sort"] = hints.sort
    return _call("selector_call_1", "summarize_spending", args)


def _spending_trend_call(frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    filters = _subject_filters(_subject(frame))
    filters.pop("merchant", None)
    metric = _spending_metric(frame, hints, entity_total=False)
    if metric == "summary":
        metric = "expenses"
    args: dict[str, Any] = {
        "view": "trend",
        "range": _trend_range(frame, hints),
        "payload": {"metric": metric, "group_by": "month"},
    }
    if filters:
        args["filters"] = filters
    return _call("selector_call_1", "summarize_spending", args)


def _spending_compare_call(frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    args: dict[str, Any] = {
        "view": "compare",
        "range_a": _range_a(frame, hints),
        "range_b": _range_b(frame, hints),
        "payload": {"metric": _spending_metric(frame, hints, entity_total=True)},
    }
    filters = _subject_filters(_subject(frame))
    if filters:
        args["filters"] = filters
    return _call("selector_call_1", "summarize_spending", args)


def _transaction_lookup_call(frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    subject = _subject(frame)
    filters: dict[str, Any] = {}
    if subject.kind == "transaction" and subject.text:
        category_alias = _transaction_subject_category_alias(subject.text)
        if category_alias:
            filters["category"] = category_alias
        else:
            filters["transaction_id"] = subject.text
    elif subject.kind in {"merchant", "category", "account"} and subject.text:
        filters[subject.kind] = subject.text
    elif hints.filters:
        for key in ("merchant", "category", "account", "search", "transaction_id", "reviewed"):
            if hints.filters.get(key) not in MISSING_VALUES:
                filters[key] = hints.filters[key]
    view = _transaction_view(frame, hints, filters)
    args: dict[str, Any] = {"view": view}
    if view == "detail":
        args["filters"] = filters
    else:
        if filters:
            args["filters"] = filters
        if _range(frame, hints, default="") and view != "latest":
            args["range"] = _range(frame, hints, default="")
        if view == "latest":
            args["limit"] = 1
            args["sort"] = "date_desc"
        elif hints.limit not in MISSING_VALUES:
            args["limit"] = hints.limit
        if hints.offset not in MISSING_VALUES:
            args["offset"] = hints.offset
        if hints.sort and view != "latest":
            args["sort"] = hints.sort
    return _call("selector_call_1", "query_transactions", args)


def _budget_call(frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    intent = str(frame.intent or "").strip().lower()
    filters = _subject_filters(_subject(frame))
    category_filter = {"category": filters["category"]} if filters.get("category") else {}
    view = "savings_capacity" if intent == "savings_capacity" else "plan"
    savings_hint = "saving" in hints.intent or str(hints.view or "").strip().lower() == "savings_capacity"
    metric_hint = str(hints.payload.get("metric") or "").strip().lower()
    if savings_hint or metric_hint in {"savings_capacity", "monthly_savings"}:
        view = "savings_capacity"
    if intent == "budget_status" and category_filter:
        view = "category_status"
    args: dict[str, Any] = {"view": view, "range": _range(frame, hints)}
    if category_filter:
        args["filters"] = category_filter
    return _call("selector_call_1", "review_budget", args)


def _cashflow_call(intent: str, hints: _CompilerHints) -> dict[str, Any]:
    payload = _copy_allowed(hints.payload, {"horizon_days", "buffer_amount"})
    args: dict[str, Any] = {"view": "shortfall" if intent == "cashflow_shortfall" else "forecast"}
    if payload:
        args["payload"] = payload
    return _call("selector_call_1", "review_cashflow", args)


def _affordability_call(frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    filters = _subject_filters(_subject(frame))
    category_filter = {"category": filters["category"]} if filters.get("category") else {}
    payload = _copy_allowed(hints.payload, {"amount", "purpose", "horizon_days", "buffer_amount", "question"})
    for key in ("amount", "purpose", "horizon_days", "buffer_amount", "question"):
        if hints.filters.get(key) not in MISSING_VALUES and payload.get(key) in MISSING_VALUES:
            payload[key] = hints.filters[key]
    args: dict[str, Any] = {"view": "purchase"}
    if category_filter:
        args["filters"] = category_filter
    if payload:
        args["payload"] = payload
    return _call("selector_call_1", "check_affordability", args)


def _recurring_call(intent: str, hints: _CompilerHints) -> dict[str, Any]:
    args: dict[str, Any] = {"view": "changes" if intent == "recurring_changes" else "summary"}
    if hints.limit not in MISSING_VALUES:
        args["limit"] = hints.limit
    status = str(hints.filters.get("status") or "").strip()
    if status and args["view"] == "summary":
        args["filters"] = {"status": status}
    if hints.payload.get("all") not in MISSING_VALUES:
        args["payload"] = {"all": hints.payload["all"]}
    return _call("selector_call_1", "review_recurring", args)


def _net_worth_call(intent: str, frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    view = {"net_worth_balance": "balances", "net_worth_delta": "delta"}.get(intent, "trend")
    if frame.output == "chart":
        view = "trend"
    args: dict[str, Any] = {"view": view}
    if view == "trend":
        args["range"] = _trend_range(frame, hints)
        interval = str(hints.payload.get("interval") or "").strip()
        if interval:
            args["payload"] = {"interval": interval}
    return _call("selector_call_1", "review_net_worth", args)


def _data_quality_call(intent: str, frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    view = {
        "data_health": "health",
        "enrichment_quality": "enrichment_summary",
        "low_confidence_transactions": "low_confidence",
        "explain_transaction": "explain_transaction",
    }[intent]
    args: dict[str, Any] = {"view": view}
    payload = _copy_allowed(hints.payload, {"threshold", "include_taxonomy"})
    if payload and view in {"enrichment_summary", "low_confidence"}:
        args["payload"] = payload
    subject = _subject(frame)
    transaction_id = subject.text if subject.kind == "transaction" else str(hints.filters.get("transaction_id") or "").strip()
    if view == "explain_transaction" and transaction_id:
        args["filters"] = {"transaction_id": transaction_id}
    if hints.limit not in MISSING_VALUES and view == "low_confidence":
        args["limit"] = hints.limit
    return _call("selector_call_1", "review_data_quality", args)


def _finance_overview_call(intent: str, frame: ConversationFrame, hints: _CompilerHints) -> dict[str, Any]:
    view = {"finance_priorities": "priorities", "explain_metric": "explain_metric"}.get(intent, "snapshot")
    args: dict[str, Any] = {"view": view}
    range_value = _range(frame, hints, default="")
    if range_value:
        args["range"] = range_value
    payload: dict[str, Any] = {}
    if view == "explain_metric":
        metric = _subject(frame).text or str(hints.payload.get("metric") or "").strip()
        if metric:
            payload["metric"] = metric
    elif hints.payload.get("focus") not in MISSING_VALUES:
        payload["focus"] = hints.payload["focus"]
    if payload:
        args["payload"] = payload
    if hints.limit not in MISSING_VALUES and view == "priorities":
        args["limit"] = hints.limit
    return _call("selector_call_1", "finance_overview", args)


def _compile_write_preview_frame(frame: ConversationFrame, hints: _CompilerHints) -> list[dict[str, Any]]:
    payload = copy.deepcopy(hints.payload or {})
    for key in ("answer_mode", "memory_action", "text", "question", "topic", "memory_id", "memory_type"):
        payload.pop(key, None)
    subject = _subject(frame)
    if subject.kind in {"merchant", "category", "account"} and subject.text:
        payload.setdefault(subject.kind, subject.text)
    for key, value in _copy_allowed(
        hints.filters,
        {
            "change_type",
            "merchant",
            "category",
            "old_name",
            "new_name",
            "pattern",
            "amount",
            "transaction_id",
            "note",
            "tags",
            "reviewed",
        },
    ).items():
        payload.setdefault(key, value)
    if payload.get("action") and not payload.get("change_type"):
        payload["change_type"] = payload.pop("action")
    if payload.get("search"):
        change_type = _preview_change_alias(payload.get("change_type"))
        if change_type == "bulk_recategorize" or payload.get("category"):
            if not any(payload.get(key) for key in ("merchant", "old_name", "pattern")):
                payload["merchant"] = payload.get("search")
            payload.pop("search", None)
    if not payload.get("change_type") and payload.get("merchant") and payload.get("category"):
        payload["change_type"] = "bulk_recategorize"
    if not payload.get("change_type"):
        hinted_change = str(hints.view or hints.intent or "").strip()
        if hinted_change not in {"", "none", "preview", "preview_finance_change", "write_preview"}:
            payload["change_type"] = hinted_change
    if payload.get("change_type"):
        payload["change_type"] = _preview_change_alias(payload.get("change_type"))
    if payload.get("change_type") == "create_rule" and payload.get("merchant") and not payload.get("pattern"):
        payload["pattern"] = payload.pop("merchant")
    return [_call("selector_call_1", "preview_finance_change", {"view": "preview", "payload": payload})]


def _compile_memory_frame(frame: ConversationFrame, hints: _CompilerHints) -> list[dict[str, Any]]:
    if not frame.memory.is_empty:
        return [_compile_memory_operation_call(frame)]
    if frame.output == "list":
        return [_call("selector_call_1", "manage_memory", {"view": "list"})]
    if not _memory_compiler_compat_enabled():
        return []

    payload = copy.deepcopy(hints.payload or {})
    view = str(hints.view or "").strip().lower()
    action = str(
        payload.pop("memory_action", "")
        or payload.pop("action", "")
        or payload.pop("operation", "")
        or ""
    ).strip().lower()
    if action in {"save", "store"}:
        action = "remember"
    if action in {"search", "read", "recall"}:
        action = "retrieve"
    if action in {"delete", "remove"}:
        action = "forget"
    if action in {"remember", "retrieve", "list", "update", "forget"} and view not in {"remember", "retrieve", "list", "update", "forget"}:
        view = action
    if view not in {"remember", "retrieve", "list", "update", "forget"}:
        if payload.get("question") or payload.get("topic") or payload.get("search"):
            view = "retrieve"
        elif payload.get("text"):
            view = "remember"
        elif payload:
            view = "retrieve"
        else:
            view = "list"
    if view == "retrieve" and payload.get("search") and not any(payload.get(key) for key in ("question", "text", "topic")):
        payload["question"] = payload.pop("search")
    else:
        payload.pop("search", None)
    if view == "list":
        payload = _copy_allowed(payload, {"include_inactive", "include_expired", "memory_type"})
    args: dict[str, Any] = {"view": view}
    if payload:
        args["payload"] = payload
    if hints.limit not in MISSING_VALUES:
        args["limit"] = hints.limit
    return [_call("selector_call_1", "manage_memory", args)]


def _compile_memory_operation_call(frame: ConversationFrame) -> dict[str, Any]:
    memory = frame.memory
    view = str(memory.action or "none").strip().lower()
    if view == "none":
        view = "list" if frame.output == "list" else ""
    if view not in {"remember", "retrieve", "list", "update", "forget"}:
        return _validation_error_call("I need one more memory detail before I can do that.")

    payload: dict[str, Any] = {}
    for key, value in (
        ("text", memory.text),
        ("question", memory.question),
        ("topic", memory.topic),
        ("memory_id", memory.memory_id),
        ("memory_type", memory.memory_type),
    ):
        if value not in MISSING_VALUES:
            payload[key] = value
    if view == "retrieve" and payload.get("topic") and not payload.get("question") and not payload.get("text"):
        payload["question"] = payload.pop("topic")
    if view == "list":
        payload = _copy_allowed(payload, {"memory_type"})

    args: dict[str, Any] = {"view": view}
    if payload:
        args["payload"] = payload
    if memory.limit not in MISSING_VALUES:
        args["limit"] = memory.limit
    return _call("selector_call_1", "manage_memory", args)


def _memory_compiler_compat_enabled() -> bool:
    return str(os.getenv("MIRA_MEMORY_COMPILER_COMPAT_ENABLED", "true")).strip().lower() not in {"0", "false", "no", "off"}


def _append_chart_call(calls: list[dict[str, Any]], frame: ConversationFrame) -> list[dict[str, Any]]:
    source = next((call for call in calls if call.get("name") != "make_chart"), None)
    if not source:
        return calls
    chart_type = str(frame.chart_type or "line").strip().lower()
    if chart_type == "pie":
        chart_type = "donut"
    if chart_type not in {"line", "bar", "donut"}:
        chart_type = "line"
    payload = {"source_step_id": str(source.get("id") or "selector_call_1")}
    title = _chart_title_for_source(source)
    if title:
        payload["title"] = title
    return [*calls, _call("selector_call_2", "make_chart", {"view": chart_type, "payload": payload})]


def _chart_title_for_source(source: dict[str, Any]) -> str:
    name = str(source.get("name") or "").strip()
    args = source.get("args") if isinstance(source.get("args"), dict) else {}
    if name == "review_net_worth":
        return "Net worth trend"
    if name == "summarize_spending":
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        metric = str(payload.get("metric") or "spending").replace("_", " ")
        return f"{metric.title()} trend"
    return ""


def _call(step_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    clean_args = _drop_missing(args)
    return {
        "id": step_id,
        "name": name,
        "args": clean_args,
        "universal_args": copy.deepcopy(clean_args),
        "compiler_source": "intent_compiler",
    }


def _validation_error_call(message: str) -> dict[str, Any]:
    return {
        "id": "selector_call_1",
        "name": "summarize_spending",
        "args": {},
        "universal_args": {},
        "compiler_source": "intent_compiler",
        "validation_error": message,
    }


def _unsupported_chart_call(scope: str) -> dict[str, Any]:
    return {
        "id": "selector_call_1",
        "name": "summarize_spending",
        "args": {},
        "universal_args": {},
        "compiler_source": "intent_compiler",
        "validation_error": f"I can chart trends, breakdowns, top lists, budgets, cash flow, recurring items, and net worth trend, but I do not have chart-ready evidence for {scope}.",
    }


def _hints_from_decision(decision: dict[str, Any], *, selector_calls: list[dict[str, Any]] | None) -> _CompilerHints:
    call = _first_call(decision, selector_calls=selector_calls)
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    universal = call.get("universal_args") if isinstance(call.get("universal_args"), dict) else {}
    details = decision.get("details") if isinstance(decision.get("details"), dict) else {}
    top_level_payload = decision.get("payload") if isinstance(decision.get("payload"), dict) else {}
    top_level_filters = decision.get("filters") if isinstance(decision.get("filters"), dict) else {}
    decision_source = {
        "filters": {key: value for key, value in {**details, **top_level_filters}.items() if key in {"merchant", "category", "account", "search", "transaction_id", "metric", "group_by", "amount", "purpose", "change_type"}},
        "payload": {key: value for key, value in {**details, **top_level_payload}.items() if key not in {"limit", "offset", "sort"}},
        "limit": details.get("limit") if details.get("limit") not in MISSING_VALUES else decision.get("limit"),
        "offset": decision.get("offset"),
        "sort": decision.get("sort"),
    }
    sources = [args, universal, decision_source]
    filters = _first_dict(sources, "filters")
    payload = _first_dict(sources, "payload")
    return _CompilerHints(
        intent=str(decision.get("intent") or "").strip().lower(),
        tool_name=str(call.get("name") or call.get("tool") or universal.get("tool") or "").strip(),
        view=_first_text(sources, "view"),
        range=_first_text(sources, "range"),
        range_a=_first_text(sources, "range_a"),
        range_b=_first_text(sources, "range_b"),
        filters=filters,
        payload=payload,
        limit=_first_value(sources, "limit"),
        offset=_first_value(sources, "offset"),
        sort=_first_text(sources, "sort"),
    )


def _effective_intent(frame: ConversationFrame, hints: _CompilerHints) -> str:
    intent = str(frame.intent or "").strip().lower()
    hint_tool = str(hints.tool_name or "").strip()
    hint_view = str(hints.view or "").strip().lower()
    hint_intent = str(hints.intent or "").strip().lower()
    if intent not in {"", "none"}:
        if intent in {"budget_plan", "budget_status"} and (
            hint_view == "savings_capacity"
            or "savings_capacity" in hint_intent
            or "monthly_savings" in hint_intent
        ):
            return "savings_capacity"
        return intent
    if hint_tool == "query_transactions":
        return "transaction_lookup"
    if hint_tool == "summarize_spending":
        return {
            "top": "spending_top",
            "breakdown": "spending_breakdown",
            "trend": "spending_trend",
            "compare": "spending_compare",
        }.get(hint_view, "spending_total")
    if hint_tool == "review_budget":
        if hint_view == "savings_capacity" or "savings" in hint_intent:
            return "savings_capacity"
        return "budget_status" if hint_view == "category_status" else "budget_plan"
    if hint_tool == "review_cashflow":
        return "cashflow_shortfall" if hint_view == "shortfall" else "cashflow_forecast"
    if hint_tool == "check_affordability":
        return "affordability"
    if hint_tool == "review_recurring":
        return "recurring_changes" if hint_view == "changes" else "recurring_summary"
    if hint_tool == "review_net_worth":
        return {"balances": "net_worth_balance", "delta": "net_worth_delta"}.get(hint_view, "net_worth_trend")
    if hint_tool == "review_data_quality":
        if hints.filters.get("low_confidence") not in MISSING_VALUES:
            return "low_confidence_transactions"
        return {
            "enrichment_summary": "enrichment_quality",
            "low_confidence": "low_confidence_transactions",
            "explain_transaction": "explain_transaction",
        }.get(hint_view, "data_health")
    if hint_tool == "finance_overview":
        return {"priorities": "finance_priorities", "explain_metric": "explain_metric"}.get(hint_view, "finance_snapshot")
    return "none"


def _first_call(decision: dict[str, Any], *, selector_calls: list[dict[str, Any]] | None) -> dict[str, Any]:
    for calls in (decision.get("calls"), selector_calls):
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict):
                return call
    return {}


def _first_text(sources: list[dict[str, Any]], key: str) -> str:
    value = _first_value(sources, key)
    return str(value or "").strip()


def _first_value(sources: list[dict[str, Any]], key: str) -> Any:
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = source.get(key)
        if value not in MISSING_VALUES:
            return value
    return None


def _first_dict(sources: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for source in sources:
        value = source.get(key) if isinstance(source, dict) else None
        if isinstance(value, dict) and value:
            return copy.deepcopy(value)
    return {}


def _subject(frame: ConversationFrame) -> MiraSubject:
    text = str(frame.subject.canonical_id or frame.subject.display_name or frame.subject.text or "").strip()
    return MiraSubject(kind=frame.subject.kind, text=text or None)


def _subject_filters(subject: MiraSubject) -> dict[str, Any]:
    if subject.kind in {"merchant", "category", "account"} and subject.text:
        return {subject.kind: subject.text}
    if subject.kind == "unknown" and subject.text:
        return {"mention": subject.text}
    return {}


def _spending_metric(frame: ConversationFrame, hints: _CompilerHints, *, entity_total: bool) -> str:
    if frame.subject.kind == "metric" and frame.subject.text:
        return _normalize_metric(frame.subject.text)
    metric = str(hints.payload.get("metric") or hints.filters.get("metric") or "").strip()
    return _normalize_metric(metric) if metric else ("expenses" if entity_total else "summary")


def _normalize_metric(value: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "expense": "expenses",
        "expense_total": "expenses",
        "spend": "expenses",
        "spend_total": "expenses",
        "spending": "expenses",
        "spending_total": "expenses",
        "total": "expenses",
        "total_spending": "expenses",
        "income_total": "income",
        "refund": "refunds",
    }
    return aliases.get(text, text or "summary")


def _group_by(hints: _CompilerHints, *, default: str) -> str:
    value = str(hints.payload.get("group_by") or hints.filters.get("group_by") or "").strip().lower()
    if value in {"merchant", "category"}:
        return value
    signal = " ".join([hints.intent, hints.view, hints.tool_name]).lower()
    if "merchant" in signal:
        return "merchant"
    if "categor" in signal:
        return "category"
    return default


def _range(frame: ConversationFrame, hints: _CompilerHints, *, default: str = "current_month") -> str:
    mapped = _range_from_time(frame.time, frame.time_a, frame.time_b)
    if mapped:
        return mapped
    if hints.range:
        return hints.range
    return default


def _range_a(frame: ConversationFrame, hints: _CompilerHints) -> str:
    if hints.range_a:
        return hints.range_a
    range_value = _range(frame, hints, default="")
    return range_value or "current_month"


def _range_b(frame: ConversationFrame, hints: _CompilerHints) -> str:
    if hints.range_b:
        return hints.range_b
    return "last_month"


def _trend_range(frame: ConversationFrame, hints: _CompilerHints) -> str:
    value = _range(frame, hints, default="")
    if value in {"", "current_month", "this_month", "all", "all_time"}:
        return "last_6_months"
    if _looks_like_month(value):
        return "last_6_months"
    return value


def _range_from_time(time_value: str, time_a: str | None, time_b: str | None) -> str:
    token = str(time_value or "").strip().lower()
    aliases = {
        "this_month": "current_month",
        "last_month": "last_month",
        "all_time": "all",
        "this_week": "this_week",
        "last_week": "last_week",
        "last_7d": "last_7d",
        "last_30d": "last_30d",
        "last_90d": "last_90d",
        "last_365d": "last_365d",
        "last_3_months": "last_3_months",
        "last_6_months": "last_6_months",
        "last_year": "last_year",
        "month_before_prior": "last_month",
        "next_month_after_prior": "current_month",
        "ytd": "ytd",
    }
    if _is_dynamic_range_token(token):
        return token
    if token == "custom" and time_a and _looks_like_date(time_a):
        month = time_a[:7]
        if not time_b or str(time_b).startswith(month):
            return month
        if _looks_like_date(str(time_b)):
            return bounded_range_token(time_a, str(time_b))
    return aliases.get(token, "")


def _time_range_issue(frame: ConversationFrame) -> str:
    token = str(frame.time or "").strip().lower()
    if token in {"", "none"}:
        return ""
    if token != "custom":
        return ""
    if not frame.time_a:
        return "I need a supported time range before I can answer that."
    if not _looks_like_date(frame.time_a):
        return "I need the time range as an ISO date."
    if frame.time_b:
        if not _looks_like_date(str(frame.time_b)):
            return "I need the end of the time range as an ISO date."
        if str(frame.time_b) < str(frame.time_a):
            return "I need the end of the time range to be after the start."
    return ""


def _is_dynamic_range_token(token: str) -> bool:
    if is_bounded_range_token(token):
        return True
    month_match = re.match(r"^last_(\d{1,2})_months$", token)
    if month_match:
        return 1 <= int(month_match.group(1)) <= 36
    day_match = re.match(r"^last_(\d{1,3})d$", token)
    if day_match:
        return 1 <= int(day_match.group(1)) <= 365
    return False


def _transaction_view(frame: ConversationFrame, hints: _CompilerHints, filters: dict[str, Any]) -> str:
    signal = " ".join([hints.intent, hints.view]).lower()
    if filters.get("transaction_id"):
        return "detail"
    if "latest" in signal or "last" in signal:
        return "latest"
    if filters.get("search"):
        return "search"
    return "list"


def _transaction_subject_category_alias(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"income", "deposit", "paycheck", "payroll", "salary"}:
        return "Income"
    return ""


def _copy_allowed(source: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in (source or {}).items() if key in allowed and value not in MISSING_VALUES}


def _preview_change_alias(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "recategorize": "bulk_recategorize",
        "recategorise": "bulk_recategorize",
        "recategorize_transactions": "bulk_recategorize",
        "recategory": "bulk_recategorize",
        "reclassify": "bulk_recategorize",
        "category": "bulk_recategorize",
        "change_category": "bulk_recategorize",
        "move_category": "bulk_recategorize",
        "rule": "create_rule",
        "rule_creation": "create_rule",
        "budget": "set_budget",
        "set_budget": "set_budget",
    }
    return aliases.get(text, text)


def _drop_missing(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in (value or {}).items():
        if isinstance(item, dict):
            item = _drop_missing(item)
        if item not in MISSING_VALUES:
            out[key] = item
    return out


def _looks_like_month(value: str) -> bool:
    text = str(value or "")
    return len(text) == 7 and text[4] == "-" and text[:4].isdigit() and text[5:].isdigit()


def _looks_like_date(value: str) -> bool:
    text = str(value or "")
    return len(text) == 10 and text[4] == "-" and text[7] == "-" and text[:4].isdigit()


def _trace(frame: ConversationFrame, hints: _CompilerHints, calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "compiler": "intent_compiler",
        "route": frame.route,
        "intent": frame.intent,
        "subject_kind": frame.subject.kind,
        "time": frame.time,
        "output": frame.output,
        "hint_tool": hints.tool_name,
        "hint_view": hints.view,
        "compiled_tool_count": len(calls),
        "compiled_tools": [call.get("name") for call in calls],
    }


__all__ = ["IntentCompilerResult", "compile_selector_decision"]
