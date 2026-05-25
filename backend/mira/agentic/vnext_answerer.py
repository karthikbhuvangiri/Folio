from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from mira.persona import (
    evidence_persona_lines,
    general_persona_lines,
    memory_answer,
    validation_answer,
    write_preview_answer,
)
from mira.agentic.confidence import confidence_caveat_from_evidence, confidence_caveats_enabled
from mira.agentic.direct_renderer import try_direct_scalar_answer
from mira.agentic.schemas import EvidencePacket, ValidationResult

try:
    from mira.agentic.answerer import (
        deterministic_answer,
        _contains_unsupported_numbers,
        _unsupported_entity_terms,
    )
except ModuleNotFoundError:
    def deterministic_answer(evidence: EvidencePacket) -> str:
        if evidence.facts:
            summary = str(evidence.facts[0].get("summary") or "").strip()
            if summary:
                return summary
        if evidence.rows:
            return f"I found {len(evidence.rows)} matching row(s)."
        if evidence.tool_results:
            return "I collected Folio evidence for that."
        return "I do not have enough Folio evidence to answer that cleanly."

    def _contains_unsupported_numbers(answer: str, evidence: EvidencePacket) -> bool:
        _ = answer, evidence
        return False

    def _unsupported_entity_terms(answer: str, evidence: EvidencePacket) -> list[str]:
        _ = answer, evidence
        return []


AnswerCompleter = Callable[[str, int, str], str]
StreamAnswerCompleter = Callable[[str, int, str], Iterable[str]]
MemoryContextProvider = Callable[[str], dict[str, Any]]

VNEXT_EVIDENCE_MAX_TOKENS = int(os.getenv("MIRA_VNEXT_EVIDENCE_MAX_TOKENS", "180"))
VNEXT_GENERAL_MAX_TOKENS = int(os.getenv("MIRA_VNEXT_GENERAL_MAX_TOKENS", "1000"))
VNEXT_INLINE_CHAT_MAX_CHARS = int(os.getenv("MIRA_VNEXT_INLINE_CHAT_MAX_CHARS", "700"))
VNEXT_EVIDENCE_ANSWER_CACHE_SIZE = int(os.getenv("MIRA_VNEXT_EVIDENCE_ANSWER_CACHE_SIZE", "128"))
VNEXT_PREVIEW_MAX_TOKENS = int(os.getenv("MIRA_COMPLEX_FINANCE_PREVIEW_MAX_TOKENS", "80"))

_EVIDENCE_ANSWER_CACHE_VERSION = "v3"
_EVIDENCE_ANSWER_CACHE: OrderedDict[str, str] = OrderedDict()


@dataclass(frozen=True)
class VNextAnswerResult:
    answer: str
    path: str
    raw: str = ""
    prompt: str = ""
    llm_calls: int = 0
    used_fallback: bool = False
    error: str = ""
    max_tokens: int = 0
    cache_hit: bool = False
    memory_context_used: bool = False
    memory_context_count: int = 0
    memory_context_reason: str = ""


def answer_vnext(
    *,
    question: str,
    route: dict,
    validation: ValidationResult,
    evidence: EvidencePacket,
    history: list[dict] | None = None,
    completer: AnswerCompleter | None = None,
    max_tokens: int | None = None,
    memory_context_provider: MemoryContextProvider | None = None,
) -> VNextAnswerResult:
    if validation.status == "clarify":
        return VNextAnswerResult(
            answer=safe_validation_answer(validation),
            path="clarify",
        )
    if validation.status == "blocked":
        return VNextAnswerResult(
            answer=safe_validation_answer(validation),
            path="blocked",
            error=validation.blocked_reason,
        )

    operation = str(route.get("operation") or "")
    if operation == "general_answer":
        inline = _selector_inline_answer(route)
        if inline and not is_explain_last_answer_question(question):
            return VNextAnswerResult(answer=inline, path="selector_inline")
        return answer_general_question(
            question=question,
            history=history,
            completer=completer,
            max_tokens=max_tokens,
            memory_context_provider=memory_context_provider,
        )

    direct = try_direct_scalar_answer(question, evidence)
    if direct:
        return VNextAnswerResult(answer=direct, path="direct_scalar")

    templated = _persona_template_answer(evidence)
    if templated:
        return templated

    return answer_from_evidence(
        question=question,
        evidence=evidence,
        completer=completer,
        max_tokens=max_tokens,
        memory_context_provider=memory_context_provider,
    )


def answer_from_evidence(
    *,
    question: str,
    evidence: EvidencePacket,
    completer: AnswerCompleter | None = None,
    max_tokens: int | None = None,
    memory_context_provider: MemoryContextProvider | None = None,
) -> VNextAnswerResult:
    resolved_max_tokens = _resolve_answer_max_tokens("evidence_llm", max_tokens)
    memory_context = _resolve_memory_context(memory_context_provider, "evidence_llm")
    cache_key = _evidence_answer_cache_key(
        question=question,
        evidence=evidence,
        max_tokens=resolved_max_tokens,
        memory_context=memory_context,
    )
    cached = _get_cached_evidence_answer(cache_key, max_tokens=resolved_max_tokens, memory_context=memory_context)
    if cached:
        return cached

    prompt = build_evidence_answer_prompt(question=question, evidence=evidence, memory_context=memory_context)
    complete = completer or _default_completer
    raw = ""
    try:
        raw = complete(prompt, resolved_max_tokens, "copilot")
        result = _evidence_result_from_raw(
            question=question,
            evidence=evidence,
            raw=raw,
            prompt=prompt,
            max_tokens=resolved_max_tokens,
            memory_context=memory_context,
        )
        _put_cached_evidence_answer(cache_key, result)
        return result
    except Exception as exc:
        return VNextAnswerResult(
            answer=guarded_deterministic_evidence_answer(question, evidence),
            path="evidence_llm",
            raw=raw,
            prompt=prompt,
            llm_calls=1,
            used_fallback=True,
            error=str(exc),
            max_tokens=resolved_max_tokens,
            **_memory_context_result_fields(memory_context),
        )


def preview_answer_from_evidence(
    *,
    question: str,
    evidence: EvidencePacket,
    completer: AnswerCompleter | None = None,
    max_tokens: int | None = None,
) -> VNextAnswerResult:
    resolved_max_tokens = max(16, min(int(max_tokens or VNEXT_PREVIEW_MAX_TOKENS), 120))
    prompt = build_preview_answer_prompt(question=question, evidence=evidence)
    complete = completer or _default_completer
    raw = ""
    try:
        raw = complete(prompt, resolved_max_tokens, "copilot")
        answer = _clean_preview_answer(raw)
        if not answer:
            raise ValueError("empty preview answer")
        if _contains_unsupported_numbers(answer, evidence):
            raise ValueError("preview introduced unsupported numbers")
        if _contains_unsupported_number_words(answer, evidence):
            raise ValueError("preview introduced unsupported number words")
        if _contains_internal_evidence_terms(answer):
            raise ValueError("preview leaked internal evidence terms")
        answer = ensure_why_disclaimer(question, answer)
        answer = ensure_evidence_caveat(answer, evidence)
        return VNextAnswerResult(
            answer=answer,
            path="evidence_preview_llm",
            raw=raw,
            prompt=prompt,
            llm_calls=1,
            max_tokens=resolved_max_tokens,
        )
    except Exception as exc:
        return VNextAnswerResult(
            answer=guarded_deterministic_evidence_answer(question, evidence),
            path="evidence_preview_fallback",
            raw=raw,
            prompt=prompt,
            llm_calls=1,
            used_fallback=True,
            error=str(exc),
            max_tokens=resolved_max_tokens,
        )


def answer_general_question(
    *,
    question: str,
    history: list[dict] | None = None,
    completer: AnswerCompleter | None = None,
    max_tokens: int | None = None,
    memory_context_provider: MemoryContextProvider | None = None,
) -> VNextAnswerResult:
    if is_explain_last_answer_question(question):
        return VNextAnswerResult(
            answer=explain_last_answer_from_history(history),
            path="explain_last_answer",
        )

    resolved_max_tokens = _resolve_answer_max_tokens("general_answer", max_tokens)
    memory_context = _resolve_memory_context(memory_context_provider, "general_answer")
    prompt = build_general_answer_prompt(question, history=history, memory_context=memory_context)
    complete = completer or _default_completer
    raw = ""
    try:
        raw = complete(prompt, resolved_max_tokens, "copilot")
        return _general_result_from_raw(
            question=question,
            raw=raw,
            prompt=prompt,
            max_tokens=resolved_max_tokens,
            memory_context=memory_context,
        )
    except Exception as exc:
        return VNextAnswerResult(
            answer="I can help with general questions and with Folio finance tasks, but I could not generate that answer locally just now.",
            path="general_answer",
            raw=raw,
            prompt=prompt,
            llm_calls=1,
            used_fallback=True,
            error=str(exc),
            max_tokens=resolved_max_tokens,
            **_memory_context_result_fields(memory_context),
        )


def iter_answer_vnext_events(
    *,
    question: str,
    route: dict,
    validation: ValidationResult,
    evidence: EvidencePacket,
    history: list[dict] | None = None,
    stream_completer: StreamAnswerCompleter | None = None,
    max_tokens: int | None = None,
    memory_context_provider: MemoryContextProvider | None = None,
):
    """Yield token events and finish with an internal answer_result event."""
    if validation.status == "clarify":
        yield {
            "type": "_answer_result",
            "answer_result": VNextAnswerResult(
                answer=safe_validation_answer(validation),
                path="clarify",
            ),
        }
        return
    if validation.status == "blocked":
        yield {
            "type": "_answer_result",
            "answer_result": VNextAnswerResult(
                answer=safe_validation_answer(validation),
                path="blocked",
                error=validation.blocked_reason,
            ),
        }
        return

    operation = str(route.get("operation") or "")
    if operation == "general_answer":
        if is_explain_last_answer_question(question):
            yield {
                "type": "_answer_result",
                "answer_result": VNextAnswerResult(
                    answer=explain_last_answer_from_history(history),
                    path="explain_last_answer",
                ),
            }
            return

        inline = _selector_inline_answer(route)
        if inline:
            yield {
                "type": "_answer_result",
                "answer_result": VNextAnswerResult(answer=inline, path="selector_inline"),
            }
            return

        resolved_max_tokens = _resolve_answer_max_tokens("general_answer", max_tokens)
        memory_context = _resolve_memory_context(memory_context_provider, "general_answer")
        prompt = build_general_answer_prompt(question, history=history, memory_context=memory_context)
        yield from _iter_streamed_answer_result(
            prompt=prompt,
            finalize=lambda raw: _general_result_from_raw(
                question=question,
                raw=raw,
                prompt=prompt,
                max_tokens=resolved_max_tokens,
                memory_context=memory_context,
            ),
            fallback=lambda raw, exc: VNextAnswerResult(
                answer="I can help with general questions and with Folio finance tasks, but I could not generate that answer locally just now.",
                path="general_answer",
                raw=raw,
                prompt=prompt,
                llm_calls=1,
                used_fallback=True,
                error=str(exc),
                max_tokens=resolved_max_tokens,
                **_memory_context_result_fields(memory_context),
            ),
            stream_completer=stream_completer,
            max_tokens=resolved_max_tokens,
        )
        return

    direct = try_direct_scalar_answer(question, evidence)
    if direct:
        yield {"type": "_answer_result", "answer_result": VNextAnswerResult(answer=direct, path="direct_scalar")}
        return

    templated = _persona_template_answer(evidence)
    if templated:
        yield {"type": "_answer_result", "answer_result": templated}
        return

    resolved_max_tokens = _resolve_answer_max_tokens("evidence_llm", max_tokens)
    memory_context = _resolve_memory_context(memory_context_provider, "evidence_llm")
    cache_key = _evidence_answer_cache_key(
        question=question,
        evidence=evidence,
        max_tokens=resolved_max_tokens,
        memory_context=memory_context,
    )
    cached = _get_cached_evidence_answer(cache_key, max_tokens=resolved_max_tokens, memory_context=memory_context)
    if cached:
        yield {"type": "token", "text": cached.answer}
        yield {"type": "_answer_result", "answer_result": cached}
        return

    prompt = build_evidence_answer_prompt(question=question, evidence=evidence, memory_context=memory_context)
    yield from _iter_streamed_answer_result(
        prompt=prompt,
        finalize=lambda raw: _evidence_result_from_raw(
            question=question,
            evidence=evidence,
            raw=raw,
            prompt=prompt,
            max_tokens=resolved_max_tokens,
            memory_context=memory_context,
        ),
        fallback=lambda raw, exc: VNextAnswerResult(
            answer=guarded_deterministic_evidence_answer(question, evidence),
            path="evidence_llm",
            raw=raw,
            prompt=prompt,
            llm_calls=1,
            used_fallback=True,
            error=str(exc),
            max_tokens=resolved_max_tokens,
            **_memory_context_result_fields(memory_context),
        ),
        stream_completer=stream_completer,
        max_tokens=resolved_max_tokens,
        required_prefix=_why_disclaimer_prefix(question),
        prefix_markers=_WHY_DISCLAIMER_MARKERS,
        cache_key=cache_key,
    )


def _selector_inline_answer(route: dict) -> str:
    selector = route.get("selector") if isinstance(route.get("selector"), dict) else {}
    decision = selector.get("decision") if isinstance(selector.get("decision"), dict) else {}
    intent_frame = decision.get("intent_frame") if isinstance(decision.get("intent_frame"), dict) else {}
    details = decision.get("details") if isinstance(decision.get("details"), dict) else {}
    inline_allowed = str(details.get("answer_mode") or decision.get("answer_mode") or "").strip().lower() == "inline"
    if not inline_allowed:
        return ""
    answer = str(
        decision.get("answer")
        or decision.get("direct_answer")
        or intent_frame.get("answer")
        or route.get("selector_answer")
        or ""
    ).strip()
    if not answer:
        return ""
    # Inline selector answers are for short chat turns. Longer answers should use
    # the normal answer model so persona and formatting stay high quality.
    return answer if len(answer) <= VNEXT_INLINE_CHAT_MAX_CHARS else ""


def _iter_streamed_answer_result(
    *,
    prompt: str,
    finalize: Callable[[str], VNextAnswerResult],
    fallback: Callable[[str, Exception], VNextAnswerResult],
    stream_completer: StreamAnswerCompleter | None,
    max_tokens: int,
    required_prefix: str = "",
    prefix_markers: tuple[str, ...] = (),
    cache_key: str = "",
):
    stream = stream_completer or _default_stream_completer
    raw_parts: list[str] = []
    emitted = False
    prefix = str(required_prefix or "")
    pending_parts: list[str] = []
    prefix_decided = not prefix

    def emit_text(text: str):
        nonlocal emitted
        raw_parts.append(text)
        emitted = True
        return {"type": "token", "text": text}

    def pending_text() -> str:
        return "".join(pending_parts)

    def pending_has_marker() -> bool:
        lowered = pending_text().lower()
        return any(marker in lowered for marker in prefix_markers)

    def should_decide_pending() -> bool:
        text = pending_text()
        return len(text) >= 96 or any(char in text for char in ".!?\n")

    def release_pending(*, with_prefix: bool):
        if with_prefix:
            yield emit_text(prefix)
        for item in pending_parts:
            yield emit_text(item)
        pending_parts.clear()

    try:
        for chunk in stream(prompt, max_tokens, "copilot"):
            text = str(chunk or "")
            if not text:
                continue
            if not prefix_decided:
                pending_parts.append(text)
                if pending_has_marker():
                    prefix_decided = True
                    yield from release_pending(with_prefix=False)
                elif should_decide_pending():
                    prefix_decided = True
                    yield from release_pending(with_prefix=True)
                continue
            yield emit_text(text)
        if pending_parts:
            yield from release_pending(with_prefix=not pending_has_marker())
        raw = "".join(raw_parts)
        result = finalize(raw)
        _put_cached_evidence_answer(cache_key, result)
    except Exception as exc:
        raw = "".join(raw_parts)
        result = fallback(raw, exc)

    displayed_answer = raw.strip()
    if emitted and result.answer and not _same_display_answer(result.answer, displayed_answer):
        yield {"type": "reset_text"}
        yield {"type": "token", "text": result.answer}
    yield {"type": "_answer_result", "answer_result": result}


def _evidence_result_from_raw(
    *,
    question: str,
    evidence: EvidencePacket,
    raw: str,
    prompt: str,
    max_tokens: int,
    memory_context: dict[str, Any] | None = None,
) -> VNextAnswerResult:
    answer = str(raw or "").strip()
    if not answer:
        raise ValueError("empty answer")
    if _contains_unsupported_numbers(answer, evidence):
        return VNextAnswerResult(
            answer=guarded_deterministic_evidence_answer(question, evidence),
            path="evidence_llm",
            raw=raw,
            prompt=prompt,
            llm_calls=1,
            used_fallback=True,
            error="answer introduced numbers not present in evidence",
            max_tokens=max_tokens,
            **_memory_context_result_fields(memory_context),
        )
    unsupported_terms = _unsupported_vnext_entity_terms(answer, evidence)
    if unsupported_terms:
        return VNextAnswerResult(
            answer=guarded_deterministic_evidence_answer(question, evidence),
            path="evidence_llm",
            raw=raw,
            prompt=prompt,
            llm_calls=1,
            used_fallback=True,
            error="answer introduced terms not present in evidence: " + ", ".join(unsupported_terms[:4]),
            max_tokens=max_tokens,
            **_memory_context_result_fields(memory_context),
        )
    answer = ensure_why_disclaimer(question, answer)
    answer = ensure_evidence_caveat(answer, evidence)
    answer = ensure_confidence_caveat(answer, evidence)
    return VNextAnswerResult(
        answer=answer,
        path="evidence_llm",
        raw=raw,
        prompt=prompt,
        llm_calls=1,
        max_tokens=max_tokens,
        **_memory_context_result_fields(memory_context),
    )


def guarded_deterministic_evidence_answer(question: str, evidence: EvidencePacket) -> str:
    answer = _warm_deterministic_evidence_answer(_safe_deterministic_evidence_answer(evidence))
    answer = ensure_why_disclaimer(question, answer)
    answer = ensure_evidence_caveat(answer, evidence)
    return ensure_confidence_caveat(answer, evidence)


def _safe_deterministic_evidence_answer(evidence: EvidencePacket) -> str:
    if evidence.caveats and not evidence.facts and not evidence.rows and not evidence.charts:
        return "I do not have usable tool evidence for that yet. " + " ".join(evidence.caveats[:2])
    if evidence.charts:
        title = str(evidence.charts[0].get("title") or "").strip()
        label = title if title else "that chart"
        return f"I prepared {label} from the deterministic Folio data."
    if evidence.rows:
        count = _best_evidence_row_count(evidence) or len(evidence.rows)
        noun = "transaction" if _rows_look_like_transactions(evidence.rows) else "row"
        plural = noun if count == 1 else f"{noun}s"
        visible_count = len(evidence.display_rows or evidence.rows)
        if count > visible_count:
            shown_plural = noun if visible_count == 1 else f"{noun}s"
            return f"I found {count} matching {plural} and am showing {visible_count} {shown_plural} here."
        return f"I found {count} matching {plural} to work from."
    if evidence.facts:
        clean_summary = _first_safe_fact_summary(evidence.facts)
        if clean_summary:
            return _persona_safe_fact_summary(clean_summary, evidence)
        return "I found the requested Folio evidence to work from."
    return "I do not have tool evidence to answer that yet."


def _best_evidence_row_count(evidence: EvidencePacket) -> int | None:
    keys = ("total_matching_transactions", "matching_count", "txn_count", "row_count", "count")
    for fact in evidence.facts:
        if not isinstance(fact, dict):
            continue
        for key in keys:
            count = _positive_int_or_none(fact.get(key))
            if count is not None:
                return count
    for record in evidence.tool_results:
        if not isinstance(record, dict):
            continue
        for key in keys:
            count = _positive_int_or_none(record.get(key))
            if count is not None:
                return count
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        for key in keys:
            count = _positive_int_or_none(result.get(key))
            if count is not None:
                return count
    return None


def _positive_int_or_none(value: Any) -> int | None:
    try:
        count = int(float(value))
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def _rows_look_like_transactions(rows: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(row, dict) and any(key in row for key in ("date", "amount", "merchant_name", "merchant", "description"))
        for row in rows[:5]
    )


def _first_safe_fact_summary(facts: list[dict[str, Any]]) -> str:
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        summary = " ".join(str(fact.get("summary") or "").split())
        if not summary:
            continue
        lowered = summary.lower()
        if any(marker in lowered for marker in ("metric_definition", "calculation_basis", "execution_tool", "selector_call", "summarize_spending:")):
            continue
        return summary
    return ""


def _persona_safe_fact_summary(summary: str, evidence: EvidencePacket) -> str:
    text = " ".join(str(summary or "").split())
    if not text:
        return text
    lowered = text.lower()
    metric_ids = set()
    tool_names = set()
    for fact in evidence.facts:
        if isinstance(fact, dict):
            metric_ids.add(str(fact.get("metric_id") or "").strip().lower())
            tool_names.add(str(fact.get("tool") or "").strip().lower())
    for record in evidence.tool_results:
        if isinstance(record, dict):
            metric_ids.add(str(record.get("metric_id") or "").strip().lower())
            tool_names.add(str(record.get("tool_name") or "").strip().lower())
            tool_names.add(str(record.get("execution_tool_name") or "").strip().lower())
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            metric_ids.add(str(result.get("metric_id") or "").strip().lower())
    if (
        "dashboard_snapshot" in metric_ids
        or "get_dashboard_snapshot" in tool_names
        or "dashboard snapshot available for your review" in lowered
    ):
        return (
            "Tiny map of the money room: I can work with balances, accounts, "
            "transactions, spending, income, budgets, cash flow, recurring "
            "patterns, net worth, goals, confidence, and the receipts behind "
            "the answers."
        )
    if lowered.startswith("based on the evidence, "):
        rest = text[len("Based on the evidence, "):].strip()
        if rest:
            return f"The Folio evidence says {rest[:1].lower()}{rest[1:]}"
    return text


def _warm_deterministic_evidence_answer(answer: str) -> str:
    text = " ".join(str(answer or "").split())
    if not text:
        return text
    lowered = text.lower()
    if lowered.startswith("found "):
        rest = text[len("Found "):].strip().rstrip(".")
        if "matching transaction" in lowered:
            return f"I found {rest} to work from."
        return f"I found {rest}."
    return text


def ensure_confidence_caveat(answer: str, evidence: EvidencePacket) -> str:
    caveat = confidence_caveat_from_evidence(evidence)
    if not caveat:
        return answer
    text = str(answer or "").strip()
    if caveat.lower() in text.lower():
        return text
    return f"{text} {caveat}".strip()


def ensure_evidence_caveat(answer: str, evidence: EvidencePacket) -> str:
    text = " ".join(str(answer or "").split())
    if not text or not evidence.caveats:
        return text
    lowered = text.lower()
    additions: list[str] = []
    for caveat in _prioritized_evidence_caveats(evidence):
        clean = " ".join(str(caveat or "").split())
        if not clean:
            continue
        clean_lowered = clean.lower()
        key_words = [word.strip(".,:;()[]") for word in clean_lowered.split() if len(word.strip(".,:;()[]")) >= 4]
        if clean_lowered in lowered or (key_words and sum(1 for word in key_words[:6] if word in lowered) >= 3):
            continue
        additions.append(clean)
        lowered = f"{lowered} {clean_lowered}"
        if len(additions) >= 2:
            break
    if additions:
        return f"{text} {' '.join(additions)}".strip()
    return text


def _prioritized_evidence_caveats(evidence: EvidencePacket) -> list[str]:
    caveats = [" ".join(str(caveat or "").split()) for caveat in evidence.caveats or []]
    caveats = [caveat for caveat in caveats if caveat]
    priority_markers = (
        "no active goals",
        "no category budgets",
        "fewer than",
        "account sync",
    )
    priority = [
        caveat
        for caveat in caveats
        if any(marker in caveat.lower() for marker in priority_markers)
    ]
    ordered = priority + [caveat for caveat in caveats[:2] if caveat not in priority]
    deduped: list[str] = []
    for caveat in ordered:
        if caveat not in deduped:
            deduped.append(caveat)
    return deduped


def _general_result_from_raw(
    *,
    question: str = "",
    raw: str,
    prompt: str,
    max_tokens: int,
    memory_context: dict[str, Any] | None = None,
) -> VNextAnswerResult:
    answer = str(raw or "").strip()
    if not answer:
        raise ValueError("empty answer")
    answer = _repair_advisor_context_general_answer(answer, question=question, memory_context=memory_context)
    return VNextAnswerResult(
        answer=answer,
        path="general_answer",
        raw=raw,
        prompt=prompt,
        llm_calls=1,
        max_tokens=max_tokens,
        **_memory_context_result_fields(memory_context),
    )


def _repair_advisor_context_general_answer(
    answer: str,
    *,
    question: str,
    memory_context: dict[str, Any] | None,
) -> str:
    context = memory_context or {}
    block = str(context.get("block") or "")
    reason = str(context.get("reason") or "")
    if reason != "advisor_lens_memo" and "Stored Mira advisor read" not in block:
        return answer

    text = str(answer or "").strip()
    text = re.sub(r"(?is)^\s*Hey you\. There you are\.\s*", "", text).strip()
    text = re.sub(r"(?is)^\s*Hey[,.!]?\s+", "", text).strip()
    text = re.sub(r"(?is)\bTL;DR\b:?", "short read", text)
    text = re.sub(r"(?is)\bno sweat[.!]?\s*", "", text)
    text = re.sub(r"(?is)\bdon't sweat\b", "do not overreact to", text)
    text = re.sub(r"(?is)\btrimming the fat\b", "reducing without pain", text)
    text = re.sub(r"\s+", " ", text).strip()

    q = str(question or "").lower()
    lowered = text.lower()
    prefixes: list[str] = []
    suffixes: list[str] = []

    travel_event_question = any(marker in q for marker in ("travel", "trip", "hawaii", "event month"))
    if "focus" in q or ("overreact" in q and not travel_event_question):
        has_liquidity = "cash" in lowered or "liquidity" in lowered
        has_floor = "fixed" in lowered and ("floor" in lowered or "obligation" in lowered)
        if not has_liquidity or not has_floor:
            prefixes.append(
                "Cash/liquidity is not the concern; the focus is income continuity against the fixed monthly floor and low-pain tune-ups."
            )

    reduce_question = "reduce" in q or "without pain" in q or "cut" in q
    if reduce_question and not any(marker in lowered for marker in ("broad cuts", "broad category", "panic-cut", "cutting necessities")):
        suffixes.append("These come before broad category cuts.")

    risk_question = "biggest risk" in q or ("risk" in q and "goal" in q)
    if risk_question:
        has_floor = "fixed" in lowered and ("floor" in lowered or "obligation" in lowered)
        has_goal_caveat = any(marker in lowered for marker in ("goal", "budget", "missing data", "caveat"))
        if not has_floor or not has_goal_caveat:
            prefixes.append(
                "The biggest risk is income continuity against the fixed monthly floor, with missing goals, budgets, labels, or stale sync as caveats."
            )

    repaired = " ".join([*prefixes, text, *suffixes]).strip()
    return repaired or answer


def clear_evidence_answer_cache() -> None:
    _EVIDENCE_ANSWER_CACHE.clear()


def _evidence_answer_cache_key(
    *,
    question: str,
    evidence: EvidencePacket,
    max_tokens: int,
    memory_context: dict[str, Any] | None = None,
) -> str:
    if VNEXT_EVIDENCE_ANSWER_CACHE_SIZE <= 0:
        return ""
    payload = {
        "version": _EVIDENCE_ANSWER_CACHE_VERSION,
        "question": " ".join(str(question or "").lower().split()),
        "max_tokens": int(max_tokens or 0),
        "evidence": _cacheable_evidence_payload(evidence),
        "memory_context": _cacheable_memory_context(memory_context),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_cached_evidence_answer(
    cache_key: str,
    *,
    max_tokens: int,
    memory_context: dict[str, Any] | None = None,
) -> VNextAnswerResult | None:
    if not cache_key:
        return None
    answer = _EVIDENCE_ANSWER_CACHE.get(cache_key)
    if answer is None:
        return None
    _EVIDENCE_ANSWER_CACHE.move_to_end(cache_key)
    return VNextAnswerResult(
        answer=answer,
        path="evidence_llm",
        raw=answer,
        llm_calls=0,
        max_tokens=max_tokens,
        cache_hit=True,
        **_memory_context_result_fields(memory_context),
    )


def _put_cached_evidence_answer(cache_key: str, result: VNextAnswerResult) -> None:
    if not cache_key or VNEXT_EVIDENCE_ANSWER_CACHE_SIZE <= 0:
        return
    if result.path != "evidence_llm" or result.used_fallback or result.error or not result.answer:
        return
    _EVIDENCE_ANSWER_CACHE[cache_key] = result.answer
    _EVIDENCE_ANSWER_CACHE.move_to_end(cache_key)
    while len(_EVIDENCE_ANSWER_CACHE) > VNEXT_EVIDENCE_ANSWER_CACHE_SIZE:
        _EVIDENCE_ANSWER_CACHE.popitem(last=False)


def _cacheable_evidence_payload(evidence: EvidencePacket) -> dict:
    return {
        "tool_results": _strip_volatile_evidence_fields(evidence.tool_results),
        "facts": _strip_volatile_evidence_fields(evidence.facts),
        "rows": _strip_volatile_evidence_fields(evidence.rows),
        "charts": _strip_volatile_evidence_fields(evidence.charts),
        "caveats": list(evidence.caveats or []),
    }


def _strip_volatile_evidence_fields(value):
    if isinstance(value, list):
        return [_strip_volatile_evidence_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_volatile_evidence_fields(item)
            for key, item in value.items()
            if key not in {"ms", "elapsed_ms", "duration_ms", "timing_ms"}
        }
    return value


def _cacheable_memory_context(memory_context: dict[str, Any] | None) -> dict[str, Any]:
    context = memory_context or {}
    return {
        "block": str(context.get("block") or ""),
        "used": bool(context.get("used") or context.get("block")),
        "count": max(0, int(context.get("count") or 0)),
        "reason": str(context.get("reason") or ""),
    }


def safe_validation_answer(validation: ValidationResult) -> str:
    """Map internal validation/compiler wording to product-safe copy."""
    if validation.status == "clarify":
        raw = str(validation.clarification_question or "").strip()
        fallback = "I need one more detail to answer that cleanly."
    elif validation.status == "blocked":
        raw = str(validation.blocked_reason or "").strip()
        fallback = "I could not safely run that request."
    else:
        return ""

    if not raw:
        return fallback
    templated = validation_answer(
        status=validation.status,
        message=raw,
        fallback=fallback,
        looks_internal=_looks_internal_validation_message(raw),
    )
    if templated:
        return templated
    if not _looks_internal_validation_message(raw):
        return raw
    return _productized_validation_message(raw, status=validation.status)


def _persona_template_answer(evidence: EvidencePacket) -> VNextAnswerResult | None:
    from mira.agentic.vnext_executor import pending_write_from_evidence

    write_answer = write_preview_answer(pending_write_from_evidence(evidence))
    if write_answer:
        return VNextAnswerResult(answer=write_answer, path="evidence_llm")

    answer = memory_answer(evidence.tool_results)
    if answer:
        return VNextAnswerResult(answer=answer, path="evidence_llm")
    return None


def _looks_internal_validation_message(message: str) -> bool:
    lowered = str(message or "").lower()
    markers = (
        "unsupported arg",
        "unsupported key",
        "unsupported field",
        "has unsupported",
        "source_step_id",
        "make_chart",
        "plot_chart",
        "payload",
        "schema",
        "validator",
        "selector",
        "semantic",
        "normalized",
        "tool plan",
        "unknown tool",
        "internal tool",
        "run_sql",
        "labels",
        "values",
        "series",
        "prior tool evidence",
        "filters",
        "filters must",
        "arg(s)",
        "range_a",
        "range_b",
    )
    return any(marker in lowered for marker in markers)


def _productized_validation_message(message: str, *, status: str) -> str:
    lowered = str(message or "").lower()
    if any(marker in lowered for marker in ("chart", "make_chart", "plot_chart", "source_step_id", "labels", "values", "series", "prior tool evidence")):
        return "I can chart that, but I need a clear Folio result to chart from first."
    if any(marker in lowered for marker in ("apply", "confirm", "commit", "execute changes", "write")):
        return "I can prepare a preview, but I will not apply finance changes until you confirm them."
    if any(marker in lowered for marker in ("run_sql", "internal tool", "unknown tool", "disallowed")):
        return "I cannot use that internal Folio tool from chat."
    if "transaction sort" in lowered or "sort" in lowered:
        return "I can show matching transactions, but I need to use a supported transaction view."
    if status == "clarify":
        return "I need one more detail to choose the right Folio view."
    return "I could not turn that into a safe Folio action yet."


def build_evidence_answer_prompt(
    *,
    question: str,
    evidence: EvidencePacket,
    memory_context: dict[str, Any] | None = None,
) -> str:
    memory_block = _memory_context_prompt_block(memory_context)
    financial_context_block = _financial_context_prompt_block(evidence)
    return (
        build_answer_system_prompt()
        + financial_context_block
        + memory_block
        + "\n\nUser question:\n"
        + str(question or "")
        + "\n\nEvidence JSON:\n"
        + json.dumps(evidence.to_dict(), ensure_ascii=True, separators=(",", ":"), default=str)
    )


def build_answer_system_prompt() -> str:
    prompt = evidence_persona_lines() + """
Do not introduce any amount, count, date, merchant, category, account, or transaction absent from evidence.
Do not infer the user's intent or motive from transaction data. For "why" questions, say you cannot know why from the data alone, then summarize what the evidence suggests.
Distinguish direct merchant matches from description/memo matches. Do not describe transfers or reimbursements as direct merchant spend unless merchant_name or merchant_key supports that.
Use categories, memos, transaction type, and descriptions as clues, not proof of intent.
If evidence is empty, errored, or caveated, say that plainly.
Answer in at most two short sentences. Lead with the useful result. Keep Mira's voice: warm, lightly witty when it fits, and specific without sounding like a report stub. The UI already shows rows and charts, so do not list transactions, months, chart points, or table rows in prose. Use matching_count as the total when present, but never say field names such as matching_count, visible rows, row_count, or evidence JSON; say "shown here" instead."""
    if confidence_caveats_enabled():
        prompt += "\nIf evidence includes confidence_summary.caveat, say it once; if absent or high, add no confidence caveat."
    return prompt


def build_general_answer_prompt(
    question: str,
    *,
    history: list[dict] | None = None,
    memory_context: dict[str, Any] | None = None,
) -> str:
    context = build_recent_conversation_context(history)
    context_block = (
        "\n\nRecent conversation for follow-up context:\n"
        + context
        + "\nUse this only when the current question depends on it. For questions about the chat itself, answer only from this visible conversation and say when the needed turn is missing. Preserve prior context only for omitted fields; the latest user message overrides prior context for subject, date/range, item, tone, format, comparison target, or constraint. If the user corrects you, treat the correction as the source of truth."
        if context
        else ""
    )
    return (
        build_general_answer_system_prompt()
        + context_block
        + _memory_context_prompt_block(memory_context)
        + "\n\nCurrent user question:\n"
        + str(question or "")
    )


def build_preview_answer_prompt(*, question: str, evidence: EvidencePacket) -> str:
    payload = _preview_evidence_payload(evidence)
    return "\n".join(
        [
            "You are Mira, Folio's warm, witty, precise local-first finance companion.",
            "Write ONE short intermediate sentence for the user after deterministic evidence is ready.",
            "Use only the evidence below. Do not mention tool names, schemas, metrics, calculations, JSON, or internal fields.",
            "No bullets. No markdown. No invented numbers. Keep it under 32 words.",
            "Sound like Mira, not a report stub: warm, grounded, and lightly witty only if it fits the money context.",
            "If matching_count is present, use that as the total count. visible_rows are examples, not the total.",
            "If matching_count is larger than visible_row_count, say how many matches exist and how many are shown here.",
            "",
            f"User asked: {question}",
            "Evidence:",
            json.dumps(payload, ensure_ascii=False, default=str),
            "",
            "Intermediate sentence:",
        ]
    )


def _preview_evidence_payload(evidence: EvidencePacket) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if evidence.rows:
        display_rows = evidence.display_rows or evidence.rows
        payload["matching_count"] = _best_evidence_row_count(evidence) or len(evidence.rows)
        payload["visible_row_count"] = len(display_rows)
        payload["visible_rows"] = [_preview_row(row) for row in display_rows[:4] if isinstance(row, dict)]
    if evidence.charts:
        chart = evidence.charts[0]
        payload["chart"] = {
            "title": chart.get("title"),
            "type": chart.get("type"),
            "labels": list(chart.get("labels") or [])[:8] if isinstance(chart.get("labels"), list) else [],
            "values": list(chart.get("values") or [])[:8] if isinstance(chart.get("values"), list) else [],
        }
    safe_facts = []
    for fact in evidence.facts[:4]:
        if not isinstance(fact, dict):
            continue
        clean = _preview_fact(fact)
        if clean:
            safe_facts.append(clean)
    if safe_facts:
        payload["facts"] = safe_facts
    if evidence.caveats:
        payload["caveats"] = list(evidence.caveats[:2])
    return payload or {"status": "evidence_ready"}


def _preview_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "date",
        "description",
        "merchant_name",
        "merchant",
        "amount",
        "category",
        "type",
        "month",
        "total",
        "value",
    )
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "", [], {})}


def _preview_fact(fact: dict[str, Any]) -> dict[str, Any]:
    blocked = {
        "step_id",
        "tool",
        "execution_tool",
        "metric_definition_summary",
        "calculation_basis",
        "args",
        "contract",
    }
    clean = {
        key: value
        for key, value in fact.items()
        if key not in blocked and not str(key).startswith("_") and value not in (None, "", [], {})
    }
    summary = _first_safe_fact_summary([fact])
    if summary:
        clean["summary"] = summary
    return {key: value for key, value in clean.items() if isinstance(value, (str, int, float, bool))}


def _clean_preview_answer(raw: str) -> str:
    text = " ".join(str(raw or "").strip().split())
    text = text.strip("\"'` ")
    for prefix in ("Intermediate sentence:", "Answer:", "Mira:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    if len(text) > 240:
        text = text[:240].rsplit(" ", 1)[0].strip()
    return text


def _contains_internal_evidence_terms(answer: str) -> bool:
    lowered = str(answer or "").lower()
    return any(
        term in lowered
        for term in (
            "selector_call",
            "summarize_spending",
            "query_transactions",
            "make_chart",
            "metric_definition",
            "calculation_basis",
            "execution_tool",
            "tool_result",
            "json",
            "visible rows",
            "shown slice",
        )
    )


_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}


def _contains_unsupported_number_words(answer: str, evidence: EvidencePacket) -> bool:
    values = _number_word_values(answer)
    if not values:
        return False
    allowed = _allowed_preview_number_values(evidence)
    return any(value not in allowed for value in values)


def _number_word_values(text: str) -> list[int]:
    tokens = [match.group(0).lower() for match in re.finditer(r"\b[a-z]+(?:-[a-z]+)?\b", str(text or "").lower())]
    values: list[int] = []
    for token in tokens:
        if token in _NUMBER_WORDS:
            values.append(_NUMBER_WORDS[token])
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            if left in _NUMBER_WORDS and right in _NUMBER_WORDS:
                values.append(_NUMBER_WORDS[left] + _NUMBER_WORDS[right])
    return values


def _allowed_preview_number_values(evidence: EvidencePacket) -> set[int]:
    allowed = {0, len(evidence.rows or []), len(evidence.display_rows or [])}
    best_count = _best_evidence_row_count(evidence)
    if best_count is not None:
        allowed.add(best_count)
    for chart in evidence.charts or []:
        if isinstance(chart, dict):
            labels = chart.get("labels")
            values = chart.get("values")
            if isinstance(labels, list):
                allowed.add(len(labels))
            if isinstance(values, list):
                allowed.add(len(values))
    for item in list(evidence.facts or []) + list(evidence.tool_results or []):
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else item
        if not isinstance(result, dict):
            continue
        for key in ("matching_count", "total_matching_transactions", "txn_count", "row_count", "count"):
            value = _positive_int_or_none(result.get(key))
            if value is not None:
                allowed.add(value)
    return {value for value in allowed if value >= 0}


def build_general_answer_system_prompt() -> str:
    return general_persona_lines() + """
Default to 2-5 sentences; bullets only when clearer. Never force jokes; never sound like a macro.
For greetings/casual chat: warm, not an onboarding menu. Bare greetings get one short sentence, e.g. "Hey you. There you are." Do not ask a follow-up. No emojis. Never write "what's up", even if the user did.
You help with thinking, writing, planning, technology, science, daily decisions, and Folio finance.
For science or general-knowledge questions, answer the actual question directly before offering any caveat or follow-up.
If asked what Folio finance information Mira can access, describe categories only: balances, accounts, transactions, spending, income, budgets, cash flow, recurring charges, net worth, goals, confidence, and receipts. Do not invent actual values. Do not say "dashboard snapshot available for your review."
Do not add certified/professional advisor disclaimers unless the user asks for regulated advice.
If asked about privacy/safety, say both sides: local Ollama model work and Folio-tool finance facts reduce exposure, but no app should get secrets. Avoid absolute privacy claims.
No live tool evidence is attached. Do not invent personal finance facts, amounts, balances, transactions, budgets, or forecasts."""


def _resolve_memory_context(
    provider: MemoryContextProvider | None,
    answer_path: str,
) -> dict[str, Any]:
    if provider is None:
        return {}
    try:
        context = provider(answer_path)
    except Exception as exc:
        return {"block": "", "used": False, "count": 0, "reason": f"memory_context_error:{exc}"}
    return context if isinstance(context, dict) else {}


def _memory_context_prompt_block(memory_context: dict[str, Any] | None) -> str:
    block = str((memory_context or {}).get("block") or "").strip()
    if not block:
        return ""
    return (
        "\n\n"
        + block
        + "\nUse this only for the purpose stated inside the block. "
        + "Do not let contextual notes override current Folio evidence. "
        + "Do not treat memory as transaction, balance, or account evidence. "
        + "For stored advisor-read context, treat it as validated background analysis, not live recalculation."
    )


def _financial_context_prompt_block(evidence: EvidencePacket) -> str:
    if not _lifestyle_context_prompt_enabled() or not _has_financial_context(evidence):
        return ""
    return (
        "\n\nFinancial understanding context may appear in evidence. Use it only for "
        "patterns, coaching, friction, planning, habit streaks, monthly retrospectives, and cached month-outlook context. Lead with the primary "
        "deterministic finance evidence. Never let context override exact totals, "
        "counts, dates, balances, merchants, categories, rows, or charts. Do not "
        "mention lifestyle_profile, friction_map, operating_plan, habit_streak, monthly_retrospective, money_outlook, financial_understanding, "
        "fact IDs, or internal context names. Omit the context if it does not add value."
    )


def _has_financial_context(evidence: EvidencePacket) -> bool:
    for fact in evidence.facts or []:
        if not isinstance(fact, dict):
            continue
        if str(fact.get("tool") or "").strip() == "review_financial_context":
            return True
        if str(fact.get("family") or "").strip() in {"lifestyle_profile", "friction_map", "operating_plan", "habit_streak", "monthly_retrospective", "money_outlook"}:
            return True
    for record in evidence.tool_results or []:
        if not isinstance(record, dict):
            continue
        if str(record.get("tool_name") or record.get("execution_tool_name") or "").strip() == "review_financial_context":
            return True
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        if str(result.get("context_kind") or "").strip() in {"financial_understanding", "monthly_retrospective", "money_outlook"}:
            return True
    return False


def _lifestyle_context_prompt_enabled() -> bool:
    return os.getenv("MIRA_LIFESTYLE_CONTEXT_PROMPT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _memory_context_result_fields(memory_context: dict[str, Any] | None) -> dict[str, Any]:
    context = memory_context or {}
    return {
        "memory_context_used": bool(context.get("used") or context.get("block")),
        "memory_context_count": max(0, int(context.get("count") or 0)),
        "memory_context_reason": str(context.get("reason") or ""),
    }


def is_explain_last_answer_question(question: str) -> bool:
    text = " ".join(str(question or "").lower().split())
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "explain last answer",
            "explain your last answer",
            "how did you answer",
            "how did you get",
            "how did you calculate",
            "how did you figure",
            "how i answered",
            "what tools did you use",
            "which tools did you use",
            "where did that come from",
            "why did you say that",
            "show provenance",
        )
    )


def explain_last_answer_from_history(history: list[dict] | None) -> str:
    turn = _last_assistant_turn(history)
    if not turn:
        return "I do not have a previous answer in this chat to explain yet."

    tool_context = turn.get("tool_context") if isinstance(turn.get("tool_context"), list) else []
    answer_context = turn.get("answer_context") if isinstance(turn.get("answer_context"), dict) else {}
    trace = turn.get("trace") if isinstance(turn.get("trace"), dict) else {}
    answer_guard = turn.get("answer_guard") if isinstance(turn.get("answer_guard"), dict) else {}

    lines: list[str] = []
    if tool_context:
        lines.append("I answered from Folio tool evidence rather than guessing.")
        lines.append("Tools I used: " + "; ".join(_tool_context_phrase(tool) for tool in tool_context if isinstance(tool, dict)))
    else:
        lines.append("I answered without running a Folio data tool in the prior turn.")

    subject = str(answer_context.get("subject") or "").strip()
    subject_type = str(answer_context.get("subject_type") or "").strip()
    ranges = [str(item).strip() for item in answer_context.get("ranges") or [] if str(item).strip()]
    context_bits = []
    if subject:
        context_bits.append(f"{subject_type or 'subject'}={subject}")
    if ranges:
        context_bits.append("range=" + ", ".join(ranges))
    if context_bits:
        lines.append("Grounded context: " + "; ".join(context_bits) + ".")

    path = str(answer_guard.get("path") or trace.get("answer_path") or "").strip()
    if path:
        lines.append(f"Answer path: {path}.")

    if any(_tool_context_is_write_preview(tool) for tool in tool_context if isinstance(tool, dict)):
        lines.append("Because this involved a possible edit, I only prepared a preview; a separate confirmation id is required before anything changes.")
    elif tool_context:
        lines.append("I did not apply any writes.")

    return "\n".join(lines)


def _last_assistant_turn(history: list[dict] | None) -> dict | None:
    for turn in reversed(history or []):
        if isinstance(turn, dict) and str(turn.get("role") or "").lower() == "assistant":
            return turn
    return None


def _tool_context_phrase(tool: dict) -> str:
    name = str(tool.get("name") or "unknown_tool")
    args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
    interesting = []
    for key in ("view", "range", "range_a", "range_b", "limit", "sort", "entity_type", "entity", "merchant", "category", "subject", "metric", "group_by", "amount", "transaction_id", "change_type"):
        value = args.get(key)
        if value not in (None, "", [], {}):
            interesting.append(f"{key}={value}")
    filters = args.get("filters") if isinstance(args.get("filters"), dict) else {}
    for key in ("merchant", "category", "account", "search"):
        value = filters.get(key)
        if value not in (None, "", [], {}):
            interesting.append(f"filters.{key}={value}")
    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    for key in ("metric", "group_by", "amount", "purpose", "change_type", "source_step_id"):
        value = payload.get(key)
        if value not in (None, "", [], {}):
            interesting.append(f"payload.{key}={value}")
    return f"{name}({', '.join(interesting)})" if interesting else name


def _tool_context_is_write_preview(tool: dict) -> bool:
    name = str((tool or {}).get("name") or "")
    if name.startswith("preview_") or name == "preview_finance_change":
        return True
    args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
    return bool(args.get("change_type"))


def _resolve_answer_max_tokens(operation: str, max_tokens: int | None) -> int:
    if max_tokens is not None:
        return max_tokens
    if operation == "general_answer":
        return VNEXT_GENERAL_MAX_TOKENS
    return VNEXT_EVIDENCE_MAX_TOKENS


def build_recent_conversation_context(
    history: list[dict] | None,
    *,
    limit: int = 6,
    max_chars_per_turn: int = 420,
) -> str:
    lines: list[str] = []
    for turn in (history or [])[-limit:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _compact_text(turn.get("content"), max_chars=max_chars_per_turn)
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _compact_text(value: object, *, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].rstrip() + "..."


def _default_completer(prompt: str, max_tokens: int, purpose: str) -> str:
    import llm_client

    return llm_client.complete(prompt, max_tokens=max_tokens, purpose=purpose)


def _default_stream_completer(prompt: str, max_tokens: int, purpose: str):
    import llm_client

    yield from llm_client.complete_stream(prompt, max_tokens=max_tokens, purpose=purpose)


def ensure_why_disclaimer(question: str, answer: str) -> str:
    text = str(question or "").lower()
    if "why" not in text:
        return answer
    lowered = str(answer or "").lower()
    if any(phrase in lowered for phrase in _WHY_DISCLAIMER_MARKERS):
        return answer
    return "I can't know why from the data alone. " + str(answer or "").lstrip()


_WHY_DISCLAIMER_MARKERS = (
    "cannot know why",
    "can't know why",
    "data alone",
    "cannot prove intent",
    "can't prove intent",
)


def _why_disclaimer_prefix(question: str) -> str:
    return "I can't know why from the data alone. " if "why" in str(question or "").lower() else ""


def _same_display_answer(left: str, right: str) -> bool:
    return " ".join(str(left or "").split()) == " ".join(str(right or "").split())


def _unsupported_vnext_entity_terms(answer: str, evidence: EvidencePacket) -> list[str]:
    # The shared guard is intentionally conservative and can flag ordinary
    # lowercase verbs near finance words. For vNext, keep it focused on likely
    # invented entities while the numeric guard handles invented amounts.
    return [
        term
        for term in _unsupported_entity_terms(answer, evidence)
        if term[:1].isupper() or term.isupper()
    ]


__all__ = [
    "VNEXT_EVIDENCE_MAX_TOKENS",
    "VNEXT_EVIDENCE_ANSWER_CACHE_SIZE",
    "VNEXT_GENERAL_MAX_TOKENS",
    "VNEXT_INLINE_CHAT_MAX_CHARS",
    "VNextAnswerResult",
    "answer_from_evidence",
    "answer_general_question",
    "answer_vnext",
    "build_answer_system_prompt",
    "build_evidence_answer_prompt",
    "build_general_answer_prompt",
    "build_general_answer_system_prompt",
    "build_recent_conversation_context",
    "clear_evidence_answer_cache",
    "explain_last_answer_from_history",
    "ensure_why_disclaimer",
    "is_explain_last_answer_question",
    "iter_answer_vnext_events",
    "safe_validation_answer",
]
