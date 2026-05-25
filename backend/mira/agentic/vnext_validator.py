from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from typing import Any, Callable

from range_parser import parse_range
from mira.agentic.temporal_parser import is_bounded_range_token

from mira.agentic.schemas import AgentDecision, ToolPlanStep, ValidationResult
from mira.agentic.semantic_catalog import SEMANTIC_TOOL_CATALOG, canonical_semantic_tool_name, is_semantic_tool
from mira.agentic.semantic_frames import complete_semantic_frame
from mira.agentic.semantic_tool_adapter import (
    contains_apply_key,
    semantic_validation_issue,
    strip_apply_keys,
)
from mira.agentic.vnext_manifest import all_tool_schemas, tools_by_name


Grounder = Callable[[str, str, str | None], dict[str, Any]]

_CANONICAL_RANGES = {
    "current_month",
    "this_month",
    "current",
    "last_month",
    "prior_month",
    "previous_month",
    "prior",
    "this_week",
    "last_week",
    "last_7d",
    "last_30d",
    "last_90d",
    "last_180d",
    "last_365d",
    "last_6_months",
    "ytd",
    "last_year",
    "all",
}
_RANGE_KEYS = {"range", "range_a", "range_b"}
_UNSUPPORTED_RANGE_TOKEN = "__unsupported_range__"
_MEMORY_TOOL_NAMES = {"manage_memory", "remember_user_context", "retrieve_relevant_memories", "list_mira_memories"}
_DIRECT_MEMORY_MUTATION_TOOLS = {"update_memory", "forget_memory"}
_GROUND_KEYS = {
    "merchant": "merchant",
    "category": "category",
    "account": "account",
    "account_name": "account",
}


def validate_selector_calls(
    calls: list[dict[str, Any]],
    *,
    question: str,
    profile: str | None = None,
    history: list[dict] | None = None,
    max_tool_count: int = 4,
    now: datetime | None = None,
    grounder: Grounder | None = None,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> ValidationResult:
    calls = _merge_structured_compare_calls(calls or [])
    decision = _decision_from_calls(calls, history=history)

    if len(calls or []) > max(1, int(max_tool_count or 1)):
        return _blocked(decision, f"tool plan has {len(calls)} steps; max is {max_tool_count}")

    by_name = tools_by_name(tool_schemas or all_tool_schemas())
    normalized_steps: list[ToolPlanStep] = []
    grounded_entities: list[dict[str, Any]] = []
    prior_steps: dict[str, str] = {}
    seen_steps: set[str] = set()

    for index, call in enumerate(calls or [], start=1):
        raw_name = str(call.get("name") or call.get("tool") or "").strip()
        name = canonical_semantic_tool_name(raw_name)
        call_validation_error = str(call.get("validation_error") or "").strip()
        if call_validation_error:
            call_entities = call.get("grounded_entities") if isinstance(call.get("grounded_entities"), list) else []
            call_pending = call.get("pending_clarification") if isinstance(call.get("pending_clarification"), dict) else None
            return _clarify(
                decision,
                call_validation_error,
                grounded_entities + copy.deepcopy(call_entities),
                pending_clarification=call_pending,
            )
        if name == "run_sql":
            return _blocked(decision, "disallowed internal tool")
        if name not in by_name and is_semantic_tool(name):
            spec = SEMANTIC_TOOL_CATALOG.get(name)
            if spec is not None:
                by_name[name] = spec.for_selector()
        if name not in by_name:
            return _blocked(decision, f"unknown tool: {raw_name or '<empty>'}")
        if name in _DIRECT_MEMORY_MUTATION_TOOLS:
            return _blocked(decision, f"{name} changes stored memory and is not supported in chat yet")

        args = copy.deepcopy(call.get("args") or {})
        if _truthy_apply_key(args):
            return _blocked(decision, "write requests cannot apply, confirm, commit, or execute changes in the selector path")
        args = _strip_apply_keys(args)
        args = _normalize_ranges(args, now=now)
        range_issue = _range_validation_issue(args)
        if range_issue:
            return _clarify(decision, range_issue, grounded_entities)
        mention = _resolve_structured_mentions(name, args, profile=profile, grounder=grounder)
        if mention.status == "clarify":
            return _clarify(
                decision,
                mention.message,
                grounded_entities + mention.entities,
                pending_clarification=mention.pending_clarification,
            )
        args = mention.args
        grounded_entities.extend(mention.entities)
        if name == "make_chart":
            args = _fill_chart_source_from_prior_steps(args, prior_steps)

        if is_semantic_tool(name):
            frame_result = complete_semantic_frame(
                name,
                args,
                history=history,
                call_meta=call.get("universal_args") if isinstance(call.get("universal_args"), dict) else args,
            )
            if frame_result.issue.status == "blocked":
                return _blocked(decision, frame_result.issue.message)
            if frame_result.issue.status == "clarify":
                return _clarify(
                    decision,
                    frame_result.issue.message,
                    grounded_entities,
                    pending_clarification=_pending_slot_from_semantic_issue(
                        name,
                        frame_result.args,
                        frame_result.current_frame,
                        frame_result.issue.message,
                    ),
                )
            name = frame_result.tool_name
            args = frame_result.args
            issue = semantic_validation_issue(name, args, prior_steps)
            if issue.status == "blocked":
                return _blocked(decision, issue.message)
            if issue.status == "clarify":
                return _clarify(decision, issue.message, grounded_entities)
            privacy_issue = _transaction_scope_issue(name, args)
            if privacy_issue:
                return _blocked(decision, privacy_issue)
        elif name == "plot_chart":
            plot_error = _validate_plot_args(args, prior_steps)
            if plot_error:
                return _blocked(decision, plot_error)

        if not is_semantic_tool(name):
            missing = _missing_required_args(by_name[name], args)
            if missing:
                return _clarify(decision, f"I need {', '.join(missing)} before I can use {name}.", grounded_entities)

        grounded = _ground_args(name, args, profile=profile, grounder=grounder)
        if grounded.status == "clarify":
            return _clarify(decision, grounded.message, grounded_entities + grounded.entities)
        args = grounded.args
        grounded_entities.extend(grounded.entities)

        step_id = str(call.get("id") or f"selector_call_{index}")
        step_signature = json_signature({"tool": name, "args": args})
        if step_signature in seen_steps:
            continue
        seen_steps.add(step_signature)
        normalized_steps.append(
            ToolPlanStep(
                step_id=step_id,
                tool_name=name,
                args=args,
                reason="vnext_selector",
                depends_on=list(call.get("depends_on") or []),
                allow_parallel=True,
            )
        )
        prior_steps[step_id] = name

    return ValidationResult(
        status="ready",
        decision=decision,
        normalized_plan=normalized_steps,
        grounded_entities=grounded_entities,
    )


def json_signature(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _merge_structured_compare_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(calls) != 2:
        return calls
    normalized: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for call in calls:
        raw_name = str(call.get("name") or call.get("tool") or "").strip()
        if canonical_semantic_tool_name(raw_name) != "summarize_spending":
            return calls
        args = copy.deepcopy(call.get("args") or {})
        view = str(args.get("view") or call.get("view") or "").strip().lower()
        if view != "compare":
            return calls
        normalized.append((call, args))

    first_args = normalized[0][1]
    second_args = normalized[1][1]
    if json_signature(first_args.get("filters") or {}) != json_signature(second_args.get("filters") or {}):
        return calls

    ranges = [
        str(first_args.get("range") or first_args.get("range_a") or "").strip(),
        str(second_args.get("range") or second_args.get("range_a") or "").strip(),
    ]
    if not all(ranges):
        return calls

    range_a = next((item for item in ranges if item in {"current_month", "this_month"}), ranges[0])
    range_b = next((item for item in ranges if item != range_a), "")
    if not range_b:
        return calls

    merged_args = copy.deepcopy(first_args)
    merged_args.pop("range", None)
    merged_args["range_a"] = "current_month" if range_a == "this_month" else range_a
    merged_args["range_b"] = "current_month" if range_b == "this_month" else range_b
    return [{**normalized[0][0], "args": merged_args}]


def validation_for_general_answer(*, question: str, history: list[dict] | None = None) -> ValidationResult:
    _ = question
    decision = AgentDecision(
        intent="chat",
        turn_kind="chat",
        tool_plan=[],
        confidence=1.0,
        uses_history=bool(history),
        reasoning_summary="vnext_general_answer",
    )
    return ValidationResult(status="ready", decision=decision, normalized_plan=[])


class _GroundedArgs:
    def __init__(
        self,
        *,
        status: str,
        args: dict[str, Any],
        entities: list[dict[str, Any]] | None = None,
        message: str = "",
        pending_clarification: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.args = args
        self.entities = entities or []
        self.message = message
        self.pending_clarification = pending_clarification or {}


def _decision_from_calls(calls: list[dict[str, Any]], *, history: list[dict] | None) -> AgentDecision:
    steps = [
        ToolPlanStep(
            step_id=str(call.get("id") or f"selector_call_{index}"),
            tool_name=str(call.get("name") or call.get("tool") or ""),
            args=copy.deepcopy(call.get("args") or {}),
            reason="vnext_selector",
            depends_on=list(call.get("depends_on") or []),
            allow_parallel=True,
        )
        for index, call in enumerate(calls or [], start=1)
    ]
    turn_kind = "chat" if not steps or all(step.tool_name in _MEMORY_TOOL_NAMES for step in steps) else "finance"
    return AgentDecision(
        intent=turn_kind,
        turn_kind=turn_kind,
        tool_plan=steps,
        confidence=1.0 if steps else 0.0,
        uses_history=bool(history),
        reasoning_summary="vnext_selector",
    )


def _normalize_ranges(args: dict[str, Any], *, now: datetime | None) -> dict[str, Any]:
    for key in list(args):
        if key in _RANGE_KEYS and args.get(key) not in (None, ""):
            args[key] = _normalize_range(str(args[key]), now=now)
    return args


def _normalize_range(value: str, *, now: datetime | None) -> str:
    token = str(value or "").strip().lower()
    if token in _CANONICAL_RANGES:
        if token in {"this_month", "current"}:
            return "current_month"
        if token in {"previous_month", "prior"}:
            return "last_month"
        return token
    if re.match(r"^\d{4}-\d{2}$", token):
        return token
    if is_bounded_range_token(token):
        return token
    if re.match(r"^last_\d+d$", token):
        return token
    if re.match(r"^last_\d{1,2}_months$", token):
        return token
    parsed = parse_range(value, now=now)
    if parsed.explicit and parsed.unsupported_reason:
        return _UNSUPPORTED_RANGE_TOKEN
    return parsed.token


def _range_validation_issue(args: dict[str, Any]) -> str:
    for key in _RANGE_KEYS:
        if args.get(key) == _UNSUPPORTED_RANGE_TOKEN:
            return "I need a supported time range before I can answer that."
    return ""


def _transaction_scope_issue(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name != "query_transactions":
        return ""
    view = str(args.get("view") or "").strip().lower()
    if view not in {"list", "search"}:
        return ""
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    scoped = any(
        filters.get(key) not in (None, "", [], {})
        for key in ("merchant", "category", "account", "search", "transaction_id", "reviewed")
    )
    if scoped or args.get("range") not in (None, "", [], {}):
        return ""
    return "I need a merchant, category, account, search term, or range before showing a transaction list."


def _missing_required_args(tool_schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    fn = tool_schema.get("function") if isinstance(tool_schema.get("function"), dict) else {}
    name = str(fn.get("name") or "")
    schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
    missing = []
    for key in schema.get("required") or []:
        if name == "plot_chart" and args.get("source_step_id") and key in {"labels", "values", "series"}:
            continue
        if key not in args or args.get(key) in (None, "", []):
            missing.append(str(key))
    return missing


def _truthy_apply_key(args: dict[str, Any]) -> bool:
    return contains_apply_key(args)


def _strip_apply_keys(args: dict[str, Any]) -> dict[str, Any]:
    return strip_apply_keys(args)


def _validate_plot_args(args: dict[str, Any], prior_steps: dict[str, str]) -> str:
    if any(key in args for key in ("labels", "values", "series")):
        return "plot_chart must use prior tool evidence instead of selector-provided labels or values"
    source_step_id = str(args.get("source_step_id") or "").strip()
    if not source_step_id:
        return "plot_chart requires source_step_id from an earlier evidence-producing tool"
    if source_step_id not in prior_steps:
        return "plot_chart source_step_id must reference an existing earlier tool step"
    return ""


def _pending_slot_from_semantic_issue(
    tool_name: str,
    args: dict[str, Any],
    current_frame: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    lowered = str(message or "").lower()
    if tool_name == "check_affordability" and "amount" in lowered:
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        return {
            "kind": "missing_slot",
            "slot": "amount",
            "tool": "check_affordability",
            "args": copy.deepcopy(args or {}),
            "frame": copy.deepcopy(current_frame or {}),
            "purpose": payload.get("purpose"),
        }
    return {}


def _fill_chart_source_from_prior_steps(args: dict[str, Any], prior_steps: dict[str, str]) -> dict[str, Any]:
    if not prior_steps:
        return args
    out = copy.deepcopy(args or {})
    payload = out.get("payload") if isinstance(out.get("payload"), dict) else {}
    if payload.get("source_step_id"):
        return out
    source_step_id = next(reversed(prior_steps))
    payload["source_step_id"] = source_step_id
    out["payload"] = payload
    return out


def _resolve_structured_mentions(
    tool_name: str,
    args: dict[str, Any],
    *,
    profile: str | None,
    grounder: Grounder | None,
) -> _GroundedArgs:
    out = copy.deepcopy(args or {})
    filters = out.get("filters") if isinstance(out.get("filters"), dict) else {}
    mention = str(filters.get("mention") or "").strip()
    if not mention:
        return _GroundedArgs(status="ready", args=out)

    type_hint = str(filters.get("mention_type") or "").strip().lower()
    result = _ground_any_entity(mention, type_hint=type_hint, profile=profile, grounder=grounder)
    record = _ground_record(result, original=mention, arg_key="filters.mention")
    entities = [record]
    if result.get("kind") == "ambiguous":
        pending = _entity_resolution_pending(raw=mention, result=result, tool_name=tool_name, args=out)
        return _GroundedArgs(
            status="clarify",
            args=out,
            entities=entities,
            message=_entity_resolution_question(mention, pending),
            pending_clarification=pending,
        )
    if result.get("kind") == "missing" or not result.get("value"):
        pending = _entity_resolution_pending(raw=mention, result=result, tool_name=tool_name, args=out)
        return _GroundedArgs(
            status="clarify",
            args=out,
            entities=entities,
            message=f"I couldn't confidently match `{mention}` to a merchant, category, or account in your data. Which one should I use?",
            pending_clarification=pending,
        )

    entity_type = str(result.get("entity_type") or "").strip()
    value = str(result.get("value") or result.get("display_name") or "").strip()
    filters = dict(filters)
    for key in ("mention", "mention_type", "merchant", "category", "account"):
        filters.pop(key, None)
    if entity_type in {"merchant", "category", "account"} and value:
        filters[entity_type] = value
    if filters:
        out["filters"] = filters
    else:
        out.pop("filters", None)
    return _GroundedArgs(status="ready", args=out, entities=entities)


def _ground_any_entity(
    text: str,
    *,
    type_hint: str,
    profile: str | None,
    grounder: Grounder | None,
) -> dict[str, Any]:
    if type_hint in {"merchant", "category", "account"}:
        return _ground(type_hint, text, profile=profile, grounder=grounder)

    if grounder is None:
        try:
            from mira.grounding import ground_entity

            result = ground_entity(
                text,
                entity_types=("category", "merchant"),
                profile=profile,
                include_transaction_evidence=True,
                limit=6,
            )
            return result.as_dict() if hasattr(result, "as_dict") else copy.deepcopy(result)
        except Exception:
            return _missing_ground("entity", text)

    results = [
        _ground("category", text, profile=profile, grounder=grounder),
        _ground("merchant", text, profile=profile, grounder=grounder),
    ]
    usable = [result for result in results if result.get("kind") in {"exact", "approximate"} and result.get("value")]
    candidates: list[dict[str, Any]] = []
    for result in results:
        for candidate in result.get("candidates") or []:
            if isinstance(candidate, dict):
                candidates.append({**candidate, "entity_type": candidate.get("entity_type") or result.get("entity_type")})
    candidates.sort(key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
    if not usable:
        return {**_missing_ground("entity", text), "candidates": candidates[:6]}
    usable.sort(key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
    top = usable[0]
    close = [
        item
        for item in usable
        if float(item.get("confidence") or 0.0) >= float(top.get("confidence") or 0.0) - 0.05
    ]
    if len(close) > 1 and str(close[0].get("entity_type")) != str(close[1].get("entity_type")):
        return {
            "kind": "ambiguous",
            "entity_type": top.get("entity_type"),
            "value": None,
            "canonical_id": None,
            "display_name": None,
            "confidence": top.get("confidence", 0.0),
            "candidates": candidates[:6],
            "evidence": {"query": text, "reason": "multiple entity types matched"},
        }
    return {**top, "candidates": candidates[:6]}


def _entity_resolution_pending(
    *,
    raw: str,
    result: dict[str, Any],
    tool_name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    resume_args = copy.deepcopy(args or {})
    filters = resume_args.get("filters") if isinstance(resume_args.get("filters"), dict) else {}
    filters = dict(filters)
    filters.pop("mention", None)
    filters.pop("mention_type", None)
    if filters:
        resume_args["filters"] = filters
    else:
        resume_args.pop("filters", None)

    options = []
    seen: set[str] = set()
    for candidate in result.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        entity_type = str(candidate.get("entity_type") or result.get("entity_type") or "").strip()
        if entity_type not in {"merchant", "category", "account"}:
            continue
        canonical = str(candidate.get("value") or candidate.get("canonical_id") or candidate.get("display_name") or "").strip()
        label = str(candidate.get("display_name") or canonical).strip()
        if not canonical or not label:
            continue
        option_id = f"{entity_type}:{canonical}"
        if option_id.lower() in seen:
            continue
        seen.add(option_id.lower())
        options.append({
            "id": option_id,
            "type": entity_type,
            "canonical": canonical,
            "label": f"{label} {entity_type}",
            "confidence": candidate.get("confidence"),
        })
    return {
        "kind": "entity_resolution",
        "raw": raw,
        "resume_frame": {"tool": tool_name, "args": resume_args},
        "options": options[:5],
    }


def _entity_resolution_question(raw: str, pending: dict[str, Any]) -> str:
    labels = [
        str(item.get("label") or "")
        for item in pending.get("options") or []
        if isinstance(item, dict) and item.get("label")
    ]
    if labels:
        return f"I found multiple possible matches for `{raw}`. Did you mean {', '.join(labels[:3])}?"
    return f"I found multiple possible matches for `{raw}`. Which one should I use?"


def _ground_args(
    tool_name: str,
    args: dict[str, Any],
    *,
    profile: str | None,
    grounder: Grounder | None,
) -> _GroundedArgs:
    grounded_entities: list[dict[str, Any]] = []
    out = copy.deepcopy(args)

    def ground_container_arg(container: dict[str, Any], arg_key: str, entity_type: str, record_key: str | None = None) -> str | None:
        raw = str(container.get(arg_key) or "").strip()
        if not raw:
            return None
        result = _ground(entity_type, raw, profile=profile, grounder=grounder)
        grounded_entities.append(_ground_record(result, original=raw, arg_key=record_key or arg_key))
        if result.get("kind") == "ambiguous":
            return f"I found multiple possible {entity_type} matches for `{raw}`. Which one should I use?"
        if result.get("kind") == "missing" or not result.get("value"):
            return f"I couldn't confidently match `{raw}` to a {entity_type} in your data. Which {entity_type} should I use?"
        cross_type_issue = _hard_typed_entity_issue(
            entity_type,
            raw,
            typed_result=result,
            profile=profile,
            grounder=grounder,
        )
        if cross_type_issue:
            return cross_type_issue
        container[arg_key] = result.get("value") or result.get("display_name") or raw
        return None

    def ground_arg(arg_key: str, entity_type: str) -> str | None:
        return ground_container_arg(out, arg_key, entity_type)

    subject_type = str(out.get("subject_type") or "").strip().lower()
    if out.get("subject") and subject_type in {"merchant", "category"}:
        message = ground_arg("subject", subject_type)
        if message:
            return _GroundedArgs(status="clarify", args=out, entities=grounded_entities, message=message)

    entity_type = str(out.get("entity_type") or "").strip().lower()
    if out.get("entity") and entity_type in {"merchant", "category"}:
        message = ground_arg("entity", entity_type)
        if message:
            return _GroundedArgs(status="clarify", args=out, entities=grounded_entities, message=message)

    filters = out.get("filters") if isinstance(out.get("filters"), dict) else {}
    for arg_key, entity_type in (("merchant", "merchant"), ("category", "category"), ("account", "account")):
        if filters.get(arg_key):
            message = ground_container_arg(filters, arg_key, entity_type, f"filters.{arg_key}")
            if message:
                return _GroundedArgs(status="clarify", args=out, entities=grounded_entities, message=message)
    if filters:
        out["filters"] = filters

    for arg_key, entity_type in _GROUND_KEYS.items():
        if out.get(arg_key):
            message = ground_arg(arg_key, entity_type)
            if message:
                return _GroundedArgs(status="clarify", args=out, entities=grounded_entities, message=message)

    if tool_name == "preview_rename_merchant" and out.get("old_name"):
        message = ground_arg("old_name", "merchant")
        if message:
            return _GroundedArgs(status="clarify", args=out, entities=grounded_entities, message=message)

    payload = out.get("payload") if isinstance(out.get("payload"), dict) else {}
    if payload:
        for arg_key, entity_type in (
            ("merchant", "merchant"),
            ("old_name", "merchant"),
            ("category", "category"),
            ("linked_category", "category"),
            ("account_name", "account"),
            ("account", "account"),
        ):
            if payload.get(arg_key):
                message = ground_container_arg(payload, arg_key, entity_type, f"payload.{arg_key}")
                if message:
                    return _GroundedArgs(status="clarify", args=out, entities=grounded_entities, message=message)
        out["payload"] = payload

    splits_container = payload if isinstance(payload.get("splits"), list) else out
    if isinstance(splits_container.get("splits"), list):
        splits = []
        for split in splits_container["splits"]:
            if not isinstance(split, dict):
                splits.append(split)
                continue
            category = str(split.get("category") or "").strip()
            if category:
                result = _ground("category", category, profile=profile, grounder=grounder)
                grounded_entities.append(_ground_record(result, original=category, arg_key="splits.category"))
                if result.get("kind") in {"ambiguous", "missing"} or not result.get("value"):
                    return _GroundedArgs(
                        status="clarify",
                        args=out,
                        entities=grounded_entities,
                        message=f"I couldn't confidently match `{category}` to a category in your data. Which category should I use?",
                    )
                split = {**split, "category": result.get("value")}
            splits.append(split)
        splits_container["splits"] = splits
        if splits_container is payload:
            out["payload"] = payload
        else:
            out["splits"] = splits

    return _GroundedArgs(status="ready", args=out, entities=grounded_entities)


def _ground(entity_type: str, text: str, *, profile: str | None, grounder: Grounder | None) -> dict[str, Any]:
    if grounder is not None:
        result = grounder(entity_type, text, profile)
        return copy.deepcopy(result) if isinstance(result, dict) else _missing_ground(entity_type, text)
    try:
        if entity_type == "merchant":
            from mira.grounding import ground_merchant

            result = ground_merchant(text, profile=profile, include_transaction_evidence=True, limit=4)
        elif entity_type == "category":
            from mira.grounding import ground_category

            result = ground_category(text, profile=profile, limit=4)
        elif entity_type == "account":
            from data_manager import get_accounts_filtered
            from mira.grounding import ground_text

            names = [
                str(account.get("name") or "").strip()
                for account in get_accounts_filtered(profile=profile)
                if str(account.get("name") or "").strip()
            ]
            result = ground_text(text, "account", names, profile=profile, limit=4)
        else:
            return _missing_ground(entity_type, text)
        return result.as_dict() if hasattr(result, "as_dict") else copy.deepcopy(result)
    except Exception:
        return _missing_ground(entity_type, text)


def _hard_typed_entity_issue(
    entity_type: str,
    text: str,
    *,
    typed_result: dict[str, Any],
    profile: str | None,
    grounder: Grounder | None,
) -> str:
    if entity_type not in {"merchant", "category"}:
        return ""
    if typed_result.get("kind") == "exact":
        return ""
    cross = _ground_any_entity(text, type_hint="", profile=profile, grounder=grounder)
    cross_type = str(cross.get("entity_type") or "").strip()
    if cross.get("kind") == "ambiguous":
        return f"I found both merchant and category matches for `{text}`. Which one should I use?"
    if cross_type not in {"merchant", "category"} or cross_type == entity_type:
        return ""
    try:
        typed_conf = float(typed_result.get("confidence") or 0.0)
        cross_conf = float(cross.get("confidence") or 0.0)
    except (TypeError, ValueError):
        typed_conf = 0.0
        cross_conf = 0.0
    if cross.get("kind") == "exact" or cross_conf >= typed_conf + 0.1:
        return f"`{text}` matched a {cross_type} more strongly than a {entity_type}. Which one should I use?"
    return ""


def _ground_record(result: dict[str, Any], *, original: str, arg_key: str) -> dict[str, Any]:
    return {
        "arg": arg_key,
        "entity_type": result.get("entity_type"),
        "original": original,
        "value": result.get("value"),
        "canonical_id": result.get("canonical_id"),
        "display_name": result.get("display_name"),
        "kind": result.get("kind"),
        "confidence": result.get("confidence", 0.0),
        "source": "vnext_resolver",
        "candidates": copy.deepcopy(result.get("candidates") or [])[:4],
    }


def _missing_ground(entity_type: str, text: str) -> dict[str, Any]:
    return {
        "kind": "missing",
        "entity_type": entity_type,
        "value": None,
        "canonical_id": None,
        "display_name": None,
        "confidence": 0.0,
        "candidates": [],
        "evidence": {"query": text},
    }


def _clarify(
    decision: AgentDecision,
    question: str,
    grounded_entities: list[dict[str, Any]],
    *,
    pending_clarification: dict[str, Any] | None = None,
) -> ValidationResult:
    return ValidationResult(
        status="clarify",
        decision=decision,
        normalized_plan=[],
        grounded_entities=grounded_entities,
        clarification_question=question,
        pending_clarification=pending_clarification or {},
    )


def _blocked(decision: AgentDecision, reason: str) -> ValidationResult:
    return ValidationResult(
        status="blocked",
        decision=decision,
        normalized_plan=[],
        blocked_reason=reason,
    )


__all__ = [
    "validate_selector_calls",
    "validation_for_general_answer",
]
