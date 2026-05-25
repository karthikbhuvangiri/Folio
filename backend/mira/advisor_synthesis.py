"""Offline Mira advisor synthesis over safe financial portrait dossiers.

This is the Phase 27.7 synthesis layer. It is intentionally off the chat path
and off by default. The LLM may draft judgment and voice, but Python validates
evidence, numbers, sensitivity, and quality before anything is stored.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from hashlib import sha1
from typing import Any, Callable

from mira.safe_finance_query import build_financial_portrait_dossier


ADVISOR_SYNTHESIS_VERSION = "mira_advisor_synthesis_v1"
ADVISOR_SYNTHESIS_VALIDATOR_VERSION = "mira_advisor_synthesis_validator_v1"
ADVISOR_SYNTHESIS_MAX_TOKENS = int(os.getenv("MIRA_ADVISOR_SYNTHESIS_MAX_TOKENS", "1400"))
ADVISOR_SYNTHESIS_MAX_OBSERVATIONS = int(os.getenv("MIRA_ADVISOR_SYNTHESIS_MAX_OBSERVATIONS", "5"))

_FALSE_VALUES = {"0", "false", "no", "off"}
_ALLOWED_TYPES = {
    "cash_flow",
    "resilience",
    "income",
    "spending_pressure",
    "commitments",
    "debt",
    "goals",
    "timing",
    "noise",
    "data_caveat",
}
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}
_ALLOWED_SENSITIVITY = {"low", "medium", "high"}
_REQUIRED_VISIBLE_FIELDS = ("point_of_view", "evidence", "interpretation", "tradeoff", "action", "caveat")

_HIGH_SENSITIVITY_TERMS = (
    "abortion",
    "adult",
    "alcohol",
    "bail",
    "casino",
    "fertility",
    "firearm",
    "gambling",
    "juul",
    "legal",
    "medical",
    "payday",
    "political",
    "religious",
    "therapy",
    "title loan",
    "tobacco",
    "vaping",
)
_MEDIUM_SENSITIVITY_TERMS = ("debt", "eviction", "food insecurity", "loan", "overdraft", "rent")
_SHAMING_TERMS = (
    "addict",
    "bad habit",
    "failure",
    "irresponsible",
    "reckless",
    "shame",
    "you failed",
    "you messed up",
    "you lack discipline",
)
_MOTIVE_TERMS = (
    "because you are",
    "because you're",
    "you are anxious",
    "you're anxious",
    "you are stressed",
    "you're stressed",
    "you wanted",
    "you were trying to",
)
_INTERNAL_TERMS = (
    "backend",
    "bundle",
    "deterministic",
    "evidence id",
    "evidence_id",
    "metric:",
    "missing_metric",
    "query layer",
    "run_sql",
    "safe_finance_query",
    "sql",
    "tool registry",
    "validator",
)
_GENERIC_DASHBOARD_PHRASES = (
    "dashboard snapshot",
    "are up this month",
    "groceries are up",
    "is up this month",
    "spending increased",
    "worth your attention",
    "available for your review",
    "review your spending",
    "track your expenses",
    "make a budget",
    "consider reducing expenses",
)
_MISSING_ROW_PHRASES = (
    "missing row",
    "missing-row",
    "no matching rows",
    "no rows",
    "no data available",
    "missing data card",
)
_RAW_BUCKET_RE = re.compile(
    r"\b(?:days_\d+(?:_\d+|_end)?|first_half|second_half|weekday|weekend|within_2_days_after_income|outside_payday_window)\b",
    re.IGNORECASE,
)
_UNKNOWN_RE = re.compile(r"\bunknown\b", re.IGNORECASE)
_NUMERIC_CLAIM_RE = re.compile(r"(?<![A-Za-z])(?:\$?\d[\d,]*(?:\.\d+)?%?)(?![A-Za-z])")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9&' -]{2,}")

_PROMPT = """You are Mira's offline advisor synthesis layer. You run off the chat path.

You are reading a Python-computed financial portrait. Python owns the math,
dates, evidence IDs, and validation. Your job is judgment: form a point of
view, rank what matters, explain tradeoffs, identify false alarms, and draft
advisor observations in Mira's voice.

Mira voice:
- warm, sharp, trusted, and lightly witty when the topic can carry it;
- never cute about rent, debt, shortfalls, or financial stress;
- no scolding, no motive claims, no moralizing, no generic finance tips;
- sound like someone who studied this user's money, not a dashboard narrator.

Hard rules:
- Use only facts in the portrait JSON.
- Never write SQL, request tools, mention metrics, buckets, evidence IDs, the
  query layer, validators, or internal implementation details in visible copy.
- Do not compute totals, deltas, percentages, dates, balances, or runway.
- Every numeric claim in visible copy must be copied exactly from cited
  evidence. If you are not sure the number is exact, omit the number.
- Do not use digits for rankings, counts, percentages, or time spans unless
  that exact digit appears in the cited evidence.
- Every observation must mention at least one cited merchant/category/account
  name or one exact cited number. "Your spending patterns" is too generic.
- Every observation must cite evidence_ids from the portrait.
- Prefer the `advisor_candidate_packets` list. Draft each observation from one
  packet unless you cite every evidence ID needed for every fact you combine.
- Copy evidence_ids exactly from the chosen packet's `evidence_ids` list. Do
  not cite packet_id values or make up new evidence IDs.
- Mark sensitivity high only when visible copy names sensitive subjects such as
  medical, gambling, alcohol, vaping, or adult content. Housing, income,
  commitments, and liabilities are not automatically high sensitivity.
- Suppress raw bucket names like days_22_28, unknown subjects, missing-row
  observations, and generic dashboard narration.
- Each observation must change what the user understands or does next.

Return JSON only:
{{
  "observations": [
    {{
      "type": "cash_flow|resilience|income|spending_pressure|commitments|debt|goals|timing|noise|data_caveat",
      "rank": 1,
      "point_of_view": "what Mira thinks matters",
      "evidence": "measured facts, written naturally; no evidence IDs",
      "interpretation": "why those facts matter together",
      "tradeoff": "what not to overreact to",
      "action": "what to check, change, defer, or monitor",
      "caveat": "what could change the recommendation",
      "confidence": "high|medium|low",
      "sensitivity": "low|medium|high",
      "evidence_ids": ["metric:cash_runway:summary"],
      "suppress_reason": ""
    }}
  ]
}}

If nothing is advisor-grade, return {{"observations":[]}}.

Financial portrait JSON:
{portrait_json}

JSON:"""


def advisor_synthesis_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_SYNTHESIS_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_synthesis_store_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_SYNTHESIS_STORE_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def run_offline_advisor_synthesis(
    *,
    conn,
    profile: str | None = None,
    question: str = "What should I understand or do differently next month?",
    dossier: dict[str, Any] | None = None,
    complete_fn: Callable[..., str] | None = None,
    force: bool = False,
    store: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build a portrait, run synthesis, validate, and optionally store outputs."""

    started = time.perf_counter()
    if not force and not advisor_synthesis_enabled():
        return {"status": "disabled", "accepted": [], "rejected": [], "stored_count": 0}

    portrait = dossier or build_financial_portrait_dossier(conn, question, profile=profile, as_of=as_of)
    draft = draft_advisor_observations(dossier=portrait, complete_fn=complete_fn, force=True)
    validation = validate_advisor_observations(portrait, draft.get("observations") or [])
    stored: list[dict[str, Any]] = []
    if store or advisor_synthesis_store_enabled():
        stored = store_advisor_outputs(conn=conn, profile=profile, observations=validation["accepted"], force=force)
    status = "ok" if validation["accepted"] else "no_accepted_observations"
    if draft.get("status") not in {"ok", "no_observations"}:
        status = draft["status"]
    return {
        "status": status,
        "accepted": validation["accepted"],
        "rejected": [*(draft.get("rejected") or []), *validation["rejected"]],
        "suppressed": draft.get("suppressed") or [],
        "errors": draft.get("errors") or [],
        "stored_count": len(stored),
        "dossier_meta": _dossier_meta(portrait),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def draft_advisor_observations(
    *,
    dossier: dict[str, Any],
    complete_fn: Callable[..., str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Return model-drafted observations. This does not store or surface them."""

    if not force and not advisor_synthesis_enabled():
        return _empty_result("disabled", dossier)
    if not dossier.get("measurements"):
        return _empty_result("no_dossier_measurements", dossier)

    prompt = build_advisor_synthesis_prompt(dossier)
    try:
        if complete_fn is not None:
            raw = complete_fn(prompt, ADVISOR_SYNTHESIS_MAX_TOKENS, "copilot", response_format="json")
        else:
            import llm_client

            if not llm_client.is_available():
                return _empty_result("llm_unavailable", dossier)
            raw = llm_client.complete(
                prompt,
                max_tokens=ADVISOR_SYNTHESIS_MAX_TOKENS,
                purpose="copilot",
                response_format="json",
            )
    except Exception as exc:
        return _empty_result("llm_error", dossier, errors=[f"{type(exc).__name__}: {exc}"])

    parsed = parse_advisor_synthesis_output(raw)
    parsed["status"] = "ok" if parsed["observations"] else "no_observations"
    parsed["dossier_meta"] = _dossier_meta(dossier)
    return parsed


def build_advisor_synthesis_prompt(dossier: dict[str, Any]) -> str:
    compact = compact_dossier_for_synthesis(dossier)
    compact_json = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _PROMPT.format(portrait_json=compact_json)


def compact_dossier_for_synthesis(dossier: dict[str, Any]) -> dict[str, Any]:
    """Return the prompt-safe portrait view, not raw DB rows."""

    packets = build_advisor_candidate_packets(dossier)
    packet_evidence_ids = _dedupe([evidence_id for packet in packets for evidence_id in (packet.get("evidence_ids") or [])])
    section_evidence_ids = _dedupe([evidence_id for section in dossier.get("portrait_sections") or [] for evidence_id in (section.get("evidence_ids") or [])[:3]])
    return {
        "version": dossier.get("version") or "",
        "question": dossier.get("question") or "",
        "profile_scope": dossier.get("profile_scope") or "household",
        "confidence": dossier.get("confidence") or "medium",
        "advisor_candidate_packets": packets,
        "portrait_sections": [_compact_section(section) for section in dossier.get("portrait_sections") or []],
        "candidate_drivers": (dossier.get("candidate_drivers") or [])[:4],
        "false_alarms": (dossier.get("false_alarms") or [])[:4],
        "constraints": (dossier.get("constraints") or [])[:4],
        "smallest_levers": (dossier.get("smallest_levers") or [])[:4],
        "caveats": (dossier.get("caveats") or [])[:10],
        "missing_metric_proposals": (dossier.get("missing_metric_proposals") or [])[:5],
        "allowed_evidence_ids": _dedupe([*packet_evidence_ids, *section_evidence_ids])[:80],
    }


def build_advisor_candidate_packets(dossier: dict[str, Any]) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()

    measurements = _measurements_by_metric(dossier)
    _append_timeline_packets(packets, seen, measurements.get("financial_timeline_events"))
    _append_trim_packets(packets, seen, measurements.get("realistic_trim_levers"))
    _append_constraint_packets(packets, seen, dossier.get("constraints") or [])
    _append_false_alarm_packets(packets, seen, dossier.get("false_alarms") or [])

    for idx, driver in enumerate(dossier.get("candidate_drivers") or [], start=1):
        subject = str(driver.get("subject") or "").strip()
        if not _usable_subject(subject) or _sensitivity_for_text(subject) == "high":
            continue
        amount = _number_or_none(driver.get("amount"))
        evidence_ids = [str(value) for value in driver.get("evidence_ids") or [] if str(value).strip()]
        if amount is None or not evidence_ids:
            continue
        _append_packet(
            packets,
            seen,
            {
                "packet_id": f"driver_{idx}",
                "type": "spending_pressure",
                "subject": subject,
                "exact_numbers": {"pressure_amount": amount},
                "evidence_ids": evidence_ids[:5],
                "point_of_view_hint": f"{subject} is a pressure point to inspect before trimming broad categories.",
                "tradeoff_hint": "Do not overcorrect if the cited pressure is one-off noise.",
            },
        )

    for idx, lever in enumerate(dossier.get("smallest_levers") or [], start=1):
        subject = str(lever.get("subject") or "").strip()
        if not _usable_subject(subject) or _sensitivity_for_text(subject) == "high":
            continue
        amount = _number_or_none(lever.get("amount"))
        evidence_ids = [str(value) for value in lever.get("evidence_ids") or [] if str(value).strip()]
        if amount is None or not evidence_ids:
            continue
        _append_packet(
            packets,
            seen,
            {
                "packet_id": f"lever_{idx}",
                "type": "spending_pressure",
                "subject": subject,
                "exact_numbers": {"repeatable_amount": amount},
                "evidence_ids": evidence_ids[:5],
                "point_of_view_hint": f"{subject} is a repeatable lever, not a whole-life verdict.",
                "tradeoff_hint": "Do not cut unrelated categories until this repeat pattern is checked.",
            },
        )
    return packets[:12]


def _measurements_by_metric(dossier: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(measurement.get("metric") or ""): measurement
        for measurement in dossier.get("measurements") or []
        if isinstance(measurement, dict)
    }


def _append_timeline_packets(packets: list[dict[str, Any]], seen: set[tuple[str, str, tuple[str, ...]]], measurement: dict[str, Any] | None) -> None:
    if not measurement:
        return
    for idx, row in enumerate(measurement.get("rows") or [], start=1):
        if not isinstance(row, dict):
            continue
        subject = str(row.get("subject") or "").strip()
        if not _usable_subject(subject) or _sensitivity_for_text(subject) == "high":
            continue
        event_type = str(row.get("event_type") or "")
        exact_numbers = _numbers_from_row(
            row,
            (
                "measured_amount",
                "housing_monthly",
                "recurring_monthly",
                "cash_like_balance",
                "liability_to_cash_ratio",
            ),
        )
        if not exact_numbers:
            continue
        _append_packet(
            packets,
            seen,
            {
                "packet_id": f"timeline_{idx}",
                "type": _timeline_packet_type(event_type),
                "subject": subject,
                "event_type": event_type,
                "exact_numbers": exact_numbers,
                "evidence_ids": [f"metric:financial_timeline_events:{idx}"],
                "point_of_view_hint": row.get("interpretation_hint") or _timeline_point_of_view_hint(row),
                "tradeoff_hint": _timeline_tradeoff_hint(row),
                "action_hint": row.get("action_hint") or "",
                "caveat_hint": row.get("caveat") or "",
            },
        )


def _append_trim_packets(packets: list[dict[str, Any]], seen: set[tuple[str, str, tuple[str, ...]]], measurement: dict[str, Any] | None) -> None:
    if not measurement:
        return
    for idx, row in enumerate(measurement.get("rows") or [], start=1):
        if not isinstance(row, dict):
            continue
        subject = str(row.get("subject") or "").strip()
        if not _usable_subject(subject) or _sensitivity_for_text(subject) == "high":
            continue
        amount = _number_or_none(row.get("measured_amount"))
        if amount is None:
            continue
        _append_packet(
            packets,
            seen,
            {
                "packet_id": f"trim_{idx}",
                "type": "commitments" if row.get("lever_type") == "comparison_shop" else "spending_pressure",
                "subject": subject,
                "lever_type": row.get("lever_type") or "",
                "exact_numbers": {"measured_amount": amount},
                "evidence_ids": [f"metric:realistic_trim_levers:{idx}"],
                "point_of_view_hint": row.get("action") or f"{subject} is a practical lever to inspect.",
                "tradeoff_hint": row.get("tradeoff") or "Treat this as a tune-up, not a broad austerity rule.",
                "action_hint": row.get("action") or "",
                "caveat_hint": row.get("caveat") or "",
            },
        )


def _append_constraint_packets(packets: list[dict[str, Any]], seen: set[tuple[str, str, tuple[str, ...]]], constraints: list[dict[str, Any]]) -> None:
    for constraint in constraints:
        metric = str(constraint.get("metric") or "")
        numbers = constraint.get("summary_numbers") if isinstance(constraint.get("summary_numbers"), dict) else {}
        exact_numbers = {key: value for key, value in numbers.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}
        if metric == "cash_runway" and exact_numbers:
            _append_packet(
                packets,
                seen,
                {
                    "packet_id": "cash_runway",
                    "type": "resilience",
                    "subject": "cash runway",
                    "exact_numbers": exact_numbers,
                    "evidence_ids": ["metric:cash_runway:summary"],
                    "point_of_view_hint": "Cash runway is the constraint to protect before optimizing categories.",
                    "tradeoff_hint": "Do not chase tiny cuts before checking the cash buffer.",
                },
            )
        if metric == "fixed_obligation_ratio" and exact_numbers:
            _append_packet(
                packets,
                seen,
                {
                    "packet_id": "fixed_obligations",
                    "type": "commitments",
                    "subject": "fixed obligations",
                    "exact_numbers": exact_numbers,
                    "evidence_ids": ["metric:fixed_obligation_ratio:summary"],
                    "point_of_view_hint": "Fixed obligations set the floor for the month.",
                    "tradeoff_hint": "Do not treat fixed obligations like discretionary drift.",
                },
            )


def _append_false_alarm_packets(packets: list[dict[str, Any]], seen: set[tuple[str, str, tuple[str, ...]]], false_alarms: list[dict[str, Any]]) -> None:
    for idx, alarm in enumerate(false_alarms, start=1):
        subject = str(alarm.get("subject") or "").strip()
        if not _usable_subject(subject) or _sensitivity_for_text(subject) == "high":
            continue
        evidence_ids = [str(value) for value in alarm.get("evidence_ids") or [] if str(value).strip()]
        _append_packet(
            packets,
            seen,
            {
                "packet_id": f"false_alarm_{idx}",
                "type": "noise",
                "subject": subject,
                "exact_numbers": {},
                "evidence_ids": evidence_ids or [f"metric:{alarm.get('metric')}:summary"],
                "point_of_view_hint": f"{subject} looks less important than the bigger pressure points.",
                "tradeoff_hint": "Do not spend energy optimizing the small signal before the larger driver.",
            },
        )


def _numbers_from_row(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    numbers: dict[str, Any] = {}
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        numbers[key] = value
    return numbers


def _timeline_packet_type(event_type: str) -> str:
    return {
        "income_continuity": "income",
        "fixed_floor": "commitments",
        "travel_or_event_cluster": "noise",
        "fee_pressure": "spending_pressure",
        "upcoming_recurring_constraint": "commitments",
        "liability_position": "debt",
    }.get(str(event_type or ""), "cash_flow")


def _timeline_point_of_view_hint(row: dict[str, Any]) -> str:
    subject = str(row.get("subject") or "This event")
    event_type = str(row.get("event_type") or "")
    if event_type == "income_continuity":
        return "Income continuity is the first assumption to verify before trusting the rest of the plan."
    if event_type == "fixed_floor":
        return "The fixed floor is the monthly constraint to respect before optimizing flexible spend."
    if event_type == "travel_or_event_cluster":
        return "This looks like an event cluster, so it should be separated from ordinary lifestyle drift."
    if event_type == "liability_position":
        return "Debt pressure should be judged against cash coverage, not as an isolated balance."
    return f"{subject} is worth inspecting before changing the broader plan."


def _timeline_tradeoff_hint(row: dict[str, Any]) -> str:
    event_type = str(row.get("event_type") or "")
    if event_type == "travel_or_event_cluster":
        return "Do not overreact to event spend as if it were a new monthly baseline."
    if event_type == "fee_pressure":
        return "If the fee is one-off or miscategorized, do not turn it into a lifestyle rule."
    if event_type == "fixed_floor":
        return "Do not treat fixed commitments like discretionary drift."
    if event_type == "income_continuity":
        return "Do not panic about a partial current month, but verify the source change."
    return "Do not overcorrect if the signal is temporary or already improving."


def _append_packet(packets: list[dict[str, Any]], seen: set[tuple[str, str, tuple[str, ...]]], packet: dict[str, Any]) -> None:
    subject = str(packet.get("subject") or "").strip().lower()
    evidence_ids = tuple(str(value) for value in (packet.get("evidence_ids") or []))
    key = (str(packet.get("type") or ""), subject, evidence_ids)
    if key in seen:
        return
    seen.add(key)
    packets.append(packet)


def _dedupe(values: list[Any]) -> list[Any]:
    out = []
    seen = set()
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def parse_advisor_synthesis_output(raw: str) -> dict[str, Any]:
    rejected: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    try:
        payload = json.loads(_json_object_text(raw))
    except Exception as exc:
        return {
            "observations": [],
            "suppressed": [],
            "rejected": [],
            "errors": [f"invalid_json:{type(exc).__name__}"],
            "raw_excerpt": str(raw or "")[:260],
        }
    raw_items = payload.get("observations") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        return {"observations": [], "suppressed": [], "rejected": [], "errors": ["observations_not_list"]}

    observations: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_items[: max(1, ADVISOR_SYNTHESIS_MAX_OBSERVATIONS)]):
        if not isinstance(item, dict):
            rejected.append({"index": idx, "reason": "observation_not_object"})
            continue
        suppress_reason = _clean_text(item.get("suppress_reason"), 220)
        if suppress_reason:
            suppressed.append({"index": idx, "reason": suppress_reason})
            continue
        cleaned = _clean_observation(item)
        observations.append(cleaned)
    return {"observations": observations, "suppressed": suppressed, "rejected": rejected, "errors": []}


def validate_advisor_observations(dossier: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_by_id = build_advisor_evidence_map(dossier)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for idx, observation in enumerate(observations):
        if not isinstance(observation, dict):
            rejected.append({"index": idx, "reason": "observation_not_object"})
            continue
        reason = _observation_rejection_reason(observation, evidence_by_id)
        if reason:
            rejected.append(
                {
                    "index": idx,
                    "reason": reason,
                    "point_of_view": observation.get("point_of_view") or "",
                    "evidence_ids": observation.get("evidence_ids") or [],
                    "sensitivity": observation.get("sensitivity") or "",
                }
            )
            continue
        accepted.append(_accepted_observation(dossier, observation, evidence_by_id))
    accepted.sort(key=lambda item: item.get("rank") or 99)
    return {"accepted": accepted[:ADVISOR_SYNTHESIS_MAX_OBSERVATIONS], "rejected": rejected}


def build_advisor_evidence_map(dossier: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for measurement in dossier.get("measurements") or []:
        if not isinstance(measurement, dict):
            continue
        metric = str(measurement.get("metric") or "")
        summary_id = f"metric:{metric}:summary"
        out[summary_id] = {
            "kind": "metric_summary",
            "metric": metric,
            "domain": measurement.get("domain"),
            "confidence": measurement.get("confidence"),
            "values": measurement.get("summary_numbers") or {},
            "time_range": measurement.get("time_range") or {},
            "basis": measurement.get("basis") or "",
            "caveats": measurement.get("caveats") or [],
        }
        for row_idx, row in enumerate(measurement.get("rows") or [], start=1):
            if not isinstance(row, dict):
                continue
            row_id = f"metric:{metric}:{row_idx}"
            out[row_id] = {
                "kind": "metric_row",
                "metric": metric,
                "domain": measurement.get("domain"),
                "confidence": measurement.get("confidence"),
                "values": row,
                "time_range": measurement.get("time_range") or {},
                "basis": measurement.get("basis") or "",
                "caveats": measurement.get("caveats") or [],
            }
            for evidence_id in row.get("sample_evidence_ids") or []:
                out.setdefault(str(evidence_id), {**out[row_id], "kind": "sample_row"})
        for evidence_id in measurement.get("evidence_ids") or []:
            out.setdefault(
                str(evidence_id),
                {
                    "kind": "metric_evidence",
                    "metric": metric,
                    "domain": measurement.get("domain"),
                    "confidence": measurement.get("confidence"),
                    "values": {
                        "summary_numbers": measurement.get("summary_numbers") or {},
                        "rows": (measurement.get("rows") or [])[:5],
                    },
                    "time_range": measurement.get("time_range") or {},
                    "basis": measurement.get("basis") or "",
                    "caveats": measurement.get("caveats") or [],
                },
            )
    for section in dossier.get("portrait_sections") or []:
        if isinstance(section, dict) and section.get("key"):
            out[f"section:{section['key']}"] = {
                "kind": "portrait_section",
                "metric": str(section.get("key") or ""),
                "domain": "portrait",
                "confidence": section.get("confidence"),
                "values": {
                    "label": section.get("label") or "",
                    "summary_numbers": section.get("summary_numbers") or {},
                    "rows": section.get("rows") or {},
                },
                "time_range": {},
                "basis": "assembled from safe finance measurements",
                "caveats": section.get("caveats") or [],
            }
    return out


def store_advisor_outputs(
    *,
    conn,
    profile: str | None = None,
    observations: list[dict[str, Any]],
    force: bool = False,
) -> list[dict[str, Any]]:
    if not force and not advisor_synthesis_store_enabled():
        return []
    _ensure_tables(conn)
    stored: list[dict[str, Any]] = []
    for observation in observations:
        fingerprint = observation.get("fingerprint") or _fingerprint_observation(profile, observation)
        payload = _storage_payload(observation)
        params = (
            _scope_profile(profile),
            int(observation.get("rank") or 99),
            observation.get("type") or "cash_flow",
            observation.get("point_of_view") or "",
            observation.get("evidence") or "",
            observation.get("interpretation") or "",
            observation.get("tradeoff") or "",
            observation.get("action") or "",
            observation.get("caveat") or "",
            json.dumps(observation.get("evidence_ids") or [], sort_keys=True),
            json.dumps(payload, sort_keys=True),
            observation.get("confidence") or "medium",
            observation.get("sensitivity") or "low",
            _now(),
            (datetime.utcnow() + timedelta(days=7)).isoformat(timespec="seconds") + "Z",
            "active",
            fingerprint,
            ADVISOR_SYNTHESIS_VERSION,
        )
        conn.execute(
            """
            INSERT INTO mira_advisor_outputs (
                profile_id, rank, observation_type, point_of_view, evidence,
                interpretation, tradeoff, action, caveat, evidence_ids_json,
                payload_json, confidence, sensitivity, generated_at,
                valid_until, status, fingerprint, version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
                rank = excluded.rank,
                observation_type = excluded.observation_type,
                point_of_view = excluded.point_of_view,
                evidence = excluded.evidence,
                interpretation = excluded.interpretation,
                tradeoff = excluded.tradeoff,
                action = excluded.action,
                caveat = excluded.caveat,
                evidence_ids_json = excluded.evidence_ids_json,
                payload_json = excluded.payload_json,
                confidence = excluded.confidence,
                sensitivity = excluded.sensitivity,
                generated_at = excluded.generated_at,
                valid_until = excluded.valid_until,
                version = excluded.version,
                status = CASE
                    WHEN mira_advisor_outputs.status = 'dismissed' THEN mira_advisor_outputs.status
                    ELSE excluded.status
                END
            """,
            params,
        )
        stored.append({**observation, "fingerprint": fingerprint})
    return stored


def list_advisor_outputs(
    *,
    conn,
    profile: str | None = None,
    include_dismissed: bool = False,
    limit: int = 8,
) -> list[dict[str, Any]]:
    _ensure_tables(conn)
    status_clause = "" if include_dismissed else "AND status = 'active'"
    rows = conn.execute(
        f"""
        SELECT *
          FROM mira_advisor_outputs
         WHERE profile_id = ?
           {status_clause}
           AND (valid_until IS NULL OR valid_until > ? OR status = 'dismissed')
         ORDER BY rank ASC, generated_at DESC, id DESC
         LIMIT ?
        """,
        (_scope_profile(profile), _now(), max(1, min(int(limit or 8), 20))),
    ).fetchall()
    return [_row_to_output(row) for row in rows]


def _observation_rejection_reason(observation: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    missing = [field for field in _REQUIRED_VISIBLE_FIELDS if not str(observation.get(field) or "").strip()]
    if missing:
        return "missing_required_field"
    ids = [str(value) for value in observation.get("evidence_ids") or [] if str(value).strip()]
    if not ids:
        return "missing_evidence_ids"
    if any(evidence_id not in evidence_by_id for evidence_id in ids):
        return "unknown_evidence_id"
    if not any(_evidence_has_rows_or_summary(evidence_by_id[evidence_id]) for evidence_id in ids):
        return "missing_row_card"

    text = _visible_text(observation)
    lowered = text.lower()
    if _RAW_BUCKET_RE.search(text):
        return "raw_bucket_visible"
    if _UNKNOWN_RE.search(text):
        return "unknown_subject_visible"
    if any(phrase in lowered for phrase in _MISSING_ROW_PHRASES):
        return "missing_row_card"
    if any(term in lowered for term in _INTERNAL_TERMS):
        return "internal_language_visible"
    if any(term in lowered for term in _MOTIVE_TERMS):
        return "motive_attribution"
    if any(term in lowered for term in _SHAMING_TERMS):
        return "shaming_or_moralizing"
    if any(phrase in lowered for phrase in _GENERIC_DASHBOARD_PHRASES):
        return "generic_dashboard_narration"
    evidence_items = [evidence_by_id[evidence_id] for evidence_id in ids]
    if _sensitivity_for_text(text) == "high" or _sensitivity_for_evidence(evidence_items) == "high":
        return "sensitive_subject_leakage"
    unsupported = _unsupported_numeric_claims(text, evidence_items)
    if unsupported:
        return "unsupported_numeric_claim"
    if not _has_specific_anchor(text, evidence_items):
        return "generic_without_user_data"
    if _word_count(observation.get("point_of_view")) > 28:
        return "point_of_view_too_long"
    if any(_word_count(observation.get(field)) > 44 for field in ("evidence", "interpretation", "tradeoff", "action", "caveat")):
        return "field_too_long"
    if _looks_like_metric_label_sentence(observation):
        return "metric_label_sentence"
    return ""


def _accepted_observation(
    dossier: dict[str, Any],
    observation: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evidence_ids = [str(value) for value in observation.get("evidence_ids") or [] if str(value).strip()]
    sensitivity = observation.get("sensitivity") if observation.get("sensitivity") in _ALLOWED_SENSITIVITY else _sensitivity_for_text(_visible_text(observation))
    accepted = {
        "version": ADVISOR_SYNTHESIS_VERSION,
        "validator_version": ADVISOR_SYNTHESIS_VALIDATOR_VERSION,
        "type": observation.get("type") if observation.get("type") in _ALLOWED_TYPES else "cash_flow",
        "rank": int(observation.get("rank") or 99),
        "point_of_view": observation.get("point_of_view") or "",
        "evidence": observation.get("evidence") or "",
        "interpretation": observation.get("interpretation") or "",
        "tradeoff": observation.get("tradeoff") or "",
        "action": observation.get("action") or "",
        "caveat": observation.get("caveat") or "",
        "confidence": observation.get("confidence") if observation.get("confidence") in _ALLOWED_CONFIDENCE else dossier.get("confidence") or "medium",
        "sensitivity": sensitivity if sensitivity in _ALLOWED_SENSITIVITY else "low",
        "evidence_ids": evidence_ids[:12],
        "cited_evidence": [_public_evidence(evidence_by_id[evidence_id]) for evidence_id in evidence_ids[:12]],
    }
    accepted["fingerprint"] = _fingerprint_observation(dossier.get("profile_scope"), accepted)
    return accepted


def _clean_observation(item: dict[str, Any]) -> dict[str, Any]:
    confidence = str(item.get("confidence") or "medium").strip().lower()
    sensitivity = str(item.get("sensitivity") or "low").strip().lower()
    evidence_ids = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
    return {
        "type": _clean_enum(item.get("type"), _ALLOWED_TYPES, "cash_flow"),
        "rank": _safe_int(item.get("rank"), 99),
        "point_of_view": _clean_text(item.get("point_of_view"), 280),
        "evidence": _clean_text(item.get("evidence"), 360),
        "interpretation": _clean_text(item.get("interpretation"), 420),
        "tradeoff": _clean_text(item.get("tradeoff"), 360),
        "action": _clean_text(item.get("action"), 340),
        "caveat": _clean_text(item.get("caveat"), 320),
        "confidence": confidence if confidence in _ALLOWED_CONFIDENCE else "medium",
        "sensitivity": sensitivity if sensitivity in _ALLOWED_SENSITIVITY else "low",
        "evidence_ids": [str(value).strip() for value in evidence_ids if str(value).strip()][:12],
    }


def _compact_section(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": section.get("key") or "",
        "label": section.get("label") or "",
        "status": section.get("status") or "",
        "metrics": (section.get("metrics") or [])[:10],
        "summary_numbers": section.get("summary_numbers") or {},
        "confidence": section.get("confidence") or "medium",
        "caveats": (section.get("caveats") or [])[:5],
        "evidence_ids": (section.get("evidence_ids") or [])[:20],
    }


def _unsupported_numeric_claims(text: str, evidence_items: list[dict[str, Any]]) -> list[str]:
    cited_numbers = _numbers_from_evidence(evidence_items)
    unsupported = []
    for number in _numbers_from_text(text):
        if number not in cited_numbers:
            unsupported.append(number)
    return unsupported


def _numbers_from_text(text: str) -> set[str]:
    return {_normalize_number(match.group(0)) for match in _NUMERIC_CLAIM_RE.finditer(text or "")}


def _numbers_from_evidence(evidence_items: list[dict[str, Any]]) -> set[str]:
    numbers: set[str] = set()
    for item in evidence_items:
        numbers.update(_numbers_from_text(json.dumps(item, sort_keys=True, default=str)))
        for value in _numeric_values(item):
            numbers.add(_normalize_float(value))
            if 0 < abs(value) < 1:
                numbers.add(_normalize_float(value * 100))
    return numbers


def _numeric_values(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        out: list[float] = []
        for nested in value.values():
            out.extend(_numeric_values(nested))
        return out
    if isinstance(value, list):
        out = []
        for nested in value:
            out.extend(_numeric_values(nested))
        return out
    return []


def _normalize_number(value: str) -> str:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return _normalize_float(float(text))
    except ValueError:
        return text


def _normalize_float(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.4f}".rstrip("0").rstrip(".")


def _has_specific_anchor(text: str, evidence_items: list[dict[str, Any]]) -> bool:
    if _numbers_from_text(text):
        return True
    lowered = text.lower()
    for anchor in _evidence_anchors(evidence_items):
        if anchor and anchor in lowered:
            return True
    return False


def _evidence_anchors(evidence_items: list[dict[str, Any]]) -> set[str]:
    anchors: set[str] = set()
    useful_keys = {
        "account_name",
        "account_type",
        "category",
        "falling_category",
        "merchant",
        "name",
        "pressure_type",
        "rising_category",
        "source",
    }
    for item in evidence_items:
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        for key, value in _walk_items(values):
            if key not in useful_keys:
                continue
            text = str(value or "").strip().lower()
            if not text or text == "unknown" or _RAW_BUCKET_RE.search(text):
                continue
            if len(text) >= 4:
                anchors.add(text)
    return anchors


def _walk_items(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        out: list[tuple[str, Any]] = []
        for key, nested in value.items():
            if isinstance(nested, (dict, list)):
                out.extend(_walk_items(nested))
            else:
                out.append((str(key), nested))
        return out
    if isinstance(value, list):
        out = []
        for nested in value:
            out.extend(_walk_items(nested))
        return out
    return []


def _looks_like_metric_label_sentence(observation: dict[str, Any]) -> bool:
    text = " ".join(str(observation.get(field) or "") for field in ("point_of_view", "evidence")).lower()
    snake_tokens = re.findall(r"\b[a-z]+_[a-z0-9_]+\b", text)
    return bool(snake_tokens)


def _usable_subject(subject: str) -> bool:
    text = str(subject or "").strip()
    lowered = text.lower()
    return bool(text and lowered != "unknown" and not _RAW_BUCKET_RE.search(text))


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _visible_text(observation: dict[str, Any]) -> str:
    return " ".join(str(observation.get(field) or "") for field in _REQUIRED_VISIBLE_FIELDS)


def _sensitivity_for_text(text: str) -> str:
    lowered = str(text or "").lower()
    if any(term in lowered for term in _HIGH_SENSITIVITY_TERMS):
        return "high"
    if any(term in lowered for term in _MEDIUM_SENSITIVITY_TERMS):
        return "medium"
    return "low"


def _sensitivity_for_evidence(evidence_items: list[dict[str, Any]]) -> str:
    text = json.dumps([item.get("values") or {} for item in evidence_items], sort_keys=True, default=str)
    return _sensitivity_for_text(text)


def _evidence_has_rows_or_summary(evidence: dict[str, Any]) -> bool:
    values = evidence.get("values") if isinstance(evidence.get("values"), dict) else {}
    for key, value in values.items():
        if key in {"label"}:
            continue
        if value not in (None, "", [], {}):
            return True
    return False


def _public_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": evidence.get("kind") or "",
        "metric": evidence.get("metric") or "",
        "domain": evidence.get("domain") or "",
        "confidence": evidence.get("confidence") or "medium",
        "values": evidence.get("values") or {},
        "time_range": evidence.get("time_range") or {},
        "basis": evidence.get("basis") or "",
        "caveats": evidence.get("caveats") or [],
    }


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mira_advisor_outputs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            rank INTEGER NOT NULL DEFAULT 99,
            observation_type TEXT NOT NULL DEFAULT 'cash_flow',
            point_of_view TEXT NOT NULL,
            evidence TEXT NOT NULL,
            interpretation TEXT NOT NULL,
            tradeoff TEXT NOT NULL,
            action TEXT NOT NULL,
            caveat TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            payload_json TEXT NOT NULL DEFAULT '{}',
            confidence TEXT NOT NULL DEFAULT 'medium',
            sensitivity TEXT NOT NULL DEFAULT 'low',
            generated_at TEXT NOT NULL,
            valid_until TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            fingerprint TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '',
            UNIQUE(profile_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mira_advisor_outputs_profile_status
            ON mira_advisor_outputs(profile_id, status, rank, generated_at DESC)
        """
    )


def _row_to_output(row: Any) -> dict[str, Any]:
    data = dict(row)
    payload = _json_load(data.get("payload_json"), {})
    return {
        "id": data.get("id"),
        "rank": data.get("rank"),
        "type": data.get("observation_type") or "",
        "point_of_view": data.get("point_of_view") or "",
        "evidence": data.get("evidence") or "",
        "interpretation": data.get("interpretation") or "",
        "tradeoff": data.get("tradeoff") or "",
        "action": data.get("action") or "",
        "caveat": data.get("caveat") or "",
        "evidence_ids": _json_load(data.get("evidence_ids_json"), []),
        "confidence": data.get("confidence") or "medium",
        "sensitivity": data.get("sensitivity") or "low",
        "generated_at": data.get("generated_at"),
        "valid_until": data.get("valid_until"),
        "status": data.get("status") or "active",
        "version": data.get("version") or "",
        "cited_evidence": payload.get("cited_evidence") or [],
    }


def _storage_payload(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "validator_version": observation.get("validator_version") or ADVISOR_SYNTHESIS_VALIDATOR_VERSION,
        "cited_evidence": observation.get("cited_evidence") or [],
    }


def _fingerprint_observation(profile: str | None, observation: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "version": ADVISOR_SYNTHESIS_VERSION,
            "profile": _scope_profile(profile),
            "type": observation.get("type"),
            "point_of_view": observation.get("point_of_view"),
            "evidence_ids": observation.get("evidence_ids") or [],
        },
        sort_keys=True,
        default=str,
    )
    return sha1(seed.encode("utf-8")).hexdigest()[:24]


def _dossier_meta(dossier: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": dossier.get("version") or "",
        "measurement_count": len(dossier.get("measurements") or []),
        "portrait_section_count": len(dossier.get("portrait_sections") or []),
        "candidate_driver_count": len(dossier.get("candidate_drivers") or []),
        "false_alarm_count": len(dossier.get("false_alarms") or []),
        "constraint_count": len(dossier.get("constraints") or []),
        "smallest_lever_count": len(dossier.get("smallest_levers") or []),
        "confidence": dossier.get("confidence") or "medium",
    }


def _empty_result(status: str, dossier: dict[str, Any], errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "observations": [],
        "suppressed": [],
        "rejected": [],
        "errors": errors or [],
        "dossier_meta": _dossier_meta(dossier),
    }


def _clean_enum(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())[:limit]


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _word_count(value: Any) -> int:
    return len(_WORD_RE.findall(str(value or "")))


def _scope_profile(profile: str | None) -> str:
    return profile if profile and profile != "household" else "household"


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _json_load(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
        return value if value is not None else default
    except Exception:
        return default


def _json_object_text(raw: str) -> str:
    text = str(raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start >= 0 and end >= start else text


__all__ = [
    "ADVISOR_SYNTHESIS_VALIDATOR_VERSION",
    "ADVISOR_SYNTHESIS_VERSION",
    "advisor_synthesis_enabled",
    "advisor_synthesis_store_enabled",
    "build_advisor_candidate_packets",
    "build_advisor_evidence_map",
    "build_advisor_synthesis_prompt",
    "compact_dossier_for_synthesis",
    "draft_advisor_observations",
    "list_advisor_outputs",
    "parse_advisor_synthesis_output",
    "run_offline_advisor_synthesis",
    "store_advisor_outputs",
    "validate_advisor_observations",
]
