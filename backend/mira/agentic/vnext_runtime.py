from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from range_parser import has_explicit_time_scope, parse_range, words

from mira.agentic.intent_frame import ConversationFrame, MiraIntentFrame, MiraSubject, is_supported_time_token
from mira.agentic.schemas import AgentDecision, EvidencePacket, ValidationResult
from mira.agentic.temporal_parser import bounded_range_dates, bounded_range_token


_RUNTIME = "agentic_vnext"
_ANSWER = "Mira vNext selected a safe route, but tool execution and answer generation are not active in this experimental path yet."
_SHADOW_TRACE_LOCK = threading.Lock()
_SELECTOR_DECISION_CACHE_LOCK = threading.Lock()
_SELECTOR_DECISION_CACHE: dict[str, tuple[float, Any]] = {}
_MEMORY_TOOL_NAMES = {"manage_memory", "remember_user_context", "retrieve_relevant_memories", "list_mira_memories"}
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}
_FRONT_CONTROLLER_META_PREFIX = "_mira_"


def build_shadow_trace(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None = None,
    forced_intent: str | None = None,
) -> dict[str, Any]:
    _ = profile
    return {
        "runtime": _RUNTIME,
        "phase": "skeleton",
        "status": "not_executed",
        "question_chars": len(question or ""),
        "history_turns": len(history or []),
        "forced_intent": forced_intent,
        "selected_tools": [],
        "llm_calls": 0,
        "legacy_router_used": False,
    }


def run_vnext_shadow(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None = None,
    forced_intent: str | None = None,
    current_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        state = _prepare_vnext_turn(question=question, profile=profile, history=history, forced_intent=forced_intent)
        validation: ValidationResult = state["validation"]
        route: dict[str, Any] = state["route"]
        safe_to_execute, skipped_reason = _shadow_execution_policy(validation)
        evidence = EvidencePacket(question=question)
        if safe_to_execute:
            evidence = _execute_vnext_evidence(
                validation=validation,
                question=question,
                profile=profile,
            )
            answer_result = _answer_vnext_safely(
                question=question,
                route=route,
                validation=validation,
                evidence=evidence,
                history=history,
                memory_context_provider=_memory_context_provider_for_turn(question=question, profile=profile, route=route),
            )
        else:
            answer_result = _shadow_skipped_answer(skipped_reason)
        done = _done_event(route=route, validation=validation, evidence=evidence, answer_result=answer_result)
        payload = _shadow_payload(
            question=question,
            profile=profile,
            current_event=current_event,
            vnext_done=done,
            validation=validation,
            safe_to_execute=safe_to_execute,
            skipped_reason=skipped_reason,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except Exception as exc:
        payload = {
            "runtime": _RUNTIME,
            "status": "error",
            "profile": profile or "household",
            "question": str(question or ""),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc),
            "legacy_router_used": False,
        }
    _record_shadow_trace(payload)
    return payload


def run_vnext_result(
    question: str,
    profile: str | None,
    history: list[dict] | None = None,
    forced_intent: str | None = None,
) -> dict[str, Any]:
    state = _prepare_vnext_turn(question=question, profile=profile, history=history, forced_intent=forced_intent)
    evidence = _execute_vnext_evidence(
        validation=state["validation"],
        question=question,
        profile=profile,
    )
    answer_result = _answer_vnext_safely(
        question=question,
        route=state["route"],
        validation=state["validation"],
        evidence=evidence,
        history=history,
        memory_context_provider=_memory_context_provider_for_turn(question=question, profile=profile, route=state["route"]),
    )
    done = _done_event(route=state["route"], validation=state["validation"], evidence=evidence, answer_result=answer_result)
    done.pop("type", None)
    return done


def run_vnext_stream(
    question: str,
    profile: str | None,
    history: list[dict] | None = None,
    forced_intent: str | None = None,
):
    selector_override = _memory_slash_bypass_selector(question=question)
    if selector_override is None:
        selector_override = _pending_state_fast_selector(question=question, history=history)
    front_controller_outcome = None
    if selector_override is None:
        front_controller_outcome = yield from _iter_front_controller_chat_fast_lane(
            question=question,
            profile=profile,
            history=history,
        )
        if bool((front_controller_outcome or {}).get("handled")):
            return
        if isinstance(front_controller_outcome, dict):
            candidate_override = front_controller_outcome.get("selector_override")
            if candidate_override is not None:
                selector_override = candidate_override
    if selector_override is None and not _vnext_selector_fallback_enabled():
        selector_override = _SelectorFallbackDisabled(outcome=front_controller_outcome)
        front_controller_outcome = None

    yield _routing_started_event()
    state = _prepare_vnext_turn(
        question=question,
        profile=profile,
        history=history,
        forced_intent=forced_intent,
        selector_override=selector_override,
        selector_fallback=_selector_fallback_trace(front_controller_outcome),
    )
    route = state["route"]
    validation = state["validation"]
    yield {"type": "route", **route}
    yield {
        "type": "controller",
        "act": route.get("controller_act"),
        "controller_act": route.get("controller_act"),
        "intent": route.get("intent"),
        "confidence": route.get("confidence"),
        "reason": "vnext_selector",
        "legacy_router_used": False,
        "mira_planner": _RUNTIME,
    }
    yield {
        "type": "action",
        "domain_action": route.get("domain_action"),
        "tool_plan": route.get("tool_plan") or [],
        "validation": route.get("validation"),
        "grounded_entities": route.get("grounded_entities") or [],
        "selected_tools": route.get("selected_tools") or [],
    }
    yield _progress_event(route)
    evidence = EvidencePacket(question=question)
    preview_answer_result = None
    if _should_execute(validation):
        from mira.agentic.vnext_executor import chart_from_evidence, iter_execute_vnext_events

        for event in iter_execute_vnext_events(
            validation,
            question=question,
            profile=profile,
            cache={},
        ):
            if event.get("type") == "evidence":
                evidence = event["evidence"]
            else:
                yield event
        chart_payload = chart_from_evidence(evidence)
        if chart_payload:
            yield {"type": "chart", "chart": chart_payload}
        evidence_preview = _complex_finance_evidence_preview_event(
            question=question,
            route=route,
            validation=validation,
            evidence=evidence,
        )
        if evidence_preview:
            yield evidence_preview
            preview_answer_event = _complex_finance_preview_answer_event(question=question, evidence=evidence)
            if preview_answer_event:
                preview_answer_result = preview_answer_event.pop("_answer_result", None)
                yield preview_answer_event
    answer_result = None
    displayed_answer_parts: list[str] = []
    saw_streamed_answer_token = False
    saw_answer_reset = False
    preview_answer_used_as_final = preview_answer_result is not None and _complex_finance_preview_only_enabled()
    if preview_answer_used_as_final:
        answer_result = replace(preview_answer_result, path="evidence_llm")
    else:
        try:
            from mira.agentic.vnext_answerer import iter_answer_vnext_events

            for event in iter_answer_vnext_events(
                question=question,
                route=route,
                validation=validation,
                evidence=evidence,
                history=history,
                memory_context_provider=_memory_context_provider_for_turn(question=question, profile=profile, route=route),
            ):
                if event.get("type") == "_answer_result":
                    answer_result = event.get("answer_result")
                else:
                    if event.get("type") == "reset_text":
                        displayed_answer_parts.clear()
                        saw_answer_reset = True
                    elif event.get("type") == "token":
                        saw_streamed_answer_token = True
                        displayed_answer_parts.append(str(event.get("text") or ""))
                    yield event
        except Exception:
            answer_result = None
    if answer_result is None:
        answer_result = _answer_vnext_safely(
            question=question,
            route=route,
            validation=validation,
            evidence=evidence,
            history=history,
            memory_context_provider=_memory_context_provider_for_turn(question=question, profile=profile, route=route),
        )
    done = _done_event(route=route, validation=validation, evidence=evidence, answer_result=answer_result)
    if preview_answer_result is not None:
        done["preview_answer"] = getattr(preview_answer_result, "answer", "")
        if not preview_answer_used_as_final:
            done["llm_calls"] = int(done.get("llm_calls") or 0) + int(getattr(preview_answer_result, "llm_calls", 0) or 0)
        answer_guard = done.get("answer_guard") if isinstance(done.get("answer_guard"), dict) else {}
        done["answer_guard"] = {
            **answer_guard,
            "preview_path": getattr(preview_answer_result, "path", ""),
            "preview_used_fallback": bool(getattr(preview_answer_result, "used_fallback", False)),
            "preview_error": getattr(preview_answer_result, "error", ""),
            "preview_only": bool(preview_answer_used_as_final),
        }
    _mark_evidence_attach_only(
        done,
        displayed_answer="".join(displayed_answer_parts),
        saw_streamed_answer_token=saw_streamed_answer_token,
        saw_answer_reset=saw_answer_reset,
    )
    rewarm_trace = _maybe_schedule_front_controller_rewarm(
        question=question,
        history=history,
        done=done,
    )
    if rewarm_trace:
        done_trace = done.get("trace") if isinstance(done.get("trace"), dict) else {}
        done["trace"] = {**done_trace, "front_controller_rewarm": rewarm_trace}
        route_payload = done.get("route") if isinstance(done.get("route"), dict) else {}
        route_trace = route_payload.get("trace") if isinstance(route_payload.get("trace"), dict) else {}
        done["route"] = {**route_payload, "trace": {**route_trace, "front_controller_rewarm": rewarm_trace}}
    yield done
    memory_update = _memory_suggestion_update_event(
        question=question,
        answer=str(done.get("answer") or ""),
        route=route,
        validation=validation,
        done=done,
    )
    if memory_update:
        yield memory_update


def _iter_front_controller_chat_fast_lane(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
):
    if _front_controller_should_skip_for_typed_state(history):
        return _front_controller_fallback(
            selector_path_reason="pending_clarification_state",
            fallback_reason="typed_pending_state",
        )
    try:
        from mira.agentic.front_controller import (
            front_controller_done_event,
            iter_front_controller_stream,
            mark_front_controller_active,
            mark_front_controller_inactive,
        )
    except Exception:
        return _front_controller_fallback(
            selector_path_reason="fallback_error",
            fallback_reason="front_controller_import_error",
        )

    mark_front_controller_active()
    try:
        streamed_events = iter_front_controller_stream(question=question, history=history, stream_chat_tokens=True)
        controller_chat_streamed = False
        for event in streamed_events:
            if event.get("type") != "_front_controller_result":
                if event.get("type") == "token":
                    controller_chat_streamed = True
                yield event
                continue
            if not event.get("handled"):
                if event.get("mode") == "PLAN" and event.get("reason") == "plan_ineligible":
                    repaired_plan_fields = _repair_front_controller_plan_fields(
                        question=question,
                        history=history,
                        plan_fields=event.get("plan_fields") if isinstance(event.get("plan_fields"), dict) else {},
                    )
                    handled_plan = yield from _iter_front_controller_scalar_plan(
                        question=question,
                        profile=profile,
                        history=history,
                        plan_fields=repaired_plan_fields,
                        latency_ms=float(event.get("latency_ms") or 0.0),
                        controller_metrics=event,
                    )
                    if handled_plan:
                        return _front_controller_handled()
                    selector = _front_controller_non_scalar_selector(
                        question=question,
                        profile=profile,
                        history=history,
                        plan_fields=repaired_plan_fields,
                        latency_ms=float(event.get("latency_ms") or 0.0),
                        controller_metrics=event,
                    )
                    if selector is not None:
                        return _front_controller_selector_override(selector)
                    event = {
                        **event,
                        "plan_fields": _front_controller_visible_plan_fields(repaired_plan_fields),
                    }
                return _front_controller_fallback_from_event(event)

            if event.get("mode") == "PLAN":
                repaired_plan_fields = _repair_front_controller_plan_fields(
                    question=question,
                    history=history,
                    plan_fields=event.get("plan_fields") if isinstance(event.get("plan_fields"), dict) else {},
                )
                handled_plan = yield from _iter_front_controller_scalar_plan(
                    question=question,
                    profile=profile,
                    history=history,
                    plan_fields=repaired_plan_fields,
                    latency_ms=float(event.get("latency_ms") or 0.0),
                    controller_metrics=event,
                )
                if handled_plan:
                    return _front_controller_handled()
                selector = _front_controller_non_scalar_selector(
                    question=question,
                    profile=profile,
                    history=history,
                    plan_fields=repaired_plan_fields,
                    latency_ms=float(event.get("latency_ms") or 0.0),
                    controller_metrics=event,
                )
                if selector is not None:
                    return _front_controller_selector_override(selector)
                return _front_controller_fallback_from_event(
                    {
                        **event,
                        "plan_fields": _front_controller_visible_plan_fields(repaired_plan_fields),
                        "reason": "scalar_gate_failed",
                    }
                )

            answer = str(event.get("answer") or "").strip()
            if answer and not controller_chat_streamed:
                yield {"type": "token", "text": answer}
            done_inputs = front_controller_done_event(
                question=question,
                answer=answer,
                latency_ms=float(event.get("latency_ms") or 0.0),
                controller_metrics=event,
            )
            done = _done_event(
                route=done_inputs["route"],
                validation=done_inputs["validation"],
                evidence=done_inputs["evidence"],
                answer_result=done_inputs["answer_result"],
            )
            yield done
            return _front_controller_handled()
    except Exception:
        return _front_controller_fallback(
            selector_path_reason="fallback_error",
            fallback_reason="front_controller_exception",
        )
    finally:
        mark_front_controller_inactive()
    return _front_controller_fallback(
        selector_path_reason="fallback_error",
        fallback_reason="front_controller_no_result",
    )


def _front_controller_handled() -> dict[str, Any]:
    return {"handled": True}


def _front_controller_selector_override(selector: Any) -> dict[str, Any]:
    return {"handled": False, "selector_override": selector}


def _front_controller_fallback(
    *,
    selector_path_reason: str,
    fallback_reason: str,
    mode: str = "",
    plan_fields: dict[str, Any] | None = None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "handled": False,
        "selector_path_reason": selector_path_reason,
        "front_controller_fallback_reason": fallback_reason,
        "front_controller_mode": mode,
        "front_controller_plan_fields": copy.deepcopy(plan_fields or {}),
        "front_controller_latency_ms": latency_ms,
    }


def _front_controller_fallback_from_event(event: dict[str, Any]) -> dict[str, Any]:
    reason = str(event.get("reason") or "").strip()
    mode = str(event.get("mode") or "").strip()
    plan_fields = event.get("plan_fields") if isinstance(event.get("plan_fields"), dict) else {}
    if reason == "disabled":
        selector_path_reason = "front_controller_disabled"
    elif reason == "invalid_protocol":
        selector_path_reason = "invalid_protocol"
    elif reason == "plan_ineligible":
        selector_path_reason = _selector_path_reason_for_plan_fields(plan_fields)
    elif reason == "scalar_gate_failed":
        selector_path_reason = _selector_path_reason_for_plan_fields(plan_fields, default="scalar_gate_failed")
    elif mode == "PLAN" and plan_fields:
        selector_path_reason = _selector_path_reason_for_plan_fields(plan_fields)
    else:
        selector_path_reason = "fallback_error"
    return _front_controller_fallback(
        selector_path_reason=selector_path_reason,
        fallback_reason=reason or "unknown",
        mode=mode,
        plan_fields=plan_fields,
        latency_ms=float(event.get("latency_ms")) if event.get("latency_ms") is not None else None,
    )


def _selector_path_reason_for_plan_fields(plan_fields: dict[str, Any], *, default: str = "plan_ineligible") -> str:
    route = str(plan_fields.get("route") or "").strip().lower()
    intent = str(plan_fields.get("intent") or "").strip().lower()
    output = str(plan_fields.get("output") or "").strip().lower()
    if route == "write_preview" or intent == "write_preview" or output == "preview":
        return "write_preview_not_yet_fast_lane"
    if route == "memory" or intent == "memory_op":
        return "memory_policy"
    if route == "explain_last":
        return "explain_last_not_yet_fast_lane"
    if route == "finance":
        if intent == "spending_total" and output in {"", "scalar"}:
            return default
        return "complex_finance_not_yet_fast_lane"
    return default


def _selector_fallback_trace(outcome: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(outcome, dict) or outcome.get("handled"):
        return {}
    if outcome.get("selector_override") is not None:
        return {}
    trace = {
        "selector_path_reason": outcome.get("selector_path_reason") or "fallback_error",
        "front_controller_fallback_reason": outcome.get("front_controller_fallback_reason") or "",
        "front_controller_mode": outcome.get("front_controller_mode") or "",
    }
    plan_fields = outcome.get("front_controller_plan_fields")
    if isinstance(plan_fields, dict) and plan_fields:
        trace["front_controller_plan_fields"] = copy.deepcopy(plan_fields)
    if outcome.get("front_controller_latency_ms") is not None:
        trace["front_controller_latency_ms"] = outcome.get("front_controller_latency_ms")
    return trace


def _front_controller_should_skip_for_typed_state(history: list[dict] | None) -> bool:
    if _pending_state_fast_resolver_enabled():
        return False
    return bool(
        _latest_pending_entity_clarification(history)
        or _latest_pending_amount_clarification(history)
    )


def _repair_front_controller_plan_fields(
    *,
    question: str,
    history: list[dict] | None,
    plan_fields: dict[str, str],
) -> dict[str, str]:
    repaired = _repair_front_controller_protocol_shape_plan(plan_fields)
    repaired = _repair_front_controller_scalar_followup_plan(
        question=question,
        history=history,
        plan_fields=repaired,
    )
    repaired = _repair_front_controller_broad_spending_scope_plan(
        question=question,
        history=history,
        plan_fields=repaired,
    )
    repaired = _repair_front_controller_transaction_lookup_plan(question=question, plan_fields=repaired)
    repaired = _repair_front_controller_finance_contract_plan(question=question, plan_fields=repaired)
    repaired = _repair_front_controller_llm_temporal_plan(question=question, plan_fields=repaired)
    return _repair_front_controller_unsupported_time_plan(repaired)


def _repair_front_controller_protocol_shape_plan(plan_fields: dict[str, str]) -> dict[str, str]:
    repaired = dict(plan_fields)
    route = str(repaired.get("route") or "").strip().lower()
    intent = str(repaired.get("intent") or "").strip().lower()
    if route == "write_preview" and intent in {"bulk_recategorize", "create_rule", "set_budget"}:
        repaired["intent"] = "write_preview"
        repaired.setdefault("change_type", intent)
    return repaired


def _repair_front_controller_broad_spending_scope_plan(
    *,
    question: str,
    history: list[dict] | None,
    plan_fields: dict[str, str],
) -> dict[str, str]:
    repaired = dict(plan_fields)
    if str(repaired.get("route") or "").strip().lower() != "finance":
        return repaired
    if not _front_controller_question_requests_broad_spending_scope(question, repaired):
        return repaired

    prior = _latest_mira_conversation_frame(history)
    intent = str(repaired.get("intent") or "").strip().lower()
    output = str(repaired.get("output") or "").strip().lower()
    tokens = words(question)
    broad_correction = len(tokens) <= 4 and prior is not None and prior.route == "finance"

    repaired["subject_kind"] = "none"
    repaired["subject"] = "none"
    # Use new so ConversationFrame.merge does not re-inherit the stale
    # merchant/category we just cleared.
    repaired["discourse_action"] = "new"

    if broad_correction and prior.intent not in {"none", ""}:
        repaired["intent"] = prior.intent
        if prior.output not in {"none", ""}:
            repaired["output"] = prior.output
    elif _front_controller_question_asks_why_spending_changed(tokens):
        repaired["intent"] = "spending_explain"
        repaired["output"] = "table"
    elif intent in {"", "none"}:
        repaired["intent"] = "spending_total"
        repaired["output"] = output or "scalar"

    time_value = str(repaired.get("time") or "").strip().lower()
    if prior is not None and prior.route == "finance":
        prior_time = _range_token_from_frame(prior) or prior.time
        if broad_correction and prior_time:
            repaired["time"] = prior_time
            if prior.time_a:
                repaired["time_a"] = prior.time_a
            if prior.time_b:
                repaired["time_b"] = prior.time_b
        elif time_value in {"", "none", "null", "unknown"} and prior_time and not has_explicit_time_scope(question):
            repaired["time"] = prior_time
            if prior.time_a:
                repaired["time_a"] = prior.time_a
            if prior.time_b:
                repaired["time_b"] = prior.time_b
    return repaired


def _front_controller_question_requests_broad_spending_scope(question: str, plan_fields: dict[str, str]) -> bool:
    tokens = words(question)
    if not tokens:
        return False
    if _front_controller_question_mentions_plan_subject(tokens, plan_fields):
        return False

    spending_terms = {"spending", "spend", "expenses", "expense", "transactions"}
    broad_modifiers = {"my", "our", "all", "overall", "total", "the"}
    if "all" in tokens and any(token in spending_terms for token in tokens):
        return True
    if "overall" in tokens and any(token in spending_terms for token in tokens):
        return True
    for idx, token in enumerate(tokens):
        if token not in spending_terms:
            continue
        if idx == 0:
            return True
        if tokens[idx - 1] in broad_modifiers:
            return True
    return False


def _front_controller_question_mentions_plan_subject(tokens: list[str], plan_fields: dict[str, str]) -> bool:
    subject = str(plan_fields.get("subject") or "").strip()
    subject_kind = str(plan_fields.get("subject_kind") or "").strip().lower()
    if subject_kind in {"", "none", "null", "unknown"} or subject.lower() in {"", "none", "null", "unknown"}:
        return False
    question_tokens = set(tokens)
    subject_tokens = [token for token in words(subject) if len(token) > 2]
    return any(token in question_tokens for token in subject_tokens)


def _front_controller_question_asks_why_spending_changed(tokens: list[str]) -> bool:
    return bool(set(tokens) & {"why", "higher", "lower", "up", "down", "increase", "increased", "decrease", "decreased"})


def _repair_front_controller_transaction_lookup_plan(*, question: str, plan_fields: dict[str, str]) -> dict[str, str]:
    repaired = dict(plan_fields)
    if str(repaired.get("route") or "").strip().lower() != "finance":
        return repaired
    if str(repaired.get("intent") or "").strip().lower() != "transaction_lookup":
        return repaired
    subject = str(repaired.get("subject") or "").strip()
    if _front_controller_income_subject_alias(subject):
        repaired["subject_kind"] = "category"
        repaired["subject"] = "Income"
    elif str(repaired.get("subject_kind") or "").strip().lower() in {"", "none", "unknown"} and subject.lower() in {"", "none", "null", "unknown"}:
        grounded_category = _front_controller_ground_category_scope(question)
        if grounded_category:
            repaired["subject_kind"] = "category"
            repaired["subject"] = grounded_category
    if not str(repaired.get("sort") or "").strip():
        sort = _front_controller_transaction_sort(question)
        if sort:
            repaired["sort"] = sort
    repaired.setdefault("time", "none")
    repaired.setdefault("output", "list")
    return repaired


def _front_controller_income_subject_alias(value: str) -> bool:
    return str(value or "").strip().lower() in {"income", "deposit", "paycheck", "payroll", "salary"}


def _front_controller_ground_category_scope(question: str) -> str:
    try:
        from mira.grounding import resolve_category_name

        return str(resolve_category_name(question) or "").strip()
    except Exception:
        return ""


def _front_controller_transaction_sort(question: str) -> str:
    try:
        from mira.grounding import normalize_text

        tokens = set(normalize_text(question).split())
    except Exception:
        tokens = set(str(question or "").lower().split())
    if tokens & {"biggest", "largest", "highest", "expensive", "priciest"}:
        return "amount_desc"
    return ""


def _repair_front_controller_finance_contract_plan(*, question: str, plan_fields: dict[str, str]) -> dict[str, str]:
    repaired = dict(plan_fields)
    if str(repaired.get("route") or "").strip().lower() != "finance":
        return repaired
    intent = str(repaired.get("intent") or "").strip().lower()
    question_text = str(question or "").strip().lower()
    if intent == "savings_capacity":
        repaired["output"] = "status"
    if intent in {"cashflow_forecast", "cashflow_shortfall"}:
        repaired["output"] = "status"
    if intent == "spending_compare" and str(repaired.get("output") or "").strip().lower() == "comparison":
        repaired["output"] = "table"
    if str(repaired.get("time") or "").strip().lower() != "custom":
        repaired.pop("time_a", None)
        repaired.pop("time_b", None)
    if intent == "cashflow_forecast" and "shortfall" in question_text:
        repaired["intent"] = "cashflow_shortfall"
        repaired["output"] = "status"
    return repaired


def _repair_front_controller_unsupported_time_plan(plan_fields: dict[str, str]) -> dict[str, str]:
    repaired = dict(plan_fields)
    if str(repaired.get("time") or "").strip().lower() == "last_n_months":
        repaired["time"] = "custom"
        repaired.pop("time_a", None)
        repaired.pop("time_b", None)
    return repaired


def _repair_front_controller_llm_temporal_plan(*, question: str, plan_fields: dict[str, str]) -> dict[str, str]:
    repaired = dict(plan_fields)
    route = str(repaired.get("route") or "").strip().lower()
    if route != "finance":
        return repaired
    time_value = str(repaired.get("time") or "").strip().lower()
    parsed = parse_range(question)
    parsed_time = _frame_time_from_range_token(str(parsed.token or "").strip())
    if parsed.explicit and parsed_time:
        time_token, time_a, time_b = parsed_time
        repaired["time"] = time_token
        if time_token == "custom":
            if not time_a:
                return repaired
            repaired["time_a"] = time_a
            if time_b:
                repaired["time_b"] = time_b
            else:
                repaired.pop("time_b", None)
        else:
            repaired.pop("time_a", None)
            repaired.pop("time_b", None)
        return _set_front_controller_plan_meta(
            repaired,
            temporal_parser_used="false",
            temporal_parser_llm_calls="0",
            temporal_parser_status="deterministic_range",
            temporal_parser_reason="range_parser",
        )
    needs_parser = (
        time_value in {"custom", "last_n_months"}
        and not str(repaired.get("time_a") or "").strip()
    ) or bool(parsed.explicit and parsed.unsupported_reason)
    if not needs_parser:
        return repaired
    try:
        from mira.agentic.temporal_parser import parse_temporal_range
    except Exception:
        return repaired

    result = parse_temporal_range(question)
    repaired = _set_front_controller_plan_meta(
        repaired,
        temporal_parser_used="true",
        temporal_parser_llm_calls=str(int(getattr(result, "llm_calls", 0) or 0)),
        temporal_parser_status=str(getattr(result, "status", "") or ""),
        temporal_parser_reason=str(getattr(result, "reason", "") or ""),
    )
    if not result.ok:
        return repaired
    if result.range_kind == "all_time":
        repaired["time"] = "all_time"
        repaired.pop("time_a", None)
        repaired.pop("time_b", None)
        return repaired
    if result.start_date and result.end_date:
        repaired["time"] = "custom"
        repaired["time_a"] = result.start_date
        repaired["time_b"] = result.end_date
    return repaired


def _set_front_controller_plan_meta(plan_fields: dict[str, str], **values: str) -> dict[str, str]:
    updated = dict(plan_fields)
    for key, value in values.items():
        updated[f"{_FRONT_CONTROLLER_META_PREFIX}{key}"] = str(value)
    return updated


def _front_controller_visible_plan_fields(plan_fields: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in plan_fields.items()
        if not str(key).startswith(_FRONT_CONTROLLER_META_PREFIX)
    }


def _front_controller_plan_meta(plan_fields: dict[str, Any]) -> dict[str, str]:
    return {
        str(key)[len(_FRONT_CONTROLLER_META_PREFIX) :]: str(value)
        for key, value in plan_fields.items()
        if str(key).startswith(_FRONT_CONTROLLER_META_PREFIX)
    }


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _front_controller_non_scalar_selector(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
    plan_fields: dict[str, str],
    latency_ms: float,
    controller_metrics: dict[str, Any] | None = None,
) -> Any | None:
    factories: list[Any] = []
    if _env_flag_enabled("MIRA_FRONT_CONTROLLER_READ_ONLY_TABLE_FAST_LANE_ENABLED"):
        factories.append(_front_controller_read_only_table_selector)
    if _env_flag_enabled("MIRA_FRONT_CONTROLLER_CHART_FAST_LANE_ENABLED"):
        factories.append(_front_controller_chart_selector)
    if _env_flag_enabled("MIRA_FRONT_CONTROLLER_EXPLAIN_COMPARE_FAST_LANE_ENABLED"):
        factories.append(_front_controller_explain_compare_selector)
    if _env_flag_enabled("MIRA_FRONT_CONTROLLER_WRITE_PREVIEW_FAST_LANE_ENABLED"):
        factories.append(_front_controller_write_preview_selector)
    for factory in factories:
        selector = factory(
            question=question,
            profile=profile,
            history=history,
            plan_fields=plan_fields,
            latency_ms=latency_ms,
            controller_metrics=controller_metrics,
        )
        if selector is not None:
            return selector
    if _env_flag_enabled("MIRA_EXPLAIN_LAST_FAST_LANE_ENABLED"):
        return _front_controller_explain_last_selector(
            plan_fields=plan_fields,
            latency_ms=latency_ms,
            controller_metrics=controller_metrics,
        )
    return None


def _env_flag_enabled(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() not in _FALSE_ENV_VALUES


def _followup_clarify_hardening_enabled() -> bool:
    return _env_flag_enabled("MIRA_FOLLOWUP_CLARIFY_HARDENING_ENABLED", "1")


def _pending_state_fast_resolver_enabled() -> bool:
    value = str(os.getenv("MIRA_PENDING_STATE_FAST_RESOLVER_ENABLED", "1")).strip().lower()
    return value not in _FALSE_ENV_VALUES


def _memory_slash_bypass_enabled() -> bool:
    value = str(os.getenv("MIRA_MEMORY_SLASH_BYPASS_ENABLED", "1")).strip().lower()
    return value not in _FALSE_ENV_VALUES


def _vnext_selector_fallback_enabled() -> bool:
    value = str(os.getenv("MIRA_VNEXT_SELECTOR_FALLBACK_ENABLED", "1")).strip().lower()
    return value not in _FALSE_ENV_VALUES


def _memory_slash_bypass_selector(*, question: str) -> Any | None:
    if not _memory_slash_bypass_enabled():
        return None
    try:
        from mira.agentic.vnext_selector import _memory_slash_selector_result

        selector = _memory_slash_selector_result(question=question, started=time.perf_counter())
    except Exception:
        return None
    if selector is None:
        return None
    trace = getattr(selector, "trace", {})
    if isinstance(trace, dict):
        return replace(
            selector,
            trace={
                **trace,
                "stage": "memory_slash_bypass",
                "memory_slash_bypass": True,
                "selector_skipped": True,
            },
        )
    return selector


def _pending_state_fast_selector(*, question: str, history: list[dict] | None) -> Any | None:
    if not _pending_state_fast_resolver_enabled():
        return None
    if not (
        _latest_pending_entity_clarification(history)
        or _latest_pending_amount_clarification(history)
    ):
        return None
    base_decision = {
        "route": "chat",
        "intent": "none",
        "subject": {"kind": "none"},
        "time": "none",
        "output": "status",
        "discourse_action": "clarification_reply",
        "answer": "",
    }
    resolved = _apply_pending_replies(base_decision, history, question)
    if resolved == base_decision:
        return None
    if not any(
        bool(resolved.get(key))
        for key in (
            "pending_amount_resolved",
            "pending_entity_resolved",
            "pending_clarification_error",
        )
    ):
        return None
    return _PendingStateSelector(decision=resolved)


def _routing_started_event() -> dict[str, Any]:
    return {
        "type": "routing_started",
        "stage": "routing",
        "label": "Routing the request",
    }


def _iter_front_controller_scalar_plan(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
    plan_fields: dict[str, str],
    latency_ms: float,
    fast_lane: str = "scalar",
    llm_calls: int = 1,
    controller_metrics: dict[str, Any] | None = None,
):
    plan_fields = _repair_front_controller_scalar_followup_plan(
        question=question,
        history=history,
        plan_fields=plan_fields,
    )
    selector = _FrontControllerPlanSelector(
        plan_fields=plan_fields,
        latency_ms=latency_ms,
        fast_lane=fast_lane,
        llm_calls=llm_calls,
        controller_metrics=controller_metrics,
    )
    validation = _validate_selector_safely(
        selector=selector,
        question=question,
        profile=profile,
        history=history,
    )
    route = _route_payload(
        question=question,
        history=history,
        forced_intent=None,
        selector=selector,
        validation=validation,
    )
    trace = route.get("trace") if isinstance(route.get("trace"), dict) else {}
    route["trace"] = {
        **trace,
        "front_controller_scalar_fast_lane": True,
        "front_controller_plan_fields": copy.deepcopy(_front_controller_visible_plan_fields(plan_fields)),
        "selector_skipped": True,
    }
    if _front_controller_scalar_clarify_allowed(route, validation):
        events: list[dict[str, Any]] = [_routing_started_event()]
        events.extend(
            [
                {"type": "route", **route},
                {
                    "type": "controller",
                    "act": route.get("controller_act"),
                    "controller_act": route.get("controller_act"),
                    "intent": route.get("intent"),
                    "confidence": route.get("confidence"),
                    "reason": "front_controller_scalar_fast_lane_clarify",
                    "legacy_router_used": False,
                    "mira_planner": _RUNTIME,
                },
                {
                    "type": "action",
                    "domain_action": route.get("domain_action"),
                    "tool_plan": route.get("tool_plan") or [],
                    "validation": route.get("validation"),
                    "grounded_entities": route.get("grounded_entities") or [],
                    "selected_tools": route.get("selected_tools") or [],
                },
            ]
        )
        try:
            from mira.agentic.vnext_answerer import VNextAnswerResult, safe_validation_answer

            answer_result = VNextAnswerResult(answer=safe_validation_answer(validation), path="clarify")
        except Exception:
            answer_result = None
        if answer_result is None:
            return False
        events.append(_done_event(route=route, validation=validation, evidence=EvidencePacket(question=question), answer_result=answer_result))
        for event in events:
            yield event
        return True

    if not _front_controller_scalar_route_allowed(route, validation):
        return False

    events: list[dict[str, Any]] = [_routing_started_event()]
    events.extend(
        [
            {"type": "route", **route},
            {
                "type": "controller",
                "act": route.get("controller_act"),
                "controller_act": route.get("controller_act"),
                "intent": route.get("intent"),
                "confidence": route.get("confidence"),
                "reason": "front_controller_scalar_fast_lane",
                "legacy_router_used": False,
                "mira_planner": _RUNTIME,
            },
            {
                "type": "action",
                "domain_action": route.get("domain_action"),
                "tool_plan": route.get("tool_plan") or [],
                "validation": route.get("validation"),
                "grounded_entities": route.get("grounded_entities") or [],
                "selected_tools": route.get("selected_tools") or [],
            },
            _progress_event(route),
        ]
    )

    evidence = EvidencePacket(question=question)
    try:
        from mira.agentic.vnext_executor import chart_from_evidence, iter_execute_vnext_events

        for event in iter_execute_vnext_events(
            validation,
            question=question,
            profile=profile,
            cache={},
        ):
            if event.get("type") == "evidence":
                evidence = event["evidence"]
            else:
                events.append(event)
        if chart_from_evidence(evidence):
            return False
    except Exception:
        return False

    if not _front_controller_evidence_is_direct_scalar(evidence):
        return False
    try:
        from mira.agentic.direct_renderer import try_direct_scalar_answer
        from mira.agentic.vnext_answerer import VNextAnswerResult

        direct = try_direct_scalar_answer(question, evidence)
        if not direct:
            return False
        answer_result = VNextAnswerResult(answer=direct, path="direct_scalar")
    except Exception:
        return False

    done = _done_event(route=route, validation=validation, evidence=evidence, answer_result=answer_result)
    events.append(done)
    memory_update = _memory_suggestion_update_event(
        question=question,
        answer=str(done.get("answer") or ""),
        route=route,
        validation=validation,
        done=done,
    )
    if memory_update:
        events.append(memory_update)

    for event in events:
        yield event
    return True


def _front_controller_scalar_clarify_allowed(route: dict[str, Any], validation: ValidationResult) -> bool:
    if validation.status != "clarify":
        return False
    selected_tools = route.get("selected_tools") if isinstance(route.get("selected_tools"), list) else []
    if selected_tools:
        return False
    frame = _route_conversation_frame_payload(route)
    if str(frame.get("route") or "").strip().lower() != "finance":
        return False
    if str(frame.get("intent") or "").strip().lower() != "spending_total":
        return False
    if str(frame.get("output") or "").strip().lower() != "scalar":
        return False
    return bool(str(validation.clarification_question or "").strip())


def _repair_front_controller_scalar_followup_plan(
    *,
    question: str,
    history: list[dict] | None,
    plan_fields: dict[str, str],
) -> dict[str, str]:
    """Apply typed follow-up facts before scalar fast-lane validation.

    The front-controller decides whether the turn is a scalar finance plan.
    Once that is true, latest-message time phrases and the latest typed finance
    subject are deterministic state, so they should not depend on the model
    copying the right prior row from visible chat.
    """

    repaired = dict(plan_fields)
    prior = _latest_mira_conversation_frame(history)
    action = str(repaired.get("discourse_action") or "").strip().lower()
    if (
        prior is not None
        and action == "correction"
        and prior.route == "finance"
        and prior.intent == "spending_total"
        and prior.output == "scalar"
    ):
        repaired["route"] = "finance"
        repaired["intent"] = "spending_total"
        repaired["output"] = "scalar"
        if not str(repaired.get("time") or "").strip() or str(repaired.get("time") or "").strip().lower() in {"none", "null", "unknown"}:
            repaired["time"] = prior.time
        if not str(repaired.get("subject") or "").strip() and not prior.subject.is_empty:
            repaired["subject_kind"] = prior.subject.kind
            repaired["subject"] = (
                prior.subject.text
                or prior.subject.display_name
                or prior.subject.canonical_id
                or ""
            )
    range_only_followup = _scalar_question_is_range_only_followup(question)
    time_override = _scalar_followup_time_override(question)
    if time_override:
        repaired["time"] = time_override
        if prior is not None:
            repaired["discourse_action"] = "follow_up"
    if prior is not None and range_only_followup:
        if prior.route == "finance" and prior.intent == "spending_total" and prior.output == "scalar":
            repaired["route"] = "finance"
            repaired["intent"] = "spending_total"
            repaired["output"] = "scalar"
        if prior.route == "finance" and not prior.subject.is_empty:
            repaired["subject_kind"] = prior.subject.kind
            repaired["subject"] = (
                prior.subject.text
                or prior.subject.display_name
                or prior.subject.canonical_id
                or repaired.get("subject")
                or ""
            )
            repaired["discourse_action"] = "follow_up"
    if (
        prior is not None
        and prior.route == "finance"
        and prior.intent == "spending_total"
        and prior.output == "scalar"
    ):
        subject_text = str(repaired.get("subject") or "").strip()
        subject_kind = str(repaired.get("subject_kind") or "").strip().lower()
        if subject_text and subject_text.lower() not in {"none", "null", "unknown"} and subject_kind in {"", "none", "null", "unknown"}:
            repaired["route"] = "finance"
            repaired["intent"] = "spending_total"
            repaired["output"] = "scalar"
            repaired["subject_kind"] = "unknown"
            repaired["discourse_action"] = "follow_up"
            if not str(repaired.get("time") or "").strip() or str(repaired.get("time") or "").strip().lower() in {"none", "null", "unknown"}:
                repaired["time"] = _range_token_from_frame(prior) or prior.time
    return repaired


def _stateful_scalar_subject_followup_plan_fields(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
) -> dict[str, str]:
    if not _followup_clarify_hardening_enabled():
        return {}
    if not _env_flag_enabled("MIRA_FRONT_CONTROLLER_FINANCE_SCALAR_FAST_LANE_ENABLED"):
        return {}
    prior = _latest_mira_conversation_frame(history)
    if (
        prior is None
        or prior.route != "finance"
        or prior.intent != "spending_total"
        or prior.output != "scalar"
    ):
        return {}
    subject_text = _stateful_scalar_subject_query_text(question)
    if not subject_text:
        return {}

    time_value = _scalar_followup_time_override(question) or _range_token_from_frame(prior) or prior.time
    if not time_value or str(time_value).strip().lower() in {"none", "null", "unknown"}:
        return {}

    probe_frame = ConversationFrame(
        route="finance",
        intent="spending_total",
        subject=MiraSubject(kind="unknown", text=subject_text),
        time=time_value,
        time_a=prior.time_a if time_value == _range_token_from_frame(prior) else None,
        time_b=prior.time_b if time_value == _range_token_from_frame(prior) else None,
        output="scalar",
    )
    try:
        from mira.agentic.entity_grounder import ground_conversation_frame

        grounded = ground_conversation_frame(probe_frame, profile=profile, source_text=question)
    except Exception:
        return {}

    subject_kind = "unknown"
    plan_subject = subject_text
    if grounded.status == "clarify":
        pending = grounded.pending_clarification if isinstance(grounded.pending_clarification, dict) else {}
        options = pending.get("options") if isinstance(pending.get("options"), list) else []
        if pending.get("kind") != "entity_resolution" or not options:
            return {}
    elif grounded.ok and grounded.frame is not None:
        subject = grounded.frame.subject
        if subject.kind not in {"merchant", "category"} or subject.is_empty:
            return {}
        subject_kind = subject.kind
        plan_subject = (
            subject.text
            or subject.display_name
            or subject.canonical_id
            or subject_text
        )
    else:
        return {}

    return {
        "route": "finance",
        "intent": "spending_total",
        "subject_kind": subject_kind,
        "subject": plan_subject,
        "time": time_value,
        "discourse_action": "follow_up",
        "output": "scalar",
    }


_STATEFUL_FOLLOWUP_NON_ENTITY_TOKENS = {
    "about",
    "after",
    "again",
    "all",
    "before",
    "current",
    "for",
    "month",
    "months",
    "next",
    "period",
    "previous",
    "prior",
    "same",
    "that",
    "then",
    "there",
    "time",
    "week",
    "weeks",
    "year",
    "years",
}


def _stateful_scalar_subject_query_text(question: str) -> str:
    try:
        from mira.grounding import significant_tokens

        tokens = significant_tokens(question, query=True)
    except Exception:
        tokens = words(question)
    cleaned = [
        token
        for token in tokens
        if token
        and token not in _STATEFUL_FOLLOWUP_NON_ENTITY_TOKENS
        and not re.match(r"^\d{4}$", token)
    ]
    return " ".join(cleaned).strip()


def _scalar_followup_time_override(question: str) -> str:
    lowered = " ".join(str(question or "").strip().lower().split())
    if not lowered:
        return ""
    if any(phrase in lowered for phrase in ("month before", "before that", "previous period")):
        return "month_before_prior"
    if any(phrase in lowered for phrase in ("month after", "after that", "next period")):
        return "next_month_after_prior"
    correction_tail = _scalar_time_correction_tail(question)
    if correction_tail:
        tail_override = _scalar_time_override_from_range_parse(correction_tail)
        if tail_override:
            return tail_override
        return ""
    return _scalar_time_override_from_range_parse(question)


def _scalar_time_correction_tail(question: str) -> str:
    text = str(question or "").strip()
    lowered = text.lower()
    if not lowered:
        return ""
    if "not " not in lowered:
        return ""
    for separator in (",", ";"):
        if separator in text:
            tail = text.rsplit(separator, 1)[-1].strip()
            if tail:
                return tail
    for marker in (" instead ", " but "):
        if marker in lowered:
            tail = text[lowered.rfind(marker) + len(marker):].strip()
            if tail:
                return tail
    return ""


def _scalar_time_override_from_range_parse(question: str) -> str:
    parsed = parse_range(question)
    token = str(parsed.token or "").strip().lower()
    if not parsed.explicit or not token:
        return ""
    if token == "all":
        return "all_time"
    if token == "current_month":
        return "this_month"
    if re.match(r"^\d{4}-\d{2}$", token):
        return token
    if token in {
        "last_month",
        "last_week",
        "this_week",
        "ytd",
        "last_year",
        "last_7d",
        "last_30d",
        "last_90d",
        "last_365d",
        "last_3_months",
        "last_6_months",
    } or re.match(r"^last_(\d{1,3})d$", token) or re.match(r"^last_(\d{1,2})_months$", token):
        return token
    return ""


def _scalar_question_is_range_only_followup(question: str) -> bool:
    if not has_explicit_time_scope(question):
        return False
    allowed = {
        "what",
        "how",
        "about",
        "the",
        "a",
        "an",
        "for",
        "in",
        "on",
        "during",
        "same",
        "period",
        "range",
        "month",
        "week",
        "year",
        "before",
        "after",
        "previous",
        "prior",
        "next",
        "last",
        "this",
        "current",
        "all",
        "time",
        "that",
        "then",
        "i",
        "meant",
        "mean",
        "actually",
        "rather",
        "instead",
        "sorry",
        "jan",
        "january",
        "feb",
        "february",
        "mar",
        "march",
        "apr",
        "april",
        "may",
        "jun",
        "june",
        "jul",
        "july",
        "aug",
        "august",
        "sep",
        "sept",
        "september",
        "oct",
        "october",
        "nov",
        "november",
        "dec",
        "december",
    }
    tokens = words(question)
    return bool(tokens) and all(token in allowed or re.match(r"^\d{4}$", token) for token in tokens)


def _front_controller_scalar_route_allowed(route: dict[str, Any], validation: ValidationResult) -> bool:
    if validation.status != "ready":
        return False
    selected_tools = route.get("selected_tools") if isinstance(route.get("selected_tools"), list) else []
    if selected_tools != ["summarize_spending"]:
        return False
    frame = _route_conversation_frame_payload(route)
    if str(frame.get("route") or "").strip().lower() != "finance":
        return False
    if str(frame.get("intent") or "").strip().lower() != "spending_total":
        return False
    if str(frame.get("output") or "").strip().lower() != "scalar":
        return False
    subject = frame.get("subject") if isinstance(frame.get("subject"), dict) else {}
    if str(subject.get("kind") or "").strip().lower() not in {"merchant", "category"}:
        return False
    if not str(subject.get("text") or "").strip():
        return False
    if str(frame.get("time") or "").strip().lower() in {"", "none"}:
        return False
    if len(validation.normalized_plan) != 1:
        return False
    step = validation.normalized_plan[0]
    args = step.args if isinstance(step.args, dict) else {}
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    return (
        step.tool_name == "summarize_spending"
        and str(args.get("view") or "").strip().lower() == "entity_total"
        and bool(filters.get("merchant") or filters.get("category"))
    )


def _route_conversation_frame_payload(route: dict[str, Any]) -> dict[str, Any]:
    frame = route.get("mira_conversation_frame") if isinstance(route.get("mira_conversation_frame"), dict) else {}
    if frame:
        return frame
    return route.get("intent_frame") if isinstance(route.get("intent_frame"), dict) else {}


def _front_controller_evidence_is_direct_scalar(evidence: EvidencePacket) -> bool:
    if len(evidence.tool_results) != 1:
        return False
    record = evidence.tool_results[0]
    if record.get("status") == "error":
        return False
    execution_tool_name = str(record.get("execution_tool_name") or "").strip()
    return execution_tool_name in {"get_merchant_spend", "get_category_spend"}


def _front_controller_read_only_table_selector(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
    plan_fields: dict[str, str],
    latency_ms: float,
    controller_metrics: dict[str, Any] | None = None,
) -> Any | None:
    selector = _FrontControllerPlanSelector(
        plan_fields=plan_fields,
        latency_ms=latency_ms,
        fast_lane="read_only_table",
        controller_metrics=controller_metrics,
    )
    validation = _validate_selector_safely(
        selector=selector,
        question=question,
        profile=profile,
        history=history,
    )
    route = _route_payload(
        question=question,
        history=history,
        forced_intent=None,
        selector=selector,
        validation=validation,
    )
    if not _front_controller_read_only_table_route_allowed(route, validation):
        return None
    return selector


def _front_controller_read_only_table_route_allowed(route: dict[str, Any], validation: ValidationResult) -> bool:
    if validation.status != "ready":
        return False
    frame = route.get("intent_frame") if isinstance(route.get("intent_frame"), dict) else {}
    if str(frame.get("route") or "").strip().lower() != "finance":
        return False
    intent = str(frame.get("intent") or "").strip().lower()
    if intent not in {
        "transaction_lookup",
        "spending_top",
        "spending_breakdown",
        "budget_status",
        "budget_plan",
        "savings_capacity",
        "recurring_summary",
        "recurring_changes",
    }:
        return False
    if str(frame.get("output") or "").strip().lower() not in {"table", "list", "status", "scalar"}:
        return False
    selected_tools = [step.tool_name for step in validation.normalized_plan]
    primary_tools = _without_optional_financial_context_tools(selected_tools)
    if len(primary_tools) != 1:
        return False
    if any(tool in _MEMORY_TOOL_NAMES or tool in {"run_sql", "make_chart"} for tool in selected_tools):
        return False
    return primary_tools[0] in {
        "query_transactions",
        "summarize_spending",
        "review_budget",
        "review_recurring",
    }


def _front_controller_chart_selector(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
    plan_fields: dict[str, str],
    latency_ms: float,
    controller_metrics: dict[str, Any] | None = None,
) -> Any | None:
    selector = _FrontControllerPlanSelector(
        plan_fields=plan_fields,
        latency_ms=latency_ms,
        fast_lane="chart",
        controller_metrics=controller_metrics,
    )
    validation = _validate_selector_safely(
        selector=selector,
        question=question,
        profile=profile,
        history=history,
    )
    route = _route_payload(
        question=question,
        history=history,
        forced_intent=None,
        selector=selector,
        validation=validation,
    )
    if not _front_controller_chart_route_allowed(route, validation):
        return None
    return selector


def _front_controller_chart_route_allowed(route: dict[str, Any], validation: ValidationResult) -> bool:
    if validation.status != "ready":
        return False
    frame = route.get("intent_frame") if isinstance(route.get("intent_frame"), dict) else {}
    if str(frame.get("route") or "").strip().lower() != "finance":
        return False
    if str(frame.get("output") or "").strip().lower() != "chart":
        return False
    if str(frame.get("intent") or "").strip().lower() not in {"spending_trend", "net_worth_trend"}:
        return False
    if len(validation.normalized_plan) != 2:
        return False
    selected_tools = [step.tool_name for step in validation.normalized_plan]
    if selected_tools[1] != "make_chart":
        return False
    if any(tool in _MEMORY_TOOL_NAMES or tool == "run_sql" for tool in selected_tools):
        return False
    return selected_tools[0] in {"summarize_spending", "review_net_worth"}


def _front_controller_explain_compare_selector(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
    plan_fields: dict[str, str],
    latency_ms: float,
    controller_metrics: dict[str, Any] | None = None,
) -> Any | None:
    selector = _FrontControllerPlanSelector(
        plan_fields=plan_fields,
        latency_ms=latency_ms,
        fast_lane="explain_compare",
        controller_metrics=controller_metrics,
    )
    validation = _validate_selector_safely(
        selector=selector,
        question=question,
        profile=profile,
        history=history,
    )
    route = _route_payload(
        question=question,
        history=history,
        forced_intent=None,
        selector=selector,
        validation=validation,
    )
    if not _front_controller_explain_compare_route_allowed(route, validation):
        return None
    return selector


def _front_controller_explain_compare_route_allowed(route: dict[str, Any], validation: ValidationResult) -> bool:
    frame = route.get("intent_frame") if isinstance(route.get("intent_frame"), dict) else {}
    if str(frame.get("route") or "").strip().lower() != "finance":
        return False
    intent = str(frame.get("intent") or "").strip().lower()
    if intent not in {
        "spending_explain",
        "spending_compare",
        "cashflow_forecast",
        "cashflow_shortfall",
        "affordability",
        "net_worth_balance",
        "net_worth_delta",
        "finance_snapshot",
        "finance_priorities",
        "explain_metric",
        "data_health",
        "enrichment_quality",
        "low_confidence_transactions",
        "explain_transaction",
    }:
        return False
    if str(frame.get("output") or "").strip().lower() in {"chart", "preview"}:
        return False
    if validation.status == "clarify":
        return intent == "affordability"
    if validation.status != "ready":
        return False
    selected_tools = [step.tool_name for step in validation.normalized_plan]
    if not selected_tools:
        return False
    if any(tool in _MEMORY_TOOL_NAMES or tool == "run_sql" for tool in selected_tools):
        return False
    if any(str(tool or "").startswith("preview_") or tool == "preview_finance_change" for tool in selected_tools):
        return False
    primary_tools = _without_optional_financial_context_tools(selected_tools)
    if not primary_tools:
        return False
    return all(
        tool in {
            "query_transactions",
            "summarize_spending",
            "review_cashflow",
            "check_affordability",
            "review_net_worth",
            "finance_overview",
            "review_data_quality",
        }
        for tool in primary_tools
    )


def _without_optional_financial_context_tools(selected_tools: list[str]) -> list[str]:
    return [tool for tool in selected_tools if tool != "review_financial_context"]


def _front_controller_write_preview_selector(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
    plan_fields: dict[str, str],
    latency_ms: float,
    controller_metrics: dict[str, Any] | None = None,
) -> Any | None:
    selector = _FrontControllerPlanSelector(
        plan_fields=plan_fields,
        latency_ms=latency_ms,
        fast_lane="write_preview",
        controller_metrics=controller_metrics,
    )
    validation = _validate_selector_safely(
        selector=selector,
        question=question,
        profile=profile,
        history=history,
    )
    route = _route_payload(
        question=question,
        history=history,
        forced_intent=None,
        selector=selector,
        validation=validation,
    )
    if not _front_controller_write_preview_route_allowed(route, validation):
        return None
    return selector


def _front_controller_write_preview_route_allowed(route: dict[str, Any], validation: ValidationResult) -> bool:
    if validation.status != "ready":
        return False
    frame = route.get("intent_frame") if isinstance(route.get("intent_frame"), dict) else {}
    if str(frame.get("route") or "").strip().lower() != "write_preview":
        return False
    if str(frame.get("intent") or "").strip().lower() != "write_preview":
        return False
    if str(frame.get("output") or "").strip().lower() != "preview":
        return False
    if len(validation.normalized_plan) != 1:
        return False
    step = validation.normalized_plan[0]
    if step.tool_name != "preview_finance_change":
        return False
    args = step.args if isinstance(step.args, dict) else {}
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    if str(args.get("view") or "").strip().lower() != "preview":
        return False
    change_type = str(payload.get("change_type") or "").strip().lower()
    if change_type == "bulk_recategorize":
        return bool(str(payload.get("merchant") or "").strip() and str(payload.get("category") or "").strip())
    if change_type == "create_rule":
        return bool(str(payload.get("pattern") or "").strip() and str(payload.get("category") or "").strip())
    if change_type == "set_budget":
        return bool(str(payload.get("category") or "").strip() and str(payload.get("amount") or "").strip())
    return False


def _front_controller_explain_last_selector(
    *,
    plan_fields: dict[str, str],
    latency_ms: float,
    controller_metrics: dict[str, Any] | None = None,
) -> Any | None:
    try:
        from mira.agentic.front_controller import explain_last_fast_lane_enabled, explain_last_plan_ready
    except Exception:
        return None
    if not explain_last_fast_lane_enabled() or not explain_last_plan_ready(plan_fields):
        return None
    return _ExplainLastFastSelector(
        plan_fields=plan_fields,
        latency_ms=latency_ms,
        controller_metrics=controller_metrics,
    )


def _prepare_vnext_turn(
    *,
    question: str,
    profile: str | None,
    history: list[dict] | None,
    forced_intent: str | None,
    selector_override: Any | None = None,
    selector_fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if selector_override is None:
        selector = _run_selector_safely(question=question, history=history, profile=profile)
        selector = _apply_pending_reply_selector(selector, history, question)
    else:
        selector = selector_override
    validation = _validate_selector_safely(
        selector=selector,
        question=question,
        profile=profile,
        history=history,
    )
    route = _route_payload(
        question=question,
        history=history,
        forced_intent=forced_intent,
        selector=selector,
        validation=validation,
    )
    if selector_fallback:
        trace = route.get("trace") if isinstance(route.get("trace"), dict) else {}
        route["trace"] = {**trace, **selector_fallback}
    return {"selector": selector, "validation": validation, "route": route}


def _route_payload(
    *,
    question: str,
    history: list[dict] | None,
    forced_intent: str | None,
    selector: Any,
    validation: ValidationResult,
) -> dict[str, Any]:
    calls = list(getattr(selector, "calls", []) or [])
    selector_status = str(getattr(selector, "status", "") or "clarify")
    selector_decision = getattr(selector, "decision", {})
    if not isinstance(selector_decision, dict):
        selector_decision = {}
    selector_decision = _apply_pending_replies(selector_decision, history, question)
    intent_frame = selector_decision.get("intent_frame") if isinstance(selector_decision.get("intent_frame"), dict) else {}
    mira_conversation_frame = _merged_conversation_frame_from_decision(selector_decision, history, question=question)
    controller_route = str(selector_decision.get("controller_route") or selector_decision.get("route") or "").strip()
    controller_intent = str(selector_decision.get("intent") or "").strip()
    selected_tools = [step.tool_name for step in validation.normalized_plan if step.tool_name and step.tool_name != "run_sql"]
    tool_plan = [step.to_dict() for step in validation.normalized_plan]
    intent = controller_intent or ("finance" if selected_tools and not _memory_only(selected_tools) else "chat")
    trace = {
        **(getattr(selector, "trace", {}) if isinstance(getattr(selector, "trace", {}), dict) else {}),
        "runtime": _RUNTIME,
        "phase": "validated_selector",
        "forced_intent": forced_intent,
        "validation_status": validation.status,
        "grounded_entity_count": len(validation.grounded_entities),
    }
    controller_act = _controller_act_for_status(validation.status, selected_tools)
    operation = _operation_for_status(selector_status, validation.status, selected_tools)
    return {
        "question": question,
        "intent": intent,
        "operation": operation,
        "controller_route": controller_route,
        "needs_folio_evidence": bool(selector_decision.get("needs_folio_evidence")) if selector_decision else bool(selected_tools),
        "uses_history": bool(history),
        "confidence": validation.decision.confidence,
        "needs_clarification": validation.status == "clarify",
        "clarification_question": validation.clarification_question,
        "pending_clarification": validation.pending_clarification,
        "intent_frame": intent_frame,
        "mira_conversation_frame": mira_conversation_frame.to_dict() if mira_conversation_frame else {},
        "controller_act": controller_act,
        "agent_decision": validation.decision.to_dict(),
        "tool_plan": tool_plan,
        "validation": validation.to_dict(),
        "grounded_entities": validation.grounded_entities,
        "selected_tools": selected_tools,
        "domain_action": {
            "name": "vnext_selector",
            "status": validation.status,
            "tool_plan": tool_plan,
            "blocked_reason": validation.blocked_reason,
            "clarification_question": validation.clarification_question,
            "pending_clarification": validation.pending_clarification,
        },
        "selector": {
            "status": selector_status,
            "decision": selector_decision,
            "calls": calls,
            "repair_used": bool(getattr(selector, "repair_used", False)),
            "family_detail_used": bool(getattr(selector, "family_detail_used", False)),
            "raw_response": str(getattr(selector, "raw", "") or ""),
            "intent_frame": intent_frame,
            "mira_conversation_frame": mira_conversation_frame.to_dict() if mira_conversation_frame else {},
            "intent_frame_source": selector_decision.get("intent_frame_source"),
            "intent_frame_error": selector_decision.get("intent_frame_error"),
        },
        "trace": trace,
        "llm_calls": int(getattr(selector, "llm_calls", 0) or 0),
        "mira_planner": _RUNTIME,
        "legacy_router_used": False,
    }


def _done_event(
    *,
    route: dict[str, Any],
    validation: ValidationResult,
    evidence: EvidencePacket,
    answer_result: Any,
) -> dict[str, Any]:
    from mira.agentic.vnext_executor import (
        chart_from_evidence,
        data_from_evidence,
        evidence_summary,
        pending_write_from_evidence,
        tool_trace_from_evidence,
    )

    trace = route.get("trace") if isinstance(route.get("trace"), dict) else {}
    trace = _done_trace(trace, validation=validation, evidence=evidence, answer_result=answer_result)
    selected_tools = route.get("selected_tools") if isinstance(route.get("selected_tools"), list) else []
    pending_write = pending_write_from_evidence(evidence)
    data, data_source = data_from_evidence(evidence, pending_write)
    chart_payload = chart_from_evidence(evidence)
    event = {
        "type": "done",
        "answer": getattr(answer_result, "answer", "") or _answer_for_route(route, validation, evidence),
        "data": data,
        "data_source": data_source,
        "tool_trace": tool_trace_from_evidence(evidence),
        "rows_total": _evidence_total_row_count(evidence, data=data),
        "iterations": 0,
        "error": _user_visible_error(validation) if validation.status == "blocked" else None,
        "route": route,
        "intent": route.get("intent") or "chat",
        "agent_decision": validation.decision.to_dict(),
        "validation": validation.to_dict(),
        "evidence": evidence_summary(evidence),
        "provenance": evidence.provenance,
        "selected_tools": selected_tools,
        "grounded_entities": validation.grounded_entities,
        "pending_clarification": validation.pending_clarification,
        "answer_context": _answer_context_from_validation(validation, evidence, route=route),
        "trace": trace,
        "llm_calls": int(route.get("llm_calls") or 0) + int(getattr(answer_result, "llm_calls", 0) or 0),
        "legacy_router_used": False,
        "answer_guard": {
            "path": getattr(answer_result, "path", ""),
            "used_fallback": bool(getattr(answer_result, "used_fallback", False)),
            "error": getattr(answer_result, "error", ""),
            "cache_hit": bool(getattr(answer_result, "cache_hit", False)),
        },
    }
    if pending_write:
        event["pending_write"] = pending_write
    if chart_payload:
        event["chart"] = chart_payload
    event["route"] = {**route, "trace": trace}
    return event


def _mark_evidence_attach_only(
    done: dict[str, Any],
    *,
    displayed_answer: str,
    saw_streamed_answer_token: bool,
    saw_answer_reset: bool,
) -> None:
    if not _evidence_attach_stability_enabled():
        return
    if not saw_streamed_answer_token or saw_answer_reset:
        return
    answer_guard = done.get("answer_guard") if isinstance(done.get("answer_guard"), dict) else {}
    if str(answer_guard.get("path") or "") != "evidence_llm":
        return
    if not _same_display_text(done.get("answer"), displayed_answer):
        return
    done["evidence_attach_only"] = True
    done["answer_guard"] = {**answer_guard, "evidence_attach_only": True}


def _evidence_attach_stability_enabled() -> bool:
    return str(os.getenv("MIRA_EVIDENCE_ATTACH_STABILITY_ENABLED", "true")).strip().lower() not in _FALSE_ENV_VALUES


def _complex_finance_react_lite_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_COMPLEX_FINANCE_REACT_LITE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def _complex_finance_preview_llm_enabled() -> bool:
    return str(os.getenv("MIRA_COMPLEX_FINANCE_PREVIEW_LLM_ENABLED", "1")).strip().lower() not in _FALSE_ENV_VALUES


def _complex_finance_preview_only_enabled() -> bool:
    return str(os.getenv("MIRA_COMPLEX_FINANCE_PREVIEW_ONLY_ENABLED", "1")).strip().lower() not in _FALSE_ENV_VALUES


def _maybe_schedule_front_controller_rewarm(
    *,
    question: str,
    history: list[dict] | None,
    done: dict[str, Any],
) -> dict[str, Any]:
    answer_guard = done.get("answer_guard") if isinstance(done.get("answer_guard"), dict) else {}
    if str(answer_guard.get("path") or "") != "evidence_llm":
        return {}
    trace = done.get("trace") if isinstance(done.get("trace"), dict) else {}
    if int(trace.get("answer_llm_calls") or 0) <= 0:
        return {}
    try:
        from mira.agentic.front_controller import schedule_front_controller_rewarm

        return schedule_front_controller_rewarm(
            question=question,
            history=history,
            reason="after_evidence_answer",
        )
    except Exception as exc:
        return {"scheduled": False, "reason": "schedule_error", "error": str(exc)}


def _memory_preference_context_enabled() -> bool:
    return str(os.getenv("MIRA_MEMORY_PREFERENCE_CONTEXT_ENABLED", "true")).strip().lower() not in _FALSE_ENV_VALUES


def _session_summary_context_enabled() -> bool:
    return _env_flag_enabled("MIRA_SESSION_SUMMARY_CONTEXT_ENABLED", "0")


def _memory_suggestions_enabled() -> bool:
    return str(os.getenv("MIRA_MEMORY_SUGGESTIONS_ENABLED", "true")).strip().lower() not in _FALSE_ENV_VALUES


def _memory_suggestions_on_evidence_enabled() -> bool:
    return _env_flag_enabled("MIRA_MEMORY_SUGGESTIONS_ON_EVIDENCE_ENABLED", "0")


def _memory_suggestion_update_event(
    *,
    question: str,
    answer: str,
    route: dict[str, Any],
    validation: ValidationResult,
    done: dict[str, Any],
) -> dict[str, Any] | None:
    if not _memory_suggestions_enabled():
        return None
    if validation.status != "ready":
        return None
    answer_guard = done.get("answer_guard") if isinstance(done.get("answer_guard"), dict) else {}
    answer_path = str(answer_guard.get("path") or "")
    if answer_path not in {"general_answer", "selector_inline", "evidence_llm"}:
        return None
    if answer_path == "evidence_llm" and not _memory_suggestions_on_evidence_enabled():
        return None
    if done.get("pending_write") or done.get("chart"):
        return None
    try:
        from mira import memory_v2

        suggested = memory_v2.suggest_memory_candidate(text=question, answer=answer, route=route)
    except Exception:
        return None
    if not suggested:
        return None
    return {"type": "memory_update", "suggested_memory": suggested}


def _memory_context_provider_for_turn(
    *,
    question: str,
    profile: str | None,
    route: dict[str, Any],
) -> Callable[[str], dict[str, Any]]:
    def provide(answer_path: str) -> dict[str, Any]:
        if answer_path not in {"evidence_llm", "general_answer"}:
            return _empty_memory_context("answer_path_not_eligible")
        if _route_disallows_answer_memory_context(route):
            return _empty_memory_context("route_not_eligible_for_memory_context")
        memory_enabled = _memory_preference_context_enabled()
        advisor_enabled = _advisor_lens_context_enabled()
        if not memory_enabled and not advisor_enabled:
            return _empty_memory_context("answer_context_disabled")
        try:
            from database import get_db
            from mira import memory_v2

            with get_db() as conn:
                contexts: list[dict[str, Any]] = []
                if memory_enabled:
                    result = memory_v2.retrieve_relevant_memories(
                        conn=conn,
                        profile=profile,
                        question=question,
                        route=route,
                        limit=3,
                    )
                    contexts.append(memory_v2.answer_prompt_context_from_packet(result.get("compact_memory"), max_tokens=80))
                    if _session_summary_context_enabled():
                        session_result = memory_v2.retrieve_relevant_session_summaries(
                            conn=conn,
                            profile=profile,
                            question=question,
                            route=route,
                            limit=2,
                        )
                        contexts.append(
                            memory_v2.session_summary_prompt_context_from_packet(
                                session_result.get("compact_session_summaries"),
                                max_tokens=80,
                            )
                        )
                advisor_context = _advisor_lens_context_for_answer(
                    conn=conn,
                    profile=profile,
                    question=question,
                    route=route,
                    enabled=advisor_enabled,
                )
                if advisor_context.get("block") or advisor_context.get("reason"):
                    contexts.append(advisor_context)
                if contexts:
                    context = memory_v2.merge_answer_contexts(
                        *contexts,
                        max_tokens=max(140, _advisor_lens_context_max_tokens() if advisor_enabled else 140),
                    )
                else:
                    context = _empty_memory_context("no_answer_context")
            return context if isinstance(context, dict) else _empty_memory_context("memory_context_unavailable")
        except Exception as exc:
            return _empty_memory_context(f"memory_context_error:{exc}")

    return provide


def _route_disallows_answer_memory_context(route: dict[str, Any] | None) -> bool:
    route = route if isinstance(route, dict) else {}
    operation = str(route.get("operation") or route.get("intent") or "").strip().lower()
    output = str(route.get("output") or "").strip().lower()
    selected_tools = route.get("selected_tools") if isinstance(route.get("selected_tools"), list) else []
    tool_names = {str(tool or "").strip().lower() for tool in selected_tools}
    if output == "chart" or tool_names & {"make_chart", "plot_chart"}:
        return True
    if operation in {"write_preview", "manage_memory", "remember_user_context", "retrieve_relevant_memories", "list_mira_memories"}:
        return True
    if str(route.get("controller_act") or "").strip().lower() in {"front_controller_chat"}:
        return True
    return False


def _empty_memory_context(reason: str) -> dict[str, Any]:
    return {"block": "", "used": False, "count": 0, "reason": str(reason or "memory_context_unused")}


def _advisor_lens_context_enabled() -> bool:
    return _env_flag_enabled("MIRA_ADVISOR_LENS_CONTEXT_ENABLED", "0")


def _advisor_lens_context_max_tokens() -> int:
    try:
        return max(140, min(int(os.getenv("MIRA_ADVISOR_LENS_CONTEXT_MAX_TOKENS", "520")), 900))
    except (TypeError, ValueError):
        return 520


def _advisor_lens_context_for_answer(
    *,
    conn,
    profile: str | None,
    question: str,
    route: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return _empty_memory_context("advisor_context_disabled")
    try:
        from mira.advisor_lens_synthesis import advisor_lens_answer_context

        return advisor_lens_answer_context(conn=conn, profile=profile, question=question, route=route)
    except Exception as exc:
        return _empty_memory_context(f"advisor_context_error:{exc}")


def _same_display_text(left: object, right: object) -> bool:
    return " ".join(str(left or "").split()) == " ".join(str(right or "").split())


def _complex_finance_evidence_preview_event(
    *,
    question: str,
    route: dict[str, Any],
    validation: ValidationResult,
    evidence: EvidencePacket,
) -> dict[str, Any] | None:
    """Expose final deterministic evidence before answer prose for read-only finance.

    This is Phase 18.7's no-regression slice: it does not change routing or
    tool choice. The selector/compiler/validator/executor have already run.
    """

    if not _complex_finance_react_lite_enabled():
        return None
    if validation.status != "ready":
        return None
    frame = route.get("mira_conversation_frame") if isinstance(route.get("mira_conversation_frame"), dict) else {}
    if str(frame.get("route") or route.get("intent") or "").strip().lower() != "finance":
        return None
    selected_tools = route.get("selected_tools") if isinstance(route.get("selected_tools"), list) else []
    if not _read_only_complex_finance_tools(selected_tools):
        return None
    try:
        from mira.agentic.direct_renderer import try_direct_scalar_answer
        from mira.agentic.vnext_executor import (
            chart_from_evidence,
            data_from_evidence,
            evidence_summary,
            pending_write_from_evidence,
            tool_trace_from_evidence,
        )

        pending_write = pending_write_from_evidence(evidence)
        if pending_write:
            return None
        if try_direct_scalar_answer(question, evidence):
            return None
        data, data_source = data_from_evidence(evidence, pending_write)
        chart_payload = chart_from_evidence(evidence)
        if not data and not chart_payload:
            return None
        return {
            "type": "evidence_preview",
            "data": data,
            "data_source": data_source,
            "chart": chart_payload,
            "tool_trace": tool_trace_from_evidence(evidence),
            "evidence": evidence_summary(evidence),
            "rows_affected": len(data) if isinstance(data, list) else 0,
            "rows_total": _evidence_total_row_count(evidence, data=data),
            "react_lite": True,
            "stage": "deterministic_evidence_ready",
        }
    except Exception:
        return None


def _complex_finance_preview_answer_event(
    *,
    question: str,
    evidence: EvidencePacket,
) -> dict[str, Any] | None:
    if not _complex_finance_preview_llm_enabled():
        return None
    try:
        from mira.agentic.vnext_answerer import preview_answer_from_evidence

        result = preview_answer_from_evidence(question=question, evidence=evidence)
        answer = str(getattr(result, "answer", "") or "").strip()
        if not answer:
            return None
        return {
            "type": "preview_answer",
            "text": answer,
            "path": getattr(result, "path", ""),
            "used_fallback": bool(getattr(result, "used_fallback", False)),
            "llm_calls": int(getattr(result, "llm_calls", 0) or 0),
            "_answer_result": result,
        }
    except Exception:
        return None


def _evidence_total_row_count(evidence: EvidencePacket, *, data: Any = None) -> int:
    keys = ("total_matching_transactions", "matching_count", "txn_count", "row_count", "count")
    for fact in evidence.facts or []:
        if not isinstance(fact, dict):
            continue
        for key in keys:
            value = _positive_int_or_none(value=fact.get(key))
            if value is not None:
                return value
    for record in evidence.tool_results or []:
        if not isinstance(record, dict):
            continue
        for key in keys:
            value = _positive_int_or_none(value=record.get(key))
            if value is not None:
                return value
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        for key in keys:
            value = _positive_int_or_none(value=result.get(key))
            if value is not None:
                return value
    return len(data) if isinstance(data, list) else len(evidence.display_rows or evidence.rows or [])


def _positive_int_or_none(*, value: Any) -> int | None:
    try:
        count = int(float(value))
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _read_only_complex_finance_tools(selected_tools: list[Any]) -> bool:
    names = {str(name or "").strip() for name in selected_tools if str(name or "").strip()}
    if not names:
        return False
    if names & (_MEMORY_TOOL_NAMES | {"run_sql"}):
        return False
    if any(name.startswith("preview_") or name == "preview_finance_change" for name in names):
        return False
    return True


def _done_trace(
    trace: dict[str, Any],
    *,
    validation: ValidationResult,
    evidence: EvidencePacket,
    answer_result: Any,
) -> dict[str, Any]:
    executor_ms = 0.0
    for record in evidence.tool_results:
        try:
            executor_ms += float(record.get("ms") or 0)
        except (TypeError, ValueError):
            continue
    answer_path = getattr(answer_result, "path", "")
    return {
        **trace,
        "validation_status": validation.status,
        "grounded_entity_count": len(validation.grounded_entities),
        "executor_ms": round(executor_ms, 2),
        "tool_result_count": len(evidence.tool_results),
        "evidence_fact_count": len(evidence.facts),
        "evidence_row_count": len(evidence.rows),
        "evidence_chart_count": len(evidence.charts),
        "answer_path": answer_path,
        "answer_llm_calls": int(getattr(answer_result, "llm_calls", 0) or 0),
        "answer_used_fallback": bool(getattr(answer_result, "used_fallback", False)),
        "answer_cache_hit": bool(getattr(answer_result, "cache_hit", False)),
        "answer_max_tokens": int(getattr(answer_result, "max_tokens", 0) or 0),
        "memory_context_used": bool(getattr(answer_result, "memory_context_used", False)),
        "memory_context_count": max(0, int(getattr(answer_result, "memory_context_count", 0) or 0)),
        "memory_context_reason": str(getattr(answer_result, "memory_context_reason", "") or ""),
    }


def _answer_vnext_safely(
    *,
    question: str,
    route: dict[str, Any],
    validation: ValidationResult,
    evidence: EvidencePacket,
    history: list[dict] | None = None,
    memory_context_provider: Callable[[str], dict[str, Any]] | None = None,
) -> Any:
    try:
        from mira.agentic.vnext_answerer import VNextAnswerResult, answer_vnext

        return answer_vnext(
            question=question,
            route=route,
            validation=validation,
            evidence=evidence,
            history=history,
            memory_context_provider=memory_context_provider,
        )
    except Exception as exc:
        from mira.agentic.vnext_answerer import VNextAnswerResult

        return VNextAnswerResult(
            answer=_answer_for_route(route, validation, evidence),
            path="fallback",
            used_fallback=True,
            error=str(exc),
        )


def _shadow_execution_policy(validation: ValidationResult) -> tuple[bool, str]:
    if validation.status != "ready":
        return False, f"validation_{validation.status}"
    if not validation.normalized_plan:
        return True, ""
    selected = [step.tool_name for step in validation.normalized_plan]
    if any(str(name or "").startswith("preview_") or name == "preview_finance_change" for name in selected):
        return False, "preview_write_skipped"
    if any(name in {"manage_memory", "remember_user_context", "update_memory", "forget_memory"} for name in selected):
        return False, "write_tool_skipped"
    return True, ""


def _shadow_skipped_answer(reason: str) -> Any:
    from mira.agentic.vnext_answerer import VNextAnswerResult

    return VNextAnswerResult(
        answer="",
        path="shadow_skipped",
        used_fallback=False,
        error=reason,
    )


def _shadow_payload(
    *,
    question: str,
    profile: str | None,
    current_event: dict[str, Any] | None,
    vnext_done: dict[str, Any],
    validation: ValidationResult,
    safe_to_execute: bool,
    skipped_reason: str,
    latency_ms: float,
) -> dict[str, Any]:
    selected_tools = list(vnext_done.get("selected_tools") or [])
    current_tools = list((current_event or {}).get("selected_tools") or [])
    answer_guard = vnext_done.get("answer_guard") if isinstance(vnext_done.get("answer_guard"), dict) else {}
    trace = vnext_done.get("trace") if isinstance(vnext_done.get("trace"), dict) else {}
    payload = {
        "runtime": _RUNTIME,
        "status": "ok" if safe_to_execute else "skipped",
        "profile": profile or "household",
        "question": str(question or ""),
        "latency_ms": latency_ms,
        "safe_to_execute": safe_to_execute,
        "skipped_reason": skipped_reason,
        "selected_tools": selected_tools,
        "tool_args": [
            {"name": step.tool_name, "args": dict(step.args or {})}
            for step in validation.normalized_plan
        ],
        "validation_status": validation.status,
        "answer_path": answer_guard.get("path") or trace.get("answer_path") or "",
        "answer_used_fallback": bool(answer_guard.get("used_fallback")),
        "answer_error": answer_guard.get("error") or "",
        "llm_calls": vnext_done.get("llm_calls") or 0,
        "data_source": vnext_done.get("data_source"),
        "evidence": vnext_done.get("evidence") or {},
        "trace": trace,
        "mismatch_reason": _shadow_mismatch_reason(
            current_tools=current_tools,
            vnext_tools=selected_tools,
            current_answer=str((current_event or {}).get("answer") or ""),
            vnext_answer=str(vnext_done.get("answer") or ""),
            skipped_reason=skipped_reason,
        ),
        "legacy_router_used": False,
    }
    if vnext_done.get("pending_write"):
        payload["pending_write"] = {
            "rows_affected": (vnext_done.get("pending_write") or {}).get("rows_affected"),
            "preview_change_count": len((vnext_done.get("pending_write") or {}).get("preview_changes") or []),
        }
    return payload


def _shadow_mismatch_reason(
    *,
    current_tools: list[str],
    vnext_tools: list[str],
    current_answer: str,
    vnext_answer: str,
    skipped_reason: str,
) -> str:
    if skipped_reason:
        return skipped_reason
    if current_tools != vnext_tools:
        return "tool_selection_diff"
    if bool(current_answer.strip()) != bool(vnext_answer.strip()):
        return "answer_presence_diff"
    return ""


def _record_shadow_trace(payload: dict[str, Any]) -> None:
    trace_dir = _shadow_trace_dir()
    if trace_dir is None:
        return
    path = trace_dir / f"mira_vnext_shadow_{datetime.now().strftime('%Y%m%d')}.jsonl"
    try:
        with _SHADOW_TRACE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    except Exception:
        return


def _shadow_trace_dir() -> Path | None:
    explicit = os.getenv("MIRA_VNEXT_SHADOW_TRACE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if _env_flag("MIRA_VNEXT_SHADOW_TRACE"):
        return Path("benchmark_runs") / "mira_vnext_shadow_traces"
    return None


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _validate_selector_safely(
    *,
    selector: Any,
    question: str,
    profile: str | None,
    history: list[dict] | None,
) -> ValidationResult:
    try:
        from mira.agentic.vnext_validator import validate_selector_calls, validation_for_general_answer

        selector_status = str(getattr(selector, "status", "") or "")
        if selector_status == "general_answer":
            return validation_for_general_answer(question=question, history=history)
        if selector_status != "tool_calls":
            return _validation_failure(
                str(getattr(selector, "error", "") or "selector did not choose a valid route"),
                history=history,
            )
        calls = _compiled_selector_calls(selector=selector, history=history, profile=profile, question=question)
        return validate_selector_calls(
            calls,
            question=question,
            profile=profile,
            history=history,
        )
    except Exception as exc:
        return _validation_failure(str(exc), history=history)


def _compiled_selector_calls(
    *,
    selector: Any,
    history: list[dict] | None,
    profile: str | None = None,
    question: str = "",
) -> list[dict[str, Any]]:
    selector_decision = getattr(selector, "decision", {})
    if not isinstance(selector_decision, dict):
        selector_decision = {}
    selector_decision = _apply_pending_replies(selector_decision, history, question)
    pending_error = str(selector_decision.get("pending_clarification_error") or "").strip()
    if pending_error:
        pending = selector_decision.get("pending_clarification") if isinstance(selector_decision.get("pending_clarification"), dict) else None
        return [_compiler_validation_error(pending_error, pending_clarification=pending)]
    frame = _merged_conversation_frame_from_decision(selector_decision, history, question=question)
    frame = _repair_write_preview_subject_from_details(frame, selector_decision)
    try:
        from mira.agentic.entity_grounder import ground_conversation_frame

        grounded = ground_conversation_frame(frame, profile=profile, source_text=question)
    except Exception as exc:
        selector_decision["entity_grounding_error"] = str(exc)
    else:
        selector_decision["entity_grounding"] = grounded.trace
        if grounded.entities:
            selector_decision["entity_grounding_entities"] = grounded.entities
        if grounded.frame is not None:
            frame = grounded.frame
            selector_decision["grounded_mira_conversation_frame"] = frame.to_dict()
        if grounded.status == "clarify":
            return [
                {
                    "id": "selector_call_1",
                    "name": "summarize_spending",
                    "args": {},
                    "validation_error": grounded.message,
                    "grounded_entities": grounded.entities,
                    "pending_clarification": grounded.pending_clarification,
                }
            ]
    frame = _transaction_evidence_frame_for_question(frame, question)
    if frame is not None:
        selector_decision["grounded_mira_conversation_frame"] = frame.to_dict()
    selector_decision = _apply_affordability_amount_from_question(selector_decision, frame, question)
    try:
        from mira.agentic.intent_compiler import compile_selector_decision

        compiled = compile_selector_decision(
            selector_decision,
            frame=frame,
            selector_calls=None,
        )
    except Exception as exc:
        selector_decision["intent_compiler_error"] = str(exc)
        return [_compiler_validation_error("I could not compile that Folio request safely.")]
    selector_decision["intent_compiler"] = compiled.trace
    selector_decision["intent_compiler_status"] = compiled.status
    if compiled.issue:
        selector_decision["intent_compiler_issue"] = compiled.issue
    if compiled.ok:
        selector_decision["compiled_calls"] = compiled.calls
        return compiled.calls
    selector_decision["compiler_fallback_removed"] = True
    return [_compiler_validation_error("I need one more detail to choose the right Folio view.")]


def _compiler_validation_error(
    message: str,
    *,
    pending_clarification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "selector_call_1",
        "name": "summarize_spending",
        "args": {},
        "validation_error": message,
        "pending_clarification": pending_clarification or {},
    }


def _apply_pending_reply_selector(selector: Any, history: list[dict] | None, question: str) -> Any:
    decision = getattr(selector, "decision", {})
    if not isinstance(decision, dict):
        return selector
    resolved = _apply_pending_replies(decision, history, question)
    if resolved is decision:
        return selector
    if resolved == decision:
        return selector
    force_tool_status = any(
        bool(resolved.get(key))
        for key in (
            "pending_amount_resolved",
            "pending_entity_resolved",
            "pending_clarification_error",
        )
    )
    if not force_tool_status:
        return selector
    return _SelectorOverride(selector, decision=resolved, status="tool_calls")


def _apply_pending_replies(
    selector_decision: dict[str, Any],
    history: list[dict] | None,
    question: str,
) -> dict[str, Any]:
    resolved = _apply_pending_entity_resolution_reply(selector_decision, history, question)
    return _apply_pending_amount_reply(resolved, history, question)


def _apply_pending_entity_resolution_reply(
    selector_decision: dict[str, Any],
    history: list[dict] | None,
    question: str,
) -> dict[str, Any]:
    pending = _latest_pending_entity_clarification(history)
    if not pending:
        return selector_decision

    resolution = _resolve_pending_entity_choice(pending, question)
    status = resolution.get("status")
    if status == "none":
        return selector_decision
    if status != "resolved":
        frame = _resume_frame_from_pending(pending)
        frame_payload = frame.to_dict() if frame else {}
        return {
            **selector_decision,
            "route": frame_payload.get("route") or "finance",
            "intent": frame_payload.get("intent") or "none",
            "subject": frame_payload.get("subject") or {"kind": "none"},
            "time": frame_payload.get("time") or "none",
            "time_a": frame_payload.get("time_a"),
            "time_b": frame_payload.get("time_b"),
            "output": frame_payload.get("output") or "status",
            "discourse_action": "clarification_reply",
            "answer": "",
            "intent_frame": frame_payload,
            "pending_clarification": pending,
            "pending_clarification_error": resolution.get("message") or _pending_entity_choice_message(pending),
        }

    frame = _resume_frame_from_pending(pending)
    option = resolution.get("option") if isinstance(resolution.get("option"), dict) else {}
    if frame is None or not option:
        return {
            **selector_decision,
            "route": "finance",
            "intent": "none",
            "subject": {"kind": "none"},
            "time": "none",
            "output": "status",
            "discourse_action": "clarification_reply",
            "answer": "",
            "pending_clarification": pending,
            "pending_clarification_error": _pending_entity_choice_message(pending),
        }

    label = _entity_option_display(option)
    selected_subject = MiraSubject(
        kind=str(option.get("type") or "").strip(),
        text=str(option.get("canonical") or label).strip() or None,
        canonical_id=str(option.get("canonical") or "").strip() or None,
        display_name=label or None,
        confidence=_float_or_none(option.get("confidence")),
    )
    resolved_frame = replace(
        frame,
        subject=selected_subject,
        pending_clarification={},
        evidence_stale=False,
        force_reground=False,
    )
    frame_payload = resolved_frame.to_dict()
    return {
        **selector_decision,
        "route": frame_payload.get("route") or "finance",
        "intent": frame_payload.get("intent") or "none",
        "subject": frame_payload.get("subject") or {"kind": selected_subject.kind, "text": selected_subject.text},
        "time": frame_payload.get("time") or "none",
        "time_a": frame_payload.get("time_a"),
        "time_b": frame_payload.get("time_b"),
        "output": frame_payload.get("output") or "status",
        "discourse_action": "clarification_reply",
        "answer": "",
        "intent_frame": frame_payload,
        "grounded_mira_conversation_frame": frame_payload,
        "pending_entity_resolved": True,
        "pending_entity_resolution": {
            "kind": selected_subject.kind,
            "canonical": selected_subject.canonical_id,
            "label": selected_subject.display_name,
            "matched_by": resolution.get("matched_by"),
        },
    }


def _latest_pending_entity_clarification(history: list[dict] | None) -> dict[str, Any]:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        if not answer_context:
            continue
        pending = answer_context.get("pending_clarification") if isinstance(answer_context.get("pending_clarification"), dict) else {}
        if pending.get("kind") == "entity_resolution":
            return pending
        return {}
    return {}


def _resume_frame_from_pending(pending: dict[str, Any]) -> ConversationFrame | None:
    payload = pending.get("resume_frame") if isinstance(pending.get("resume_frame"), dict) else {}
    if not payload:
        return None
    try:
        return ConversationFrame.from_dict(payload)
    except ValueError:
        return None


def _resolve_pending_entity_choice(pending: dict[str, Any], question: str) -> dict[str, Any]:
    options = _pending_entity_options(pending)
    choice = _normalized_choice(question)
    if not choice:
        return {"status": "none"}
    if choice in {"all", "both", "everything"}:
        return {
            "status": "unsupported",
            "message": "I can only use one match here. Please pick one option, like `category`, `merchant`, or `1`.",
        }

    by_exact: dict[str, list[dict[str, Any]]] = {}
    for option in options:
        for candidate in _option_match_values(option):
            by_exact.setdefault(candidate, []).append(option)
    if choice in by_exact:
        matches = _unique_options(by_exact[choice])
        if len(matches) == 1:
            return {"status": "resolved", "option": matches[0], "matched_by": "exact"}
        return {"status": "ambiguous", "message": _pending_entity_choice_message(pending)}

    ordinal = _choice_ordinal(choice)
    if ordinal is not None:
        if 0 <= ordinal < len(options):
            return {"status": "resolved", "option": options[ordinal], "matched_by": "ordinal"}
        return {"status": "ambiguous", "message": _pending_entity_choice_message(pending)}

    type_choice = _choice_entity_type(choice)
    if type_choice:
        matches = [option for option in options if str(option.get("type") or "").strip() == type_choice]
        if len(matches) == 1:
            return {"status": "resolved", "option": matches[0], "matched_by": "type"}
        return {"status": "ambiguous", "message": _pending_entity_choice_message(pending)}

    if _looks_like_short_clarification_reply(choice):
        return {"status": "ambiguous", "message": _pending_entity_choice_message(pending)}
    return {"status": "none"}


def _pending_entity_options(pending: dict[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for option in pending.get("options") or []:
        if not isinstance(option, dict):
            continue
        entity_type = str(option.get("type") or "").strip()
        if entity_type not in {"account", "category", "merchant"}:
            continue
        canonical = str(option.get("canonical") or option.get("id") or option.get("label") or "").strip()
        if not canonical:
            continue
        options.append(option)
    return options


def _option_match_values(option: dict[str, Any]) -> set[str]:
    values = {
        _normalized_choice(option.get("id")),
        _normalized_choice(option.get("canonical")),
        _normalized_choice(option.get("label")),
        _normalized_choice(_entity_option_display(option)),
    }
    entity_type = str(option.get("type") or "").strip()
    canonical = str(option.get("canonical") or "").strip()
    label = _entity_option_display(option)
    if entity_type and canonical:
        values.add(_normalized_choice(f"{canonical} {entity_type}"))
    if entity_type and label:
        values.add(_normalized_choice(f"{label} {entity_type}"))
    return {value for value in values if value}


def _entity_option_display(option: dict[str, Any]) -> str:
    label = str(option.get("label") or "").strip()
    entity_type = str(option.get("type") or "").strip()
    if label and entity_type and label.lower().endswith(f" {entity_type}".lower()):
        return label[: -len(entity_type)].strip()
    return label or str(option.get("canonical") or option.get("id") or "").strip()


def _unique_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in options:
        key = str(option.get("id") or f"{option.get('type')}:{option.get('canonical')}").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(option)
    return unique


def _normalized_choice(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    for char in "`'\".,!?()[]{}":
        text = text.replace(char, " ")
    return " ".join(text.replace("_", " ").split())


def _choice_ordinal(choice: str) -> int | None:
    ordinal_words = {
        "1": 0,
        "one": 0,
        "first": 0,
        "2": 1,
        "two": 1,
        "second": 1,
        "3": 2,
        "three": 2,
        "third": 2,
        "4": 3,
        "four": 3,
        "fourth": 3,
        "5": 4,
        "five": 4,
        "fifth": 4,
    }
    filler = {"the", "option", "choice", "number", "one", "use", "pick", "select"}
    tokens = choice.split()
    non_filler_ordinals = {
        token
        for token in tokens
        if token in ordinal_words and token not in {"one"}
    }
    found = [
        ordinal_words[token]
        for token in tokens
        if token in ordinal_words and (token != "one" or not non_filler_ordinals)
    ]
    if len(found) != 1:
        return None
    if any(token not in ordinal_words and token not in filler for token in tokens):
        return None
    return found[0]


def _choice_entity_type(choice: str) -> str:
    filler = {"the", "one", "option", "choice", "use", "pick", "select"}
    tokens = choice.split()
    entity_types = [token for token in tokens if token in {"account", "category", "merchant"}]
    if len(entity_types) != 1:
        return ""
    if any(token not in filler and token not in {"account", "category", "merchant"} for token in tokens):
        return ""
    return entity_types[0]


def _looks_like_short_clarification_reply(choice: str) -> bool:
    tokens = choice.split()
    return 0 < len(tokens) <= 4


def _pending_entity_choice_message(pending: dict[str, Any]) -> str:
    options = _pending_entity_options(pending)
    labels = [str(option.get("label") or _entity_option_display(option) or "").strip() for option in options]
    labels = [label for label in labels if label]
    if labels:
        return "Please pick one match: " + ", ".join(labels[:5]) + "."
    raw = str(pending.get("raw") or "that").strip()
    return f"I still need the exact merchant, category, or account for `{raw}`."


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _apply_affordability_amount_from_question(
    selector_decision: dict[str, Any],
    frame: ConversationFrame | None,
    question: str,
) -> dict[str, Any]:
    if frame is None or frame.intent != "affordability":
        return selector_decision
    details = selector_decision.get("details") if isinstance(selector_decision.get("details"), dict) else {}
    if _float_or_none(details.get("amount")) is not None:
        return selector_decision
    amount = _currency_amount_from_text(question)
    if amount is None:
        return selector_decision
    patched_details = dict(details)
    patched_details["amount"] = amount
    return {
        **selector_decision,
        "details": patched_details,
        "affordability_amount_inferred": True,
    }


def _currency_amount_from_text(text: str) -> float | None:
    compact = " ".join(str(text or "").replace(",", "").split())
    if not compact:
        return None
    match = re.search(
        r"(?:\$\s*(\d+(?:\.\d+)?))|(?:(\d+(?:\.\d+)?)\s*(?:dollars?|bucks|usd)\b)",
        compact,
        flags=re.I,
    )
    if not match:
        return None
    return _positive_amount_or_none(match.group(1) or match.group(2))


def _positive_amount_or_none(value: Any) -> float | None:
    amount = _float_or_none(value)
    if amount is None or amount <= 0:
        return None
    return amount


def _apply_pending_amount_reply(
    selector_decision: dict[str, Any],
    history: list[dict] | None,
    question: str,
) -> dict[str, Any]:
    pending = _latest_pending_amount_clarification(history)
    amount = _standalone_amount_reply(question)
    if not pending or amount is None:
        return selector_decision

    prior_frame = _latest_mira_conversation_frame(history)
    if prior_frame is None or prior_frame.intent != "affordability":
        return selector_decision

    frame_payload = prior_frame.to_dict()
    frame_payload.update(
        {
            "route": "finance",
            "intent": "affordability",
            "subject": {"kind": "none"},
            "output": "status",
            "discourse_action": "clarification_reply",
            "answer": "",
        }
    )
    details = selector_decision.get("details") if isinstance(selector_decision.get("details"), dict) else {}
    details = dict(details)
    details["amount"] = amount
    if pending.get("purpose") not in (None, "", [], {}) and details.get("purpose") in (None, "", [], {}):
        details["purpose"] = pending.get("purpose")

    return {
        **selector_decision,
        "route": "finance",
        "intent": "affordability",
        "subject": {"kind": "none", "text": None},
        "time": frame_payload.get("time") or "none",
        "time_a": frame_payload.get("time_a"),
        "time_b": frame_payload.get("time_b"),
        "output": "status",
        "discourse_action": "clarification_reply",
        "answer": "",
        "details": details,
        "intent_frame": frame_payload,
        "pending_amount_resolved": True,
    }


def _latest_pending_amount_clarification(history: list[dict] | None) -> dict[str, Any]:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        if not answer_context:
            continue
        pending = answer_context.get("pending_clarification") if isinstance(answer_context.get("pending_clarification"), dict) else {}
        if pending.get("kind") == "missing_slot" and pending.get("slot") == "amount":
            return pending
        return {}
    return {}


def _standalone_amount_reply(question: str) -> float | None:
    text = " ".join(str(question or "").replace(",", "").split())
    if not text:
        return None
    cleaned = text[1:] if text.startswith("$") else text
    if cleaned.count(".") > 1:
        return None
    return _positive_amount_or_none(cleaned)


def _execute_vnext_evidence(
    *,
    validation: ValidationResult,
    question: str,
    profile: str | None,
) -> EvidencePacket:
    if not _should_execute(validation):
        return EvidencePacket(question=question)
    from mira.agentic.vnext_executor import execute_vnext_plan

    return execute_vnext_plan(
        validation,
        question=question,
        profile=profile,
        cache={},
    )


def _should_execute(validation: ValidationResult) -> bool:
    return validation.status == "ready" and bool(validation.normalized_plan)


def _validation_failure(error: str, *, history: list[dict] | None) -> ValidationResult:
    decision = AgentDecision(
        intent="chat",
        turn_kind="chat",
        tool_plan=[],
        confidence=0.0,
        uses_history=bool(history),
        reasoning_summary="vnext_validation_failure",
    )
    return ValidationResult(
        status="clarify",
        decision=decision,
        normalized_plan=[],
        clarification_question="I need one more detail to choose the right Folio tool.",
        blocked_reason=error,
    )


def _selector_decision_cache_enabled() -> bool:
    return str(os.getenv("MIRA_SELECTOR_DECISION_CACHE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def _selector_decision_cache_ttl_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("MIRA_SELECTOR_DECISION_CACHE_TTL_SECONDS", "300")))
    except (TypeError, ValueError):
        return 300.0


def _selector_decision_cache_max_entries() -> int:
    try:
        return max(0, int(os.getenv("MIRA_SELECTOR_DECISION_CACHE_MAX", "128")))
    except (TypeError, ValueError):
        return 128


def _selector_decision_cache_key(*, question: str, history: list[dict] | None, profile: str | None) -> str:
    from mira.agentic.vnext_selector import format_recent_context

    payload = {
        "v": 1,
        "date": datetime.now().date().isoformat(),
        "profile": str(profile or ""),
        "question": _compact_selector_cache_text(question),
        "recent_context": format_recent_context(history),
        "persona_v2": str(os.getenv("MIRA_PERSONA_V2_ENABLED", "")).strip().lower(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _compact_selector_cache_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _selector_decision_cache_allowed(*, question: str) -> bool:
    text = str(question or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return False
    return _selector_decision_cache_enabled() and _selector_decision_cache_max_entries() > 0


def clear_selector_decision_cache() -> None:
    with _SELECTOR_DECISION_CACHE_LOCK:
        _SELECTOR_DECISION_CACHE.clear()


def _get_selector_decision_cache_hit(key: str) -> Any | None:
    ttl_seconds = _selector_decision_cache_ttl_seconds()
    if ttl_seconds <= 0:
        return None
    now = time.time()
    with _SELECTOR_DECISION_CACHE_LOCK:
        entry = _SELECTOR_DECISION_CACHE.get(key)
        if not entry:
            return None
        stored_at, selector = entry
        age_seconds = now - stored_at
        if age_seconds > ttl_seconds:
            _SELECTOR_DECISION_CACHE.pop(key, None)
            return None
        cached = copy.deepcopy(selector)
    trace = getattr(cached, "trace", {})
    if isinstance(trace, dict):
        llm_calls = int(getattr(cached, "llm_calls", 0) or trace.get("selector_llm_calls", 0) or 0)
        cached = replace(
            cached,
            llm_calls=0,
            trace={
                **trace,
                "selector_cache_hit": True,
                "selector_cache_age_ms": round(age_seconds * 1000, 2),
                "selector_llm_calls_skipped": llm_calls,
                "selector_ms": 0.0,
            },
        )
    return cached


def _store_selector_decision_cache(key: str, selector: Any) -> Any:
    if not hasattr(selector, "decision") or not hasattr(selector, "trace"):
        return selector
    now = time.time()
    max_entries = _selector_decision_cache_max_entries()
    to_store = copy.deepcopy(selector)
    trace = getattr(to_store, "trace", {})
    if isinstance(trace, dict):
        to_store = replace(to_store, trace={**trace, "selector_cache_hit": False})
    with _SELECTOR_DECISION_CACHE_LOCK:
        _SELECTOR_DECISION_CACHE[key] = (now, to_store)
        while len(_SELECTOR_DECISION_CACHE) > max_entries:
            oldest_key = next(iter(_SELECTOR_DECISION_CACHE))
            _SELECTOR_DECISION_CACHE.pop(oldest_key, None)
    trace = getattr(selector, "trace", {})
    if isinstance(trace, dict):
        return replace(selector, trace={**trace, "selector_cache_hit": False})
    return selector


def _run_selector_safely(*, question: str, history: list[dict] | None, profile: str | None = None) -> Any:
    try:
        from mira.agentic.vnext_selector import run_selector

        cache_key = ""
        if _selector_decision_cache_allowed(question=question):
            cache_key = _selector_decision_cache_key(question=question, history=history, profile=profile)
            cached = _get_selector_decision_cache_hit(cache_key)
            if cached is not None:
                return cached

        selector = run_selector(question=question, history=history)
        if cache_key:
            selector = _store_selector_decision_cache(cache_key, selector)
        return selector
    except Exception as exc:
        return _SelectorFailure(str(exc))


def _controller_act_for_status(status: str, selected_tools: list[str]) -> str:
    if selected_tools:
        return "execute_action"
    if status == "clarify":
        return "clarify"
    return "answer_direct"


def _operation_for_status(selector_status: str, validation_status: str, selected_tools: list[str]) -> str:
    if validation_status == "blocked":
        return "blocked"
    if validation_status == "clarify":
        return "clarify"
    if selected_tools:
        return "selector_tool_plan"
    if selector_status == "general_answer":
        return "general_answer"
    return "clarify"


def _memory_only(selected_tools: list[str]) -> bool:
    return bool(selected_tools) and all(name in _MEMORY_TOOL_NAMES for name in selected_tools)


def _progress_event(route: dict[str, Any]) -> dict[str, Any]:
    status = (route.get("selector") or {}).get("status") if isinstance(route.get("selector"), dict) else ""
    validation = route.get("validation") if isinstance(route.get("validation"), dict) else {}
    validation_status = validation.get("status")
    if validation_status == "blocked":
        stage = "blocked"
        label = "Stopping on a safety check"
    elif validation_status == "clarify":
        stage = "clarify"
        label = "Checking the vNext route"
    elif status == "tool_calls":
        stage = "action"
        label = "Selected Folio tools"
    elif status == "general_answer":
        stage = "model"
        label = "Selected general answer"
    else:
        stage = "clarify"
        label = "Checking the vNext route"
    return {
        "type": "progress",
        "stage": stage,
        "label": label,
        "intent": route.get("intent"),
        "operation": route.get("operation"),
        "selected_tools": route.get("selected_tools") or [],
        "domain_action_name": "vnext_selector",
        "domain_action_status": (route.get("domain_action") or {}).get("status") if isinstance(route.get("domain_action"), dict) else status,
    }


def _answer_for_route(route: dict[str, Any], validation: ValidationResult, evidence: EvidencePacket) -> str:
    status = (route.get("selector") or {}).get("status") if isinstance(route.get("selector"), dict) else ""
    selected_tools = route.get("selected_tools") if isinstance(route.get("selected_tools"), list) else []
    if validation.status == "clarify":
        return _user_visible_error(validation)
    if validation.status == "blocked":
        return _user_visible_error(validation)
    if status == "tool_calls" and selected_tools and evidence.tool_results:
        return f"Mira vNext ran {', '.join(selected_tools)} and collected evidence. Final evidence-grounded answering comes next."
    if status == "tool_calls" and selected_tools:
        return f"Mira vNext selected {', '.join(selected_tools)}. Tool execution comes next."
    if status == "general_answer":
        return "Mira vNext routed this as a general answer. General answer generation comes next."
    return _ANSWER


def _user_visible_error(validation: ValidationResult) -> str:
    try:
        from mira.agentic.vnext_answerer import safe_validation_answer

        answer = safe_validation_answer(validation)
        if answer:
            return answer
    except Exception:
        pass
    if validation.status == "clarify":
        return "I need one more detail to choose the right Folio tool."
    return "I could not safely run that request."


def _merged_conversation_frame_from_decision(
    selector_decision: dict[str, Any],
    history: list[dict] | None,
    *,
    question: str = "",
) -> ConversationFrame | None:
    if not isinstance(selector_decision, dict):
        return None
    grounded_payload = selector_decision.get("grounded_mira_conversation_frame")
    if isinstance(grounded_payload, dict) and grounded_payload:
        try:
            return ConversationFrame.from_dict(grounded_payload)
        except ValueError:
            pass
    intent_frame = _intent_frame_from_decision(selector_decision)
    if intent_frame is None:
        return None
    prior = _latest_mira_conversation_frame(history)
    if prior is not None:
        intent_frame = _contextual_finance_frame(intent_frame, prior)
    try:
        frame = ConversationFrame.merge(prior, intent_frame)
    except ValueError:
        frame = ConversationFrame.from_intent_frame(intent_frame)
    frame = _repair_subject_only_followup_range(
        frame=frame,
        prior=prior,
        intent_frame=intent_frame,
        question=question,
    )
    frame = _repair_explicit_time_from_question(frame=frame, intent_frame=intent_frame, question=question)
    return _repair_empty_custom_chart_time(frame=frame, question=question)


def _contextual_finance_frame(intent_frame: MiraIntentFrame, prior: ConversationFrame) -> MiraIntentFrame:
    if intent_frame.route != "chat" or prior.route not in {"finance", "write_preview", "memory"}:
        return intent_frame
    has_context_slot = (
        not intent_frame.subject.is_empty
        or intent_frame.time != "none"
        or intent_frame.output != "none"
        or intent_frame.intent != "none"
    )
    if not has_context_slot:
        return intent_frame
    payload = intent_frame.to_dict()
    payload["route"] = prior.route
    if payload.get("intent") == "none":
        payload["intent"] = prior.intent
    if payload.get("output") == "none":
        payload["output"] = prior.output
    payload["discourse_action"] = "follow_up"
    payload["answer"] = ""
    try:
        return MiraIntentFrame.from_dict(payload)
    except ValueError:
        return intent_frame


def _repair_subject_only_followup_range(
    *,
    frame: ConversationFrame,
    prior: ConversationFrame | None,
    intent_frame: MiraIntentFrame,
    question: str,
) -> ConversationFrame:
    if prior is None:
        return frame
    if intent_frame.time not in {"month_before_prior", "next_month_after_prior"}:
        return frame
    if intent_frame.subject.is_empty:
        return frame
    if has_explicit_time_scope(question):
        return frame
    return replace(frame, time=prior.time, time_a=prior.time_a, time_b=prior.time_b)


def _repair_explicit_time_from_question(
    *,
    frame: ConversationFrame,
    intent_frame: MiraIntentFrame,
    question: str,
) -> ConversationFrame:
    parsed = parse_range(question)
    token = str(parsed.token or "").strip()
    if parsed.explicit and parsed.unsupported_reason:
        if frame.time == "custom" and frame.time_a:
            return frame
        return replace(frame, time="custom", time_a=None, time_b=None)
    if not parsed.explicit or not token:
        return frame
    if intent_frame.time in {"month_before_prior", "next_month_after_prior"}:
        return frame
    current_range = _range_token_from_frame(frame)
    if current_range == token:
        return frame
    should_repair = intent_frame.time in {"none", "custom"} or frame.time in {"none", "custom"}
    if not should_repair and token not in {"current_month", "this_month"}:
        should_repair = True
    if not should_repair:
        return frame
    repaired = _frame_time_from_range_token(token)
    if repaired is None:
        return replace(frame, time="custom", time_a=None, time_b=None)
    time_value, time_a, time_b = repaired
    return replace(frame, time=time_value, time_a=time_a, time_b=time_b)


def _repair_empty_custom_chart_time(*, frame: ConversationFrame, question: str) -> ConversationFrame:
    if frame.output != "chart" and frame.intent not in {"spending_trend", "net_worth_trend"}:
        return frame
    if frame.time != "custom" or frame.time_a:
        return frame
    if has_explicit_time_scope(question):
        return frame
    return replace(frame, time="all_time", time_a=None, time_b=None)


def _repair_write_preview_subject_from_details(
    frame: ConversationFrame | None,
    selector_decision: dict[str, Any],
) -> ConversationFrame | None:
    if frame is None or frame.route != "write_preview":
        return frame
    details = selector_decision.get("details") if isinstance(selector_decision.get("details"), dict) else {}
    merchant = str(details.get("merchant") or "").strip()
    if not merchant:
        return frame
    if frame.subject.kind == "merchant" and frame.subject.text == merchant:
        return frame
    return replace(frame, subject=MiraSubject(kind="merchant", text=merchant))


def _frame_time_from_range_token(token: str) -> tuple[str, str | None, str | None] | None:
    value = str(token or "").strip().lower()
    aliases = {
        "all": "all_time",
        "current": "this_month",
        "current_month": "this_month",
        "prior": "last_month",
        "prior_month": "last_month",
        "previous_month": "last_month",
    }
    value = aliases.get(value, value)
    bounded = bounded_range_dates(value)
    if bounded:
        start, end = bounded
        return "custom", start, end
    if len(value) == 7 and value[4] == "-":
        try:
            int(value[:4])
            month = int(value[5:7])
        except ValueError:
            return None
        if 1 <= month <= 12:
            return "custom", f"{value}-01", None
        return None
    if is_supported_time_token(value):
        return value, None, None
    return None


def _range_token_from_frame(frame: ConversationFrame) -> str:
    token = str(frame.time or "").strip().lower()
    if token == "custom" and frame.time_a:
        if frame.time_b and str(frame.time_b)[:7] != str(frame.time_a)[:7]:
            return bounded_range_token(str(frame.time_a), str(frame.time_b))
        month = str(frame.time_a)[:7]
        if len(month) == 7 and month[4] == "-":
            return month
        return ""
    aliases = {
        "all_time": "all",
        "this_month": "current_month",
    }
    return aliases.get(token, token)


def _transaction_evidence_frame_for_question(frame: ConversationFrame | None, question: str) -> ConversationFrame | None:
    if frame is None or frame.route != "finance":
        return frame
    if frame.intent not in {"spending_total", "spending_breakdown"}:
        return frame
    if frame.subject.is_empty:
        return frame
    token_set = set(words(question))
    if not ({"why", "when"} & token_set):
        return frame
    return replace(frame, intent="spending_explain", output="table")


def _latest_mira_conversation_frame(history: list[dict] | None) -> ConversationFrame | None:
    for turn in reversed(history or []):
        if not isinstance(turn, dict):
            continue
        answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
        if not answer_context:
            continue
        try:
            frame = ConversationFrame.from_answer_context(answer_context)
        except ValueError:
            frame = None
        if frame:
            return frame
        legacy = answer_context.get("conversation_frame") if isinstance(answer_context.get("conversation_frame"), dict) else {}
        intent = _intent_frame_from_legacy_conversation_frame(legacy, fallback={})
        if intent:
            return ConversationFrame.from_intent_frame(intent)
    return None


def _intent_frame_from_decision(selector_decision: dict[str, Any]) -> MiraIntentFrame | None:
    compiled = selector_decision.get("compiled_conversation_frame")
    if isinstance(compiled, dict) and compiled:
        frame = _intent_frame_from_legacy_conversation_frame(compiled, fallback=selector_decision)
        if frame:
            return frame
    payload = selector_decision.get("intent_frame") if isinstance(selector_decision.get("intent_frame"), dict) else {}
    if not payload:
        payload = selector_decision
    try:
        return MiraIntentFrame.from_dict(payload)
    except ValueError:
        return None


def _conversation_frame_from_route(route: dict[str, Any]) -> ConversationFrame | None:
    payload = route.get("mira_conversation_frame") if isinstance(route.get("mira_conversation_frame"), dict) else {}
    if not payload:
        return None
    try:
        return ConversationFrame.from_dict(payload)
    except ValueError:
        return None


def _answer_context_from_validation(
    validation: ValidationResult,
    evidence: EvidencePacket,
    route: dict[str, Any] | None = None,
) -> dict | None:
    route = route if isinstance(route, dict) else {}
    route_frame = _conversation_frame_from_route(route)
    if validation.status == "clarify" and validation.pending_clarification:
        context = {
            "version": 2,
            "kind": "finance_pending_clarification",
            "pending_clarification": validation.pending_clarification,
            "agentic": True,
            "runtime": _RUNTIME,
        }
        if route_frame:
            context["mira_conversation_frame"] = route_frame.to_dict()
        return context
    if validation.status != "ready" or not validation.normalized_plan:
        return None
    from mira.agentic.semantic_frames import primary_semantic_frame, semantic_frame_from_args

    subject_type = ""
    subject = ""
    ranges: list[str] = []
    tools = []
    frames: list[dict[str, Any]] = []
    for step in validation.normalized_plan:
        args = step.args if isinstance(step.args, dict) else {}
        tools.append({"id": step.step_id, "name": step.tool_name, "args": dict(args)})
        frame = semantic_frame_from_args(step.tool_name, args)
        if frame:
            frames.append(frame)
        if not subject:
            filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
            subject_type = str(args.get("entity_type") or args.get("subject_type") or "").strip()
            subject = str(args.get("entity") or args.get("subject") or args.get("merchant") or args.get("category") or filters.get("merchant") or filters.get("category") or "").strip()
            if not subject_type and args.get("merchant"):
                subject_type = "merchant"
            elif not subject_type and args.get("category"):
                subject_type = "category"
            elif not subject_type and filters.get("merchant"):
                subject_type = "merchant"
            elif not subject_type and filters.get("category"):
                subject_type = "category"
        for key in ("range", "range_a", "range_b"):
            value = str(args.get(key) or "").strip()
            if value and value not in ranges:
                ranges.append(value)
    current_frame = primary_semantic_frame(frames)
    legacy_conversation_frame = _conversation_frame_from_answer_context(
        current_frame=current_frame,
        subject_type=subject_type,
        subject=subject,
        ranges=ranges,
        tools=tools,
    )
    mira_conversation_frame = _mira_conversation_frame_from_answer_context(
        route_frame=route_frame,
        legacy_frame=legacy_conversation_frame,
        tools=tools,
        evidence=evidence,
    )
    return {
        "version": 2,
        "kind": "finance_answer_context",
        "subject_type": subject_type,
        "subject": subject,
        "ranges": ranges,
        "tools": tools,
        "mira_conversation_frame": mira_conversation_frame.to_dict() if mira_conversation_frame else {},
        "provenance_id": evidence.provenance.get("provenance_id") or evidence.provenance.get("id"),
        "agentic": True,
        "runtime": _RUNTIME,
    }


def _conversation_frame_from_answer_context(
    *,
    current_frame: dict[str, Any],
    subject_type: str,
    subject: str,
    ranges: list[str],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(current_frame, dict) or not current_frame.get("tool"):
        return {}
    filters = current_frame.get("filters") if isinstance(current_frame.get("filters"), dict) else {}
    resolved_subject_type = subject_type
    resolved_subject = subject
    if not resolved_subject and filters.get("merchant"):
        resolved_subject_type = "merchant"
        resolved_subject = str(filters.get("merchant") or "")
    elif not resolved_subject and filters.get("category"):
        resolved_subject_type = "category"
        resolved_subject = str(filters.get("category") or "")
    elif not resolved_subject and filters.get("account"):
        resolved_subject_type = "account"
        resolved_subject = str(filters.get("account") or "")
    view = str(current_frame.get("view") or "").strip().lower()
    requested_output = "scalar_total" if view in {"entity_total", "period_total"} else "summary"
    source_step_id = ""
    for tool in tools:
        if str(tool.get("name") or "") != "make_chart":
            source_step_id = str(tool.get("id") or "")
            break
    return {
        "intent": "spend_total" if current_frame.get("tool") == "summarize_spending" else str(current_frame.get("tool") or ""),
        "tool": str(current_frame.get("tool") or ""),
        "view": str(current_frame.get("view") or ""),
        "subject": {
            "type": resolved_subject_type,
            "canonical": resolved_subject,
            "raw": resolved_subject,
        } if resolved_subject else {},
        "range": str(current_frame.get("range") or (ranges[0] if ranges else "") or ""),
        "requested_output": requested_output,
        "source_step_id": source_step_id,
        "payload": current_frame.get("payload") if isinstance(current_frame.get("payload"), dict) else {},
    }


def _mira_conversation_frame_from_answer_context(
    *,
    route_frame: ConversationFrame | None,
    legacy_frame: dict[str, Any],
    tools: list[dict[str, Any]],
    evidence: EvidencePacket,
) -> ConversationFrame | None:
    frame = route_frame
    if frame is None:
        intent = _intent_frame_from_legacy_conversation_frame(legacy_frame, fallback={})
        frame = ConversationFrame.from_intent_frame(intent) if intent else None
    if frame is None:
        return None

    payload = frame.to_dict()
    legacy_subject = _subject_from_legacy_conversation_frame(legacy_frame)
    current_subject = payload.get("subject") if isinstance(payload.get("subject"), dict) else {}
    if legacy_subject and not str(current_subject.get("text") or current_subject.get("canonical_id") or "").strip():
        payload["subject"] = legacy_subject.to_dict()

    legacy_range = str(legacy_frame.get("range") or "").strip()
    if legacy_range and payload.get("time") == "none":
        time_value, time_a, time_b = _time_from_legacy_range(legacy_range)
        payload["time"] = time_value
        payload["time_a"] = time_a
        payload["time_b"] = time_b

    source_step_id = str(legacy_frame.get("source_step_id") or "").strip()
    if not source_step_id:
        for tool in tools:
            if str(tool.get("name") or "") != "make_chart":
                source_step_id = str(tool.get("id") or "")
                break
    if source_step_id:
        payload["last_evidence_step_id"] = source_step_id

    backend_tool = _backend_tool_from_evidence(evidence)
    if backend_tool:
        payload["last_backend_tool"] = backend_tool

    try:
        return ConversationFrame.from_dict(payload)
    except ValueError:
        return frame


def _backend_tool_from_evidence(evidence: EvidencePacket) -> str:
    for record in evidence.tool_results:
        if not isinstance(record, dict):
            continue
        name = str(record.get("execution_tool_name") or record.get("tool_name") or record.get("tool") or "").strip()
        if name and name != "plot_chart":
            return name
    return ""


def _intent_frame_from_legacy_conversation_frame(
    legacy_frame: dict[str, Any],
    *,
    fallback: dict[str, Any],
) -> MiraIntentFrame | None:
    if not isinstance(legacy_frame, dict) or not legacy_frame:
        return None
    tool = str(legacy_frame.get("tool") or "").strip()
    view = str(legacy_frame.get("view") or "").strip()
    intent = _intent_from_legacy_tool_view(tool=tool, view=view, raw_intent=str(legacy_frame.get("intent") or fallback.get("intent") or ""))
    subject = _subject_from_legacy_conversation_frame(legacy_frame)
    time_value, time_a, time_b = _time_from_legacy_range(str(legacy_frame.get("range") or "").strip())
    output = _output_from_legacy_frame(legacy_frame)
    discourse_action = _discourse_action_from_selector_fallback(fallback)
    try:
        return MiraIntentFrame.from_dict(
            {
                "route": "finance" if tool else str(fallback.get("route") or "finance"),
                "intent": intent,
                "subject": subject.to_dict() if subject else {"kind": "none"},
                "time": time_value,
                "time_a": time_a,
                "time_b": time_b,
                "output": output,
                "chart_type": "line" if output == "chart" else None,
                "discourse_action": discourse_action,
            }
        )
    except ValueError:
        return None


def _intent_from_legacy_tool_view(*, tool: str, view: str, raw_intent: str) -> str:
    aliases = {
        "spend_total": "spending_total",
        "spending": "spending_total",
        "budget": "budget_status",
        "net_worth": "net_worth_trend",
    }
    raw = aliases.get(str(raw_intent or "").strip().lower(), str(raw_intent or "").strip().lower())
    if raw in {
        "affordability",
        "budget_plan",
        "budget_status",
        "cashflow_forecast",
        "cashflow_shortfall",
        "data_health",
        "enrichment_quality",
        "explain_metric",
        "explain_transaction",
        "finance_priorities",
        "finance_snapshot",
        "low_confidence_transactions",
        "memory_op",
        "net_worth_balance",
        "net_worth_delta",
        "net_worth_trend",
        "none",
        "recurring_changes",
        "recurring_summary",
        "savings_capacity",
        "spending_breakdown",
        "spending_compare",
        "spending_explain",
        "spending_top",
        "spending_total",
        "spending_trend",
        "transaction_lookup",
        "write_preview",
    }:
        return raw
    tool = str(tool or "").strip()
    view = str(view or "").strip()
    if tool == "summarize_spending":
        return {
            "top": "spending_top",
            "breakdown": "spending_breakdown",
            "trend": "spending_trend",
            "compare": "spending_compare",
        }.get(view, "spending_total")
    if tool == "query_transactions":
        return "transaction_lookup"
    if tool == "review_budget":
        return "savings_capacity" if view == "savings_capacity" else "budget_status"
    if tool == "review_cashflow":
        return "cashflow_shortfall" if view == "shortfall" else "cashflow_forecast"
    if tool == "review_recurring":
        return "recurring_changes" if view == "changes" else "recurring_summary"
    if tool == "review_net_worth":
        return {"balances": "net_worth_balance", "delta": "net_worth_delta"}.get(view, "net_worth_trend")
    if tool == "review_data_quality":
        return {
            "enrichment_summary": "enrichment_quality",
            "low_confidence": "low_confidence_transactions",
            "explain_transaction": "explain_transaction",
        }.get(view, "data_health")
    if tool == "check_affordability":
        return "affordability"
    if tool == "preview_finance_change":
        return "write_preview"
    return "none"


def _subject_from_legacy_conversation_frame(legacy_frame: dict[str, Any]) -> MiraSubject | None:
    subject = legacy_frame.get("subject") if isinstance(legacy_frame.get("subject"), dict) else {}
    kind = str(subject.get("type") or subject.get("kind") or subject.get("type_hint") or "").strip().lower()
    text = str(subject.get("canonical") or subject.get("text") or subject.get("raw") or "").strip()
    if not kind or kind not in {"merchant", "category", "account", "transaction", "metric", "net_worth", "self", "unknown"}:
        filters = legacy_frame.get("filters") if isinstance(legacy_frame.get("filters"), dict) else {}
        for candidate in ("merchant", "category", "account"):
            value = str(filters.get(candidate) or "").strip()
            if value:
                kind = candidate
                text = value
                break
    if not kind and not text:
        return None
    return MiraSubject(kind=kind or "unknown", text=text or None, canonical_id=text or None, display_name=text or None)


def _time_from_legacy_range(range_value: str) -> tuple[str, str | None, str | None]:
    token = str(range_value or "").strip().lower()
    aliases = {
        "": "none",
        "all": "all_time",
        "current": "this_month",
        "current_month": "this_month",
        "prior_month": "last_month",
        "previous_month": "last_month",
    }
    token = aliases.get(token, token)
    bounded = bounded_range_dates(token)
    if bounded:
        start, end = bounded
        return "custom", start, end
    if token in {
        "all_time",
        "last_30d",
        "last_365d",
        "last_3_months",
        "last_6_months",
        "last_7d",
        "last_90d",
        "last_month",
        "last_week",
        "last_year",
        "month_before_prior",
        "next_month_after_prior",
        "none",
        "this_month",
        "this_week",
        "ytd",
    }:
        return token, None, None
    if len(token) == 7 and token[4] == "-":
        year, month = token.split("-", 1)
        if year.isdigit() and month.isdigit() and 1 <= int(month) <= 12:
            return "custom", f"{token}-01", None
    return "custom" if token else "none", None, None


def _output_from_legacy_frame(legacy_frame: dict[str, Any]) -> str:
    requested = str(legacy_frame.get("requested_output") or "").strip().lower()
    return {
        "scalar_total": "scalar",
        "summary": "status",
        "rows": "table",
    }.get(requested, "chart" if str(legacy_frame.get("tool") or "") == "make_chart" else "status")


def _discourse_action_from_selector_fallback(fallback: dict[str, Any]) -> str:
    intent_frame = fallback.get("intent_frame") if isinstance(fallback.get("intent_frame"), dict) else {}
    action = str(intent_frame.get("discourse_action") or "").strip().lower()
    if action in {"clarification_reply", "clear", "correction", "follow_up", "new", "refine"}:
        return action
    patch = fallback.get("frame_patch") if isinstance(fallback.get("frame_patch"), dict) else {}
    frame_action = str(patch.get("frame_action") or "").strip().lower()
    if frame_action == "clarification_reply":
        return "clarification_reply"
    if frame_action == "patch_prior":
        return "follow_up"
    return "new"


class _SelectorFailure:
    def __init__(self, error: str):
        self.calls: list[dict[str, Any]] = []
        self.decision = {"error": error, "validation_errors": [error], "calls": []}
        self.raw = ""
        self.status = "clarify"
        self.error = error
        self.family_detail_used = False
        self.repair_used = False
        self.llm_calls = 0
        self.trace = {
            "runtime": _RUNTIME,
            "stage": "selector",
            "status": "clarify",
            "error": error,
            "llm_calls": 0,
        }


class _SelectorFallbackDisabled:
    def __init__(self, *, outcome: dict[str, Any] | None):
        outcome = outcome if isinstance(outcome, dict) else {}
        selector_path_reason = str(outcome.get("selector_path_reason") or "fallback_disabled").strip()
        fallback_reason = str(outcome.get("front_controller_fallback_reason") or "").strip()
        plan_fields = outcome.get("front_controller_plan_fields") if isinstance(outcome.get("front_controller_plan_fields"), dict) else {}
        answer = "I need one more detail before I can answer that safely."
        self.calls: list[dict[str, Any]] = []
        self.decision = {
            "route": "chat",
            "intent": "none",
            "subject": {"kind": "none", "text": None},
            "time": "none",
            "output": "none",
            "discourse_action": "new",
            "answer": answer,
            "details": {"answer_mode": "inline"},
            "intent_frame": {
                "route": "chat",
                "intent": "none",
                "subject": {"kind": "none", "text": None},
                "time": "none",
                "output": "none",
                "discourse_action": "new",
                "answer": answer,
            },
            "intent_frame_source": "selector_fallback_disabled",
        }
        self.raw = ""
        self.status = "general_answer"
        self.error = ""
        self.family_detail_used = False
        self.repair_used = False
        self.llm_calls = 0
        self.trace = {
            "runtime": _RUNTIME,
            "stage": "selector_fallback_disabled",
            "status": "general_answer",
            "selector_fallback_disabled": True,
            "selector_skipped": True,
            "selector_path_reason": selector_path_reason,
            "front_controller_fallback_reason": fallback_reason,
            "front_controller_mode": outcome.get("front_controller_mode") or "",
            "front_controller_plan_fields": copy.deepcopy(plan_fields),
            "front_controller_latency_ms": outcome.get("front_controller_latency_ms"),
            "llm_calls": 0,
        }


class _FrontControllerPlanSelector:
    def __init__(
        self,
        *,
        plan_fields: dict[str, str],
        latency_ms: float,
        fast_lane: str = "scalar",
        llm_calls: int = 1,
        controller_metrics: dict[str, Any] | None = None,
    ):
        from mira.agentic.front_controller import plan_fields_to_selector_decision

        meta = _front_controller_plan_meta(plan_fields)
        visible_plan_fields = _front_controller_visible_plan_fields(plan_fields)
        temporal_parser_llm_calls = _safe_int(meta.get("temporal_parser_llm_calls"))
        decision = plan_fields_to_selector_decision(visible_plan_fields)
        self.calls: list[dict[str, Any]] = []
        self.decision = decision
        self.raw = json.dumps({"mode": "PLAN", "fields": visible_plan_fields}, sort_keys=True)
        self.status = "tool_calls"
        self.error = ""
        self.family_detail_used = False
        self.repair_used = False
        self.llm_calls = int(llm_calls or 0) + temporal_parser_llm_calls
        self.trace = {
            "runtime": _RUNTIME,
            "stage": "front_controller_plan",
            "status": f"{fast_lane}_fast_lane_candidate",
            "front_controller_latency_ms": round(float(latency_ms or 0.0), 2),
            "front_controller_fast_lane": fast_lane,
            "front_controller_plan_fields": copy.deepcopy(visible_plan_fields),
            "llm_calls": self.llm_calls,
            "selector_skipped": True,
        }
        metrics = controller_metrics if isinstance(controller_metrics, dict) else {}
        for key in (
            "front_controller_first_text_ms",
            "front_controller_mode_detect_ms",
            "front_controller_seen_chars",
        ):
            if metrics.get(key) is not None:
                self.trace[key] = metrics.get(key)
        if meta:
            self.trace.update(
                {
                    "front_controller_temporal_parser_used": meta.get("temporal_parser_used") == "true",
                    "front_controller_temporal_parser_llm_calls": temporal_parser_llm_calls,
                    "front_controller_temporal_parser_status": meta.get("temporal_parser_status", ""),
                    "front_controller_temporal_parser_reason": meta.get("temporal_parser_reason", ""),
                }
            )


class _PendingStateSelector:
    def __init__(self, *, decision: dict[str, Any]):
        self.calls: list[dict[str, Any]] = []
        self.decision = decision
        self.raw = json.dumps({"mode": "PENDING_STATE", "decision": decision}, sort_keys=True, default=str)
        self.status = "tool_calls"
        self.error = ""
        self.family_detail_used = False
        self.repair_used = False
        self.llm_calls = 0
        self.trace = {
            "runtime": _RUNTIME,
            "stage": "pending_state_resolver",
            "status": "tool_calls",
            "llm_calls": 0,
            "selector_skipped": True,
            "pending_state_fast_resolver": True,
        }


class _ExplainLastFastSelector:
    def __init__(
        self,
        *,
        plan_fields: dict[str, str],
        latency_ms: float,
        controller_metrics: dict[str, Any] | None = None,
    ):
        decision = {
            "route": "chat",
            "intent": "none",
            "subject": {"kind": "none", "text": None},
            "time": "none",
            "output": "none",
            "discourse_action": "clarification_reply",
            "answer": "",
            "intent_frame": {
                "route": "chat",
                "intent": "none",
                "subject": {"kind": "none", "text": None},
                "time": "none",
                "output": "none",
                "discourse_action": "clarification_reply",
                "answer": "",
            },
            "intent_frame_source": "front_controller_explain_last",
            "front_controller_plan": True,
        }
        self.calls: list[dict[str, Any]] = []
        self.decision = decision
        self.raw = json.dumps({"mode": "PLAN", "fields": plan_fields}, sort_keys=True)
        self.status = "general_answer"
        self.error = ""
        self.family_detail_used = False
        self.repair_used = False
        self.llm_calls = 1
        self.trace = {
            "runtime": _RUNTIME,
            "stage": "front_controller_plan",
            "status": "explain_last_fast_lane_candidate",
            "front_controller_latency_ms": round(float(latency_ms or 0.0), 2),
            "front_controller_fast_lane": "explain_last",
            "front_controller_plan_fields": copy.deepcopy(plan_fields),
            "llm_calls": 1,
            "selector_skipped": True,
        }
        metrics = controller_metrics if isinstance(controller_metrics, dict) else {}
        for key in (
            "front_controller_first_text_ms",
            "front_controller_mode_detect_ms",
            "front_controller_seen_chars",
        ):
            if metrics.get(key) is not None:
                self.trace[key] = metrics.get(key)


class _SelectorOverride:
    def __init__(self, selector: Any, *, decision: dict[str, Any], status: str):
        self.calls = list(getattr(selector, "calls", []) or [])
        self.decision = decision
        self.raw = str(getattr(selector, "raw", "") or "")
        self.status = status
        self.error = str(getattr(selector, "error", "") or "")
        self.family_detail_used = bool(getattr(selector, "family_detail_used", False))
        self.repair_used = bool(getattr(selector, "repair_used", False))
        self.llm_calls = int(getattr(selector, "llm_calls", 0) or 0)
        trace = getattr(selector, "trace", {})
        self.trace = {**(trace if isinstance(trace, dict) else {}), "pending_reply_override": True}


__all__ = [
    "build_shadow_trace",
    "clear_selector_decision_cache",
    "run_vnext_shadow",
    "run_vnext_result",
    "run_vnext_stream",
]
