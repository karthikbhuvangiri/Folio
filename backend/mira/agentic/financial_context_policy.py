from __future__ import annotations

import os
from typing import Any

from mira.agentic.intent_frame import ConversationFrame


_FALSE_VALUES = {"0", "false", "no", "off"}

_ELIGIBLE_INTENTS = {
    "budget_plan",
    "budget_status",
    "cashflow_forecast",
    "cashflow_shortfall",
    "explain_metric",
    "finance_priorities",
    "finance_snapshot",
    "savings_capacity",
    "spending_compare",
    "spending_explain",
}

_MONEY_OUTLOOK_CONTEXT_VIEW = "forecast_month_outlook.snapshot"
_SAFE_TO_SPEND_CONTEXT_VIEW = "review_cashflow.safe_to_spend"
_MONEY_OUTLOOK_INTENTS = {
    "budget_status",
    "cashflow_forecast",
    "cashflow_shortfall",
    "savings_capacity",
}

_EXCLUDED_TOOLS = {
    "make_chart",
    "manage_memory",
    "preview_finance_change",
    "query_transactions",
}


def financial_context_tool_enabled() -> bool:
    return os.getenv("MIRA_FINANCIAL_CONTEXT_TOOL_ENABLED", "1").strip().lower() not in _FALSE_VALUES


def lifestyle_context_prompt_enabled() -> bool:
    return os.getenv("MIRA_LIFESTYLE_CONTEXT_PROMPT_ENABLED", "1").strip().lower() not in _FALSE_VALUES


def money_outlook_context_enabled() -> bool:
    return os.getenv("MIRA_MONEY_OUTLOOK_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def safe_to_spend_context_enabled() -> bool:
    return (
        money_outlook_context_enabled()
        and os.getenv("MIRA_SAFE_TO_SPEND_ENABLED", "0").strip().lower() not in _FALSE_VALUES
    )


def maybe_append_financial_context_call(
    calls: list[dict[str, Any]],
    *,
    frame: ConversationFrame,
) -> list[dict[str, Any]]:
    if not financial_context_tool_enabled() or not calls:
        return calls
    if any(str(call.get("name") or "") == "review_financial_context" for call in calls):
        return calls
    outlook_amount = _affordability_amount_from_calls(calls) if str(frame.intent or "").strip().lower() == "affordability" else None
    attach_safe_to_spend = should_attach_safe_to_spend_context(frame=frame, calls=calls)
    attach_outlook = (not attach_safe_to_spend) and should_attach_money_outlook_context(frame=frame, calls=calls, affordability_amount=outlook_amount)
    if not should_attach_financial_context(frame=frame, calls=calls) and not attach_outlook and not attach_safe_to_spend:
        return calls

    subject = frame.subject
    filters: dict[str, Any] = {}
    if subject.kind in {"category", "merchant", "subscription", "account", "cashflow"} and subject.text:
        filters = {"subject_type": subject.kind, "subject_key": subject.text}

    view = _context_view_for_intent(frame.intent)
    if attach_safe_to_spend:
        view = _SAFE_TO_SPEND_CONTEXT_VIEW
    if attach_outlook:
        view = _MONEY_OUTLOOK_CONTEXT_VIEW

    payload = {
        "intent": frame.intent,
        "max_facts": _max_context_facts(),
    }
    if outlook_amount is not None:
        payload["amount"] = outlook_amount
    args: dict[str, Any] = {"view": view, "payload": payload}
    if filters:
        args["filters"] = filters
    return [
        *calls,
        {
            "id": f"selector_call_{len(calls) + 1}",
            "name": "review_financial_context",
            "args": args,
            "universal_args": args.copy(),
            "compiler_source": "financial_context_policy",
        },
    ]


def should_attach_financial_context(
    *,
    frame: ConversationFrame,
    calls: list[dict[str, Any]],
) -> bool:
    if str(frame.route or "").strip().lower() != "finance":
        return False
    intent = str(frame.intent or "").strip().lower()
    output = str(frame.output or "").strip().lower()
    if output in {"chart", "preview", "list"}:
        return False
    if intent not in _ELIGIBLE_INTENTS:
        return False
    names = {str(call.get("name") or call.get("tool") or "").strip() for call in calls}
    if names & _EXCLUDED_TOOLS:
        return False
    if any(name.startswith("preview_") for name in names):
        return False
    if intent == "budget_status" and output == "scalar":
        return False
    return True


def should_attach_money_outlook_context(
    *,
    frame: ConversationFrame,
    calls: list[dict[str, Any]],
    affordability_amount: float | None = None,
) -> bool:
    if not money_outlook_context_enabled():
        return False
    if str(frame.route or "").strip().lower() != "finance":
        return False
    if str(frame.output or "").strip().lower() in {"chart", "preview", "list"}:
        return False
    intent = str(frame.intent or "").strip().lower()
    names = {str(call.get("name") or call.get("tool") or "").strip() for call in calls}
    if names & _EXCLUDED_TOOLS:
        return False
    if intent in _MONEY_OUTLOOK_INTENTS:
        return True
    return intent == "affordability" and affordability_amount is not None


def should_attach_safe_to_spend_context(
    *,
    frame: ConversationFrame,
    calls: list[dict[str, Any]],
) -> bool:
    if not safe_to_spend_context_enabled():
        return False
    if str(frame.route or "").strip().lower() != "finance":
        return False
    if str(frame.output or "").strip().lower() in {"chart", "preview", "list"}:
        return False
    if str(frame.intent or "").strip().lower() != "affordability":
        return False
    names = {str(call.get("name") or call.get("tool") or "").strip() for call in calls}
    if names & _EXCLUDED_TOOLS:
        return False
    return True


def _context_view_for_intent(intent: str) -> str:
    text = str(intent or "").strip().lower()
    if text in {"cashflow_forecast", "cashflow_shortfall", "budget_plan", "budget_status"}:
        return "operating_plan"
    if text in {"spending_explain", "spending_compare"}:
        return "friction_map"
    if text in {"savings_capacity", "finance_priorities", "finance_snapshot", "explain_metric"}:
        return "relevant"
    return "relevant"


def _affordability_amount_from_calls(calls: list[dict[str, Any]]) -> float | None:
    for call in calls:
        if str(call.get("name") or call.get("tool") or "").strip() != "check_affordability":
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        value = payload.get("amount")
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            return round(amount, 2)
    return None


def _max_context_facts() -> int:
    try:
        return max(1, min(int(os.getenv("MIRA_FINANCIAL_CONTEXT_MAX_FACTS", "5")), 8))
    except (TypeError, ValueError):
        return 5


__all__ = [
    "financial_context_tool_enabled",
    "lifestyle_context_prompt_enabled",
    "maybe_append_financial_context_call",
    "money_outlook_context_enabled",
    "safe_to_spend_context_enabled",
    "should_attach_financial_context",
    "should_attach_money_outlook_context",
    "should_attach_safe_to_spend_context",
]
