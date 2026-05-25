from __future__ import annotations

import os
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}

LEGACY_SELECTOR_PERSONA_LINE = "You are Mira: best-friend energy, Folio finance expert when needed. Return compact JSON only."
SELECTOR_PERSONA_V2_LINE = "You are Mira: warm, witty, precise Folio expert. Return compact JSON only."
LEGACY_SELECTOR_DEFAULT_ROUTE_LINE = "Default chat unless Folio data, memory, write preview, or explain-last is clearly requested."
SELECTOR_DEFAULT_ROUTE_V2_LINE = "Default chat unless clear Folio/write/explain ask; /memory commands are handled before selector."

LEGACY_EVIDENCE_PERSONA_LINES = (
    "You are Mira, the user's warm, sharp AI companion inside Folio.\n"
    "Answer warmly and concisely using only the evidence JSON."
)
EVIDENCE_PERSONA_V2_LINES = (
    "You are Mira, the user's quick-witted Folio companion: warm, candid, and evidence-first.\n"
    "Sound like a smart friend with receipts, not a report. Be specific, not blunt. Light wit is welcome when it fits; if money looks stressful, skip jokes."
)

LEGACY_GENERAL_PERSONA_LINES = (
    "You are Mira, the user's warm, sharp, broadly capable AI companion inside Folio.\n"
    "Answer normally and conversationally."
)
GENERAL_PERSONA_V2_LINES = (
    "You are Mira, Folio's local-first AI companion: quick-witted, warm, candid, and broadly useful.\n"
    "Sound like texting a smart, funny friend: a little spark, then the useful bit. Vary wording. No corporate 'I can assist' tone; light wit only when natural; never shame."
)


def persona_templates_enabled() -> bool:
    return str(os.getenv("MIRA_PERSONA_TEMPLATES_ENABLED", "true")).strip().lower() not in FALSE_VALUES


def persona_v2_enabled() -> bool:
    return str(os.getenv("MIRA_PERSONA_V2_ENABLED", "true")).strip().lower() not in FALSE_VALUES


def selector_persona_line() -> str:
    return SELECTOR_PERSONA_V2_LINE if persona_v2_enabled() else LEGACY_SELECTOR_PERSONA_LINE


def selector_default_route_line() -> str:
    return SELECTOR_DEFAULT_ROUTE_V2_LINE if persona_v2_enabled() else LEGACY_SELECTOR_DEFAULT_ROUTE_LINE


def evidence_persona_lines() -> str:
    return EVIDENCE_PERSONA_V2_LINES if persona_v2_enabled() else LEGACY_EVIDENCE_PERSONA_LINES


def general_persona_lines() -> str:
    return GENERAL_PERSONA_V2_LINES if persona_v2_enabled() else LEGACY_GENERAL_PERSONA_LINES


def direct_scalar_spend_answer(
    *,
    amount: str,
    subject_phrase: str,
    range_phrase: str = "",
    count: int | None = None,
) -> str:
    if not persona_templates_enabled():
        return ""
    subject = str(subject_phrase or "").strip()
    if not amount or not subject:
        return ""

    sentence = f"Receipt check: you spent {amount} {subject}{range_phrase}."
    if count == 0:
        return sentence + " I found no matching transactions."
    if count is not None:
        noun = "transaction" if count == 1 else "transactions"
        sentence += f" I counted {count} {noun}."
    return sentence


def validation_answer(
    *,
    status: str,
    message: str,
    fallback: str,
    looks_internal: bool,
) -> str:
    if not persona_templates_enabled():
        return ""
    lowered = str(message or "").lower()
    if status == "clarify":
        if "supported time range" in lowered or "unsupported range" in lowered:
            return "I can answer that; give me a supported time range and I am in."
        if "multiple possible" in lowered and not looks_internal:
            return str(message or "").strip()
        if "amount" in lowered:
            return "Give me the amount and I will check it against your Folio picture."
        if looks_internal:
            return "I need one more detail to pick the right Folio view."
        return str(message or "").strip() or fallback

    if any(marker in lowered for marker in ("apply", "confirm", "commit", "execute changes", "write")):
        return "I can prep the preview, but nothing moves until you confirm it."
    if any(marker in lowered for marker in ("run_sql", "internal tool", "unknown tool", "disallowed")):
        return "That stays behind the Folio curtain; I cannot use it from chat."
    if any(marker in lowered for marker in ("chart", "make_chart", "plot_chart", "source_step_id", "labels", "values", "series")):
        return "I can chart that, but I need a grounded Folio result first."
    return fallback or "I could not turn that into a safe Folio action yet."


def write_preview_answer(pending_write: dict[str, Any] | None) -> str:
    if not persona_templates_enabled() or not isinstance(pending_write, dict):
        return ""
    rows = _int_or_none(pending_write.get("rows_affected"))
    summary = str(pending_write.get("summary") or "").strip()
    if rows is not None:
        noun = "change" if rows == 1 else "changes"
        return f"Preview ready: {rows} {noun}. I will keep my hands off the buttons until you confirm."
    if summary:
        return f"Preview ready. {summary} I will wait for your confirmation."
    return "Preview ready. Nothing moves until you confirm."


def memory_answer(tool_results: list[dict[str, Any]]) -> str:
    if not persona_templates_enabled():
        return ""
    for record in tool_results:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        tool_name = str(record.get("tool_name") or record.get("execution_tool_name") or "")
        if result.get("saved"):
            return "Got it. I will keep that in mind next time."
        if result.get("saved") is False:
            return "I did not save that as a durable memory. I can still use it in this chat."
        if result.get("updated"):
            return "Updated. I will use the new version going forward."
        if result.get("forgot"):
            return "Forgotten. I will stop using that saved memory."
        if tool_name in {"manage_memory", "retrieve_relevant_memories", "list_mira_memories"}:
            count = _int_or_none(result.get("count"))
            memories = result.get("memories") if isinstance(result.get("memories"), list) else result.get("items")
            if isinstance(memories, list) and memories:
                lines = []
                for item in memories[:5]:
                    if not isinstance(item, dict):
                        continue
                    text = str(item.get("normalized_text") or item.get("summary") or "").strip()
                    if text:
                        lines.append(f"- {text}")
                if lines:
                    extra = "" if count is None or count <= len(lines) else f"\nPlus {count - len(lines)} more."
                    return "I remember:\n" + "\n".join(lines) + extra
            if count == 0 or memories == []:
                return "I do not have a saved memory for that yet."
    return ""


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "direct_scalar_spend_answer",
    "evidence_persona_lines",
    "general_persona_lines",
    "memory_answer",
    "persona_templates_enabled",
    "persona_v2_enabled",
    "selector_default_route_line",
    "selector_persona_line",
    "validation_answer",
    "write_preview_answer",
]
