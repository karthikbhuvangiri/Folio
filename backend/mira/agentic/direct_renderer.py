from __future__ import annotations

from typing import Any

from mira.agentic.confidence import confidence_caveat_from_summary
from mira.agentic.temporal_parser import bounded_range_dates
from mira.persona import direct_scalar_spend_answer
from mira.agentic.schemas import EvidencePacket


DIRECT_ANSWER_TOOLS = {"get_merchant_spend", "get_category_spend"}
DIRECT_ANSWER_BLOCKERS = {
    "why",
    "should",
    "could",
    "would",
    "can",
    "afford",
    "compare",
    "comparison",
    "versus",
    "vs",
    "explain",
    "breakdown",
    "trend",
    "chart",
    "plot",
    "forecast",
    "predict",
    "plan",
    "normal",
    "usual",
    "average",
    "avg",
    "high",
    "higher",
    "lower",
    "transaction",
    "transactions",
    "details",
    "list",
}


def try_direct_scalar_answer(question: str, evidence: EvidencePacket) -> str:
    if len(evidence.tool_results) != 1:
        return ""
    if evidence.caveats:
        return ""
    record = evidence.tool_results[0]
    tool_name = str(record.get("tool_name") or record.get("tool") or "")
    execution_tool_name = str(record.get("execution_tool_name") or "")
    direct_tool_name = execution_tool_name if execution_tool_name in DIRECT_ANSWER_TOOLS else tool_name
    if direct_tool_name not in DIRECT_ANSWER_TOOLS:
        return ""
    if record.get("status") == "error":
        return ""
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    if any(result.get(key) for key in ("error", "dry_run")):
        return ""
    if record_has_caveats(result):
        return ""
    matched_merchants = result.get("matched_merchants")
    if isinstance(matched_merchants, list) and len(matched_merchants) > 1:
        return ""
    if isinstance(result.get("preview_changes"), list):
        return ""
    if question_blocks_direct_answer(question):
        return ""

    total = first_number(result.get("total"), result.get("amount"))
    if total is None:
        return ""
    count = first_int(result.get("count"), result.get("txn_count"), result.get("row_count"))
    args = result.get("args") if isinstance(result.get("args"), dict) else {}
    if direct_tool_name == "get_merchant_spend":
        subject = str(result.get("merchant_query") or result.get("merchant") or args.get("merchant") or "").strip()
        if not subject:
            return ""
        subject_phrase = f"at {subject}"
    else:
        subject = str(result.get("category") or args.get("category") or "").strip()
        if not subject:
            return ""
        subject_phrase = f"on {subject}"

    range_phrase = direct_range_phrase(result, args)
    amount_text = format_money(total)
    templated = direct_scalar_spend_answer(
        amount=amount_text,
        subject_phrase=subject_phrase,
        range_phrase=range_phrase,
        count=count,
    )
    if templated:
        return append_confidence_caveat(templated, result)

    sentence = f"You spent {amount_text} {subject_phrase}{range_phrase}."
    if count is not None:
        noun = "transaction" if count == 1 else "transactions"
        sentence += f" That is based on {count} {noun}."
    return append_confidence_caveat(sentence, result)


def append_confidence_caveat(answer: str, result: dict[str, Any]) -> str:
    caveat = confidence_caveat_from_summary(result.get("confidence_summary"))
    if not caveat:
        return answer
    if caveat.lower() in str(answer or "").lower():
        return answer
    return f"{answer} {caveat}"


def record_has_caveats(record: dict[str, Any]) -> bool:
    caveats = record.get("caveats")
    if isinstance(caveats, list) and caveats:
        return True
    data_quality = record.get("data_quality")
    if isinstance(data_quality, dict):
        nested = data_quality.get("caveats")
        return isinstance(nested, list) and bool(nested)
    return False


def question_blocks_direct_answer(question: str) -> bool:
    text = " ".join(str(question or "").lower().split())
    if any(phrase in text for phrase in ("how did", "how do", "how should", "how can", "how come")):
        return True
    tokens = {
        "".join(ch for ch in part.lower() if ch.isalnum() or ch == "_")
        for part in text.split()
    }
    return bool(tokens & DIRECT_ANSWER_BLOCKERS)


def first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def format_money(value: float) -> str:
    return f"${value:,.2f}"


def direct_range_phrase(record: dict[str, Any], args: dict[str, Any]) -> str:
    range_token = str(args.get("range") or record.get("range") or "").strip()
    month = str(record.get("month") or args.get("month") or "").strip()
    bounded = bounded_range_dates(range_token)
    if bounded:
        start, end = bounded
        return f" from {start} to {end}"
    if range_token == "current_month":
        return " this month"
    if range_token == "last_month":
        return " last month"
    if range_token == "ytd":
        return " year to date"
    if range_token == "last_90d":
        return " in the last 90 days"
    if range_token == "last_6_months":
        return " in the last 6 months"
    if range_token == "last_year":
        return " last year"
    if range_token and len(range_token) == 7 and range_token[4] == "-":
        return f" in {range_token}"
    if month:
        return f" in {month}"
    return ""


__all__ = [
    "DIRECT_ANSWER_TOOLS",
    "try_direct_scalar_answer",
]
