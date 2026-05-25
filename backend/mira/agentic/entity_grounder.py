from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any

from mira.agentic import vnext_validator
from mira.agentic.intent_frame import ConversationFrame, MiraSubject
from mira.agentic.vnext_validator import Grounder


GROUNDABLE_SUBJECT_KINDS = {"account", "category", "merchant", "unknown"}
GROUNDABLE_ROUTES = {"finance", "write_preview"}
NET_WORTH_INTENTS = {"net_worth_balance", "net_worth_delta", "net_worth_trend"}
SPENDING_INTENTS = {
    "spending_breakdown",
    "spending_compare",
    "spending_explain",
    "spending_top",
    "spending_total",
    "spending_trend",
}
SPENDING_METRIC_SUBJECTS = {
    "cost",
    "costs",
    "expense",
    "expenses",
    "expense trend",
    "expenses trend",
    "monthly expense",
    "monthly expenses",
    "monthly spend",
    "monthly spending",
    "spend",
    "spend trend",
    "spending",
    "spending trend",
    "total",
}
SUBJECTLESS_FINANCE_INTENTS = {
    "cashflow_forecast",
    "cashflow_shortfall",
    "data_health",
    "enrichment_quality",
    "finance_priorities",
    "finance_snapshot",
    "low_confidence_transactions",
    "recurring_changes",
    "recurring_summary",
}


@dataclass(frozen=True)
class FrameGroundingResult:
    frame: ConversationFrame | None = None
    status: str = "ready"
    message: str = ""
    entities: list[dict[str, Any]] = field(default_factory=list)
    pending_clarification: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ready"


def ground_conversation_frame(
    frame: ConversationFrame | None,
    *,
    profile: str | None = None,
    grounder: Grounder | None = None,
    source_text: str | None = None,
) -> FrameGroundingResult:
    if frame is None:
        return FrameGroundingResult(status="ready", trace={"status": "skipped", "reason": "missing_frame"})
    if frame.route not in GROUNDABLE_ROUTES:
        return FrameGroundingResult(frame=frame, status="ready", trace={"status": "skipped", "reason": "route"})

    frame, normalized_trace = _normalize_subject_for_intent(frame)
    subject = frame.subject
    text = str(subject.text or subject.display_name or subject.canonical_id or "").strip()
    if subject.kind not in GROUNDABLE_SUBJECT_KINDS or not text:
        trace = {"status": "skipped", "reason": "subject"}
        if normalized_trace:
            trace["subject_normalized_for_intent"] = normalized_trace
        return FrameGroundingResult(frame=frame, status="ready", trace=trace)

    result = _ground_subject(subject, text, profile=profile, grounder=grounder, source_text=source_text)
    record = vnext_validator._ground_record(result, original=text, arg_key="intent_frame.subject")
    trace = {
        "status": "grounded",
        "original_kind": subject.kind,
        "original_text": text,
        "result_kind": result.get("kind"),
        "entity_type": result.get("entity_type"),
        "value": result.get("value"),
        "confidence": result.get("confidence", 0.0),
    }
    if normalized_trace:
        trace["subject_normalized_for_intent"] = normalized_trace

    if result.get("kind") == "ambiguous":
        pending = _entity_resolution_pending(raw=text, result=result, frame=frame)
        return FrameGroundingResult(
            frame=frame,
            status="clarify",
            message=_entity_resolution_question(text, pending),
            entities=[record],
            pending_clarification=pending,
            trace={**trace, "status": "clarify"},
        )
    if result.get("kind") == "missing" or not result.get("value"):
        pending = _entity_resolution_pending(raw=text, result=result, frame=frame)
        return FrameGroundingResult(
            frame=frame,
            status="clarify",
            message=f"I couldn't confidently match `{text}` to a merchant, category, or account in your data. Which one should I use?",
            entities=[record],
            pending_clarification=pending,
            trace={**trace, "status": "clarify"},
        )

    entity_type = str(result.get("entity_type") or subject.kind).strip()
    value = str(result.get("value") or result.get("display_name") or text).strip()
    grounded_subject = MiraSubject(
        kind=entity_type if entity_type in GROUNDABLE_SUBJECT_KINDS else subject.kind,
        text=value,
        canonical_id=_nullable_text(result.get("canonical_id") or value),
        display_name=_nullable_text(result.get("display_name") or value),
        confidence=_confidence_or_none(result.get("confidence")),
    )
    return FrameGroundingResult(
        frame=replace(frame, subject=grounded_subject, force_reground=False),
        status="ready",
        entities=[record],
        trace=trace,
    )


def _normalize_subject_for_intent(frame: ConversationFrame) -> tuple[ConversationFrame, dict[str, Any]]:
    intent = str(frame.intent or "").strip()
    subject = frame.subject
    placeholder_text = str(subject.text or subject.display_name or subject.canonical_id or "").strip().lower()
    if subject.kind == "unknown" and placeholder_text in {"unknown", "none", "n/a", "na", "null"}:
        normalized = MiraSubject(kind="none", text=None, canonical_id=None, display_name=None, confidence=None)
        return replace(frame, subject=normalized, force_reground=False), {
            "reason": "placeholder_unknown_subject",
            "from_kind": subject.kind,
            "from_text": subject.text,
            "to_kind": "none",
        }
    if intent in NET_WORTH_INTENTS and subject.kind != "net_worth":
        normalized = MiraSubject(kind="net_worth", text="net worth", canonical_id="net_worth", display_name="Net worth", confidence=1.0)
        return replace(frame, subject=normalized, force_reground=False), {
            "reason": "net_worth_intent",
            "from_kind": subject.kind,
            "from_text": subject.text,
            "to_kind": "net_worth",
        }
    metric_text = _spending_metric_subject_text(subject)
    if intent in SPENDING_INTENTS and metric_text:
        normalized = MiraSubject(kind="metric", text=metric_text, canonical_id=metric_text, display_name=metric_text.title(), confidence=1.0)
        return replace(frame, subject=normalized, force_reground=False), {
            "reason": "spending_metric_subject",
            "from_kind": subject.kind,
            "from_text": subject.text,
            "to_kind": "metric",
        }
    if intent == "affordability" and subject.kind in {"merchant", "account", "unknown"}:
        normalized = MiraSubject(kind="none", text=None, canonical_id=None, display_name=None, confidence=None)
        return replace(frame, subject=normalized, force_reground=False), {
            "reason": "generic_affordability_subject",
            "from_kind": subject.kind,
            "from_text": subject.text,
            "to_kind": "none",
        }
    if intent in SUBJECTLESS_FINANCE_INTENTS and subject.kind in GROUNDABLE_SUBJECT_KINDS:
        normalized = MiraSubject(kind="none", text=None, canonical_id=None, display_name=None, confidence=None)
        return replace(frame, subject=normalized, force_reground=False), {
            "reason": "subjectless_finance_intent",
            "from_kind": subject.kind,
            "from_text": subject.text,
            "to_kind": "none",
        }
    return frame, {}


def _spending_metric_subject_text(subject: MiraSubject) -> str:
    if subject.kind not in GROUNDABLE_SUBJECT_KINDS | {"metric"}:
        return ""
    text = str(subject.text or subject.display_name or subject.canonical_id or "").strip().lower()
    text = " ".join(text.replace("_", " ").split())
    if text not in SPENDING_METRIC_SUBJECTS:
        return ""
    return "expenses" if text != "income" else "income"


def _ground_subject(
    subject: MiraSubject,
    text: str,
    *,
    profile: str | None,
    grounder: Grounder | None,
    source_text: str | None = None,
) -> dict[str, Any]:
    if subject.kind == "unknown":
        return _ground_across_types(text, profile=profile, grounder=grounder)

    typed = vnext_validator._ground(subject.kind, text, profile=profile, grounder=grounder)
    cross = _ground_across_types(text, profile=profile, grounder=grounder)
    source = _source_grounding_candidate(source_text, text, profile=profile, grounder=grounder)

    if cross.get("kind") == "ambiguous" and typed.get("kind") == "exact" and typed.get("value"):
        return typed
    if source and _source_grounding_should_win(source, typed=typed, cross=cross):
        return source
    if cross.get("kind") == "ambiguous":
        return cross
    if typed.get("kind") == "ambiguous":
        return typed

    cross_type = str(cross.get("entity_type") or "").strip()
    typed_confidence = _confidence(typed.get("confidence"))
    cross_confidence = _confidence(cross.get("confidence"))
    if (
        cross_type in GROUNDABLE_SUBJECT_KINDS
        and cross_type != subject.kind
        and cross.get("value")
        and (cross.get("kind") == "exact" or cross_confidence >= typed_confidence + 0.1 or typed.get("kind") != "exact")
    ):
        return cross

    if typed.get("kind") in {"exact", "approximate"} and typed.get("value"):
        return typed
    if cross.get("kind") in {"exact", "approximate"} and cross.get("value"):
        return cross
    return typed if typed.get("kind") != "missing" else cross


def _source_grounding_candidate(
    source_text: str | None,
    subject_text: str,
    *,
    profile: str | None,
    grounder: Grounder | None,
) -> dict[str, Any] | None:
    query = str(source_text or "").strip()
    if not query or query.lower() == str(subject_text or "").strip().lower():
        return None
    result = _ground_across_types(query, profile=profile, grounder=grounder)
    if result.get("kind") in {"exact", "approximate"} and result.get("value"):
        return result
    return None


def _source_grounding_should_win(source: dict[str, Any], *, typed: dict[str, Any], cross: dict[str, Any]) -> bool:
    if cross.get("kind") in {"exact", "approximate"} and cross.get("value"):
        return False
    source_kind = str(source.get("kind") or "")
    if source_kind == "exact" and typed.get("kind") != "exact":
        return True
    if typed.get("kind") in {"ambiguous", "missing"} or cross.get("kind") in {"ambiguous", "missing"}:
        return _confidence(source.get("confidence")) >= max(_confidence(typed.get("confidence")), _confidence(cross.get("confidence"))) - 0.05
    return _confidence(source.get("confidence")) >= max(_confidence(typed.get("confidence")), _confidence(cross.get("confidence"))) + 0.15


def _ground_across_types(
    text: str,
    *,
    profile: str | None,
    grounder: Grounder | None,
) -> dict[str, Any]:
    results = [
        vnext_validator._ground("category", text, profile=profile, grounder=grounder),
        vnext_validator._ground("merchant", text, profile=profile, grounder=grounder),
        vnext_validator._ground("account", text, profile=profile, grounder=grounder),
    ]
    candidates: list[dict[str, Any]] = []
    for result in results:
        entity_type = str(result.get("entity_type") or "").strip()
        for candidate in result.get("candidates") or []:
            if isinstance(candidate, dict):
                candidates.append({**candidate, "entity_type": candidate.get("entity_type") or entity_type})
    candidates.sort(key=lambda item: _confidence(item.get("confidence")), reverse=True)

    ambiguous = [result for result in results if result.get("kind") == "ambiguous"]
    if ambiguous:
        ambiguous.sort(key=lambda result: _confidence(result.get("confidence")), reverse=True)
        top = ambiguous[0]
        return {
            **copy.deepcopy(top),
            "candidates": candidates[:6],
        }

    usable = [result for result in results if result.get("kind") in {"exact", "approximate"} and result.get("value")]
    if not usable:
        return {
            "kind": "missing",
            "entity_type": "entity",
            "value": None,
            "canonical_id": None,
            "display_name": None,
            "confidence": 0.0,
            "candidates": candidates[:6],
            "evidence": {"query": text},
        }

    usable.sort(key=lambda result: _confidence(result.get("confidence")), reverse=True)
    top = usable[0]
    close = [
        result
        for result in usable
        if _confidence(result.get("confidence")) >= _confidence(top.get("confidence")) - 0.05
    ]
    if len(close) > 1 and str(close[0].get("entity_type") or "") != str(close[1].get("entity_type") or ""):
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
    return {**copy.deepcopy(top), "candidates": candidates[:6]}


def _entity_resolution_pending(*, raw: str, result: dict[str, Any], frame: ConversationFrame) -> dict[str, Any]:
    options = []
    seen: set[str] = set()
    for candidate in result.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        entity_type = str(candidate.get("entity_type") or result.get("entity_type") or "").strip()
        if entity_type not in {"account", "category", "merchant"}:
            continue
        canonical = str(candidate.get("value") or candidate.get("canonical_id") or candidate.get("display_name") or "").strip()
        label = str(candidate.get("display_name") or canonical).strip()
        if not canonical or not label:
            continue
        option_id = f"{entity_type}:{canonical}"
        if option_id.lower() in seen:
            continue
        seen.add(option_id.lower())
        options.append(
            {
                "id": option_id,
                "type": entity_type,
                "canonical": canonical,
                "label": f"{label} {entity_type}",
                "confidence": candidate.get("confidence"),
            }
        )
    return {
        "kind": "entity_resolution",
        "raw": raw,
        "resume_frame": frame.to_dict(),
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


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _confidence_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return _confidence(value)


def _nullable_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["FrameGroundingResult", "ground_conversation_frame"]
