from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


def _dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return copy.deepcopy(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class ToolPlanStep:
    step_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    depends_on: list[str] = field(default_factory=list)
    allow_parallel: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, index: int = 1) -> "ToolPlanStep":
        if not isinstance(payload, dict):
            raise ValueError("tool plan step must be an object")
        step_id = _text(payload.get("step_id") or payload.get("id") or f"step_{index}")
        tool_name = _text(payload.get("tool_name") or payload.get("name"))
        if not tool_name:
            raise ValueError("tool plan step is missing tool_name")
        depends_on = [_text(item) for item in _list(payload.get("depends_on")) if _text(item)]
        return cls(
            step_id=step_id,
            tool_name=tool_name,
            args=_dict(payload.get("args")),
            reason=_text(payload.get("reason")),
            depends_on=depends_on,
            allow_parallel=_bool(payload.get("allow_parallel", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "args": copy.deepcopy(self.args),
            "reason": self.reason,
            "depends_on": list(self.depends_on or []),
            "allow_parallel": bool(self.allow_parallel),
        }


@dataclass(frozen=True)
class AgentDecision:
    intent: str
    turn_kind: str
    tool_plan: list[ToolPlanStep] = field(default_factory=list)
    answer_mode: str = "concise"
    needs_clarification: bool = False
    clarification_question: str = ""
    confidence: float = 0.0
    uses_history: bool = False
    reasoning_summary: str = ""

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        allowed_tools: set[str] | None = None,
    ) -> "AgentDecision":
        if not isinstance(payload, dict):
            raise ValueError("agent decision must be an object")
        if isinstance(payload.get("agent_decision"), dict):
            payload = payload["agent_decision"]
        elif isinstance(payload.get("decision"), dict):
            payload = payload["decision"]

        steps = [
            ToolPlanStep.from_dict(item, index=index)
            for index, item in enumerate(_list(payload.get("tool_plan")), start=1)
        ]
        seen_ids: set[str] = set()
        for step in steps:
            if step.step_id in seen_ids:
                raise ValueError(f"duplicate step_id: {step.step_id}")
            seen_ids.add(step.step_id)
            if step.tool_name == "run_sql":
                raise ValueError("disallowed internal tool")
            if allowed_tools is not None and step.tool_name not in allowed_tools:
                raise ValueError(f"unknown tool: {step.tool_name}")

        return cls(
            intent=_text(payload.get("intent") or "unknown"),
            turn_kind=_text(payload.get("turn_kind") or "finance"),
            tool_plan=steps,
            answer_mode=_text(payload.get("answer_mode") or "concise"),
            needs_clarification=_bool(payload.get("needs_clarification")),
            clarification_question=_text(payload.get("clarification_question")),
            confidence=_confidence(payload.get("confidence")),
            uses_history=_bool(payload.get("uses_history")),
            reasoning_summary=_text(payload.get("reasoning_summary")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "turn_kind": self.turn_kind,
            "tool_plan": [step.to_dict() for step in self.tool_plan],
            "answer_mode": self.answer_mode,
            "needs_clarification": bool(self.needs_clarification),
            "clarification_question": self.clarification_question,
            "confidence": self.confidence,
            "uses_history": bool(self.uses_history),
            "reasoning_summary": self.reasoning_summary,
        }


@dataclass(frozen=True)
class ValidationResult:
    status: str
    decision: AgentDecision
    normalized_plan: list[ToolPlanStep] = field(default_factory=list)
    grounded_entities: list[dict[str, Any]] = field(default_factory=list)
    clarification_question: str = ""
    blocked_reason: str = ""
    pending_clarification: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision.to_dict(),
            "normalized_plan": [step.to_dict() for step in self.normalized_plan],
            "grounded_entities": copy.deepcopy(self.grounded_entities),
            "clarification_question": self.clarification_question,
            "blocked_reason": self.blocked_reason,
            "pending_clarification": copy.deepcopy(self.pending_clarification),
        }


@dataclass(frozen=True)
class EvidencePacket:
    question: str
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    display_rows: list[dict[str, Any]] = field(default_factory=list)
    charts: list[dict[str, Any]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "tool_results": copy.deepcopy(self.tool_results),
            "facts": copy.deepcopy(self.facts),
            "rows": copy.deepcopy(self.rows),
            "charts": copy.deepcopy(self.charts),
            "caveats": list(self.caveats or []),
            "provenance": copy.deepcopy(self.provenance),
        }
