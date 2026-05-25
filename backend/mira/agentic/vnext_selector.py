from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from range_parser import resolve_followup_range

from mira.persona import selector_default_route_line, selector_persona_line
from mira.agentic.intent_frame import (
    DISCOURSE_ACTION_VALUES,
    INTENT_VALUES,
    MiraIntentFrame,
    OUTPUT_VALUES,
    SUBJECT_KIND_VALUES,
    TIME_ALIASES,
    TIME_VALUES,
    is_supported_time_token,
)
from mira.agentic.semantic_catalog import canonical_semantic_tool_name
from mira.agentic.semantic_frames import latest_prior_frame
from mira.agentic.vnext_args import UNIVERSAL_ARG_FIELDS, adapt_universal_args
from mira.agentic.vnext_manifest import (
    all_tool_schemas,
    build_family_detail_manifest,
    build_grouped_tool_manifest,
    selected_family_name,
    selector_manifest_coverage,
    tools_by_name,
)


SelectorCompleter = Callable[[str, int, str], str]

_RECENT_CONTEXT_MAX_CHARS = 600
_COMPACT_FIELD_MAX_CHARS = 72
_COMPACT_PENDING_OPTION_MAX_CHARS = 36
_COMPACT_PENDING_RAW_MAX_CHARS = 56

_SINGLE_RANGE_TOOLS = {
    "query_transactions",
    "summarize_spending",
    "finance_overview",
    "review_budget",
    "review_net_worth",
}
_CHAT_ROUTES = {"chat", "explain_last_answer"}
_TOOL_ROUTES = {"finance_tool", "memory", "write_preview"}
_MEMORY_SEMANTIC_TOOLS = {"manage_memory"}
_WRITE_PREVIEW_SEMANTIC_TOOLS = {"preview_finance_change"}
_FINANCE_SEMANTIC_TOOLS = {
    "query_transactions",
    "summarize_spending",
    "finance_overview",
    "review_budget",
    "review_cashflow",
    "check_affordability",
    "review_recurring",
    "review_net_worth",
    "review_data_quality",
    "make_chart",
}
_ROUTE_ALIASES = {
    "": "",
    "answer": "chat",
    "chat": "chat",
    "general": "chat",
    "general_answer": "chat",
    "no_tool": "chat",
    "none": "chat",
    "explain": "explain_last_answer",
    "explain_last_answer": "explain_last_answer",
    "tool": "finance_tool",
    "tools": "finance_tool",
    "finance": "finance_tool",
    "finance_tool": "finance_tool",
    "finance_tools": "finance_tool",
    "chart": "finance_tool",
    "charts": "finance_tool",
    "write_chart": "finance_tool",
    "writes_chart": "finance_tool",
    "write_charts": "finance_tool",
    "writes_charts": "finance_tool",
    "make_chart": "finance_tool",
    "memory": "memory",
    "write": "write_preview",
    "write_preview": "write_preview",
    "preview": "write_preview",
}

SELECTOR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "route": {"type": "string"},
        "answer": {"type": "string"},
        "intent": {"type": "string"},
        "subject": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "text": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "time": {"type": "string"},
        "time_a": {"type": ["string", "null"]},
        "time_b": {"type": ["string", "null"]},
        "output": {"type": "string"},
        "chart_type": {"type": ["string", "null"]},
        "discourse_action": {"type": "string"},
        "details": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metric": {"type": ["string", "null"]},
                "group_by": {"type": ["string", "null"]},
                "amount": {"type": ["number", "string", "null"]},
                "purpose": {"type": ["string", "null"]},
                "change_type": {"type": ["string", "null"]},
                "merchant": {"type": ["string", "null"]},
                "category": {"type": ["string", "null"]},
                "search": {"type": ["string", "null"]},
                "transaction_id": {"type": ["string", "null"]},
                "limit": {"type": ["integer", "null"]},
                "answer_mode": {"type": ["string", "null"]},
                "memory_action": {"type": ["string", "null"]},
                "text": {"type": ["string", "null"]},
                "question": {"type": ["string", "null"]},
                "topic": {"type": ["string", "null"]},
                "memory_id": {"type": ["integer", "string", "null"]},
                "memory_type": {"type": ["string", "null"]},
            },
        },
    },
    "required": ["route", "intent", "subject", "time", "output", "discourse_action"],
    "additionalProperties": False,
}

COMPACT_SELECTOR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "route": {"type": "string"},
        "intent": {"type": "string"},
        "subject": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "text": {"type": ["string", "null"]},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        "time": {"type": "string"},
        "time_a": {"type": ["string", "null"]},
        "time_b": {"type": ["string", "null"]},
        "output": {"type": "string"},
        "chart_type": {"type": ["string", "null"]},
        "discourse_action": {"type": "string"},
        "details": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metric": {"type": ["string", "null"]},
                "group_by": {"type": ["string", "null"]},
                "amount": {"type": ["number", "string", "null"]},
                "purpose": {"type": ["string", "null"]},
                "change_type": {"type": ["string", "null"]},
                "merchant": {"type": ["string", "null"]},
                "category": {"type": ["string", "null"]},
                "search": {"type": ["string", "null"]},
                "transaction_id": {"type": ["string", "null"]},
                "limit": {"type": ["integer", "null"]},
                "memory_action": {"type": ["string", "null"]},
                "text": {"type": ["string", "null"]},
                "question": {"type": ["string", "null"]},
                "topic": {"type": ["string", "null"]},
                "memory_id": {"type": ["integer", "string", "null"]},
                "memory_type": {"type": ["string", "null"]},
            },
        },
    },
    "required": ["route", "intent", "subject", "time", "output", "discourse_action"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SelectorResult:
    calls: list[dict[str, Any]]
    decision: dict[str, Any]
    raw: str
    prompt: str
    manifest: str
    attempts: int
    llm_calls: int
    status: str
    error: str = ""
    family_detail_used: bool = False
    repair_used: bool = False
    trace: dict[str, Any] = field(default_factory=dict)
    intent_frame: dict[str, Any] | None = None


def build_selector_system_prompt(*, compact_output: bool | None = None) -> str:
    compact = _selector_compact_output_enabled() if compact_output is None else bool(compact_output)
    schema_line = (
        "Schema: {route,intent,subject:{kind,text},time,time_a,time_b,output,chart_type,discourse_action,details}"
        if compact
        else "Schema: {route,intent,subject:{kind,text},time,time_a,time_b,output,chart_type,discourse_action,answer,details}"
    )
    chat_line = (
        'Chat: intent=none, subject.kind=none, time=none, output=none. Leave answer text out; answerer writes prose. Finance/write_preview/explain_last leave answer out.'
        if compact
        else 'Chat: intent=none, subject.kind=none, time=none, output=none. Greetings/thanks/ok/small-talk may include answer with details.answer_mode="inline"; help, explanations, knowledge, advice, and creative writing leave answer="". Finance/write_preview/explain_last leave answer="".'
    )
    details_line = (
        "Details allowed: metric,group_by,amount,purpose,change_type,merchant,category,search,transaction_id,limit,memory_action,text,question,topic,memory_id,memory_type. Include only non-null details; omit details unless a non-empty detail is needed."
        if compact
        else "Details allowed: metric,group_by,amount,purpose,change_type,merchant,category,search,transaction_id,limit,answer_mode,memory_action,text,question,topic,memory_id,memory_type. Include only non-null details."
    )
    compact_line = (
        "\nCompact output: omit answer, answer_mode, null fields, empty details, time_a/time_b unless custom, chart_type unless chart, and subject.text when subject.kind is none. Return one minimal JSON object."
        if compact
        else ""
    )
    return selector_persona_line() + f"""

{schema_line}
No tool names, views, filters, calls, SQL, or chart data. Python compiles tools.

route: chat|finance|memory|write_preview|explain_last
intent: none|spending_total|spending_explain|spending_top|spending_breakdown|spending_trend|spending_compare|transaction_lookup|budget_status|budget_plan|savings_capacity|cashflow_forecast|cashflow_shortfall|affordability|recurring_summary|recurring_changes|net_worth_balance|net_worth_trend|net_worth_delta|finance_snapshot|finance_priorities|explain_metric|data_health|enrichment_quality|low_confidence_transactions|explain_transaction|memory_op|write_preview
subject.kind: merchant|category|account|metric|transaction|net_worth|self|unknown|none
time: this_month|last_month|ytd|all_time|last_Nd|last_N_months|last_year|custom|month_before_prior|next_month_after_prior|none
output: scalar|table|list|chart|comparison|status|preview|none
discourse_action: new|follow_up|correction|refine|clarification_reply|clear

""" + selector_default_route_line() + f""" {chat_line}
Finance: Folio lookup -> route finance. how much/total spent at/on merchant/category -> spending_total scalar. why/high/spike -> spending_explain table. top/biggest/list/show -> table intent. recent/latest/when income/deposit -> transaction_lookup; income/deposit/paycheck subject=category Income unless txn id. budget/savings/cashflow/afford/can I spend/recurring/net worth -> matching intent. chart/plot/graph -> output=chart + chart_type. "expenses"/"spending" alone is metric, not subject. move/set/recategorize/change charges/category -> route write_preview intent write_preview output preview with details.change_type=bulk_recategorize, details.merchant, details.category; never memory_action/text.
""" + _memory_selector_instruction_line() + """
""" + details_line + compact_line + """
Month names become custom with time_a=YYYY-MM-01. "last month" is last_month. Only relative follow-ups like "the month before" use month_before_prior. Follow-ups inherit omitted prior slots. Latest user words override prior context."""


def build_selector_repair_system_prompt() -> str:
    return """Repair Mira controller JSON. Return JSON only.
Use exact tool names from the manifest. Do not shorten or invent tool names.
Preserve the user's intent and arguments when possible.
Latest user message wins over prior context for any field it mentions or corrects.
Explicit month names must become YYYY-MM.
Keep context_action and range_source accurate.
If no Folio tool is needed, return {"route":"chat","intent":"chat","needs_folio_evidence":false,"frame_patch":null,"calls":[]}.
If saving explicit user memory, use manage_memory view=remember and include payload.text.
If a finance tool is needed, return route=finance_tool with needed envelope fields.
For a follow-up that only changes the raw subject, use frame_patch.frame_action=patch_prior, subject_action=replace, subject.raw, range_action=inherit, and calls=[].
If a write is requested, return route=write_preview and preview_finance_change only."""


def build_selector_prompt(*, question: str, manifest: str, recent_context: str = "", today: str | None = None) -> str:
    today = today or datetime.now().date().isoformat()
    context_block = (
        "Context for follow-ups only:\n"
        + recent_context
        + "\nPreserve omitted prior slots; latest user words override.\n\n"
        if recent_context.strip()
        else ""
    )
    return (
        build_selector_system_prompt()
        + "\n\nToday:\n"
        + today
        + "\n\n"
        + context_block
        + "User question:\n"
        + str(question or "")
        + "\n\nReturn the JSON object now."
    )


def build_repair_prompt(
    *,
    question: str,
    manifest: str,
    recent_context: str,
    invalid_decision: dict[str, Any],
    today: str | None = None,
) -> str:
    today = today or datetime.now().date().isoformat()
    return (
        build_selector_repair_system_prompt()
        + "\n\nManifest:\n"
        + manifest
        + "\n\nToday:\n"
        + today
        + "\n\nUser question:\n"
        + str(question or "")
        + ("\n\nRecent context:\n" + recent_context if recent_context.strip() else "")
        + "\n\nInvalid selector output:\n"
        + str(invalid_decision.get("raw_response") or "")
        + "\n\nValidation errors:\n"
        + json.dumps(invalid_decision.get("validation_errors") or invalid_decision.get("error") or [], ensure_ascii=True, default=str)
        + "\n\nReturn repaired selector JSON now."
    )


def run_selector(
    *,
    question: str,
    history: list[dict[str, Any]] | None = None,
    base_tools: list[dict[str, Any]] | None = None,
    completer: SelectorCompleter | None = None,
    max_tokens: int = 220,
    allow_legacy_executable_calls: bool = False,
) -> SelectorResult:
    tools = base_tools or all_tool_schemas()
    started = time.perf_counter()
    slash = _memory_slash_selector_result(question=question, started=started)
    if slash is not None:
        return slash
    manifest = ""
    recent_context = format_recent_context(history)
    complete = completer or _default_completer
    attempts = 0
    llm_calls = 0
    family_detail_used = False
    repair_used = False

    prompt = build_selector_prompt(question=question, manifest=manifest, recent_context=recent_context)
    raw = complete(prompt, max_tokens, "controller")
    attempts += 1
    llm_calls += 1
    calls, decision = normalize_selector_decision(
        raw=raw,
        base_tools=tools,
        allow_legacy_executable_calls=allow_legacy_executable_calls,
    )
    calls, decision = apply_discourse_frames(calls=calls, decision=decision, history=history, tools=tools, question=question)
    calls, decision = apply_context_semantics(calls=calls, decision=decision, history=history, question=question)
    calls, decision = _demote_non_slash_memory_route(calls=calls, decision=decision)

    status = selector_status(decision, calls)
    error = selector_error(decision, calls) if status == "clarify" else ""
    trace = {
        "runtime": "agentic_vnext",
        "stage": "selector",
        "status": status,
        "attempts": attempts,
        "llm_calls": llm_calls,
        "selector_ms": round((time.perf_counter() - started) * 1000, 2),
        "repair_used": repair_used,
        "family_detail_used": family_detail_used,
        "prompt_tokens_est": estimate_tokens(prompt),
        "manifest_tokens_est": estimate_tokens(manifest),
        "coverage": selector_manifest_coverage(tools),
    }
    intent_frame = decision.get("intent_frame") if isinstance(decision.get("intent_frame"), dict) else None
    return SelectorResult(
        calls=calls,
        decision=decision,
        raw=raw,
        prompt=prompt,
        manifest=manifest,
        attempts=attempts,
        llm_calls=llm_calls,
        status=status,
        error=error,
        family_detail_used=family_detail_used,
        repair_used=repair_used,
        trace=trace,
        intent_frame=intent_frame,
    )


def _memory_slash_selector_result(*, question: str, started: float) -> SelectorResult | None:
    parsed = _parse_memory_slash_command(question)
    if parsed is None:
        return None

    action = str(parsed.get("action") or "").strip().lower()
    body = str(parsed.get("body") or "").strip()
    normalizer_calls = 0
    normalizer_result: dict[str, Any] | None = None
    if action == "help":
        decision = _inline_slash_answer(_memory_slash_help())
    elif action == "remember":
        from mira import memory_v2

        normalizer_result = memory_v2.normalize_explicit_memory_command(body)
        normalizer_calls = 1
        if not normalizer_result.get("ok"):
            decision = _inline_slash_answer(str(normalizer_result.get("reason") or "I could not turn that into a safe memory."))
        else:
            decision = _memory_command_decision(
                action="remember",
                output="status",
                memory={
                    "action": "remember",
                    "text": normalizer_result["text"],
                    "memory_type": normalizer_result.get("memory_type"),
                    "topic": normalizer_result.get("topic"),
                },
                details={
                    "memory_action": "remember",
                    "text": normalizer_result["text"],
                    "memory_type": normalizer_result.get("memory_type"),
                    "topic": normalizer_result.get("topic"),
                },
            )
    elif action == "list":
        memory_type = body if body in _SLASH_MEMORY_TYPES else ""
        decision = _memory_command_decision(
            action="list",
            output="list",
            memory={"action": "list", **({"memory_type": memory_type} if memory_type else {})},
            details={"memory_action": "list", **({"memory_type": memory_type} if memory_type else {})},
        )
    elif action == "search":
        if not body:
            decision = _inline_slash_answer("Use `/memory search answer style` to search saved memories.")
        else:
            decision = _memory_command_decision(
                action="retrieve",
                output="list",
                memory={"action": "retrieve", "question": body},
                details={"memory_action": "retrieve", "question": body},
            )
    elif action == "forget":
        if not body:
            decision = _inline_slash_answer("Use `/memory forget 12` or `/memory forget answer style`.")
        else:
            memory: dict[str, Any] = {"action": "forget"}
            if body.isdigit():
                memory["memory_id"] = int(body)
            else:
                memory["topic"] = body
            decision = _memory_command_decision(
                action="forget",
                output="status",
                memory=memory,
                details={"memory_action": "forget", **{k: v for k, v in memory.items() if k != "action"}},
            )
    else:
        decision = _inline_slash_answer(_memory_slash_help())

    decision = normalize_intent_frame_decision(decision)
    status = selector_status(decision, [])
    trace = {
        "runtime": "agentic_vnext",
        "stage": "slash_command",
        "status": status,
        "attempts": normalizer_calls,
        "llm_calls": normalizer_calls,
        "selector_ms": round((time.perf_counter() - started) * 1000, 2),
        "repair_used": False,
        "family_detail_used": False,
        "prompt_tokens_est": 0,
        "manifest_tokens_est": 0,
        "slash_command": "memory",
    }
    if normalizer_result is not None:
        trace["memory_command_normalized"] = bool(normalizer_result.get("ok"))
    return SelectorResult(
        calls=[],
        decision=decision,
        raw=str(question or ""),
        prompt="",
        manifest="",
        attempts=normalizer_calls,
        llm_calls=normalizer_calls,
        status=status,
        error=selector_error(decision, []) if status == "clarify" else "",
        family_detail_used=False,
        repair_used=False,
        trace=trace,
        intent_frame=decision.get("intent_frame") if isinstance(decision.get("intent_frame"), dict) else None,
    )


def _demote_non_slash_memory_route(*, calls: list[dict[str, Any]], decision: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not _memory_slash_commands_enabled():
        return calls, decision
    frame = decision.get("intent_frame") if isinstance(decision.get("intent_frame"), dict) else {}
    route = str(frame.get("route") or decision.get("route") or "").strip().lower()
    if route != "memory":
        return calls, decision
    return [], normalize_intent_frame_decision(_inline_slash_answer(_memory_slash_help()))


_SLASH_MEMORY_TYPES = {
    "preference",
    "goal",
    "constraint",
    "stressor",
    "commitment",
    "identity_fact",
    "tone_preference",
}


def _parse_memory_slash_command(question: str) -> dict[str, str] | None:
    if not _memory_slash_commands_enabled():
        return None
    text = str(question or "").strip()
    prefix = "/memory"
    if text != prefix and not text.startswith(prefix + " "):
        return None
    rest = text[len(prefix):].strip()
    if not rest:
        return {"action": "help", "body": ""}
    head, _, tail = rest.partition(" ")
    action = head.strip().lower()
    aliases = {
        "add": "remember",
        "find": "search",
        "recall": "search",
        "remove": "forget",
        "save": "remember",
    }
    return {"action": aliases.get(action, action), "body": tail.strip()}


def _memory_slash_commands_enabled() -> bool:
    return str(os.getenv("MIRA_MEMORY_SLASH_COMMANDS_ENABLED", "true")).strip().lower() not in {"0", "false", "no", "off"}


def _memory_selector_instruction_line() -> str:
    if _memory_slash_commands_enabled():
        return "Memory slash commands are handled before this selector. For natural-language memory questions, stay chat and suggest `/memory remember ...`, `/memory list`, `/memory search ...`, or `/memory forget ...` when useful. Questions about this chat or last message are chat."
    return "Memory only: route=memory intent=memory_op. save/remember -> output=status details.memory_action=remember details.text=<memory>. ask/list/forget/update saved memories -> details.memory_action=retrieve/list/forget/update plus question/topic/text/memory_id. Questions about this chat or last message are chat."


def _memory_command_decision(*, action: str, output: str, memory: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    return {
        "route": "memory",
        "intent": "memory_op",
        "subject": {"kind": "none", "text": None},
        "time": "none",
        "output": output,
        "discourse_action": "new",
        "answer": "",
        "details": details,
        "intent_frame": {
            "route": "memory",
            "intent": "memory_op",
            "subject": {"kind": "none", "text": None},
            "time": "none",
            "output": output,
            "discourse_action": "new",
            "memory": memory,
        },
        "slash_command": "memory",
        "memory_command_action": action,
    }


def _inline_slash_answer(answer: str) -> dict[str, Any]:
    return {
        "route": "chat",
        "intent": "none",
        "subject": {"kind": "none", "text": None},
        "time": "none",
        "output": "none",
        "discourse_action": "new",
        "answer": str(answer or "").strip(),
        "details": {"answer_mode": "inline"},
        "slash_command": "memory",
    }


def _memory_slash_help() -> str:
    return "Use `/memory remember ...`, `/memory list`, `/memory search ...`, or `/memory forget ...`."


def normalize_selector_decision(
    *,
    raw: str,
    base_tools: list[dict[str, Any]],
    allow_legacy_executable_calls: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        decision = parse_json_object(raw)
    except Exception as exc:
        raw_text = str(raw or "").lstrip()
        if raw_text.startswith('{"answer"') or raw_text.startswith('{"route": "chat"') or raw_text.startswith('{"route":"chat"'):
            decision = {
                "route": "general_answer",
                "selector_answer_truncated": True,
                "raw_response": raw,
                "validation_errors": [],
            }
            return [], decision
        decision = {
            "calls": [],
            "error": f"selector JSON parse failed: {exc}",
        }

    if not isinstance(decision, dict):
        decision = {"calls": [], "error": "selector response must be a JSON object"}

    frame_source = decision
    if not allow_legacy_executable_calls:
        frame_source = {
            key: value
            for key, value in decision.items()
            if key not in {"calls", "tool_calls", "frame_patch"}
        }
    normalized_frame_decision = normalize_intent_frame_decision(frame_source)
    for key in ("intent_frame", "intent_frame_source", "intent_frame_error"):
        if key in normalized_frame_decision:
            decision[key] = normalized_frame_decision[key]
    if allow_legacy_executable_calls:
        decision = normalize_frame_patch_decision(decision)
    elif "frame_patch" in decision:
        decision["ignored_frame_patch"] = True
        decision.pop("frame_patch", None)

    by_name = tools_by_name(base_tools)
    calls = []
    validation_errors: list[str] = []
    raw_calls = decision.get("calls")
    if raw_calls is None:
        raw_calls = decision.get("tool_calls") or []
    if raw_calls and not isinstance(raw_calls, list):
        if allow_legacy_executable_calls:
            validation_errors.append("calls must be an array")
        else:
            decision["ignored_selector_call_count"] = 1
        raw_calls = []
    if raw_calls and not allow_legacy_executable_calls:
        decision["ignored_selector_call_count"] = len(raw_calls)
        if not decision_has_tool_intent_frame(decision) and not decision_requests_general_answer(decision):
            validation_errors.append("selector emitted executable calls; ignored")
        raw_calls = []
    if _frame_patch_action(decision) in {"patch_prior", "clarification_reply"}:
        route = _ROUTE_ALIASES.get(str(decision.get("route") or "").strip().lower(), str(decision.get("route") or "").strip().lower())
        if route in {"memory", "write_preview"}:
            decision.pop("frame_patch", None)
        else:
            if raw_calls:
                decision["dropped_calls_due_to_frame_patch"] = len(raw_calls)
            raw_calls = []

    for index, raw_call in enumerate(raw_calls or [], start=1):
        if not isinstance(raw_call, dict):
            validation_errors.append(f"call {index} must be an object")
            continue
        raw_name = str(raw_call.get("tool") or raw_call.get("name") or "").strip()
        name = canonical_semantic_tool_name(raw_name)
        if name not in by_name:
            validation_errors.append(f"unknown tool name: {raw_name or '<empty>'}")
            continue
        nested_args = raw_call.get("args") if isinstance(raw_call.get("args"), dict) else {}
        normalized_call = {**nested_args, **{key: value for key, value in raw_call.items() if key != "args"}, "tool": name}
        _normalize_structured_filter_values(normalized_call)
        _repair_structured_call_from_selector_intent(normalized_call, decision)
        args, validation_error = adapt_universal_args(name, normalized_call, by_name[name])
        if validation_error:
            validation_errors.append(validation_error)
        calls.append({
            "id": str(raw_call.get("id") or f"selector_call_{index}"),
            "name": name,
            "universal_args": {
                key: normalized_call.get(key)
                for key in UNIVERSAL_ARG_FIELDS
                if normalized_call.get(key) not in (None, "", [], {})
            },
            "args": args,
            "validation_error": validation_error,
        })

    if allow_legacy_executable_calls:
        calls, validation_errors = append_chart_call_from_structured_intent(
            calls=calls,
            decision=decision,
            by_name=by_name,
            validation_errors=validation_errors,
        )

    selector_answer = str(decision.get("answer") or decision.get("direct_answer") or "").strip()
    if selector_answer and not calls and not decision_has_tool_intent_frame(decision):
        decision["route"] = "general_answer"
        decision["selector_answer_chars"] = len(selector_answer)

    calls, validation_errors = apply_controller_route_permissions(
        decision=decision,
        calls=calls,
        validation_errors=validation_errors,
    )

    if (
        not calls
        and not selected_family(decision)
        and not decision_requests_general_answer(decision)
        and not decision_has_discourse_frame(decision)
        and not decision_has_tool_intent_frame(decision)
    ):
        validation_errors.append("selector returned no calls, family, or route")

    decision["calls"] = calls
    decision["validation_errors"] = validation_errors
    decision["raw_response"] = raw
    return calls, decision


def normalize_intent_frame_decision(decision: dict[str, Any]) -> dict[str, Any]:
    explicit = decision.get("intent_frame") if isinstance(decision.get("intent_frame"), dict) else None
    payload = explicit or _intent_frame_payload_from_selector_decision(decision)
    try:
        frame = MiraIntentFrame.from_dict(payload)
        return {
            **decision,
            "intent_frame": frame.to_dict(),
            "intent_frame_source": "explicit" if explicit else "synthetic",
        }
    except ValueError as exc:
        fallback = _intent_frame_payload_from_selector_decision({key: value for key, value in decision.items() if key != "intent_frame"})
        try:
            frame = MiraIntentFrame.from_dict(fallback)
            return {
                **decision,
                "intent_frame": frame.to_dict(),
                "intent_frame_source": "synthetic_after_error",
                "intent_frame_error": str(exc),
            }
        except ValueError as fallback_exc:
            return {
                **decision,
                "intent_frame_error": str(fallback_exc if not explicit else exc),
            }


def _intent_frame_payload_from_selector_decision(decision: dict[str, Any]) -> dict[str, Any]:
    route = _intent_frame_route_from_decision(decision)
    intent = _intent_frame_intent_from_decision(decision, route=route)
    output = _intent_frame_output_from_decision(decision, intent=intent)
    subject = _intent_frame_subject_from_decision(decision)
    time_value, time_a, time_b = _intent_frame_time_from_decision(decision)
    payload = {
        "route": route,
        "answer": str(decision.get("answer") or decision.get("direct_answer") or ""),
        "intent": intent,
        "subject": subject,
        "time": time_value,
        "time_a": time_a,
        "time_b": time_b,
        "output": output,
        "chart_type": _intent_frame_chart_type_from_decision(decision, output=output),
        "discourse_action": _intent_frame_discourse_action(decision),
    }
    memory = _intent_frame_memory_from_decision(decision, route=route)
    if memory:
        payload["memory"] = memory
    return payload


def _intent_frame_memory_from_decision(decision: dict[str, Any], *, route: str) -> dict[str, Any]:
    if route != "memory":
        return {}
    details = decision.get("details") if isinstance(decision.get("details"), dict) else {}
    out: dict[str, Any] = {}
    action = str(details.get("memory_action") or details.get("action") or "").strip()
    if action:
        out["action"] = action
    for key in ("text", "question", "topic", "memory_id", "memory_type", "limit"):
        value = details.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    if details.get("search") not in (None, "", [], {}) and "question" not in out:
        out["question"] = details.get("search")
    return out


def _intent_frame_route_from_decision(decision: dict[str, Any]) -> str:
    raw_intent = str(decision.get("intent") or "").strip().lower()
    details = decision.get("details") if isinstance(decision.get("details"), dict) else {}
    requested_output = str(decision.get("output") or decision.get("requested_output") or "").strip().lower()
    if requested_output == "preview" or str(details.get("change_type") or "").strip():
        return "write_preview"
    if raw_intent in INTENT_VALUES and raw_intent != "none":
        if raw_intent == "memory_op":
            return "memory"
        if raw_intent == "write_preview":
            return "write_preview"
        return "finance"
    route = canonical_controller_route(decision, [])
    if route == "finance_tool":
        return "finance"
    if route == "explain_last_answer":
        return "explain_last"
    if route in {"chat", "memory", "write_preview"}:
        return route
    if str(decision.get("answer") or decision.get("direct_answer") or "").strip():
        return "chat"
    return "chat"


def _intent_frame_intent_from_decision(decision: dict[str, Any], *, route: str) -> str:
    if route == "memory":
        return "memory_op"
    if route == "write_preview":
        return "write_preview"
    if route in {"chat", "explain_last", "clarify_response"}:
        return "none"

    raw = str(decision.get("intent") or "").strip().lower()
    if raw in INTENT_VALUES:
        return raw
    signal = " ".join([raw, _first_raw_tool_name(decision), _first_raw_view(decision)]).strip().lower()
    if "transaction" in signal or "deposit" in signal or "paycheck" in signal or "income_timing" in signal:
        return "transaction_lookup"
    if "net_worth" in signal or "networth" in signal or "net worth" in signal:
        if "delta" in signal or "change" in signal:
            return "net_worth_delta"
        if "balance" in signal:
            return "net_worth_balance"
        return "net_worth_trend"
    if "budget" in signal:
        return "budget_status" if "status" in signal or "category" in signal else "budget_plan"
    if "saving" in signal or "save_capacity" in signal:
        return "savings_capacity"
    if "shortfall" in signal:
        return "cashflow_shortfall"
    if "cashflow" in signal or "cash_flow" in signal:
        return "cashflow_forecast"
    if "afford" in signal:
        return "affordability"
    if "recurring" in signal or "subscription" in signal:
        return "recurring_changes" if "change" in signal else "recurring_summary"
    if "data_health" in signal or "health" in signal:
        return "data_health"
    if "enrichment" in signal:
        return "enrichment_quality"
    if "low_confidence" in signal or "low confidence" in signal:
        return "low_confidence_transactions"
    if "priority" in signal:
        return "finance_priorities"
    if "dashboard" in signal or "snapshot" in signal or "overview" in signal:
        return "finance_snapshot"
    if "metric" in signal or "explain" in signal:
        return "explain_metric"
    if "top" in signal or "biggest" in signal:
        return "spending_top"
    if "breakdown" in signal:
        return "spending_breakdown"
    if "trend" in signal or "chart" in signal or "plot" in signal or "graph" in signal:
        return "spending_trend"
    if "compare" in signal:
        return "spending_compare"
    if "spend" in signal or "expense" in signal or "merchant" in signal or "categor" in signal:
        return "spending_total"
    return "none"


def _intent_frame_subject_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    subject = _decision_subject(decision)
    if subject.get("raw"):
        return {
            "kind": _intent_subject_kind(subject.get("type_hint")),
            "text": subject.get("raw"),
        }

    raw_call = _first_raw_call(decision)
    if raw_call:
        nested_args = raw_call.get("args") if isinstance(raw_call.get("args"), dict) else {}
        merged = {**nested_args, **{key: value for key, value in raw_call.items() if key != "args"}}
        filters = merged.get("filters") if isinstance(merged.get("filters"), dict) else {}
        for key in ("merchant", "category", "account", "transaction_id"):
            value = _selector_scalar_text(filters.get(key) or merged.get(key))
            if value:
                return {"kind": "transaction" if key == "transaction_id" else key, "text": value}
        payload = merged.get("payload") if isinstance(merged.get("payload"), dict) else {}
        metric = str(payload.get("metric") or merged.get("metric") or "").strip()
        if metric:
            return {"kind": "metric", "text": metric}
    return {"kind": "none", "text": None}


def _intent_subject_kind(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in SUBJECT_KIND_VALUES:
        return text
    if text in {"transaction_id", "txn"}:
        return "transaction"
    return "unknown" if text else "none"


def _intent_frame_time_from_decision(decision: dict[str, Any]) -> tuple[str, str | None, str | None]:
    explicit_time = str(decision.get("time") or "").strip().lower()
    if explicit_time:
        explicit_time = TIME_ALIASES.get(explicit_time, explicit_time)
        time_a = str(decision.get("time_a") or "").strip() or None
        time_b = str(decision.get("time_b") or "").strip() or None
        if is_supported_time_token(explicit_time):
            return explicit_time, time_a, time_b
    range_value = str(decision.get("range") or "").strip()
    if not range_value:
        raw_call = _first_raw_call(decision)
        if raw_call:
            nested_args = raw_call.get("args") if isinstance(raw_call.get("args"), dict) else {}
            merged = {**nested_args, **{key: value for key, value in raw_call.items() if key != "args"}}
            range_value = str(merged.get("range") or merged.get("range_a") or "").strip()
    token = range_value.strip().lower()
    aliases = {
        "": "none",
        "all": "all_time",
        "current": "this_month",
        "current_month": "this_month",
        "prior": "last_month",
        "prior_month": "last_month",
        "previous_month": "last_month",
    }
    token = aliases.get(token, token)
    if token in TIME_VALUES:
        return token, None, None
    if token.startswith("last_") and (token.endswith("d") or token.endswith("_months")):
        return token if is_supported_time_token(token) else "custom", None, None
    if token and _is_month_token(token):
        return "custom", f"{token}-01", None
    return "none", None, None


def _intent_frame_output_from_decision(decision: dict[str, Any], *, intent: str) -> str:
    requested = str(decision.get("output") or decision.get("requested_output") or "").strip().lower()
    if intent == "affordability":
        return "status"
    output_aliases = {
        "scalar_total": "scalar",
        "rows": "table",
        "summary": "status",
    }
    if requested in output_aliases:
        return output_aliases[requested]
    if requested in OUTPUT_VALUES:
        return requested
    if _decision_requests_chart(decision):
        return "chart"
    if intent in {"write_preview"}:
        return "preview"
    if intent in {"spending_explain", "transaction_lookup", "spending_top", "spending_breakdown", "low_confidence_transactions"}:
        return "table"
    if intent.endswith("_trend"):
        return "chart" if _decision_requests_chart(decision) else "table"
    if intent in {"budget_status", "budget_plan", "cashflow_forecast", "cashflow_shortfall", "affordability", "recurring_summary", "recurring_changes", "data_health", "enrichment_quality", "finance_snapshot", "finance_priorities", "explain_metric", "explain_transaction"}:
        return "status"
    if intent in {"spending_total", "net_worth_balance", "net_worth_delta", "savings_capacity"}:
        return "scalar"
    return "none"


def _intent_frame_chart_type_from_decision(decision: dict[str, Any], *, output: str) -> str | None:
    if output != "chart":
        return None
    explicit = str(decision.get("chart_type") or "").strip().lower()
    if explicit in {"line", "bar", "pie", "donut"}:
        return explicit
    raw_call = _first_raw_call(decision)
    sources: list[dict[str, Any]] = []
    if raw_call:
        nested_args = raw_call.get("args") if isinstance(raw_call.get("args"), dict) else {}
        merged = {**nested_args, **{key: value for key, value in raw_call.items() if key != "args"}}
        sources.append(merged)
        payload = merged.get("payload") if isinstance(merged.get("payload"), dict) else {}
        sources.append(payload)
    for source in sources:
        for key in ("chart_type", "type", "view"):
            value = str(source.get(key) or "").strip().lower()
            if value in {"line", "bar", "pie", "donut"}:
                return value
    return "line"


def _intent_frame_discourse_action(decision: dict[str, Any]) -> str:
    patch = decision.get("frame_patch") if isinstance(decision.get("frame_patch"), dict) else {}
    frame_action = str(patch.get("frame_action") or "").strip().lower()
    if frame_action == "clarification_reply":
        return "clarification_reply"
    if frame_action == "patch_prior":
        return "follow_up"
    action = str(decision.get("discourse_action") or "").strip().lower()
    aliases = {
        "followup": "follow_up",
        "followup_same_subject": "follow_up",
        "followup_replace_subject": "follow_up",
        "patch_prior": "follow_up",
    }
    action = aliases.get(action, action)
    return action if action in DISCOURSE_ACTION_VALUES else "new"


def _first_raw_call(decision: dict[str, Any]) -> dict[str, Any]:
    calls = decision.get("calls")
    if not isinstance(calls, list):
        calls = decision.get("tool_calls") if isinstance(decision.get("tool_calls"), list) else []
    for call in calls:
        if isinstance(call, dict):
            return call
    return {}


def _first_raw_tool_name(decision: dict[str, Any]) -> str:
    call = _first_raw_call(decision)
    return canonical_semantic_tool_name(str(call.get("tool") or call.get("name") or ""))


def _first_raw_view(decision: dict[str, Any]) -> str:
    call = _first_raw_call(decision)
    nested_args = call.get("args") if isinstance(call.get("args"), dict) else {}
    return str(call.get("view") or nested_args.get("view") or "").strip()


def _is_month_token(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


def _repair_structured_call_from_selector_intent(call: dict[str, Any], decision: dict[str, Any]) -> None:
    tool_name = canonical_semantic_tool_name(str(call.get("tool") or ""))
    intent = str(decision.get("intent") or "").strip().lower()
    if tool_name == "query_transactions":
        view = str(call.get("view") or "").strip().lower()
        if view in {"", "list", "search"} and "latest" in intent and "transaction" in intent:
            call["view"] = "latest"
            call.setdefault("limit", 1)
            call.setdefault("sort", "date_desc")
        return

    if tool_name == "preview_finance_change":
        payload = call.get("payload") if isinstance(call.get("payload"), dict) else {}
        filters = call.get("filters") if isinstance(call.get("filters"), dict) else {}
        action = str(payload.get("action") or call.get("action") or "").strip().lower()
        if not payload.get("change_type") and action in {"move_all", "move", "recategorize", "reclassify"}:
            payload["change_type"] = "bulk_recategorize"
        if payload.get("change_type") == "bulk_recategorize":
            merchant = str(payload.get("merchant") or filters.get("merchant") or "").strip()
            category = str(payload.get("category") or payload.get("target_category") or "").strip()
            if merchant:
                payload["merchant"] = merchant
            if category:
                payload["category"] = category
            payload.pop("action", None)
            payload.pop("target_category", None)
            call["payload"] = payload
        return

    if tool_name != "summarize_spending":
        return
    _repair_summarize_spending_view(call, decision)
    if str(call.get("view") or "").strip().lower() != "top":
        return
    payload = call.get("payload") if isinstance(call.get("payload"), dict) else {}
    if payload.get("group_by"):
        return
    if "merchant" in intent:
        payload["group_by"] = "merchant"
    elif "categor" in intent:
        payload["group_by"] = "category"
    if payload:
        call["payload"] = payload


def _repair_summarize_spending_view(call: dict[str, Any], decision: dict[str, Any]) -> None:
    view = str(call.get("view") or "").strip().lower()
    filters = call.get("filters") if isinstance(call.get("filters"), dict) else {}
    payload = call.get("payload") if isinstance(call.get("payload"), dict) else {}
    intent = str(decision.get("intent") or "").strip().lower()

    group_by = str(payload.get("group_by") or filters.get("group_by") or "").strip().lower()
    if group_by in {"category", "merchant"}:
        payload["group_by"] = group_by
        filters.pop("group_by", None)
    if payload.get("top") is not None:
        payload.pop("top", None)

    metric = str(payload.get("metric") or filters.get("metric") or "").strip().lower()
    type_hint = str(filters.get("type") or "").strip().lower()
    if type_hint in {"income", "expenses", "expense"} and not metric:
        metric = "expenses" if type_hint == "expense" else type_hint
    if metric:
        payload["metric"] = metric
    filters.pop("type", None)
    filters.pop("metric", None)

    if view in {"", "summarize_spending", "spending", "summary"}:
        if group_by or "top" in intent or "biggest" in intent:
            call["view"] = "top"
        elif filters.get("merchant") or filters.get("category"):
            call["view"] = "entity_total"
        else:
            call["view"] = "period_total"
    call["filters"] = {key: value for key, value in filters.items() if value not in (None, "", [], {})}
    call["payload"] = {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def _normalize_structured_filter_values(call: dict[str, Any]) -> None:
    filters = call.get("filters")
    if not isinstance(filters, dict):
        return
    out: dict[str, Any] = {}
    changed = False
    for key, value in filters.items():
        scalar = _selector_scalar_text(value)
        if scalar and isinstance(value, dict):
            out[key] = scalar
            changed = True
        else:
            out[key] = value
    if changed:
        call["filters"] = out


def append_chart_call_from_structured_intent(
    *,
    calls: list[dict[str, Any]],
    decision: dict[str, Any],
    by_name: dict[str, dict[str, Any]],
    validation_errors: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    chart_calls = [call for call in calls if str(call.get("name") or "") == "make_chart"]
    if not _decision_requests_chart(decision) and not chart_calls:
        return calls, validation_errors
    if "make_chart" not in by_name:
        return calls, validation_errors

    evidence_calls = [
        call
        for call in calls
        if str(call.get("name") or "") in _FINANCE_SEMANTIC_TOOLS and str(call.get("name") or "") != "make_chart"
    ]
    source = next((call for call in evidence_calls if not str(call.get("validation_error") or "").strip()), None)
    if not source:
        source = _synthesize_chart_source_call(
            decision=decision,
            chart_calls=chart_calls,
            by_name=by_name,
            source_index=len(evidence_calls) + 1,
        )
        if not source or str(source.get("validation_error") or "").strip():
            return calls, validation_errors
        evidence_calls = [*evidence_calls, source]

    source_id = str(source.get("id") or "selector_call_1")
    title = _chart_title(source=source, chart_calls=chart_calls)
    raw_call = {
        "tool": "make_chart",
        "view": _chart_view(chart_calls),
        "payload": {"source_step_id": source_id, "title": title},
        "context_action": "new",
        "range_source": "none",
    }
    args, validation_error = adapt_universal_args("make_chart", raw_call, by_name["make_chart"])
    if validation_error:
        validation_errors.append(validation_error)
        return calls, validation_errors
    validation_errors = [error for error in validation_errors if "make_chart" not in str(error)]
    return [
        *evidence_calls,
        {
            "id": f"selector_call_{len(evidence_calls) + 1}",
            "name": "make_chart",
            "universal_args": {
                key: raw_call.get(key)
                for key in UNIVERSAL_ARG_FIELDS
                if raw_call.get(key) not in (None, "", [], {})
            },
            "args": args,
            "validation_error": "",
        },
    ], validation_errors


def _synthesize_chart_source_call(
    *,
    decision: dict[str, Any],
    chart_calls: list[dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    source_index: int,
) -> dict[str, Any]:
    signal = _chart_signal(decision=decision, chart_calls=chart_calls)
    range_value = _chart_range(decision=decision, chart_calls=chart_calls)
    if "net_worth" in signal or "networth" in signal or "net worth" in signal:
        raw_call = {
            "tool": "review_net_worth",
            "view": "trend",
            "range": _chart_trend_range(range_value),
            "context_action": "new",
            "range_source": "explicit_user" if range_value else "none",
        }
        name = "review_net_worth"
    elif any(token in signal for token in ("expense", "expenses", "spend", "spending")):
        raw_call = {
            "tool": "summarize_spending",
            "view": "trend",
            "range": _chart_trend_range(range_value),
            "payload": {"metric": "expenses", "group_by": "month"},
            "context_action": "new",
            "range_source": "explicit_user" if range_value else "none",
        }
        name = "summarize_spending"
    else:
        return {}

    if name not in by_name:
        return {}
    args, validation_error = adapt_universal_args(name, raw_call, by_name[name])
    return {
        "id": f"selector_call_{source_index}",
        "name": name,
        "universal_args": {
            key: raw_call.get(key)
            for key in UNIVERSAL_ARG_FIELDS
            if raw_call.get(key) not in (None, "", [], {})
        },
        "args": args,
        "validation_error": validation_error,
    }


def _chart_trend_range(value: str) -> str:
    token = str(value or "").strip().lower()
    if token in {"", "current", "current_month", "this_month", "latest"}:
        return "last_6_months"
    if token in {"all", "all_time"}:
        return "last_6_months"
    if _is_month_token(token):
        return "last_6_months"
    return str(value or "").strip() or "last_6_months"


def _chart_signal(*, decision: dict[str, Any], chart_calls: list[dict[str, Any]]) -> str:
    parts: list[str] = [
        str(decision.get("intent") or ""),
        str(decision.get("requested_output") or ""),
    ]
    for call in chart_calls:
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        universal = call.get("universal_args") if isinstance(call.get("universal_args"), dict) else {}
        for source in (args, universal):
            parts.append(str(source.get("view") or ""))
            filters = source.get("filters") if isinstance(source.get("filters"), dict) else {}
            payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
            parts.extend(str(value) for value in filters.values())
            parts.extend(str(value) for value in payload.values())
    return " ".join(parts).strip().lower()


def _chart_range(*, decision: dict[str, Any], chart_calls: list[dict[str, Any]]) -> str:
    for source in [decision, *(call.get("args") if isinstance(call.get("args"), dict) else {} for call in chart_calls)]:
        value = str(source.get("range") or "").strip()
        if value:
            return value
    for call in chart_calls:
        universal = call.get("universal_args") if isinstance(call.get("universal_args"), dict) else {}
        value = str(universal.get("range") or "").strip()
        if value:
            return value
    return ""


def _chart_view(chart_calls: list[dict[str, Any]]) -> str:
    for call in chart_calls:
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        universal = call.get("universal_args") if isinstance(call.get("universal_args"), dict) else {}
        for source in (args, universal):
            payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
            for value in (source.get("view"), payload.get("chart_type"), payload.get("type")):
                chart_type = str(value or "").strip().lower()
                if chart_type in {"line", "bar", "donut"}:
                    return chart_type
    return "line"


def _chart_title(*, source: dict[str, Any], chart_calls: list[dict[str, Any]]) -> str:
    for call in chart_calls:
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        universal = call.get("universal_args") if isinstance(call.get("universal_args"), dict) else {}
        for source_args in (args, universal):
            payload = source_args.get("payload") if isinstance(source_args.get("payload"), dict) else {}
            title = str(payload.get("title") or "").strip()
            if title:
                return title
    return "Net worth trend" if source.get("name") == "review_net_worth" else "Spending trend"


def _decision_requests_chart(decision: dict[str, Any]) -> bool:
    requested_output = str(decision.get("requested_output") or "").strip().lower()
    if requested_output == "chart":
        return True
    intent = str(decision.get("intent") or "").strip().lower()
    return "chart" in intent or "plot" in intent or "graph" in intent


def apply_controller_route_permissions(
    *,
    decision: dict[str, Any],
    calls: list[dict[str, Any]],
    validation_errors: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    route = canonical_controller_route(decision, calls)
    decision["controller_route"] = route
    if route in _CHAT_ROUTES:
        if calls:
            decision["dropped_calls_due_to_route"] = [
                str(call.get("name") or call.get("tool") or "")
                for call in calls
            ]
        decision["calls"] = []
        decision["route"] = "general_answer"
        if route == "explain_last_answer":
            decision["answer_path"] = "explain_last_answer"
        return [], validation_errors

    if not route:
        return calls, validation_errors

    if route not in _TOOL_ROUTES:
        validation_errors.append(f"unsupported route: {route}")
        return calls, validation_errors

    allowed = allowed_tools_for_controller_route(route)
    disallowed = [
        str(call.get("name") or call.get("tool") or "")
        for call in calls
        if str(call.get("name") or call.get("tool") or "") not in allowed
    ]
    if disallowed:
        validation_errors.append(f"{route} route cannot call: {', '.join(sorted(set(disallowed)))}")
    if not calls and not decision_has_discourse_frame(decision) and not decision_has_tool_intent_frame(decision):
        validation_errors.append(f"{route} route needs at least one tool call")
    decision["route"] = route
    return calls, validation_errors


def canonical_controller_route(decision: dict[str, Any], calls: list[dict[str, Any]]) -> str:
    frame = decision.get("intent_frame") if isinstance(decision.get("intent_frame"), dict) else {}
    frame_route = str(frame.get("route") or "").strip().lower()
    frame_intent = str(frame.get("intent") or "").strip().lower()
    if frame_route in {"finance", "memory", "write_preview"} and frame_intent and frame_intent != "none":
        return "finance_tool" if frame_route == "finance" else frame_route
    raw_route = str(decision.get("route") or decision.get("answer_path") or "").strip().lower()
    route = _ROUTE_ALIASES.get(raw_route, raw_route)
    if route:
        return route

    if calls:
        names = {str(call.get("name") or call.get("tool") or "").strip() for call in calls}
        names.discard("")
        if names and names <= _MEMORY_SEMANTIC_TOOLS:
            return "memory"
        if names and names <= _WRITE_PREVIEW_SEMANTIC_TOOLS:
            return "write_preview"
        return "finance_tool"

    answer = str(decision.get("answer") or decision.get("direct_answer") or "").strip()
    if answer:
        return "chat"
    return ""


def allowed_tools_for_controller_route(route: str) -> set[str]:
    if route == "finance_tool":
        return set(_FINANCE_SEMANTIC_TOOLS)
    if route == "memory":
        return set(_MEMORY_SEMANTIC_TOOLS)
    if route == "write_preview":
        return set(_WRITE_PREVIEW_SEMANTIC_TOOLS)
    return set()


def normalize_frame_patch_decision(decision: dict[str, Any]) -> dict[str, Any]:
    patch = _normalize_frame_patch(decision.get("frame_patch") if isinstance(decision.get("frame_patch"), dict) else {})
    call_patch = _frame_patch_from_calls(
        decision.get("calls") if isinstance(decision.get("calls"), list) else [],
        allow_inherit_action=True,
    )
    if patch and call_patch:
        patch = _merge_frame_patch_with_call_patch(patch, call_patch)
    if not patch:
        patch = _legacy_frame_patch_from_decision(decision)
    if patch:
        return {**decision, "frame_patch": patch}
    return decision


def _merge_frame_patch_with_call_patch(patch: dict[str, Any], call_patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(patch)
    patch_subject = patch.get("subject") if isinstance(patch.get("subject"), dict) else {}
    call_subject = call_patch.get("subject") if isinstance(call_patch.get("subject"), dict) else {}
    if not patch_subject.get("raw") and call_subject.get("raw"):
        out["subject_action"] = "replace"
        out["subject"] = call_subject
    if not out.get("range_action") or str(out.get("range_action")) == "inherit":
        if call_patch.get("range_action") in {"replace", "previous_of_prior", "next_of_prior", "clear"}:
            out["range_action"] = call_patch.get("range_action")
            if call_patch.get("range"):
                out["range"] = call_patch.get("range")
    return _normalize_frame_patch(out)


def _normalize_frame_patch(patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        return {}
    frame_action = str(patch.get("frame_action") or "").strip().lower()
    if frame_action not in {"new", "patch_prior", "clarification_reply"}:
        frame_action = ""
    subject_action = str(patch.get("subject_action") or "").strip().lower()
    if subject_action not in {"inherit", "replace", "clear"}:
        subject_action = ""
    range_action = str(patch.get("range_action") or "").strip().lower()
    if range_action not in {"inherit", "replace", "previous_of_prior", "next_of_prior", "clear"}:
        range_action = ""
    output_action = str(patch.get("output_action") or "").strip().lower()
    if output_action not in {"inherit", "replace"}:
        output_action = ""
    subject = _subject_from_value(patch.get("subject"))
    requested_output = str(patch.get("requested_output") or "").strip().lower()
    if requested_output not in {"scalar_total", "rows", "chart", "comparison", "status", "summary", "preview"}:
        requested_output = ""
    range_value = str(patch.get("range") or "").strip()
    if range_action == "replace" and _is_previous_of_prior_range_token(range_value):
        range_action = "previous_of_prior"
        range_value = ""
    if not any((frame_action, subject_action, subject.get("raw"), range_action, str(patch.get("range") or "").strip(), output_action, requested_output, str(patch.get("clarification_choice") or "").strip())):
        return {}
    if subject_action == "replace" and not subject.get("raw"):
        subject_action = "inherit"
    out = {
        "frame_action": frame_action,
        "subject_action": subject_action,
        "subject": subject if subject.get("raw") else {},
        "range_action": range_action,
        "range": range_value,
        "output_action": output_action,
        "requested_output": requested_output,
        "clarification_choice": str(patch.get("clarification_choice") or "").strip(),
    }
    return {key: value for key, value in out.items() if value not in (None, "", {}, [])}


def _is_previous_of_prior_range_token(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text in {"last_month_before", "last_month_minus_1", "month_before_last_month", "previous_of_prior"}


def _legacy_frame_patch_from_decision(decision: dict[str, Any]) -> dict[str, Any]:
    action = str(decision.get("discourse_action") or "").strip().lower()
    if not action:
        return {}
    if action == "clarification_reply":
        return _normalize_frame_patch({
            "frame_action": "clarification_reply",
            "clarification_choice": decision.get("clarification_choice") or _decision_subject(decision).get("raw"),
        })
    if action not in {"followup", "followup_same_subject", "followup_replace_subject", "correction"}:
        return {}
    subject = _decision_subject(decision)
    range_value = str(decision.get("range") or "").strip()
    range_action = "inherit"
    if range_value.lower() in {"inherited", "same", "prior"}:
        range_action = "inherit"
        range_value = ""
    elif range_value:
        range_action = "replace"
    return _normalize_frame_patch({
        "frame_action": "patch_prior",
        "subject_action": "replace" if subject.get("raw") else "inherit",
        "subject": subject,
        "range_action": range_action,
        "range": range_value,
        "output_action": "replace" if decision.get("requested_output") else "inherit",
        "requested_output": decision.get("requested_output") or "",
    })


def _frame_patch_from_calls(calls: list[dict[str, Any]], *, allow_inherit_action: bool = False) -> dict[str, Any]:
    if not calls:
        return {}
    call = calls[0]
    action = _call_context_action(call)
    allowed_actions = {"followup", "followup_same_subject", "followup_replace_subject", "correction", "patch_prior"}
    if allow_inherit_action:
        allowed_actions.add("inherit")
    if action not in allowed_actions:
        return {}
    args = _selector_call_args(call)
    universal = call.get("universal_args") if isinstance(call.get("universal_args"), dict) else {}
    subject = _subject_from_discourse_call(call)
    range_source = str(args.get("range_source") or universal.get("range_source") or "").strip().lower()
    range_value = str(args.get("range") or universal.get("range") or "").strip()
    range_action = "inherit"
    if range_source in {"previous_of_prior", "next_of_prior"}:
        range_action = range_source
        range_value = ""
    elif _is_previous_of_prior_range_token(range_value):
        range_action = "previous_of_prior"
        range_value = ""
    elif action == "followup" and range_value:
        range_action = "replace"
    elif action == "followup_same_subject" and range_value:
        range_action = "replace"
    elif action in {"patch_prior", "inherit"} and range_value and range_value.lower() not in {"inherited", "same", "prior"}:
        range_action = "replace"
    elif action in {"followup_replace_subject", "correction"}:
        range_action = "inherit"
        range_value = ""
    return _normalize_frame_patch({
        "frame_action": "patch_prior",
        "subject_action": "replace" if subject.get("raw") else "inherit",
        "subject": subject,
        "range_action": range_action,
        "range": range_value,
        "output_action": "inherit",
    })


def _frame_patch_action(decision: dict[str, Any]) -> str:
    patch = decision.get("frame_patch") if isinstance(decision.get("frame_patch"), dict) else {}
    return str(patch.get("frame_action") or "").strip().lower()


def decision_has_discourse_frame(decision: dict[str, Any]) -> bool:
    if not isinstance(decision, dict):
        return False
    if _frame_patch_action(decision):
        return True
    if str(decision.get("discourse_action") or "").strip():
        return True
    if str(decision.get("requested_output") or "").strip():
        return True
    return bool(_decision_subject(decision).get("raw"))


def apply_discourse_frames(
    *,
    calls: list[dict[str, Any]],
    decision: dict[str, Any],
    history: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]],
    question: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    route = canonical_controller_route(decision, calls)
    if route != "finance_tool":
        return calls, decision

    patch = decision.get("frame_patch") if isinstance(decision.get("frame_patch"), dict) else {}
    if not patch:
        patch = _frame_patch_from_calls(calls)
        if patch:
            decision = {**decision, "frame_patch": patch}
    action = str(patch.get("frame_action") or "").strip().lower()
    if not action or action == "new":
        return calls, decision

    pending = latest_pending_clarification(history)
    if action == "clarification_reply":
        if not pending:
            return [], {
                **decision,
                "calls": [],
                "route": "",
                "validation_errors": ["There is no pending clarification to resolve."],
            }
        resolved = _call_from_pending_clarification(decision=decision, pending=pending, tools=tools)
        if resolved:
            return [resolved], {
                **decision,
                "calls": [resolved],
                "frame_patch_compiled": True,
                "pending_clarification_resolved": True,
            }
        return [], {
            **decision,
            "calls": [],
            "route": "",
            "validation_errors": ["I need one of the pending clarification options before continuing."],
        }

    if action != "patch_prior":
        return calls, decision

    prior_frame = latest_conversation_frame(history)
    if not prior_frame:
        return [], {
            **decision,
            "calls": [],
            "route": "",
            "validation_errors": ["I need prior finance context before applying that follow-up."],
        }
    patch = _patch_range_from_followup_question(patch=patch, question=question, prior_frame=prior_frame)
    if _frame_patch_is_noop(patch):
        return [], {
            **decision,
            "calls": [],
            "route": "general_answer",
            "frame_patch_rejected": "noop",
        }

    compiled = _call_from_frame_patch(patch=patch, decision=decision, prior_frame=prior_frame, tools=tools)
    if not compiled:
        return [], {
            **decision,
            "calls": [],
            "route": "",
            "validation_errors": ["I need one more detail before applying that follow-up."],
        }
    call, conversation_frame = compiled
    return [call], {
        **decision,
        "frame_patch": patch,
        "calls": [call],
        "frame_patch_compiled": True,
        "compiled_conversation_frame": conversation_frame,
    }


def _patch_range_from_followup_question(
    *,
    patch: dict[str, Any],
    question: str,
    prior_frame: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(patch, dict) or str(patch.get("frame_action") or "").strip().lower() != "patch_prior":
        return patch
    prior_range = str(prior_frame.get("range") or "").strip()
    if not prior_range:
        return patch
    parsed = resolve_followup_range(str(question or ""), prior_range)
    if not parsed.explicit:
        return patch
    return _normalize_frame_patch(
        {
            **patch,
            "range_action": "replace",
            "range": parsed.token,
        }
    )


def latest_conversation_frame(history: list[dict[str, Any]] | None) -> dict[str, Any]:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        mira_frame = answer_context.get("mira_conversation_frame") if isinstance(answer_context.get("mira_conversation_frame"), dict) else {}
        converted_mira = _conversation_frame_from_mira_frame(mira_frame)
        if converted_mira:
            return converted_mira
        frame = answer_context.get("conversation_frame") if isinstance(answer_context.get("conversation_frame"), dict) else {}
        if frame:
            return json.loads(json.dumps(frame, default=str))
        current_frame = answer_context.get("current_frame") if isinstance(answer_context.get("current_frame"), dict) else {}
        converted = _conversation_frame_from_semantic_frame(current_frame)
        if converted:
            return converted
    prior = latest_prior_frame(history)
    return _conversation_frame_from_semantic_frame(prior)


def _conversation_frame_from_semantic_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(frame, dict) or not frame.get("tool"):
        return {}
    filters = frame.get("filters") if isinstance(frame.get("filters"), dict) else {}
    subject: dict[str, str] = {}
    if filters.get("merchant"):
        subject = {"type": "merchant", "canonical": str(filters.get("merchant")), "raw": str(filters.get("merchant"))}
    elif filters.get("category"):
        subject = {"type": "category", "canonical": str(filters.get("category")), "raw": str(filters.get("category"))}
    elif frame.get("entity") and frame.get("entity_type"):
        subject = {
            "type": str(frame.get("entity_type") or ""),
            "canonical": str(frame.get("entity") or ""),
            "raw": str(frame.get("entity") or ""),
        }
    output = "scalar_total" if str(frame.get("view") or "").strip().lower() in {"entity_total", "period_total"} else "summary"
    return {
        "intent": "spend_total" if frame.get("tool") == "summarize_spending" else str(frame.get("tool") or ""),
        "tool": str(frame.get("tool") or ""),
        "view": str(frame.get("view") or ""),
        "subject": subject,
        "range": str(frame.get("range") or frame.get("range_b") or frame.get("range_a") or ""),
        "requested_output": output,
        "payload": frame.get("payload") if isinstance(frame.get("payload"), dict) else {},
    }


def _conversation_frame_from_mira_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(frame, dict) or not frame:
        return {}
    intent = str(frame.get("intent") or "").strip()
    tool, view = _tool_view_from_mira_intent(intent, frame)
    if not tool:
        return {}
    subject_payload = frame.get("subject") if isinstance(frame.get("subject"), dict) else {}
    kind = str(subject_payload.get("kind") or "").strip()
    text = str(subject_payload.get("display_name") or subject_payload.get("canonical_id") or subject_payload.get("text") or "").strip()
    subject = {"type": kind, "canonical": text, "raw": text} if kind and kind != "none" and text else {}
    payload = _payload_from_mira_intent(intent)
    return {
        "intent": "spend_total" if intent == "spending_total" else intent,
        "tool": tool,
        "view": view,
        "subject": subject,
        "range": _range_from_mira_frame(frame),
        "requested_output": _requested_output_from_mira_frame(frame),
        "source_step_id": str(frame.get("last_evidence_step_id") or ""),
        "payload": payload,
    }


def _tool_view_from_mira_intent(intent: str, frame: dict[str, Any]) -> tuple[str, str]:
    subject = frame.get("subject") if isinstance(frame.get("subject"), dict) else {}
    has_subject = bool(str(subject.get("text") or subject.get("canonical_id") or subject.get("display_name") or "").strip())
    if intent == "spending_total":
        return "summarize_spending", "entity_total" if has_subject else "period_total"
    if intent == "spending_top":
        return "summarize_spending", "top"
    if intent == "spending_breakdown":
        return "summarize_spending", "breakdown"
    if intent == "spending_trend":
        return "summarize_spending", "trend"
    if intent == "spending_compare":
        return "summarize_spending", "compare"
    if intent in {"spending_explain", "transaction_lookup"}:
        return "query_transactions", "list"
    if intent in {"budget_status", "budget_plan", "savings_capacity"}:
        return "review_budget", "savings_capacity" if intent == "savings_capacity" else "category_status" if has_subject else "plan"
    if intent in {"cashflow_forecast", "cashflow_shortfall"}:
        return "review_cashflow", "shortfall" if intent == "cashflow_shortfall" else "forecast"
    if intent == "affordability":
        return "check_affordability", "purchase"
    if intent in {"recurring_summary", "recurring_changes"}:
        return "review_recurring", "changes" if intent == "recurring_changes" else "summary"
    if intent in {"net_worth_balance", "net_worth_trend", "net_worth_delta"}:
        return "review_net_worth", {"net_worth_balance": "balances", "net_worth_delta": "delta"}.get(intent, "trend")
    if intent in {"data_health", "enrichment_quality", "low_confidence_transactions", "explain_transaction"}:
        return "review_data_quality", {
            "enrichment_quality": "enrichment_summary",
            "low_confidence_transactions": "low_confidence",
            "explain_transaction": "explain_transaction",
        }.get(intent, "health")
    return "", ""


def _range_from_mira_frame(frame: dict[str, Any]) -> str:
    token = str(frame.get("time") or "").strip()
    if token == "this_month":
        return "current_month"
    if token == "all_time":
        return "all"
    if token == "custom":
        time_a = str(frame.get("time_a") or "").strip()
        if len(time_a) >= 7:
            return time_a[:7]
        return ""
    return "" if token == "none" else token


def _requested_output_from_mira_frame(frame: dict[str, Any]) -> str:
    output = str(frame.get("output") or "").strip()
    return {
        "scalar": "scalar_total",
        "table": "rows",
        "list": "rows",
        "chart": "chart",
        "comparison": "comparison",
        "preview": "preview",
    }.get(output, "summary")


def _payload_from_mira_intent(intent: str) -> dict[str, str]:
    if intent.startswith("spending_"):
        return {"metric": "expenses"}
    return {}


def _frame_patch_is_noop(patch: dict[str, Any]) -> bool:
    subject_action = str(patch.get("subject_action") or "").strip().lower()
    range_action = str(patch.get("range_action") or "").strip().lower()
    output_action = str(patch.get("output_action") or "").strip().lower()
    if subject_action in {"replace", "clear"}:
        return False
    if range_action in {"replace", "previous_of_prior", "next_of_prior", "clear"}:
        return False
    if output_action == "replace":
        return False
    return True


def _call_from_frame_patch(
    *,
    patch: dict[str, Any],
    decision: dict[str, Any],
    prior_frame: dict[str, Any],
    tools: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    subject = _patched_subject(patch, prior_frame)
    if subject is None:
        return None
    range_value = _patched_range(patch, prior_frame)
    if range_value is None:
        return None
    requested_output = _patched_output(patch, prior_frame)
    tool_name, view = _tool_view_from_frame_patch(decision=decision, prior_frame=prior_frame, subject=subject, requested_output=requested_output)
    if not tool_name:
        return None

    args: dict[str, Any] = {
        "view": view,
        "context_action": "patch_prior",
        "range_source": _compiled_range_source(patch),
    }
    filters = _filters_from_patched_subject(subject)
    if filters:
        args["filters"] = filters
    if range_value and tool_name in _SINGLE_RANGE_TOOLS:
        args["range"] = range_value
    payload = _payload_from_conversation(tool_name=tool_name, prior_frame=prior_frame, requested_output=requested_output)
    if payload:
        args["payload"] = payload
    call = _override_call(tool_name, args, tools)
    if not call:
        return None
    conversation_frame = {
        "intent": str(decision.get("intent") or prior_frame.get("intent") or "").strip(),
        "tool": tool_name,
        "view": view,
        "subject": subject,
        "range": range_value,
        "requested_output": requested_output,
        "payload": payload,
    }
    return call, conversation_frame


def _patched_subject(patch: dict[str, Any], prior_frame: dict[str, Any]) -> dict[str, str] | None:
    action = str(patch.get("subject_action") or "inherit").strip().lower()
    if action == "clear":
        return {}
    if action == "replace":
        subject = _subject_from_value(patch.get("subject"))
        if not subject.get("raw"):
            return None
        return {"type": str(subject.get("type_hint") or "unknown"), "canonical": "", "raw": str(subject.get("raw") or "")}
    prior_subject = prior_frame.get("subject") if isinstance(prior_frame.get("subject"), dict) else {}
    if prior_subject:
        return {
            "type": str(prior_subject.get("type") or prior_subject.get("type_hint") or "unknown"),
            "canonical": str(prior_subject.get("canonical") or prior_subject.get("value") or prior_subject.get("raw") or ""),
            "raw": str(prior_subject.get("raw") or prior_subject.get("canonical") or prior_subject.get("value") or ""),
        }
    return {}


def _patched_range(patch: dict[str, Any], prior_frame: dict[str, Any]) -> str | None:
    action = str(patch.get("range_action") or "inherit").strip().lower()
    prior_range = str(prior_frame.get("range") or "").strip()
    if action == "clear":
        return ""
    if action == "inherit":
        return prior_range
    if action == "replace":
        value = str(patch.get("range") or "").strip()
        return value or None
    if action == "previous_of_prior":
        return resolve_followup_range("month before", prior_range).token if prior_range else None
    if action == "next_of_prior":
        return resolve_followup_range("month after", prior_range).token if prior_range else None
    return prior_range


def _patched_output(patch: dict[str, Any], prior_frame: dict[str, Any]) -> str:
    if str(patch.get("output_action") or "").strip().lower() == "replace":
        value = str(patch.get("requested_output") or "").strip().lower()
        if value:
            return value
    return str(prior_frame.get("requested_output") or patch.get("requested_output") or "scalar_total").strip().lower()


def _tool_view_from_frame_patch(
    *,
    decision: dict[str, Any],
    prior_frame: dict[str, Any],
    subject: dict[str, str],
    requested_output: str,
) -> tuple[str, str]:
    intent = str(decision.get("intent") or prior_frame.get("intent") or "").strip().lower()
    prior_tool = canonical_semantic_tool_name(str(prior_frame.get("tool") or ""))
    prior_view = str(prior_frame.get("view") or "").strip().lower()
    if requested_output == "rows":
        return "query_transactions", "list"
    if requested_output == "chart":
        return "make_chart", "line"
    if prior_tool == "query_transactions":
        return "query_transactions", prior_view if prior_view in {"latest", "list", "search", "detail"} else "list"
    if prior_tool == "review_budget" or "budget" in intent:
        return "review_budget", "category_status" if subject else "plan"
    if prior_tool == "check_affordability" or "afford" in intent:
        return "check_affordability", "purchase"
    if prior_tool == "summarize_spending" or "spend" in intent or "expense" in intent:
        return "summarize_spending", "entity_total" if subject else "period_total"
    return prior_tool or "summarize_spending", prior_view or ("entity_total" if subject else "period_total")


def _filters_from_patched_subject(subject: dict[str, str]) -> dict[str, str]:
    if not subject:
        return {}
    canonical = str(subject.get("canonical") or "").strip()
    entity_type = str(subject.get("type") or subject.get("type_hint") or "").strip().lower()
    raw = str(subject.get("raw") or "").strip()
    if canonical and entity_type in {"merchant", "category", "account"}:
        return {entity_type: canonical}
    if raw:
        filters = {"mention": raw}
        if entity_type in {"merchant", "category", "account"}:
            filters["mention_type"] = entity_type
        return filters
    return {}


def _payload_from_conversation(*, tool_name: str, prior_frame: dict[str, Any], requested_output: str) -> dict[str, Any]:
    if tool_name == "summarize_spending":
        payload = prior_frame.get("payload") if isinstance(prior_frame.get("payload"), dict) else {}
        metric = str(payload.get("metric") or "").strip()
        if not metric and requested_output in {"scalar_total", "summary"}:
            metric = "expenses"
        return {"metric": metric} if metric else {}
    if tool_name == "make_chart":
        source_step_id = str(prior_frame.get("source_step_id") or "").strip()
        return {"source_step_id": source_step_id, "title": "Chart"} if source_step_id else {}
    return {}


def _compiled_range_source(patch: dict[str, Any]) -> str:
    action = str(patch.get("range_action") or "").strip().lower()
    if action == "inherit":
        return "inherited"
    if action in {"previous_of_prior", "next_of_prior"}:
        return action
    if action == "replace":
        return "explicit_user"
    return "none"


def latest_pending_clarification(history: list[dict[str, Any]] | None) -> dict[str, Any]:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        pending = answer_context.get("pending_clarification") if isinstance(answer_context.get("pending_clarification"), dict) else {}
        if pending:
            return json.loads(json.dumps(pending, default=str))
    return {}


def _decision_subject(decision: dict[str, Any]) -> dict[str, str]:
    subject = _subject_from_value(decision.get("subject"))
    raw = subject.get("raw") or str(decision.get("subject_raw") or "").strip()
    type_hint = subject.get("type_hint") or str(decision.get("subject_type") or decision.get("type_hint") or "").strip().lower()
    if type_hint not in {"merchant", "category", "account", "unknown"}:
        type_hint = "unknown"
    return {"raw": raw, "type_hint": type_hint}


def _subject_from_value(raw_subject: Any) -> dict[str, str]:
    if isinstance(raw_subject, dict):
        raw = str(raw_subject.get("raw") or raw_subject.get("text") or "").strip()
        type_hint = str(raw_subject.get("type_hint") or raw_subject.get("type") or raw_subject.get("kind") or "").strip().lower()
    else:
        raw = str(raw_subject or "").strip()
        type_hint = ""
    if type_hint not in {"merchant", "category", "account", "unknown"}:
        type_hint = "unknown"
    return {"raw": raw, "type_hint": type_hint}


def _subject_from_discourse_call(call: dict[str, Any]) -> dict[str, str]:
    name = canonical_semantic_tool_name(str(call.get("name") or call.get("tool") or ""))
    if name not in {"summarize_spending", "query_transactions", "review_budget", "check_affordability"}:
        return {}
    args = _selector_call_args(call)
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    if filters.get("mention"):
        return {"raw": _selector_scalar_text(filters.get("mention")), "type_hint": "unknown"}
    for key in ("merchant", "category", "account"):
        value = _selector_scalar_text(filters.get(key))
        if value:
            return {"raw": value, "type_hint": key}
    return {}


def _selector_scalar_text(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("canonical", "value", "label", "raw", "text", "name"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


def _call_context_action(call: dict[str, Any]) -> str:
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    universal = call.get("universal_args") if isinstance(call.get("universal_args"), dict) else {}
    return str(args.get("context_action") or universal.get("context_action") or call.get("context_action") or "").strip().lower()


def _selector_call_args(call: dict[str, Any]) -> dict[str, Any]:
    args = copy_args(call)
    for key in UNIVERSAL_ARG_FIELDS:
        if key in call and call.get(key) not in (None, "", [], {}):
            args[key] = call.get(key)
    return args


def _call_from_discourse_frame(
    *,
    decision: dict[str, Any],
    subject: dict[str, str],
    history: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    prior_frame = latest_prior_frame(history)
    tool_name, view = _tool_view_from_discourse(decision=decision, prior_frame=prior_frame)
    if not tool_name:
        return {}
    args: dict[str, Any] = {
        "view": view,
        "filters": _mention_filters(subject),
        "context_action": str(decision.get("discourse_action") or "followup").strip() or "followup",
        "range_source": _range_source_from_discourse(decision),
    }
    range_value = _range_from_discourse(decision=decision, prior_frame=prior_frame, history=history)
    if range_value and tool_name in _SINGLE_RANGE_TOOLS:
        args["range"] = range_value
    payload = _payload_from_discourse(tool_name=tool_name, decision=decision, prior_frame=prior_frame)
    if payload:
        args["payload"] = payload
    return _override_call(tool_name, args, tools)


def _call_with_discourse_subject(
    *,
    call: dict[str, Any],
    decision: dict[str, Any],
    subject: dict[str, str],
    history: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    name = canonical_semantic_tool_name(str(call.get("name") or call.get("tool") or ""))
    if name not in {"summarize_spending", "query_transactions", "review_budget", "check_affordability"}:
        return call
    args = copy_args(call)
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    filters = dict(filters)
    for key in ("merchant", "category", "account", "search"):
        filters.pop(key, None)
    filters.update(_mention_filters(subject))
    args["filters"] = filters

    prior_frame = latest_prior_frame(history)
    requested_output = str(decision.get("requested_output") or "").strip().lower()
    intent = str(decision.get("intent") or "").strip().lower()
    if name == "summarize_spending" and (requested_output == "scalar_total" or "spend" in intent or "expense" in intent):
        args["view"] = "entity_total"
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        if not payload.get("metric"):
            payload["metric"] = "expenses"
        args["payload"] = payload

    range_value = _range_from_discourse(decision=decision, prior_frame=prior_frame, history=history)
    if range_value and name in _SINGLE_RANGE_TOOLS:
        args["range"] = range_value
    args["context_action"] = str(decision.get("discourse_action") or args.get("context_action") or "followup").strip() or "followup"
    args["range_source"] = _range_source_from_discourse(decision)
    rebuilt = _override_call(name, args, tools)
    if rebuilt:
        rebuilt["id"] = str(call.get("id") or rebuilt.get("id") or "selector_call_1")
    return rebuilt or call


def copy_args(call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(call.get("args") if isinstance(call.get("args"), dict) else {}, default=str))


def _mention_filters(subject: dict[str, str]) -> dict[str, str]:
    filters = {"mention": str(subject.get("raw") or "").strip()}
    type_hint = str(subject.get("type_hint") or "").strip().lower()
    if type_hint in {"merchant", "category", "account"}:
        filters["mention_type"] = type_hint
    return {key: value for key, value in filters.items() if value}


def _tool_view_from_discourse(*, decision: dict[str, Any], prior_frame: dict[str, Any]) -> tuple[str, str]:
    requested_output = str(decision.get("requested_output") or "").strip().lower()
    intent = str(decision.get("intent") or "").strip().lower()
    prior_tool = canonical_semantic_tool_name(str(prior_frame.get("tool") or "")) if prior_frame else ""
    prior_view = str(prior_frame.get("view") or "").strip().lower()

    if requested_output == "rows":
        return "query_transactions", "list"
    if prior_tool == "query_transactions":
        return "query_transactions", prior_view if prior_view in {"latest", "list", "search", "detail"} else "list"
    if prior_tool == "review_budget" or "budget" in intent:
        return "review_budget", "category_status"
    if prior_tool == "check_affordability" or "afford" in intent:
        return "check_affordability", "purchase"
    if prior_tool == "summarize_spending" or requested_output == "scalar_total" or "spend" in intent or "expense" in intent:
        return "summarize_spending", "entity_total"
    return "summarize_spending", "entity_total"


def _range_from_discourse(
    *,
    decision: dict[str, Any],
    prior_frame: dict[str, Any],
    history: list[dict[str, Any]] | None,
) -> str:
    value = str(decision.get("range") or "").strip()
    if value.lower() in {"inherited", "same", "prior"}:
        return str(prior_frame.get("range") or prior_frame.get("range_b") or prior_frame.get("range_a") or latest_context_range(history) or "").strip()
    return value


def _range_source_from_discourse(decision: dict[str, Any]) -> str:
    value = str(decision.get("range") or "").strip().lower()
    if value in {"inherited", "same", "prior"}:
        return "inherited"
    if value:
        return "explicit_user"
    return "none"


def _payload_from_discourse(*, tool_name: str, decision: dict[str, Any], prior_frame: dict[str, Any]) -> dict[str, Any]:
    if tool_name != "summarize_spending":
        return {}
    prior_payload = prior_frame.get("payload") if isinstance(prior_frame.get("payload"), dict) else {}
    metric = str(prior_payload.get("metric") or "").strip()
    intent = str(decision.get("intent") or "").strip().lower()
    if not metric and ("spend" in intent or "expense" in intent or str(decision.get("requested_output") or "") == "scalar_total"):
        metric = "expenses"
    return {"metric": metric} if metric else {}


def _call_from_pending_clarification(
    *,
    decision: dict[str, Any],
    pending: dict[str, Any],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    option = _pending_option_from_choice(decision=decision, pending=pending)
    if not option:
        return {}
    resume = pending.get("resume_frame") if isinstance(pending.get("resume_frame"), dict) else {}
    tool_name = str(resume.get("tool") or "summarize_spending").strip()
    args = resume.get("args") if isinstance(resume.get("args"), dict) else {}
    args = json.loads(json.dumps(args, default=str))
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    filters = dict(filters)
    entity_type = str(option.get("type") or option.get("entity_type") or "").strip()
    value = str(option.get("canonical") or option.get("value") or option.get("label") or "").strip()
    if entity_type and value:
        for key in ("mention", "mention_type", "merchant", "category", "account"):
            filters.pop(key, None)
        filters[entity_type] = value
    args["filters"] = filters
    args["context_action"] = "clarification_reply"
    return _override_call(tool_name, args, tools)


def _pending_option_from_choice(*, decision: dict[str, Any], pending: dict[str, Any]) -> dict[str, Any]:
    patch = decision.get("frame_patch") if isinstance(decision.get("frame_patch"), dict) else {}
    choice = str(patch.get("clarification_choice") or decision.get("clarification_choice") or _decision_subject(decision).get("raw") or "").strip().lower()
    if not choice:
        return {}
    options = pending.get("options") if isinstance(pending.get("options"), list) else []
    typed = [item for item in options if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == choice]
    if len(typed) == 1:
        return typed[0]
    for item in options:
        if not isinstance(item, dict):
            continue
        candidates = {
            str(item.get("id") or "").strip().lower(),
            str(item.get("label") or "").strip().lower(),
            str(item.get("canonical") or item.get("value") or "").strip().lower(),
        }
        if choice in candidates:
            return item
    return {}


def apply_context_semantics(
    *,
    calls: list[dict[str, Any]],
    decision: dict[str, Any],
    history: list[dict[str, Any]] | None,
    question: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if decision.get("frame_patch_compiled"):
        return calls, decision
    prior_range = latest_context_range(history)
    if not prior_range:
        return calls, decision
    question_range = resolve_followup_range(str(question or ""), prior_range)

    changed = False
    out_calls: list[dict[str, Any]] = []
    for call in calls:
        call_out = {**call, "args": dict(call.get("args") or {})}
        universal = dict(call.get("universal_args") or {})
        source = str(universal.get("range_source") or "").strip().lower()
        action = str(universal.get("context_action") or "").strip().lower()
        range_value = str(universal.get("range") or call_out["args"].get("range") or "").strip().lower()
        resolved = ""
        if question_range.explicit and _call_accepts_single_range(call_out):
            resolved = question_range.token
            universal["range_source"] = "explicit_user"
        elif source == "previous_of_prior" or range_value == "previous_of_prior":
            resolved = resolve_followup_range("month before", prior_range).token
            universal["range_source"] = "previous_of_prior"
        elif _is_previous_of_prior_range_token(range_value) and source not in {"inherited", "inherit"}:
            resolved = resolve_followup_range("month before", prior_range).token
            universal["range_source"] = "previous_of_prior"
        elif source == "next_of_prior" or range_value == "next_of_prior":
            resolved = resolve_followup_range("next month", prior_range).token
            universal["range_source"] = "next_of_prior"
        elif (
            prior_range
            and _call_accepts_single_range(call_out)
            and action in {"followup", "followup_same_subject", "followup_replace_subject", "correction"}
            and source in {"inherited", "inherit"}
        ):
            resolved = prior_range
            universal["range_source"] = "inherited"
        if resolved and call_out["args"].get("range") != resolved:
            call_out["args"]["range"] = resolved
            universal["range"] = resolved
            call_out["universal_args"] = universal
            changed = True
        out_calls.append(call_out)

    if changed:
        decision = {
            **decision,
            "calls": out_calls,
            "context_semantics_applied": True,
            "prior_range": prior_range,
        }
    return out_calls, decision


def _call_accepts_single_range(call: dict[str, Any]) -> bool:
    name = str(call.get("name") or call.get("tool") or "").strip()
    return name in _SINGLE_RANGE_TOOLS


def _override_call(tool_name: str, args: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    by_name = tools_by_name(tools)
    tool_name = canonical_semantic_tool_name(tool_name)
    if tool_name not in by_name:
        return {}
    normalized_call = {
        "tool": tool_name,
        "view": args.get("view") or "list",
        "context_action": "new",
        "range_source": "none",
        **args,
    }
    adapted_args, validation_error = adapt_universal_args(tool_name, normalized_call, by_name[tool_name])
    return {
        "id": "selector_call_1",
        "name": tool_name,
        "universal_args": {
            key: normalized_call.get(key)
            for key in UNIVERSAL_ARG_FIELDS
            if normalized_call.get(key) not in (None, "", [], {})
        },
        "args": adapted_args,
        "validation_error": validation_error,
    }


def latest_context_range(history: list[dict[str, Any]] | None) -> str:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        mira_frame = context.get("mira_conversation_frame") if isinstance(context.get("mira_conversation_frame"), dict) else {}
        value = _range_from_mira_frame(mira_frame)
        if value:
            return value
        conversation_frame = context.get("conversation_frame") if isinstance(context.get("conversation_frame"), dict) else {}
        value = str(conversation_frame.get("range") or "").strip()
        if value:
            return value
        ranges = context.get("ranges") if isinstance(context.get("ranges"), list) else []
        for item in ranges:
            value = str(item or "").strip()
            if value:
                return value
        tools = context.get("tools") if isinstance(context.get("tools"), list) else []
        for tool in tools:
            args = tool.get("args") if isinstance(tool, dict) and isinstance(tool.get("args"), dict) else {}
            value = str(args.get("range") or "").strip()
            if value:
                return value
        tool_context = turn.get("tool_context") if isinstance(turn.get("tool_context"), list) else []
        for tool in tool_context:
            args = tool.get("args") if isinstance(tool, dict) and isinstance(tool.get("args"), dict) else {}
            value = str(args.get("range") or "").strip()
            if value:
                return value
    prior_frame = latest_prior_frame(history)
    value = str(prior_frame.get("range") or prior_frame.get("range_b") or prior_frame.get("range_a") or "").strip()
    if value:
        return value
    return ""


def parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = json.loads(_extract_json_object(text))
    if not isinstance(payload, dict):
        raise ValueError("selector response must be a JSON object")
    return payload


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise ValueError("selector response did not contain a JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError("selector response contained incomplete JSON")


def decision_needs_family_detail(decision: dict[str, Any], calls: list[dict[str, Any]]) -> bool:
    if calls:
        return False
    need_detail = decision.get("need_detail")
    if isinstance(need_detail, str):
        need_detail = need_detail.strip().lower() in {"1", "true", "yes"}
    return bool(need_detail or selected_family(decision))


def decision_requests_general_answer(decision: dict[str, Any]) -> bool:
    controller_route = str(decision.get("controller_route") or "").strip().lower()
    if controller_route in _CHAT_ROUTES:
        return True
    route = str(decision.get("route") or decision.get("answer_path") or "").strip().lower()
    return _ROUTE_ALIASES.get(route, route) in _CHAT_ROUTES


def selected_family(decision: dict[str, Any]) -> str:
    return selected_family_name(decision.get("family") or decision.get("tool_family"))


def selector_needs_repair(decision: dict[str, Any], calls: list[dict[str, Any]]) -> bool:
    if decision_requests_general_answer(decision):
        return False
    if decision.get("error"):
        return True
    if decision.get("validation_errors"):
        return True
    return any(str(call.get("validation_error") or "").strip() for call in calls)


def selector_status(decision: dict[str, Any], calls: list[dict[str, Any]]) -> str:
    if calls and not selector_needs_repair(decision, calls):
        return "tool_calls"
    if decision_has_tool_intent_frame(decision) and not selector_needs_repair(decision, calls):
        return "tool_calls"
    if decision_requests_general_answer(decision):
        return "general_answer"
    return "clarify"


def selector_error(decision: dict[str, Any], calls: list[dict[str, Any]]) -> str:
    if decision.get("error"):
        return str(decision.get("error"))
    errors = [str(item) for item in decision.get("validation_errors") or [] if str(item)]
    errors.extend(str(call.get("validation_error")) for call in calls if str(call.get("validation_error") or ""))
    return "; ".join(errors)


def decision_has_tool_intent_frame(decision: dict[str, Any]) -> bool:
    if not isinstance(decision, dict):
        return False
    frame = decision.get("intent_frame") if isinstance(decision.get("intent_frame"), dict) else {}
    route = str(frame.get("route") or "").strip().lower()
    intent = str(frame.get("intent") or "").strip().lower()
    if route in {"finance", "memory", "write_preview"} and intent and intent != "none":
        return True
    route = _ROUTE_ALIASES.get(str(decision.get("route") or "").strip().lower(), str(decision.get("route") or "").strip().lower())
    return route in _TOOL_ROUTES and intent and intent != "none"


def format_recent_context(
    history: list[dict[str, Any]] | None,
    *,
    limit: int = 3,
    max_chars: int = 120,
    max_context_chars: int = _RECENT_CONTEXT_MAX_CHARS,
) -> str:
    structured_lines: list[tuple[int, bool, str]] = []
    prose_lines: list[tuple[int, bool, str]] = []
    sequence = 0
    for turn in (history or [])[-limit:]:
        role = str(turn.get("role") or "").strip()
        answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        if answer_context:
            compact_context = _compact_answer_context(answer_context)
            if compact_context:
                structured_lines.append(
                    (
                        sequence,
                        bool(compact_context.get("pending")),
                        "ctx: " + _compact_json(compact_context),
                    )
                )
                sequence += 1
        content = " ".join(str(turn.get("content") or "").split())
        if role not in {"user", "assistant"} or not content:
            continue
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."
        prose_lines.append((sequence, False, f"{role}: {content}"))
        sequence += 1
    return _bounded_recent_context(structured_lines, prose_lines, max_context_chars=max_context_chars)


def _compact_answer_context(answer_context: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mira_frame = answer_context.get("mira_conversation_frame") if isinstance(answer_context.get("mira_conversation_frame"), dict) else {}
    frame = _conversation_frame_from_mira_frame(mira_frame)
    if not frame:
        frame = answer_context.get("conversation_frame") if isinstance(answer_context.get("conversation_frame"), dict) else {}
    if not frame:
        current_frame = answer_context.get("current_frame") if isinstance(answer_context.get("current_frame"), dict) else {}
        frame = _conversation_frame_from_semantic_frame(current_frame)
    compact_frame = _compact_conversation_frame(frame)
    if compact_frame:
        out["frame"] = compact_frame
    else:
        subject = str(answer_context.get("subject") or "").strip()
        ranges = answer_context.get("ranges") if isinstance(answer_context.get("ranges"), list) else []
        if subject:
            out["subject"] = _compact_text(subject)
        if answer_context.get("subject_type"):
            out["subject_type"] = _compact_text(answer_context.get("subject_type"))
        if ranges:
            out["range"] = _compact_text(ranges[0])

    pending = answer_context.get("pending_clarification") if isinstance(answer_context.get("pending_clarification"), dict) else {}
    compact_pending = _compact_pending_clarification(pending)
    if compact_pending:
        out["pending"] = compact_pending
    return out


def _compact_conversation_frame(frame: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(frame, dict):
        return {}
    subject = frame.get("subject") if isinstance(frame.get("subject"), dict) else {}
    subject_type = subject.get("type") or subject.get("kind")
    subject_display = (
        subject.get("display")
        or subject.get("display_name")
        or subject.get("canonical")
        or subject.get("canonical_id")
        or subject.get("raw")
        or subject.get("text")
    )
    compact_subject = {
        key: _compact_text(value)
        for key, value in (("type", subject_type), ("display", subject_display))
        if value not in (None, "", [], {})
    }
    out = {
        "route": frame.get("route") or ("finance" if frame.get("intent") or frame.get("tool") else None),
        "intent": frame.get("intent"),
        "subject": compact_subject,
        "range": frame.get("range"),
        "output": frame.get("requested_output"),
    }
    return {key: _compact_text(value) if isinstance(value, str) else value for key, value in out.items() if value not in (None, "", {}, [])}


def _compact_pending_clarification(pending: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(pending, dict) or not pending:
        return {}
    options = []
    raw_options = pending.get("options") if isinstance(pending.get("options"), list) else []
    for option in raw_options[:3]:
        if not isinstance(option, dict):
            continue
        label = option.get("label") or option.get("display_name") or option.get("canonical")
        compact = {
            "type": _compact_text(option.get("type"), max_chars=_COMPACT_PENDING_OPTION_MAX_CHARS),
            "label": _compact_text(label, max_chars=_COMPACT_PENDING_OPTION_MAX_CHARS),
        }
        compact = {key: value for key, value in compact.items() if value not in (None, "", [], {})}
        if compact:
            options.append(compact)
    out = {
        "kind": _compact_text(pending.get("kind")),
        "slot": _compact_text(pending.get("slot")),
        "tool": _compact_text(pending.get("tool")),
        "raw": _compact_text(pending.get("raw"), max_chars=_COMPACT_PENDING_RAW_MAX_CHARS),
        "options": options,
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _bounded_recent_context(
    structured_lines: list[tuple[int, bool, str]],
    prose_lines: list[tuple[int, bool, str]],
    *,
    max_context_chars: int,
) -> str:
    if max_context_chars <= 0:
        return ""
    selected: dict[int, str] = {}
    used = 0

    def try_add(item: tuple[int, bool, str]) -> None:
        nonlocal used
        sequence, _, line = item
        if not line or sequence in selected:
            return
        additional = len(line) + (1 if selected else 0)
        if used + additional > max_context_chars:
            return
        selected[sequence] = line
        used += additional

    pending_structured = [item for item in structured_lines if item[1]]
    other_structured = [item for item in structured_lines if not item[1]]
    for group in (pending_structured, other_structured, prose_lines):
        for item in sorted(group, key=lambda value: value[0], reverse=True):
            try_add(item)
    return "\n".join(selected[key] for key in sorted(selected))


def _compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)


def _compact_text(value: Any, *, max_chars: int = _COMPACT_FIELD_MAX_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit(" ", 1)[0].rstrip()
    if not clipped:
        clipped = text[:max_chars].rstrip()
    return clipped + "..."


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _selector_compact_output_enabled() -> bool:
    return str(os.getenv("MIRA_SELECTOR_COMPACT_OUTPUT_ENABLED", "0")).strip().lower() not in {"0", "false", "no", "off"}


def selector_response_schema() -> dict[str, Any]:
    return COMPACT_SELECTOR_RESPONSE_SCHEMA if _selector_compact_output_enabled() else SELECTOR_RESPONSE_SCHEMA


def resolve_selector_num_ctx(
    *,
    selector_model: str,
    answer_model: str,
    selector_num_ctx: int | None,
    answer_num_ctx: int | None,
) -> tuple[int | None, str]:
    if (
        selector_model
        and answer_model
        and selector_model == answer_model
        and selector_num_ctx
        and answer_num_ctx
        and selector_num_ctx != answer_num_ctx
    ):
        return answer_num_ctx, "selector and answer use the same model; using answer_num_ctx for selector to avoid Ollama reloads"
    return selector_num_ctx, ""


def _default_completer(prompt: str, max_tokens: int, purpose: str) -> str:
    import llm_client

    return llm_client.complete(
        prompt,
        max_tokens=max_tokens,
        purpose=purpose,
        response_format=selector_response_schema(),
    )


__all__ = [
    "COMPACT_SELECTOR_RESPONSE_SCHEMA",
    "SELECTOR_RESPONSE_SCHEMA",
    "SelectorResult",
    "apply_context_semantics",
    "apply_controller_route_permissions",
    "apply_discourse_frames",
    "build_repair_prompt",
    "build_selector_prompt",
    "build_selector_repair_system_prompt",
    "build_selector_system_prompt",
    "canonical_controller_route",
    "decision_needs_family_detail",
    "decision_requests_general_answer",
    "format_recent_context",
    "latest_pending_clarification",
    "latest_context_range",
    "normalize_selector_decision",
    "parse_json_object",
    "resolve_selector_num_ctx",
    "run_selector",
    "selected_family",
    "selector_response_schema",
    "selector_needs_repair",
    "selector_status",
]
