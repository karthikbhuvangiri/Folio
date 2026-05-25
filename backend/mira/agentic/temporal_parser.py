from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

import llm_client


_FALSE_VALUES = {"0", "false", "off", "no"}
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class TemporalRangeParse:
    status: str
    range_kind: str = "none"
    start_date: str | None = None
    end_date: str | None = None
    display_label: str = ""
    confidence: float = 0.0
    reason: str = ""
    llm_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def bounded(self) -> bool:
        return self.ok and bool(self.start_date and self.end_date)


TEMPORAL_RANGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "s": {"type": "string", "enum": ["ok", "all", "clarify", "unsupported"]},
        "a": {"type": ["string", "null"]},
        "b": {"type": ["string", "null"]},
    },
    "required": ["s", "a", "b"],
    "additionalProperties": False,
}


def temporal_parser_enabled() -> bool:
    return str(os.getenv("MIRA_LLM_TEMPORAL_PARSER_ENABLED", "1")).strip().lower() not in _FALSE_VALUES


def parse_temporal_range(
    question: str,
    *,
    now: datetime | date | None = None,
    completer: Callable[[str], str] | None = None,
) -> TemporalRangeParse:
    if not temporal_parser_enabled():
        return TemporalRangeParse(status="unsupported", reason="temporal parser disabled")

    today = _coerce_date(now) or date.today()
    prompt = build_temporal_prompt(question, today=today)
    try:
        raw = (
            completer(prompt)
            if completer is not None
            else llm_client.complete(
                prompt,
                max_tokens=70,
                purpose="controller",
                response_format=TEMPORAL_RANGE_SCHEMA,
            )
        )
    except Exception as exc:
        return TemporalRangeParse(status="unsupported", reason=f"temporal parser failed: {exc}", llm_calls=1)
    parsed = validate_temporal_parse(raw, today=today)
    return TemporalRangeParse(
        status=parsed.status,
        range_kind=parsed.range_kind,
        start_date=parsed.start_date,
        end_date=parsed.end_date,
        display_label=parsed.display_label,
        confidence=parsed.confidence,
        reason=parsed.reason,
        llm_calls=1,
    )


def build_temporal_prompt(question: str, *, today: date) -> str:
    return f"""Parse one finance question's time phrase into exact date bounds.

Today: {today.isoformat()}

Return compact JSON only:
{{"s":"ok|all|clarify|unsupported","a":"YYYY-MM-DD|null","b":"YYYY-MM-DD|null"}}

Rules:
- Interpret only exact calendar language. Do not compute money.
- Dates are inclusive. If the exact phrase means the current open period, b is today.
- Use "all" only for all-time/lifetime/ever.
- If the phrase is future-only, vague, event-based, impossible, or needs outside knowledge, use "clarify".
- If no time range is requested, use "clarify".

Examples for Today={today.isoformat()}:
- "previous quarter" -> {{"s":"ok","a":"2026-01-01","b":"2026-03-31"}}
- "this quarter" -> {{"s":"ok","a":"2026-04-01","b":"{today.isoformat()}"}}
- "between January and March" -> {{"s":"ok","a":"2026-01-01","b":"2026-03-31"}}
- "January through March 2026" -> {{"s":"ok","a":"2026-01-01","b":"2026-03-31"}}
- "first half of last year" -> {{"s":"ok","a":"2025-01-01","b":"2025-06-30"}}
- "around tax season" -> {{"s":"clarify","a":null,"b":null}}

Question: {question.strip()}
"""


def validate_temporal_parse(raw: str, *, today: date) -> TemporalRangeParse:
    try:
        payload = json.loads(_json_object_text(raw))
    except Exception:
        return TemporalRangeParse(status="unsupported", reason="temporal parser returned invalid JSON")
    if not isinstance(payload, dict):
        return TemporalRangeParse(status="unsupported", reason="temporal parser returned non-object JSON")

    compact_status = str(payload.get("s") or "").strip().lower()
    if compact_status:
        if compact_status == "all":
            return TemporalRangeParse(status="ok", range_kind="all_time", display_label="all time", confidence=1.0)
        if compact_status in {"clarify", "unsupported"}:
            return TemporalRangeParse(status=compact_status, reason="compact temporal parser did not return exact bounds")
        if compact_status != "ok":
            return TemporalRangeParse(status="unsupported", reason="temporal parser returned invalid status")
        start_text = _nullable_date_text(payload.get("a"))
        end_text = _nullable_date_text(payload.get("b"))
        return _validate_bounded_dates(
            start_text=start_text,
            end_text=end_text,
            range_kind="bounded_range",
            display_label="",
            confidence=1.0,
            today=today,
        )

    status = str(payload.get("status") or "").strip().lower()
    if status not in {"ok", "clarify", "unsupported"}:
        return TemporalRangeParse(status="unsupported", reason="temporal parser returned invalid status")
    range_kind = str(payload.get("range_kind") or "none").strip().lower()
    if range_kind not in {"single_month", "bounded_range", "rolling_window", "all_time", "none"}:
        range_kind = "none"
    confidence = _confidence(payload.get("confidence"))
    reason = str(payload.get("reason") or "").strip()
    display_label = str(payload.get("display_label") or "").strip()

    if status != "ok":
        return TemporalRangeParse(status=status, range_kind=range_kind, confidence=confidence, reason=reason)
    if confidence < 0.65:
        return TemporalRangeParse(status="clarify", range_kind=range_kind, confidence=confidence, reason="low temporal confidence")
    if range_kind == "all_time":
        return TemporalRangeParse(status="ok", range_kind="all_time", display_label=display_label or "all time", confidence=confidence)

    start_text = _nullable_date_text(payload.get("start_date"))
    end_text = _nullable_date_text(payload.get("end_date"))
    return _validate_bounded_dates(
        start_text=start_text,
        end_text=end_text,
        range_kind=range_kind,
        display_label=display_label,
        confidence=confidence,
        today=today,
    )


def _validate_bounded_dates(
    *,
    start_text: str,
    end_text: str,
    range_kind: str,
    display_label: str,
    confidence: float,
    today: date,
) -> TemporalRangeParse:
    if not start_text or not end_text:
        return TemporalRangeParse(status="clarify", range_kind=range_kind, confidence=confidence, reason="missing date bounds")
    start = _parse_iso_date(start_text)
    end = _parse_iso_date(end_text)
    if start is None or end is None:
        return TemporalRangeParse(status="clarify", range_kind=range_kind, confidence=confidence, reason="invalid date bounds")
    if start > end:
        return TemporalRangeParse(status="clarify", range_kind=range_kind, confidence=confidence, reason="reversed date bounds")
    if start > today and end > today:
        return TemporalRangeParse(status="clarify", range_kind=range_kind, confidence=confidence, reason="future-only range")
    if end > today:
        end = today
        end_text = end.isoformat()
    if (end - start).days > 366 * 5:
        return TemporalRangeParse(status="clarify", range_kind=range_kind, confidence=confidence, reason="range is too broad")

    return TemporalRangeParse(
        status="ok",
        range_kind=range_kind if range_kind != "none" else "bounded_range",
        start_date=start.isoformat(),
        end_date=end_text,
        display_label=display_label or _display_label(start, end),
        confidence=confidence,
    )


def bounded_range_token(start_date: str, end_date: str) -> str:
    return f"{start_date}..{end_date}"


def is_bounded_range_token(value: str) -> bool:
    token = str(value or "").strip()
    if ".." not in token:
        return False
    start, end = token.split("..", 1)
    return bool(_parse_iso_date(start) and _parse_iso_date(end))


def bounded_range_dates(value: str) -> tuple[str, str] | None:
    if not is_bounded_range_token(value):
        return None
    start, end = str(value).split("..", 1)
    return start, end


def _json_object_text(raw: str) -> str:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        return text[start : end + 1]
    return text


def _coerce_date(value: datetime | date | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _nullable_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "none", "null"}:
        return ""
    return text


def _parse_iso_date(value: str) -> date | None:
    text = str(value or "").strip()
    if not _ISO_DATE_RE.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _display_label(start: date, end: date) -> str:
    if start == end:
        return start.isoformat()
    if start.year == end.year:
        return f"{start.isoformat()} to {end.strftime('%m-%d')}"
    return f"{start.isoformat()} to {end.isoformat()}"
