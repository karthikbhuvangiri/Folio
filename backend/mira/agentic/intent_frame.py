from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from range_parser import resolve_followup_range


ROUTE_VALUES = {"chat", "finance", "memory", "write_preview", "explain_last", "clarify_response"}
ROUTE_ALIASES = {
    "finance_tool": "finance",
    "general_answer": "chat",
    "explain_last_answer": "explain_last",
}

INTENT_VALUES = {
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
}
INTENT_ALIASES = {
    "spend": "spending_total",
    "spend_total": "spending_total",
    "spending": "spending_total",
    "recategorize": "write_preview",
    "recategorize_transactions": "write_preview",
}

SUBJECT_KIND_VALUES = {
    "account",
    "category",
    "merchant",
    "metric",
    "net_worth",
    "none",
    "self",
    "transaction",
    "unknown",
}

TIME_VALUES = {
    "all_time",
    "custom",
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
}
TIME_ALIASES = {
    "all": "all_time",
    "current": "this_month",
    "current_month": "this_month",
    "last month": "last_month",
    "prior_month": "last_month",
    "previous_month": "last_month",
    "this month": "this_month",
}

OUTPUT_VALUES = {"chart", "comparison", "list", "none", "preview", "scalar", "status", "table"}
OUTPUT_ALIASES = {
    "rows": "table",
    "query": "table",
    "scalar_total": "scalar",
    "total": "scalar",
}

CHART_TYPE_VALUES = {"bar", "donut", "line", "pie"}
CHART_TYPE_ALIASES = {
    "column": "bar",
}

DISCOURSE_ACTION_VALUES = {
    "clarification_reply",
    "clear",
    "correction",
    "follow_up",
    "new",
    "refine",
}
DISCOURSE_ACTION_ALIASES = {
    "followup": "follow_up",
    "inherit": "follow_up",
    "patch_prior": "follow_up",
}
MEMORY_ACTION_VALUES = {"forget", "list", "none", "remember", "retrieve", "update"}
MEMORY_ACTION_ALIASES = {
    "delete": "forget",
    "read": "retrieve",
    "recall": "retrieve",
    "remove": "forget",
    "save": "remember",
    "search": "retrieve",
    "store": "remember",
}

EXECUTABLE_SELECTOR_KEYS = {
    "args",
    "calls",
    "depends_on",
    "filters",
    "name",
    "payload",
    "range_source",
    "source_step_id",
    "tool",
    "tool_name",
    "view",
}


def _dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


_EMPTY_TEXT_VALUES = {"", "none", "null", "n/a", "na"}


def _nullable_text(value: Any) -> str | None:
    text = _text(value)
    return None if text.lower() in _EMPTY_TEXT_VALUES else text


def _canonical(value: Any, *, default: str, aliases: dict[str, str] | None = None) -> str:
    text = _text(value).lower()
    if not text:
        return default
    aliases = aliases or {}
    return aliases.get(text, text)


def _require_allowed(field: str, value: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(sorted(allowed))}; got {value or '<empty>'}")
    return value


def _is_dynamic_range_time(value: str) -> bool:
    text = str(value or "").strip().lower()
    months = re.match(r"^last_(\d{1,2})_months$", text)
    if months:
        return 1 <= int(months.group(1)) <= 36
    days = re.match(r"^last_(\d{1,3})d$", text)
    if days:
        return 1 <= int(days.group(1)) <= 365
    return False


def is_supported_time_token(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text in TIME_VALUES or _is_dynamic_range_time(text)


def _require_allowed_time(value: str) -> str:
    if is_supported_time_token(value):
        return value
    raise ValueError(f"time must be one of the supported time tokens; got {value or '<empty>'}")


def _reject_executable_keys(payload: dict[str, Any]) -> None:
    present = sorted(key for key in EXECUTABLE_SELECTOR_KEYS if key in payload)
    if present:
        raise ValueError("intent frame cannot include executable selector key(s): " + ", ".join(present))


def _validate_iso_date(field: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ValueError(f"{field} must be an ISO date in YYYY-MM-DD format")
    return value


@dataclass(frozen=True)
class MiraSubject:
    kind: str = "none"
    text: str | None = None
    canonical_id: str | None = None
    display_name: str | None = None
    confidence: float | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "MiraSubject":
        data = _dict(payload)
        kind = _canonical(data.get("kind"), default="none")
        _require_allowed("subject.kind", kind, SUBJECT_KIND_VALUES)
        return cls(
            kind=kind,
            text=_nullable_text(data.get("text")),
            canonical_id=_nullable_text(data.get("canonical_id")),
            display_name=_nullable_text(data.get("display_name")),
            confidence=_confidence_or_none(data.get("confidence")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "canonical_id": self.canonical_id,
            "display_name": self.display_name,
            "confidence": self.confidence,
        }

    @property
    def is_empty(self) -> bool:
        return self.kind in {"none", "unknown"} and not any(
            (self.text, self.canonical_id, self.display_name)
        )


@dataclass(frozen=True)
class MiraMemoryOperation:
    action: str = "none"
    text: str | None = None
    question: str | None = None
    topic: str | None = None
    memory_id: Any = None
    memory_type: str | None = None
    limit: Any = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "MiraMemoryOperation":
        data = _dict(payload)
        if not data:
            return cls()
        _reject_executable_keys(data)
        action = _canonical(data.get("action") or data.get("memory_action"), default="none", aliases=MEMORY_ACTION_ALIASES)
        text = _nullable_text(data.get("text"))
        question = _nullable_text(data.get("question") or data.get("search"))
        topic = _nullable_text(data.get("topic"))
        if action == "none":
            if text:
                action = "remember"
            elif question or topic:
                action = "retrieve"
        _require_allowed("memory.action", action, MEMORY_ACTION_VALUES)
        return cls(
            action=action,
            text=text,
            question=question,
            topic=topic,
            memory_id=data.get("memory_id"),
            memory_type=_nullable_text(data.get("memory_type")),
            limit=data.get("limit"),
        )

    @property
    def is_empty(self) -> bool:
        return (
            self.action == "none"
            and not self.text
            and not self.question
            and not self.topic
            and self.memory_id in (None, "")
            and not self.memory_type
            and self.limit in (None, "")
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action}
        for key, value in (
            ("text", self.text),
            ("question", self.question),
            ("topic", self.topic),
            ("memory_id", self.memory_id),
            ("memory_type", self.memory_type),
            ("limit", self.limit),
        ):
            if value not in (None, ""):
                out[key] = value
        return out


@dataclass(frozen=True)
class MiraIntentFrame:
    route: str
    intent: str
    subject: MiraSubject = field(default_factory=MiraSubject)
    time: str = "none"
    time_a: str | None = None
    time_b: str | None = None
    output: str = "none"
    discourse_action: str = "new"
    answer: str = ""
    chart_type: str | None = None
    memory: MiraMemoryOperation = field(default_factory=MiraMemoryOperation)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MiraIntentFrame":
        if not isinstance(payload, dict):
            raise ValueError("intent frame must be an object")
        _reject_executable_keys(payload)

        route = _canonical(payload.get("route"), default="chat", aliases=ROUTE_ALIASES)
        intent = _canonical(payload.get("intent"), default="none", aliases=INTENT_ALIASES)
        time = _canonical(payload.get("time"), default="none", aliases=TIME_ALIASES)
        output = _canonical(payload.get("output"), default="none", aliases=OUTPUT_ALIASES)
        discourse_action = _canonical(payload.get("discourse_action"), default="new", aliases=DISCOURSE_ACTION_ALIASES)
        chart_type = _canonical(payload.get("chart_type"), default="", aliases=CHART_TYPE_ALIASES) or None

        _require_allowed("route", route, ROUTE_VALUES)
        _require_allowed("intent", intent, INTENT_VALUES)
        _require_allowed_time(time)
        _require_allowed("output", output, OUTPUT_VALUES)
        _require_allowed("discourse_action", discourse_action, DISCOURSE_ACTION_VALUES)
        if chart_type is not None:
            _require_allowed("chart_type", chart_type, CHART_TYPE_VALUES)

        return cls(
            route=route,
            intent=intent,
            subject=MiraSubject.from_dict(payload.get("subject") if isinstance(payload.get("subject"), dict) else None),
            time=time,
            time_a=_validate_iso_date("time_a", _nullable_text(payload.get("time_a"))),
            time_b=_validate_iso_date("time_b", _nullable_text(payload.get("time_b"))),
            output=output,
            chart_type=chart_type,
            discourse_action=discourse_action,
            answer=_text(payload.get("answer")),
            memory=MiraMemoryOperation.from_dict(payload.get("memory") if isinstance(payload.get("memory"), dict) else None),
        )

    def to_dict(self) -> dict[str, Any]:
        out = {
            "route": self.route,
            "answer": self.answer,
            "intent": self.intent,
            "subject": self.subject.to_dict(),
            "time": self.time,
            "time_a": self.time_a,
            "time_b": self.time_b,
            "output": self.output,
            "chart_type": self.chart_type,
            "discourse_action": self.discourse_action,
        }
        if not self.memory.is_empty:
            out["memory"] = self.memory.to_dict()
        return out


@dataclass(frozen=True)
class ConversationFrame:
    route: str = "chat"
    intent: str = "none"
    subject: MiraSubject = field(default_factory=MiraSubject)
    time: str = "none"
    time_a: str | None = None
    time_b: str | None = None
    output: str = "none"
    chart_type: str | None = None
    last_evidence_step_id: str | None = None
    last_backend_tool: str | None = None
    pending_clarification: dict[str, Any] = field(default_factory=dict)
    evidence_stale: bool = False
    force_reground: bool = False
    memory: MiraMemoryOperation = field(default_factory=MiraMemoryOperation)

    @classmethod
    def from_intent_frame(cls, frame: MiraIntentFrame) -> "ConversationFrame":
        return cls(
            route=frame.route,
            intent=frame.intent,
            subject=frame.subject,
            time=frame.time,
            time_a=frame.time_a,
            time_b=frame.time_b,
            output=frame.output,
            chart_type=frame.chart_type,
            evidence_stale=frame.discourse_action == "correction",
            force_reground=frame.discourse_action in {"correction", "refine"},
            memory=frame.memory,
        )

    @classmethod
    def merge(
        cls,
        prior: "ConversationFrame | dict[str, Any] | None",
        frame: MiraIntentFrame,
    ) -> "ConversationFrame":
        prior_frame = prior if isinstance(prior, ConversationFrame) else cls.from_dict(prior) if isinstance(prior, dict) else None
        action = frame.discourse_action
        if action == "clear":
            return cls()
        if action == "new" and prior_frame is not None and _looks_like_range_only_followup(prior_frame, frame):
            return cls(
                route=frame.route,
                intent=frame.intent if frame.intent != "none" else prior_frame.intent,
                subject=prior_frame.subject,
                time=frame.time,
                time_a=frame.time_a,
                time_b=frame.time_b,
                output=frame.output if frame.output != "none" else prior_frame.output,
                chart_type=frame.chart_type if frame.chart_type is not None else prior_frame.chart_type,
                last_evidence_step_id=prior_frame.last_evidence_step_id,
                last_backend_tool=prior_frame.last_backend_tool,
                pending_clarification=copy.deepcopy(prior_frame.pending_clarification),
                memory=frame.memory,
            )
        if action == "new" or prior_frame is None:
            return cls.from_intent_frame(frame)
        if _is_pure_chat_frame(frame):
            return cls.from_intent_frame(frame)
        if prior_frame.route == "chat" and frame.route != "chat":
            return cls.from_intent_frame(frame)

        subject = _merge_subject(prior_frame.subject, frame.subject)
        time, time_a, time_b = _merge_time(prior_frame, frame)
        output = frame.output if frame.output != "none" else prior_frame.output
        route = frame.route if frame.route != "chat" or prior_frame.route == "chat" else prior_frame.route
        intent = frame.intent if frame.intent != "none" else prior_frame.intent

        return cls(
            route=route,
            intent=intent,
            subject=subject,
            time=time,
            time_a=time_a,
            time_b=time_b,
            output=output,
            chart_type=frame.chart_type if frame.chart_type is not None else prior_frame.chart_type,
            last_evidence_step_id=prior_frame.last_evidence_step_id,
            last_backend_tool=prior_frame.last_backend_tool,
            pending_clarification={} if action == "clarification_reply" else copy.deepcopy(prior_frame.pending_clarification),
            evidence_stale=action in {"correction", "refine"} or prior_frame.evidence_stale,
            force_reground=action in {"correction", "refine"} or prior_frame.force_reground,
            memory=frame.memory,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ConversationFrame":
        data = _dict(payload)
        route = _canonical(data.get("route"), default="chat", aliases=ROUTE_ALIASES)
        intent = _canonical(data.get("intent"), default="none")
        time = _canonical(data.get("time"), default="none", aliases=TIME_ALIASES)
        output = _canonical(data.get("output"), default="none", aliases=OUTPUT_ALIASES)
        chart_type = _canonical(data.get("chart_type"), default="", aliases=CHART_TYPE_ALIASES) or None

        _require_allowed("route", route, ROUTE_VALUES)
        _require_allowed("intent", intent, INTENT_VALUES)
        _require_allowed_time(time)
        _require_allowed("output", output, OUTPUT_VALUES)
        if chart_type is not None:
            _require_allowed("chart_type", chart_type, CHART_TYPE_VALUES)

        return cls(
            route=route,
            intent=intent,
            subject=MiraSubject.from_dict(data.get("subject") if isinstance(data.get("subject"), dict) else None),
            time=time,
            time_a=_validate_iso_date("time_a", _nullable_text(data.get("time_a"))),
            time_b=_validate_iso_date("time_b", _nullable_text(data.get("time_b"))),
            output=output,
            chart_type=chart_type,
            last_evidence_step_id=_nullable_text(data.get("last_evidence_step_id")),
            last_backend_tool=_nullable_text(data.get("last_backend_tool")),
            pending_clarification=_dict(data.get("pending_clarification")),
            evidence_stale=bool(data.get("evidence_stale")),
            force_reground=bool(data.get("force_reground")),
            memory=MiraMemoryOperation.from_dict(data.get("memory") if isinstance(data.get("memory"), dict) else None),
        )

    @classmethod
    def from_answer_context(cls, payload: dict[str, Any] | None) -> "ConversationFrame | None":
        context = _dict(payload)
        frame_payload = context.get("mira_conversation_frame")
        if not isinstance(frame_payload, dict):
            frame_payload = context.get("conversation_frame")
            if isinstance(frame_payload, dict) and any(key in frame_payload for key in ("tool", "view", "requested_output")):
                return None
        if not isinstance(frame_payload, dict):
            return None
        return cls.from_dict(frame_payload)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "route": self.route,
            "intent": self.intent,
            "subject": self.subject.to_dict(),
            "time": self.time,
            "time_a": self.time_a,
            "time_b": self.time_b,
            "output": self.output,
            "chart_type": self.chart_type,
            "last_evidence_step_id": self.last_evidence_step_id,
            "last_backend_tool": self.last_backend_tool,
            "pending_clarification": copy.deepcopy(self.pending_clarification),
            "evidence_stale": self.evidence_stale,
            "force_reground": self.force_reground,
        }
        if not self.memory.is_empty:
            out["memory"] = self.memory.to_dict()
        return out

    def to_answer_context(self) -> dict[str, Any]:
        return {
            "version": 3,
            "kind": "mira_conversation_frame",
            "mira_conversation_frame": self.to_dict(),
        }


def _merge_subject(prior: MiraSubject, incoming: MiraSubject) -> MiraSubject:
    return prior if incoming.is_empty else incoming


def _merge_time(prior: ConversationFrame, incoming: MiraIntentFrame) -> tuple[str, str | None, str | None]:
    if incoming.time == "none":
        return prior.time, prior.time_a, prior.time_b
    if incoming.time in {"month_before_prior", "next_month_after_prior"}:
        prior_range = _range_token_from_conversation(prior)
        if not prior_range:
            return incoming.time, incoming.time_a, incoming.time_b
        phrase = "month before" if incoming.time == "month_before_prior" else "month after"
        parsed = resolve_followup_range(phrase, prior_range)
        return _time_from_range_token(parsed.token)
    return incoming.time, incoming.time_a, incoming.time_b


def _range_token_from_conversation(frame: ConversationFrame) -> str:
    if frame.time == "custom" and frame.time_a and re.match(r"^\d{4}-\d{2}-\d{2}$", frame.time_a):
        return frame.time_a[:7]
    if _is_dynamic_range_time(frame.time):
        return frame.time
    aliases = {
        "all_time": "all",
        "last_month": "last_month",
        "this_month": "current_month",
    }
    return aliases.get(frame.time, frame.time)


def _time_from_range_token(token: str) -> tuple[str, str | None, str | None]:
    value = str(token or "").strip().lower()
    aliases = {
        "all": "all_time",
        "current_month": "this_month",
    }
    value = aliases.get(value, value)
    if re.match(r"^\d{4}-\d{2}$", value):
        return "custom", f"{value}-01", None
    if is_supported_time_token(value):
        return value, None, None
    return "custom" if value else "none", None, None


def _looks_like_range_only_followup(prior: ConversationFrame, incoming: MiraIntentFrame) -> bool:
    if incoming.route != "finance" or prior.route != "finance":
        return False
    if prior.subject.is_empty or not incoming.subject.is_empty:
        return False
    if incoming.time == "none":
        return False
    return incoming.intent in {"none", "spending_total", "transaction_lookup"}


def _is_pure_chat_frame(frame: MiraIntentFrame) -> bool:
    return (
        frame.route == "chat"
        and frame.intent == "none"
        and frame.subject.is_empty
        and frame.time == "none"
        and frame.output == "none"
    )


def _confidence_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        raise ValueError("subject.confidence must be a number between 0 and 1") from None


__all__ = [
    "CHART_TYPE_VALUES",
    "ConversationFrame",
    "DISCOURSE_ACTION_VALUES",
    "EXECUTABLE_SELECTOR_KEYS",
    "INTENT_VALUES",
    "MiraIntentFrame",
    "MiraMemoryOperation",
    "MiraSubject",
    "OUTPUT_VALUES",
    "ROUTE_VALUES",
    "SUBJECT_KIND_VALUES",
    "TIME_VALUES",
    "is_supported_time_token",
]
