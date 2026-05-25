from __future__ import annotations

import json
import os
from typing import Any


FALSE_VALUES = {"0", "false", "no", "off"}
LOW_CONFIDENCE_THRESHOLD = 0.7


def confidence_caveats_enabled() -> bool:
    return str(os.getenv("MIRA_CONFIDENCE_CAVEATS_ENABLED", "true")).strip().lower() not in FALSE_VALUES


def compact_confidence_summary(result: Any) -> dict[str, Any]:
    if not confidence_caveats_enabled() or not isinstance(result, dict):
        return {}

    existing = _normalize_existing_summary(result.get("confidence_summary"))
    if existing:
        return existing

    top_values = _confidence_values(result.get("confidence"))
    top_values.extend(_confidence_values(result.get("confidence_json")))

    row_values = _row_confidence_values(result)
    values = [*top_values, *row_values]
    if not values:
        return {}

    minimum = min(values)
    weighted = sum(values) / len(values)
    low_count = sum(1 for value in values if value < LOW_CONFIDENCE_THRESHOLD)
    row_low_count = sum(1 for value in row_values if value < LOW_CONFIDENCE_THRESHOLD)
    summary: dict[str, Any] = {
        "confidence_available": True,
        "min_confidence": round(minimum, 3),
        "weighted_confidence": round(weighted, 3),
        "low_confidence_count": low_count,
    }
    if row_values:
        summary["sampled_count"] = len(row_values)
    if low_count:
        summary["caveat"] = _confidence_caveat(row_low_count=row_low_count, low_count=low_count)
    return summary


def confidence_caveat_from_summary(summary: Any) -> str:
    if not confidence_caveats_enabled() or not isinstance(summary, dict):
        return ""
    return str(summary.get("caveat") or "").strip()


def confidence_caveat_from_evidence(evidence: Any) -> str:
    for record in getattr(evidence, "tool_results", []) or []:
        if not isinstance(record, dict):
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        caveat = confidence_caveat_from_summary(result.get("confidence_summary"))
        if caveat:
            return caveat
    return ""


def _normalize_existing_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    available = bool(value.get("confidence_available", True))
    min_confidence = _confidence_number(value.get("min_confidence"))
    weighted = _confidence_number(value.get("weighted_confidence"))
    low_count = _int_or_none(value.get("low_confidence_count"))
    if not available or min_confidence is None or weighted is None:
        return {}
    summary: dict[str, Any] = {
        "confidence_available": True,
        "min_confidence": round(min_confidence, 3),
        "weighted_confidence": round(weighted, 3),
        "low_confidence_count": int(low_count or 0),
    }
    sampled = _int_or_none(value.get("sampled_count"))
    if sampled is not None:
        summary["sampled_count"] = sampled
    caveat = str(value.get("caveat") or "").strip()
    if caveat and summary["low_confidence_count"] > 0:
        summary["caveat"] = caveat[:240]
    elif summary["low_confidence_count"] > 0:
        summary["caveat"] = _confidence_caveat(row_low_count=0, low_count=summary["low_confidence_count"])
    return summary


def _row_confidence_values(result: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for key in ("recent", "rows", "transactions", "data", "items"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            confidence = _first_confidence(
                row.get("confidence"),
                row.get("category_confidence"),
                row.get("merchant_confidence"),
            )
            if confidence is not None:
                values.append(confidence)
    return values


def _confidence_values(value: Any) -> list[float]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        parsed = _json_dict(value)
        if parsed:
            return _confidence_values(parsed)
    if isinstance(value, dict):
        values: list[float] = []
        for item in value.values():
            confidence = _confidence_number(item)
            if confidence is not None:
                values.append(confidence)
        return values
    confidence = _confidence_number(value)
    return [confidence] if confidence is not None else []


def _first_confidence(*values: Any) -> float | None:
    for value in values:
        confidence = _confidence_number(value)
        if confidence is not None:
            return confidence
    return None


def _confidence_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        label = " ".join(value.strip().lower().replace("_", "-").split())
        if label in {"manual", "user", "stated", "saved", "exact", "high", "rule", "rule-high"}:
            return 0.95
        if label in {"medium", "rule-medium"}:
            return 0.75
        if label in {"low", "fallback"}:
            return 0.4
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    if number > 1 and number <= 100:
        number = number / 100
    if number > 1:
        return None
    return max(0.0, min(number, 1.0))


def _confidence_caveat(*, row_low_count: int, low_count: int) -> str:
    if row_low_count > 1:
        return "A few sampled transactions have lower category confidence, so treat this as an estimate."
    if row_low_count == 1:
        return "One sampled transaction has lower category confidence, so treat this as an estimate."
    if low_count > 0:
        return "Some confidence metadata is low, so treat this as an estimate."
    return ""


def _json_dict(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "compact_confidence_summary",
    "confidence_caveat_from_evidence",
    "confidence_caveat_from_summary",
    "confidence_caveats_enabled",
]
