"""Offline lens-based Mira advisor memo synthesis.

This is the Phase 27.11 product candidate promoted from the bakeoff harness.
It is intentionally off the chat path and disabled by default. The model may
form advisor judgment inside focused lenses, but Python owns safe evidence,
packet validation, final memo validation, and storage.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha1
from typing import Any, Callable

from mira.advisor_fact_snapshot import (
    build_advisor_fact_snapshot,
    build_portrait_delta_packet,
    diff_advisor_fact_snapshots,
    expire_portrait_delta_packets,
    load_advisor_fact_snapshot,
    list_portrait_delta_packets,
    store_portrait_delta_packet,
    store_advisor_fact_snapshot,
)
from mira.safe_finance_query import execute_metric


ADVISOR_LENS_SYNTHESIS_VERSION = "mira_advisor_lens_synthesis_v1"
ADVISOR_LENS_VALIDATOR_VERSION = "mira_advisor_lens_validator_v1"
ADVISOR_LENS_MAX_TOKENS = int(os.getenv("MIRA_ADVISOR_LENS_MAX_TOKENS", "1800"))
ADVISOR_LENS_FINAL_MAX_TOKENS = int(os.getenv("MIRA_ADVISOR_LENS_FINAL_MAX_TOKENS", "2600"))
ADVISOR_LENS_MIN_MEMO_CHARS = int(os.getenv("MIRA_ADVISOR_LENS_MIN_MEMO_CHARS", "2200"))
ADVISOR_LENS_MIN_INTERVAL_MINUTES = int(os.getenv("MIRA_ADVISOR_LENS_MIN_INTERVAL_MINUTES", "1440"))
ADVISOR_LENS_CONTEXT_MAX_CHARS = int(os.getenv("MIRA_ADVISOR_LENS_CONTEXT_MAX_CHARS", "2200"))

_FALSE_VALUES = {"0", "false", "no", "off"}

SECTION_THE_READ = "The Read"
SECTION_NORMAL_MONTH = "The Month I Would Plan Around"
SECTION_NOISE = "What I Am Keeping Out Of The Verdict"
SECTION_MONEY_MAP = "The Money Map"
SECTION_ACTIONS = "Moves I Would Make First"
SECTION_DO_NOT_OVERREACT = "What I Would Leave Alone"
SECTION_VERIFY = "What Could Change This Read"

_METRIC_QUERIES: tuple[dict[str, Any], ...] = (
    {"metric": "advisor_period_reliability", "range": "all_time", "limit": 8},
    {"metric": "cash_flow_compression", "range": "all_time", "limit": 16},
    {"metric": "cash_vs_liability_position", "range": "last_6_months", "limit": 8},
    {"metric": "cash_runway", "range": "last_6_months", "limit": 8},
    {"metric": "floor_burn", "range": "last_6_months", "limit": 8},
    {"metric": "recurring_obligation_calendar", "range": "last_6_months", "limit": 20},
    {"metric": "income_series", "range": "last_12_months", "limit": 12},
    {"metric": "income_cadence", "range": "last_12_months", "limit": 20},
    {"metric": "income_source_concentration", "range": "last_12_months", "limit": 12},
    {"metric": "income_source_continuity", "range": "last_12_months", "limit": 12},
    {"metric": "income_volatility", "range": "last_12_months", "limit": 12},
    {"metric": "monthly_spend_series", "range": "last_12_months", "limit": 12},
    {"metric": "money_flow_baseline", "range": "last_12_months", "limit": 16},
    {"metric": "category_advisor_ledger", "range": "all_time", "limit": 18},
    {"metric": "merchant_lifecycle", "range": "all_time", "limit": 24},
    {"metric": "external_transfer_pressure", "range": "all_time", "limit": 16},
    {"metric": "monthly_operating_statement", "range": "last_12_months", "limit": 16},
    {"metric": "goal_capacity_statement", "range": "last_12_months", "limit": 16},
    {"metric": "savings_rate_trend", "range": "last_12_months", "limit": 12},
    {"metric": "savings_scenarios", "range": "all_time", "limit": 16},
    {"metric": "financial_timeline_events", "range": "last_12_months", "limit": 12},
    {"metric": "spending_event_clusters", "range": "last_12_months", "limit": 5},
    {"metric": "private_discretionary_patterns", "range": "last_12_months", "limit": 12},
    {"metric": "realistic_trim_levers", "range": "last_12_months", "limit": 12},
    {"metric": "avoidable_leakage", "range": "last_12_months", "limit": 12},
    {"metric": "small_frequent_leak", "range": "last_6_months", "limit": 12},
    {"metric": "category_driver_decomposition", "range": "last_6_months", "limit": 16},
    {"metric": "merchant_driver_decomposition", "range": "last_6_months", "limit": 16},
    {"metric": "goal_feasibility", "range": "last_6_months", "limit": 12},
    {"metric": "safe_to_spend_status", "range": "current_month", "limit": 8},
    {"metric": "debt_payment_pressure", "range": "last_6_months", "limit": 8},
    {"metric": "net_worth_series", "range": "last_6_months", "limit": 12},
    {"metric": "advisor_data_quality_profile", "range": "all_time", "limit": 12},
)

_LENSES: tuple[dict[str, Any], ...] = (
    {
        "id": "money_map",
        "name": "where money normally goes, event-adjusted baseline, controllability, and leakage before cuts",
        "theme_obligations": ("money_map_baseline", "category_ledger_matters", "avoidable_leakage_first"),
        "metrics": (
            "money_flow_baseline",
            "category_advisor_ledger",
            "monthly_operating_statement",
            "goal_capacity_statement",
            "avoidable_leakage",
            "monthly_spend_series",
            "recurring_obligation_calendar",
        ),
    },
    {
        "id": "operating_capacity",
        "name": "leftover capacity, goal feasibility, debt movement, and what can actually go to goals",
        "theme_obligations": ("goal_capacity_reality",),
        "metrics": ("monthly_operating_statement", "goal_capacity_statement", "goal_feasibility", "savings_rate_trend", "debt_payment_pressure"),
    },
    {
        "id": "resilience",
        "name": "liquidity, runway, cash-flow compression, debt pressure, and what not to panic about",
        "theme_obligations": ("liquidity_not_primary_risk", "cash_flow_compression_matters"),
        "metrics": ("cash_vs_liability_position", "cash_runway", "cash_flow_compression", "debt_payment_pressure", "net_worth_series", "savings_rate_trend"),
    },
    {
        "id": "income",
        "name": "income continuity, source changes, cadence, and timing assumptions",
        "theme_obligations": ("income_continuity_uncertain",),
        "metrics": ("income_series", "income_cadence", "income_source_concentration", "income_source_continuity", "income_volatility"),
    },
    {
        "id": "floor_commitments",
        "name": "fixed floor, recurring obligations, and monthly constraints",
        "theme_obligations": ("fixed_floor_matters",),
        "metrics": ("floor_burn", "recurring_obligation_calendar", "safe_to_spend_status"),
    },
    {
        "id": "events_noise",
        "name": "event clusters, trip spend, refunds/noise, and baseline exclusions",
        "theme_obligations": ("trip_event_exclusion", "period_reliability_matters"),
        "metrics": ("advisor_period_reliability", "financial_timeline_events", "spending_event_clusters", "monthly_spend_series"),
    },
    {
        "id": "discretionary_levers",
        "name": "spend types, practical reduction levers, sensitive categories, and tune-ups",
        "theme_obligations": (
            "merchant_lifecycle_matters",
            "protect_vaping_pause",
            "alcohol_soft_ceiling",
            "fees_inspection_first",
            "amazon_tune_up",
            "geico_vendor_review",
        ),
        "metrics": (
            "merchant_lifecycle",
            "private_discretionary_patterns",
            "realistic_trim_levers",
            "small_frequent_leak",
            "category_driver_decomposition",
            "merchant_driver_decomposition",
            "recurring_obligation_calendar",
        ),
    },
    {
        "id": "goals_caveats",
        "name": "goal feasibility, savings scenarios, external-transfer labels, missing data, confidence, and caveats",
        "theme_obligations": ("external_transfer_labeling", "savings_scenarios_are_options", "data_quality_limits_precision", "missing_data_caveats"),
        "metrics": (
            "external_transfer_pressure",
            "savings_scenarios",
            "advisor_data_quality_profile",
            "goal_capacity_statement",
            "goal_feasibility",
            "safe_to_spend_status",
            "savings_rate_trend",
        ),
    },
)

THESIS_CATALOG: dict[str, str] = {
    "period_reliability_matters": "The read must state which period is reliable, whether the latest month is partial, and why that affects conclusions.",
    "cash_flow_compression_matters": "The read must compare recent complete-month cash flow with the trailing view and explain whether pressure is tightening.",
    "money_map_baseline": "The read must start with where money normally goes: income, normal spend, fixed floor, flexible spend, and event exclusions.",
    "category_ledger_matters": "The read must include the category ledger: top categories, merchant drivers, ticket size, and what is controllable.",
    "merchant_lifecycle_matters": "The read must notice merchant lifecycle patterns such as top, new, dormant, or split-label merchants.",
    "external_transfer_labeling": "External transfers must be separated from lifestyle spending and labeled before they are treated as goals, debt, support, or discretionary outflow.",
    "goal_capacity_reality": "The read must state what monthly capacity is actually available for configured goals after reconciled operating burn, leakage, and debt movement context.",
    "savings_scenarios_are_options": "Savings scenarios must be framed as optional sensitivities, not commands or moral judgments.",
    "liquidity_not_primary_risk": "Liquidity is strong enough that cash panic is not the main read.",
    "fixed_floor_matters": "The fixed monthly floor should anchor the operating plan.",
    "income_continuity_uncertain": "Income continuity/source labeling needs verification before trusting forward assumptions.",
    "trip_event_exclusion": "Trip/event spend should be separated from normal lifestyle baseline.",
    "avoidable_leakage_first": "Avoidable leakage such as fees or duplicate/review rows should be fixed before painful lifestyle cuts.",
    "protect_vaping_pause": "Private vaping spend has changed materially; protect any reduction if sync confirms it.",
    "alcohol_soft_ceiling": "Private alcohol rhythm is a soft-ceiling/tuning issue, not a morality read.",
    "fees_inspection_first": "Fees should be inspected before broad category cuts.",
    "amazon_tune_up": "Amazon-style small purchases are a low-friction tune-up, not the main thesis.",
    "geico_vendor_review": "GEICO is a vendor-review/quote candidate, not day-to-day overspending.",
    "data_quality_limits_precision": "The read must state data-quality limits such as low-confidence rows, missing investments, duplicates, or unreviewed transactions.",
    "missing_data_caveats": "The recommendation must state what incomplete data could change.",
}
REQUIRED_THESES = tuple(THESIS_CATALOG.keys())

_THESIS_DIRECT_METRICS: dict[str, tuple[str, ...]] = {
    "period_reliability_matters": ("advisor_period_reliability",),
    "cash_flow_compression_matters": ("cash_flow_compression",),
    "money_map_baseline": ("money_flow_baseline", "monthly_operating_statement", "monthly_spend_series", "floor_burn"),
    "category_ledger_matters": ("category_advisor_ledger", "money_flow_baseline"),
    "merchant_lifecycle_matters": ("merchant_lifecycle",),
    "external_transfer_labeling": ("external_transfer_pressure",),
    "goal_capacity_reality": ("goal_capacity_statement", "monthly_operating_statement", "goal_feasibility"),
    "savings_scenarios_are_options": ("savings_scenarios",),
    "liquidity_not_primary_risk": ("cash_runway", "cash_vs_liability_position"),
    "fixed_floor_matters": ("floor_burn", "recurring_obligation_calendar"),
    "income_continuity_uncertain": ("income_source_continuity", "financial_timeline_events", "income_source_concentration"),
    "trip_event_exclusion": ("financial_timeline_events", "spending_event_clusters"),
    "avoidable_leakage_first": ("avoidable_leakage", "realistic_trim_levers", "financial_timeline_events"),
    "protect_vaping_pause": ("realistic_trim_levers", "private_discretionary_patterns", "financial_timeline_events"),
    "alcohol_soft_ceiling": ("realistic_trim_levers", "private_discretionary_patterns", "financial_timeline_events"),
    "fees_inspection_first": ("realistic_trim_levers", "financial_timeline_events", "category_driver_decomposition"),
    "amazon_tune_up": ("realistic_trim_levers", "small_frequent_leak"),
    "geico_vendor_review": ("recurring_obligation_calendar", "realistic_trim_levers", "financial_timeline_events"),
    "data_quality_limits_precision": ("advisor_data_quality_profile",),
    "missing_data_caveats": ("safe_to_spend_status", "goal_capacity_statement", "goal_feasibility", "floor_burn", "income_source_continuity", "advisor_data_quality_profile"),
}

_FALLBACK_PREFERRED_THESES = {
    "external_transfer_labeling",
    "savings_scenarios_are_options",
    "data_quality_limits_precision",
}
_ANCHOR_MERGE_THESES = {
    "money_map_baseline",
    "goal_capacity_reality",
    "liquidity_not_primary_risk",
    "trip_event_exclusion",
    "category_ledger_matters",
    "merchant_lifecycle_matters",
    "savings_scenarios_are_options",
    "external_transfer_labeling",
}

_LENS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "lens_id": {"type": "string"},
        "lens_read": {"type": "string"},
        "supported_theses": {"type": "array", "items": {"type": "object"}},
        "missing_or_uncertain": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["lens_id", "lens_read", "supported_theses"],
}

_FINAL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "memo_markdown": {"type": "string"},
        "thesis_order": {"type": "array", "items": {"type": "string"}},
        "quality_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["memo_markdown", "thesis_order"],
}

_NUMERIC_RE = re.compile(r"(?<![A-Za-z])(?:\$?\d[\d,]*(?:\.\d+)?%?)(?![A-Za-z])")
_BROKEN_NUMERIC_TOKEN_RE = re.compile(r"\d[\s\u00a0_]+\d")
_CORRUPT_YEAR_TOKEN_RE = re.compile(r"\b(?:19|20)\d{0,2}[A-Za-z]{2,}\b")
_CORRUPT_ALNUM_TOKEN_RE = re.compile(r"\b(?:\d+[A-Za-z]{2,}|[A-Za-z]{2,}\d+[A-Za-z]*)\b")
_NON_ASCII_NUMERIC_RE = re.compile(r"(?:[\d$][^\x00-\x7F]+|[^\x00-\x7F]+[\d])")
_SPLIT_YEAR_PUNCT_RE = re.compile(r"\b(?:19|20)[\s\u00a0_/,;-]{1,12}\d{2}\b")
_TRUNCATED_YEAR_TOKEN_RE = re.compile(r"\b(?:early|mid|late)\s+20\b", re.IGNORECASE)
_RAW_FIELD_NAME_RE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")
_LOOSE_APPROXIMATION_RE = re.compile(
    r"(?:~|roughly|approximately|\babout\s+\$|\baround\s+\$|\bover\s+\$|\bunder\s+\$)",
    re.IGNORECASE,
)
_UPPERCASE_PHRASE_RE = re.compile(r"\b[A-Z][A-Z0-9&.\-]*(?:\s+[A-Z][A-Z0-9&.\-]*)*\b")
_INTERNAL_TERM_REPLACEMENTS = {
    "cash_like_balance": "cash-like balance",
    "cash_runway_days": "cash runway",
    "normal_monthly_burn": "normal monthly burn",
    "liability_to_cash_ratio": "liability-to-cash ratio",
    "payment_total": "payment total",
    "savings_rate_trend": "savings rate trend",
    "savings_rate": "savings rate",
    "floor_burn": "floor burn",
    "monthly_total_income": "monthly income",
    "monthly_expenses": "monthly expenses",
    "deduped_monthly_equivalent": "monthly equivalent",
    "structural_housing_monthly": "structural housing costs",
}
_TONE_REPLACEMENTS = {
    "exceptionally": "very",
    "massive": "large",
    "negligible": "small",
}
_FORBIDDEN_MEMO_PHRASES = (
    "as an ai",
    "dashboard snapshot",
    "available for your review",
    "review your spending",
    "track your expenses",
    "make a budget",
    "consider reducing expenses",
    "safe_finance_query",
    "run_sql",
    "sql",
    "query layer",
    "validator",
    "backend",
    "evidence id",
    "evidence_id",
    "metric:",
    "txn:",
    "status is marked",
    "unlabeled_or_changed_source",
    "exceptionally resilient",
    "exceptionally stable",
    "exceptionally strong",
    "exceptionally",
    "massive cash",
    "massive buffer",
    "massive",
    "negligible debt",
    "negligible",
    "sync is an active obligation",
    "data sync is an active obligation",
    "7 and a half months",
    "state the goal capacity reality",
)
_SHAMING_PHRASES = (
    "addiction",
    "bad habit",
    "cut back on alcohol",
    "cut back on vaping",
    "irresponsible",
    "lack discipline",
    "reckless",
    "shame",
    "you failed",
    "you messed up",
)


def advisor_lens_synthesis_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_LENS_SYNTHESIS_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_lens_store_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_LENS_STORE_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_lens_background_auto_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_LENS_BACKGROUND_AUTO_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_lens_context_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_LENS_CONTEXT_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_lens_ui_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_LENS_UI_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def advisor_lens_post_rewarm_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_LENS_POST_REWARM_ENABLED", "0").strip().lower() not in _FALSE_VALUES


def rewarm_chat_after_advisor(
    *,
    force: bool = False,
    reason: str = "advisor_lens_background_memo",
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Best-effort chat-model rewarm after a heavy advisor-model run."""

    if not force and not advisor_lens_post_rewarm_enabled():
        return {"status": "skipped", "reason": "disabled"}

    purposes = _advisor_lens_post_rewarm_purposes()
    max_tokens = _advisor_lens_post_rewarm_max_tokens()
    prompt = "Mira advisor background work finished. Reply with exactly: ready"
    started = time.perf_counter()
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for purpose in purposes:
        trace_token = None
        try:
            if complete_fn is None:
                import llm_client

                trace_token = llm_client.start_trace()
                output = llm_client.complete(prompt, max_tokens=max_tokens, purpose=purpose)
                calls.extend(llm_client.finish_trace(trace_token))
                trace_token = None
            else:
                output = complete_fn(prompt, max_tokens, purpose)
            results.append({"purpose": purpose, "ok": True, "output_chars": len(str(output or ""))})
        except Exception as exc:
            errors.append(f"{purpose}:{type(exc).__name__}:{exc}")
        finally:
            if trace_token is not None:
                try:
                    import llm_client

                    calls.extend(llm_client.finish_trace(trace_token))
                except Exception:
                    pass

    status = "ok" if not errors else ("partial" if results else "error")
    return {
        "status": status,
        "reason": reason,
        "purposes": purposes,
        "max_tokens": max_tokens,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "results": results,
        "errors": errors,
        "llm_calls": [_compact_rewarm_call(call) for call in calls if isinstance(call, dict)],
    }


def advisor_lens_answer_context(
    *,
    conn,
    profile: str | None = None,
    question: str = "",
    route: dict[str, Any] | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Return compact stored advisor memo context for follow-up answers."""

    if not advisor_lens_context_enabled():
        return _empty_answer_context("advisor_context_disabled")
    if not _advisor_context_relevant(question=question, route=route):
        return _empty_answer_context("advisor_context_not_relevant")
    memos = list_lens_advisor_memos(conn=conn, profile=profile, limit=1)
    if not memos:
        return _empty_answer_context("advisor_context_no_memo")
    memo = memos[0]
    delta = _latest_advisor_context_delta(conn=conn, profile=profile, memo=memo)
    block = _advisor_context_block(memo, delta=delta, max_chars=max_chars)
    if not block:
        return _empty_answer_context("advisor_context_empty")
    return {
        "block": block,
        "used": True,
        "count": 1,
        "reason": "advisor_lens_memo",
        "items": [
            {
                "id": memo.get("id"),
                "generated_at": memo.get("generated_at"),
                "version": memo.get("version"),
                "has_delta": bool(delta),
            }
        ],
    }


def advisor_lens_background_auto_decision(
    *,
    profile: str | None = None,
    conn,
    minutes: int | None = None,
) -> dict[str, Any]:
    """Return whether a background advisor memo should be queued."""

    if not advisor_lens_background_auto_enabled():
        return {"should_queue": False, "reason": "auto_disabled"}
    if not advisor_lens_synthesis_enabled():
        return {"should_queue": False, "reason": "synthesis_disabled"}
    if not advisor_lens_store_enabled():
        return {"should_queue": False, "reason": "store_disabled"}
    if has_fresh_advisor_lens_memo(conn=conn, profile=profile, minutes=minutes):
        return {"should_queue": False, "reason": "fresh_cache"}
    return {
        "should_queue": True,
        "reason": "stale_or_missing",
        "min_interval_minutes": int(minutes if minutes is not None else _advisor_lens_min_interval_minutes()),
    }


def run_advisor_lens_background_memo(
    *,
    conn,
    profile: str | None = None,
    complete_fn: Callable[..., str] | None = None,
    force: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Run and store the private advisor memo from a background task only."""

    portrait_delta: dict[str, Any] | None = None
    if not force:
        decision = advisor_lens_background_auto_decision(profile=profile, conn=conn)
        if not decision.get("should_queue"):
            status = "fresh_cache" if decision.get("reason") == "fresh_cache" else "skipped"
            return {"status": status, "fresh_cache": decision.get("reason") == "fresh_cache", "decision": decision}
        try:
            portrait_delta = run_advisor_lens_portrait_delta(conn=conn, profile=profile, as_of=as_of, store=True)
        except Exception as exc:
            portrait_delta = {
                "status": "error",
                "decision": "queue_full_advisor_synthesis",
                "error": f"{type(exc).__name__}:{exc}",
            }
        else:
            if portrait_delta.get("decision") == "keep_stored_read":
                return {
                    "status": "fresh_cache",
                    "fresh_cache": True,
                    "decision": decision,
                    "portrait_delta": portrait_delta,
                }
            if portrait_delta.get("decision") in {"store_targeted_delta", "keep_existing_delta"}:
                return {
                    "status": "targeted_delta",
                    "fresh_cache": False,
                    "decision": decision,
                    "portrait_delta": portrait_delta,
                }
    elif not advisor_lens_synthesis_enabled():
        return {"status": "disabled", "fresh_cache": False, "decision": {"reason": "synthesis_disabled"}}
    elif not advisor_lens_store_enabled():
        return {"status": "store_disabled", "fresh_cache": False, "decision": {"reason": "store_disabled"}}

    run = run_offline_advisor_lens_synthesis(
        conn=conn,
        profile=profile,
        complete_fn=complete_fn,
        force=False,
        store=True,
        as_of=as_of,
    )
    run["fresh_cache"] = False
    if portrait_delta is not None:
        run["portrait_delta"] = portrait_delta
    run["post_advisor_rewarm"] = rewarm_chat_after_advisor(reason="advisor_lens_background_memo")
    return run


def run_offline_advisor_lens_synthesis(
    *,
    conn,
    profile: str | None = None,
    complete_fn: Callable[..., str] | None = None,
    force: bool = False,
    store: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Run the validated lens memo path. Does not touch the chat path."""

    started = time.perf_counter()
    if not force and not advisor_lens_synthesis_enabled():
        return {"status": "disabled", "memo": None, "stored_count": 0}

    bundle = build_lens_evidence_bundle(conn, profile=profile, as_of=as_of)
    draft = draft_lens_advisor_memo(bundle=bundle, complete_fn=complete_fn, force=True)
    quality = validate_lens_advisor_memo(draft.get("payload") or {}, build_lens_evidence_map(bundle))
    status = "ok" if quality.get("ok") else "no_valid_memo"
    stored: list[dict[str, Any]] = []
    if status == "ok" and (store or advisor_lens_store_enabled()):
        stored = store_lens_advisor_memo(
            conn=conn,
            profile=profile,
            payload=draft.get("payload") or {},
            quality=quality,
            force=force or store,
            as_of=as_of,
        )
    return {
        "status": status if draft.get("status") == "ok" else draft.get("status"),
        "memo": (draft.get("payload") or {}).get("memo_markdown") if quality.get("ok") else None,
        "payload": draft.get("payload") or {},
        "quality": quality,
        "errors": draft.get("errors") or [],
        "lens_reads": draft.get("lens_reads") or [],
        "candidate_theses": draft.get("candidate_theses") or [],
        "stored_count": len(stored),
        "evidence_meta": _bundle_meta(bundle),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def draft_lens_advisor_memo(
    *,
    bundle: dict[str, Any],
    complete_fn: Callable[..., str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run focused advisor lenses and compose one validated private memo."""

    if not force and not advisor_lens_synthesis_enabled():
        return {"status": "disabled", "payload": {}, "errors": []}

    evidence_map = build_lens_evidence_map(bundle)
    lens_reads: list[dict[str, Any]] = []
    errors: list[str] = []
    for lens in _LENSES:
        prompt = build_lens_prompt(bundle, lens)
        try:
            parsed = _complete_json(
                prompt,
                max_tokens=ADVISOR_LENS_MAX_TOKENS,
                response_format=_LENS_OUTPUT_SCHEMA,
                complete_fn=complete_fn,
            )
            if isinstance(parsed, dict):
                lens_reads.append(parsed)
            else:
                errors.append(f"lens_{lens['id']}:not_object")
        except Exception as exc:
            errors.append(f"lens_{lens['id']}:{type(exc).__name__}:{exc}")

    candidate_theses = merge_lens_theses(lens_reads, evidence_map)
    if lens_reads:
        candidate_theses = _fill_required_thesis_fallbacks(candidate_theses, evidence_map)
    missing = [thesis_id for thesis_id in REQUIRED_THESES if thesis_id not in {item.get("thesis_id") for item in candidate_theses}]
    if missing:
        try:
            repair = _complete_json(
                build_missing_thesis_repair_prompt(bundle, missing),
                max_tokens=ADVISOR_LENS_MAX_TOKENS,
                response_format=_LENS_OUTPUT_SCHEMA,
                complete_fn=complete_fn,
            )
            if isinstance(repair, dict):
                lens_reads.append(repair)
                candidate_theses = merge_lens_theses(lens_reads, evidence_map)
                if lens_reads:
                    candidate_theses = _fill_required_thesis_fallbacks(candidate_theses, evidence_map)
            else:
                errors.append("missing_thesis_repair:not_object")
        except Exception as exc:
            errors.append(f"missing_thesis_repair:{type(exc).__name__}:{exc}")

    if not candidate_theses:
        payload = {}
        quality = validate_lens_advisor_memo(payload, evidence_map)
        return {
            "status": "no_valid_memo",
            "payload": payload,
            "quality": quality,
            "errors": errors,
            "lens_reads": lens_reads,
            "candidate_theses": candidate_theses,
        }

    quality: dict[str, Any] = {}
    try:
        final = _complete_json(
            build_lens_final_prompt(bundle, lens_reads, candidate_theses),
            max_tokens=ADVISOR_LENS_FINAL_MAX_TOKENS,
            response_format=_FINAL_OUTPUT_SCHEMA,
            complete_fn=complete_fn,
        )
        payload = compose_lens_final_payload(final, candidate_theses, evidence_map=evidence_map)
        quality = validate_lens_advisor_memo(payload, evidence_map)
        if not quality.get("ok") and candidate_theses:
            try:
                repaired = _complete_json(
                    build_lens_final_repair_prompt(candidate_theses, payload, quality),
                    max_tokens=ADVISOR_LENS_FINAL_MAX_TOKENS,
                    response_format=_FINAL_OUTPUT_SCHEMA,
                    complete_fn=complete_fn,
                )
                repaired_payload = compose_lens_final_payload(repaired, candidate_theses, evidence_map=evidence_map)
                repaired_quality = validate_lens_advisor_memo(repaired_payload, evidence_map)
                if repaired_quality.get("ok") or _quality_score(repaired_quality) > _quality_score(quality):
                    payload = repaired_payload
                    quality = repaired_quality
            except Exception as exc:
                errors.append(f"final_repair:{type(exc).__name__}:{exc}")
    except Exception as exc:
        payload = {}
        errors.append(f"final:{type(exc).__name__}:{exc}")

    if not quality:
        quality = validate_lens_advisor_memo(payload, evidence_map)
    return {
        "status": "ok" if quality.get("ok") else "no_valid_memo",
        "payload": payload,
        "quality": quality,
        "errors": errors,
        "lens_reads": lens_reads,
        "candidate_theses": candidate_theses,
    }


def build_lens_evidence_bundle(conn, *, profile: str | None = None, as_of: str | None = None) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for query in _METRIC_QUERIES:
        result = execute_metric(conn, query, profile=profile, as_of=as_of)
        metrics[query["metric"]] = _compact_metric(result)
        if result.get("status") == "error" or result.get("errors"):
            errors.append({"metric": query["metric"], "status": result.get("status"), "errors": result.get("errors")})
    return {
        "version": ADVISOR_LENS_SYNTHESIS_VERSION,
        "profile": _scope_profile(profile),
        "metric_count": len(metrics),
        "metrics": metrics,
        "errors": errors,
    }


def build_lens_evidence_map(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for metric_name, metric in (bundle.get("metrics") or {}).items():
        if not isinstance(metric, dict):
            continue
        summary_id = f"metric:{metric_name}:summary"
        out[summary_id] = {
            "kind": "metric_summary",
            "metric": metric_name,
            "values": {
                "summary_numbers": metric.get("summary_numbers") or {},
                "caveats": metric.get("caveats") or [],
                "basis": metric.get("basis") or "",
            },
        }
        for row_idx, row in enumerate(metric.get("rows") or [], start=1):
            if not isinstance(row, dict):
                continue
            row_id = f"metric:{metric_name}:{row_idx}"
            out[row_id] = {"kind": "metric_row", "metric": metric_name, "values": row}
            for evidence_id in row.get("sample_evidence_ids") or []:
                out.setdefault(str(evidence_id), {"kind": "sample_row", "metric": metric_name, "values": row})
        for evidence_id in metric.get("evidence_ids") or []:
            out.setdefault(str(evidence_id), out[summary_id])
    return out


def merge_lens_theses(lens_reads: list[dict[str, Any]], evidence_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    for lens_read in lens_reads:
        if not isinstance(lens_read, dict):
            continue
        for thesis in lens_read.get("supported_theses") or []:
            if not isinstance(thesis, dict):
                continue
            candidate = _validated_candidate(thesis, evidence_map)
            if not candidate:
                continue
            thesis_id = candidate["thesis_id"]
            score = _thesis_evidence_score(thesis_id, candidate["evidence_ids"])
            score += min(len(candidate.get("paragraph") or ""), 500) // 50
            if thesis_id not in best or score > best[thesis_id][0]:
                best[thesis_id] = (score, candidate)
    return [best[thesis_id][1] for thesis_id in REQUIRED_THESES if thesis_id in best]


def _fill_required_thesis_fallbacks(
    candidate_theses: list[dict[str, Any]],
    evidence_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(item.get("thesis_id") or ""): item for item in candidate_theses if isinstance(item, dict)}
    for thesis_id in REQUIRED_THESES:
        fallback = _fallback_thesis_candidate(thesis_id, evidence_map)
        if not fallback:
            continue
        if thesis_id not in by_id:
            by_id[thesis_id] = fallback
            continue
        current = by_id[thesis_id]
        current_score = _thesis_evidence_score(thesis_id, current.get("evidence_ids") or [])
        fallback_score = _thesis_evidence_score(thesis_id, fallback.get("evidence_ids") or [])
        if fallback_score > current_score or (fallback_score == current_score and thesis_id in _FALLBACK_PREFERRED_THESES):
            by_id[thesis_id] = fallback
        elif thesis_id in _ANCHOR_MERGE_THESES:
            by_id[thesis_id] = _merge_anchor_candidate(current, fallback)
    return [by_id[thesis_id] for thesis_id in REQUIRED_THESES if thesis_id in by_id]


def _merge_anchor_candidate(current: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Keep model phrasing while preserving deterministic anchor facts."""
    merged = dict(current)
    for field in ("paragraph", "summary"):
        current_text = clean_memo_text(merged.get(field))
        fallback_text = clean_memo_text(fallback.get(field))
        if fallback_text and not _normalized_contains(current_text, fallback_text):
            merged[field] = " ".join(piece for piece in (current_text, fallback_text) if piece).strip()
    if fallback.get("caveat") and not _normalized_contains(str(merged.get("caveat") or ""), str(fallback.get("caveat") or "")):
        merged["caveat"] = " ".join(piece for piece in (merged.get("caveat"), fallback.get("caveat")) if piece).strip()
    merged["evidence_ids"] = _dedupe_strings([*(current.get("evidence_ids") or []), *(fallback.get("evidence_ids") or [])])
    return merged


def _dedupe_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _fallback_thesis_candidate(thesis_id: str, evidence_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    packet = _fallback_thesis_packet(thesis_id, evidence_map)
    if not packet:
        return None
    return _validated_candidate(packet, evidence_map)


def _fallback_thesis_packet(thesis_id: str, evidence_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if thesis_id == "period_reliability_matters":
        summary_id = "metric:advisor_period_reliability:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        if not values.get("analysis_start"):
            return None
        partial = "the latest month is partial" if values.get("current_month_partial") else "the latest month appears complete"
        return _fallback_packet(
            thesis_id,
            [summary_id],
            f"The reliable analysis period starts at {values.get('analysis_start')}.",
            f"The reliable analysis period starts at {values.get('analysis_start')} after the first observed income row on {values.get('first_income_date')}, and {partial}. That period check should shape every trend read.",
        )
    if thesis_id == "cash_flow_compression_matters":
        summary_id = "metric:cash_flow_compression:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        if values.get("recent_3_cash_flow_rate") is None or values.get("trailing_12_cash_flow_rate") is None:
            return None
        return _fallback_packet(
            thesis_id,
            [summary_id],
            "Recent complete-month cash flow is tighter than the trailing view.",
            f"Recent complete-month cash flow is {_percent(values.get('recent_3_cash_flow_rate'))} versus {_percent(values.get('trailing_12_cash_flow_rate'))} for the trailing complete-month view, so Mira should decide whether that compression is structural pressure or timing noise.",
        )
    if thesis_id == "money_map_baseline":
        summary_id = "metric:money_flow_baseline:summary"
        operating_id = "metric:monthly_operating_statement:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        operating = _evidence_values(evidence_map, operating_id).get("summary_numbers") or {}
        if not values.get("avg_monthly_spend_after_event_exclusions"):
            return None
        spend = _money(values.get("avg_monthly_spend_after_event_exclusions"))
        floor = _money(values.get("fixed_floor_monthly"))
        flexible = _money(values.get("flexible_monthly_estimate"))
        income = _money(operating.get("avg_monthly_income"))
        burn = _money(operating.get("reconciled_operating_burn"))
        return _fallback_packet(
            thesis_id,
            [summary_id, operating_id if operating else ""],
            f"Where the money normally goes starts with a {spend} event-adjusted spending baseline.",
            f"Where the money normally goes should come before risk ranking: average monthly income is {income}, normal spending is {spend}, reconciled operating burn is {burn}, the fixed monthly floor is {floor}, and visible flexible spending is {flexible}.",
        )
    if thesis_id == "category_ledger_matters":
        evidence_id = _first_metric_row_id(evidence_map, "category_advisor_ledger") or "metric:category_advisor_ledger:summary"
        values = _evidence_values(evidence_map, evidence_id)
        if not values:
            return None
        category = values.get("category") or (values.get("summary_numbers") or {}).get("top_category") or "the top category"
        monthly = _money(values.get("monthly_average"))
        ticket = _money(values.get("avg_ticket_size"))
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            f"The category ledger should explain {category}, not just name it.",
            f"The category ledger should explain where the money goes by category and merchant: {category} shows {monthly} per month and {ticket} average ticket size, so Mira should distinguish repeat pressure from one-off noise before recommending cuts.",
        )
    if thesis_id == "merchant_lifecycle_matters":
        summary_id = "metric:merchant_lifecycle:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        if not values.get("merchant_count"):
            return None
        return _fallback_packet(
            thesis_id,
            [summary_id],
            "Merchant lifecycle matters because top, new, dormant, and split-label merchants tell different stories.",
            f"Merchant lifecycle matters because there are {_number(values.get('merchant_count'))} merchants in the advisor view, the top merchant is {values.get('top_merchant')}, and split-label groups need cleanup before Mira treats merchant drift as a behavior change.",
        )
    if thesis_id == "external_transfer_labeling":
        summary_id = "metric:external_transfer_pressure:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        if not values.get("net_external_transfer_outflow"):
            return None
        return _fallback_packet(
            thesis_id,
            [summary_id],
            "External transfers should be labeled before they are judged as spending.",
            f"External transfers should be labeled before they are judged as lifestyle spending: incoming external transfers are {_money(values.get('incoming_external_transfer_total'))}, outgoing external transfers are {_money(values.get('outgoing_external_transfer_total'))}, and net external transfer outflow is {_money(values.get('net_external_transfer_outflow'))}.",
        )
    if thesis_id == "goal_capacity_reality":
        summary_id = "metric:goal_capacity_statement:summary"
        operating_id = "metric:monthly_operating_statement:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        operating = _evidence_values(evidence_map, operating_id).get("summary_numbers") or {}
        if not values.get("capacity_before_configured_goals") and not values.get("reconciled_operating_burn"):
            return None
        capacity = _money(values.get("capacity_before_configured_goals"))
        burn = _money(values.get("reconciled_operating_burn") or operating.get("reconciled_operating_burn"))
        required = _money(values.get("required_goal_contribution_monthly"))
        after_goals = _money(values.get("capacity_after_required_goals"))
        return _fallback_packet(
            thesis_id,
            [summary_id, operating_id if operating else ""],
            f"Goal capacity starts with {capacity} of monthly room after reconciled operating burn.",
            f"Goal capacity should be judged from the operating statement: reconciled operating burn is {burn}, capacity before configured goals is {capacity}, configured goals require {required} monthly, and capacity after those goal targets is {after_goals}.",
        )
    if thesis_id == "savings_scenarios_are_options":
        evidence_id = _first_metric_row_id(evidence_map, "savings_scenarios")
        if not evidence_id:
            return None
        values = _evidence_values(evidence_map, evidence_id)
        subject = values.get("subject") or "the top scenario"
        effect = _money(values.get("monthly_effect"))
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "Savings scenarios should be treated as options, not commands.",
            f"Savings scenarios should be treated as options, not commands: {subject} has a planning sensitivity of {effect} per month, and the tradeoff is to avoid turning a scenario into a moral judgment.",
        )
    if thesis_id == "liquidity_not_primary_risk":
        summary_id = "metric:cash_runway:summary"
        position_id = "metric:cash_vs_liability_position:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        position = _evidence_values(evidence_map, position_id).get("summary_numbers") or {}
        if not values.get("cash_runway_days"):
            return None
        cash_like = _money(position.get("cash_like_balance", values.get("cash_like_balance")))
        liability = _money(position.get("liability_total"))
        return _fallback_packet(
            thesis_id,
            [summary_id, position_id if position else ""],
            "Liquidity is strong enough that cash panic is not the main read.",
            f"Liquidity is strong enough that cash panic is not the main read: cash-like balance is {cash_like}, cash runway is {_number(values.get('cash_runway_days'))} days against a normal monthly burn of {_money(values.get('normal_monthly_burn'))}, with liabilities at {liability}.",
        )
    if thesis_id == "fixed_floor_matters":
        summary_id = "metric:floor_burn:summary"
        recurring_id = "metric:recurring_obligation_calendar:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        recurring = _evidence_values(evidence_map, recurring_id).get("summary_numbers") or {}
        if not values.get("floor_burn_monthly"):
            return None
        duplicate_count = _number(recurring.get("duplicate_row_count")) if recurring else "0"
        return _fallback_packet(
            thesis_id,
            [summary_id, recurring_id if recurring else ""],
            f"The fixed monthly floor is {_money(values.get('floor_burn_monthly'))}.",
            f"The fixed monthly floor is {_money(values.get('floor_burn_monthly'))}, recurring commitments are {_money(values.get('recurring_monthly'))}, and recurring duplicate candidates are {duplicate_count}, so the floor should anchor the operating plan before flexible spending decisions.",
        )
    if thesis_id == "income_continuity_uncertain":
        evidence_id = _first_metric_row_id(evidence_map, "income_source_continuity") or "metric:income_source_continuity:summary"
        if evidence_id not in evidence_map:
            return None
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "Income continuity and source labeling need verification.",
            "Income continuity and source labeling need verification before trusting forward assumptions.",
        )
    if thesis_id == "trip_event_exclusion":
        cluster_ids = _event_cluster_anchor_ids(evidence_map)
        evidence_id = cluster_ids[0] if cluster_ids else "metric:money_flow_baseline:summary"
        if evidence_id not in evidence_map:
            return None
        paragraph = _event_cluster_anchor_paragraph(evidence_map, cluster_ids)
        return _fallback_packet(
            thesis_id,
            cluster_ids or [evidence_id],
            "Trip/event spend should be separated from the normal baseline.",
            paragraph or "Trip/event spend should be separated from the normal lifestyle baseline before deciding what actually changed.",
        )
    if thesis_id == "avoidable_leakage_first":
        summary_id = "metric:avoidable_leakage:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        if not values.get("fee_or_interest_total") and not values.get("recurring_duplicate_candidate_count"):
            return None
        amount = _money(values.get("fee_or_interest_total"))
        return _fallback_packet(
            thesis_id,
            [summary_id, *(_first_metric_row_ids(evidence_map, "avoidable_leakage", limit=1))],
            "Avoidable leakage should be inspected before painful lifestyle cuts.",
            f"Avoidable leakage should be inspected before painful lifestyle cuts; the safe evidence shows {amount} in fee or interest review rows.",
        )
    if thesis_id == "protect_vaping_pause":
        evidence_id = _metric_row_id_with_value(evidence_map, "private_discretionary_patterns", "category", "Vaping")
        if not evidence_id:
            return None
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "Private vaping spend is lower than prior peaks and should be protected if sync is complete.",
            "Private vaping spend is lower than prior peaks and the reduction should be protected if the current sync is complete.",
        )
    if thesis_id == "alcohol_soft_ceiling":
        evidence_id = _metric_row_id_with_value(evidence_map, "private_discretionary_patterns", "category", "Alcohol")
        if not evidence_id:
            return None
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "Alcohol is a soft-ceiling/tuning issue, not a morality read.",
            "Alcohol should be treated as a soft-ceiling/tuning issue, not a morality read or the primary financial risk.",
        )
    if thesis_id == "fees_inspection_first":
        evidence_id = _first_metric_row_id(evidence_map, "avoidable_leakage") or _first_metric_row_id(evidence_map, "category_driver_decomposition")
        if not evidence_id:
            return None
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "Fees should be inspected before broad category cuts.",
            "Fees should be inspected before broad category cuts because they are preventable friction, not intentional lifestyle spending.",
        )
    if thesis_id == "amazon_tune_up":
        evidence_id = _first_metric_row_id(evidence_map, "small_frequent_leak") or _first_metric_row_id(evidence_map, "realistic_trim_levers")
        if not evidence_id:
            return None
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "Amazon-style purchases are a tune-up, not the main thesis.",
            "Amazon-style purchases are a tune-up that can be consolidated, not the main thesis or a reason to overcorrect.",
        )
    if thesis_id == "geico_vendor_review":
        evidence_id = _metric_row_id_with_value(evidence_map, "recurring_obligation_calendar", "merchant", "GEICO")
        if not evidence_id:
            return None
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "GEICO is a vendor-review and quote candidate.",
            "GEICO should be treated as a vendor-review and quote candidate rather than day-to-day overspending.",
        )
    if thesis_id == "data_quality_limits_precision":
        summary_id = "metric:advisor_data_quality_profile:summary"
        values = _evidence_values(evidence_map, summary_id).get("summary_numbers") or {}
        if not values.get("visible_transaction_count"):
            return None
        return _fallback_packet(
            thesis_id,
            [summary_id],
            "Data quality limits precision in the smaller recommendations.",
            f"Data quality limits precision: the safe profile has {_number(values.get('visible_transaction_count'))} visible transactions, {_number(values.get('low_confidence_spend_count'))} low-confidence spending rows, {_number(values.get('recurring_duplicate_row_count'))} recurring duplicate row, and {_number(values.get('investment_holding_count'))} investment holdings.",
        )
    if thesis_id == "missing_data_caveats":
        evidence_id = "metric:goal_feasibility:summary" if "metric:goal_feasibility:summary" in evidence_map else "metric:safe_to_spend_status:summary"
        if "metric:advisor_data_quality_profile:summary" in evidence_map:
            evidence_id = "metric:advisor_data_quality_profile:summary"
        if evidence_id not in evidence_map:
            return None
        return _fallback_packet(
            thesis_id,
            [evidence_id],
            "Missing goals, budgets, labels, or sync data can change the read.",
            "Missing goals, budgets, labels, or sync data can change the read, so the recommendation should stay conditional.",
        )
    return None


def _fallback_packet(thesis_id: str, evidence_ids: list[str], summary: str, paragraph: str) -> dict[str, Any]:
    return {
        "thesis_id": thesis_id,
        "summary": summary,
        "paragraph": paragraph,
        "caveat": "This read can change if the cited source data is incomplete, stale, or reclassified.",
        "evidence_ids": [evidence_id for evidence_id in evidence_ids if evidence_id],
        "confidence": "medium",
    }


def _evidence_values(evidence_map: dict[str, dict[str, Any]], evidence_id: str) -> dict[str, Any]:
    item = evidence_map.get(evidence_id) or {}
    values = item.get("values") if isinstance(item.get("values"), dict) else {}
    return values


def _first_metric_row_id(evidence_map: dict[str, dict[str, Any]], metric: str) -> str | None:
    ids = _first_metric_row_ids(evidence_map, metric, limit=1)
    return ids[0] if ids else None


def _first_metric_row_ids(evidence_map: dict[str, dict[str, Any]], metric: str, *, limit: int) -> list[str]:
    prefix = f"metric:{metric}:"
    return [
        evidence_id
        for evidence_id, item in sorted(evidence_map.items(), key=lambda pair: _metric_row_sort_key(pair[0]))
        if evidence_id.startswith(prefix)
        and not evidence_id.endswith(":summary")
        and isinstance(item, dict)
        and item.get("kind") == "metric_row"
    ][:limit]


def _metric_row_sort_key(evidence_id: str) -> tuple[str, int, int | str]:
    prefix, _, row_id = str(evidence_id or "").rpartition(":")
    return (prefix, 0, int(row_id)) if row_id.isdigit() else (prefix, 1, row_id)


def _metric_row_id_with_value(evidence_map: dict[str, dict[str, Any]], metric: str, key: str, expected: str) -> str | None:
    expected_lower = expected.lower()
    for evidence_id in _first_metric_row_ids(evidence_map, metric, limit=40):
        values = _evidence_values(evidence_map, evidence_id)
        if str(values.get(key) or "").lower() == expected_lower:
            return evidence_id
    return None


def _event_cluster_anchor_ids(evidence_map: dict[str, dict[str, Any]]) -> list[str]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for evidence_id in _first_metric_row_ids(evidence_map, "spending_event_clusters", limit=8):
        values = _evidence_values(evidence_map, evidence_id)
        if values.get("estimated_event_total"):
            rows.append((evidence_id, values))
    if not rows:
        return []
    top_id, _top = max(rows, key=lambda item: _float_value(item[1].get("estimated_event_total")))
    latest_id, _latest = max(rows, key=lambda item: str(item[1].get("activity_window_end") or item[1].get("window_end") or item[1].get("travel_window_end") or ""))
    out = [top_id]
    if latest_id != top_id:
        out.append(latest_id)
    return out


def _event_cluster_anchor_paragraph(evidence_map: dict[str, dict[str, Any]], evidence_ids: list[str]) -> str:
    pieces: list[str] = []
    for index, evidence_id in enumerate(evidence_ids[:2]):
        values = _evidence_values(evidence_map, evidence_id)
        amount = _money(values.get("estimated_event_total"))
        start = values.get("travel_window_start") or values.get("activity_window_start") or values.get("window_start")
        end = values.get("activity_window_end") or values.get("travel_window_end") or values.get("window_end")
        if not start or not end or amount == "$0.00":
            continue
        label = "largest validated cluster" if index == 0 else "latest material cluster"
        pieces.append(f"the {label} runs from {start} to {end} at {amount}")
    if not pieces:
        return ""
    cluster_text = "; ".join(pieces)
    return f"Trip/event spend should be separated from the normal lifestyle baseline before deciding what actually changed: {cluster_text}."


def _money(value: Any) -> str:
    number = _float_value(value)
    if number < 0:
        return f"-${abs(number):,.2f}"
    return f"${number:,.2f}"


def _number(value: Any) -> str:
    number = _float_value(value)
    return f"{number:.1f}" if number % 1 else str(int(number))


def _percent(value: Any) -> str:
    number = _float_value(value) * 100
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _float_value(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def compose_lens_final_payload(
    final_payload: Any,
    candidate_theses: list[dict[str, Any]],
    *,
    evidence_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(final_payload, dict):
        return {}
    by_id = {str(thesis.get("thesis_id") or ""): thesis for thesis in candidate_theses}
    requested_order = [str(value) for value in final_payload.get("thesis_order") or [] if str(value) in by_id]
    ordered_ids = requested_order + [thesis_id for thesis_id in by_id if thesis_id not in requested_order]
    ordered_theses = [by_id[thesis_id] for thesis_id in ordered_ids]
    memo_markdown = _repair_supported_name_phrases(
        clean_memo_text(final_payload.get("memo_markdown")),
        [{"values": thesis} for thesis in candidate_theses if isinstance(thesis, dict)],
    )
    memo_markdown = _repair_final_memo_theme_language(memo_markdown, candidate_theses, ordered_ids)
    memo_markdown = _repair_final_memo_anchor_language(memo_markdown, candidate_theses, ordered_ids)
    action_plan = build_advisor_ranked_actions({"memo_markdown": memo_markdown, "theses": ordered_theses})
    if evidence_map:
        ordered_theses = _augment_structured_memo_evidence(ordered_theses, evidence_map)
        memo_markdown = _compose_structured_advisor_memo(memo_markdown, ordered_theses, action_plan, evidence_map)
    else:
        memo_markdown = _attach_ranked_action_plan_section(memo_markdown, action_plan)
    cards = build_advisor_read_cards(
        {
            "memo_markdown": memo_markdown,
            "theses": ordered_theses,
            "action_plan": action_plan,
        },
        evidence_map=evidence_map,
    )
    return {
        "version": ADVISOR_LENS_SYNTHESIS_VERSION,
        "validator_version": ADVISOR_LENS_VALIDATOR_VERSION,
        "memo_markdown": memo_markdown,
        "theses": ordered_theses,
        "action_plan": action_plan,
        "cards": cards,
        "quality_notes": final_payload.get("quality_notes") if isinstance(final_payload.get("quality_notes"), list) else [],
        "final_thesis_order": requested_order,
    }


def _augment_structured_memo_evidence(
    theses: list[dict[str, Any]],
    evidence_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(thesis.get("thesis_id") or ""): dict(thesis) for thesis in theses if isinstance(thesis, dict)}

    def add(thesis_id: str, evidence_ids: list[str]) -> None:
        thesis = by_id.get(thesis_id)
        if not thesis:
            return
        thesis["evidence_ids"] = _dedupe_strings([*(thesis.get("evidence_ids") or []), *evidence_ids])

    add("money_map_baseline", ["metric:money_flow_baseline:summary", "metric:monthly_operating_statement:summary"])
    add("goal_capacity_reality", ["metric:goal_capacity_statement:summary", "metric:monthly_operating_statement:summary"])
    add("liquidity_not_primary_risk", ["metric:cash_runway:summary", "metric:cash_vs_liability_position:summary"])
    add("fixed_floor_matters", ["metric:floor_burn:summary", "metric:recurring_obligation_calendar:summary"])
    add("external_transfer_labeling", ["metric:external_transfer_pressure:summary"])
    add("period_reliability_matters", ["metric:advisor_period_reliability:summary"])
    add("cash_flow_compression_matters", ["metric:cash_flow_compression:summary"])
    add("data_quality_limits_precision", ["metric:advisor_data_quality_profile:summary"])
    add("missing_data_caveats", ["metric:advisor_data_quality_profile:summary"])
    add("savings_scenarios_are_options", _first_metric_row_ids(evidence_map, "savings_scenarios", limit=1))
    add("category_ledger_matters", _first_metric_row_ids(evidence_map, "category_advisor_ledger", limit=6))
    add("merchant_lifecycle_matters", ["metric:merchant_lifecycle:summary", *_first_metric_row_ids(evidence_map, "merchant_lifecycle", limit=6)])
    add("trip_event_exclusion", _event_cluster_anchor_ids(evidence_map))
    add("avoidable_leakage_first", ["metric:avoidable_leakage:summary", *_first_metric_row_ids(evidence_map, "avoidable_leakage", limit=2)])
    add("fees_inspection_first", _first_metric_row_ids(evidence_map, "avoidable_leakage", limit=2))
    return [by_id.get(str(thesis.get("thesis_id") or ""), thesis) for thesis in theses]


def _compose_structured_advisor_memo(
    base_memo: str,
    ordered_theses: list[dict[str, Any]],
    action_plan: list[dict[str, Any]],
    evidence_map: dict[str, dict[str, Any]],
) -> str:
    sections = [
        "# Mira's Financial Read",
        _structured_bottom_line(evidence_map),
        _structured_normal_month(evidence_map),
        _structured_noise_section(evidence_map),
        _structured_money_section(evidence_map),
        _structured_action_section(action_plan),
        _structured_do_not_overcorrect(evidence_map),
        _structured_verify_section(evidence_map),
    ]
    return "\n\n".join(section for section in sections if section).strip()


def _structured_bottom_line(evidence_map: dict[str, dict[str, Any]]) -> str:
    operating = _metric_summary(evidence_map, "monthly_operating_statement")
    runway = _metric_summary(evidence_map, "cash_runway")
    position = _metric_summary(evidence_map, "cash_vs_liability_position")
    leakage = _metric_summary(evidence_map, "avoidable_leakage")
    income = _metric_summary(evidence_map, "income_source_continuity")
    cash = _money(position.get("cash_like_balance"))
    liabilities = _money(position.get("liability_total"))
    runway_days = _number(runway.get("cash_runway_days"))
    burn = _money(operating.get("reconciled_operating_burn"))
    capacity = _money(operating.get("capacity_before_configured_goals"))
    fee_total = _money(leakage.get("fee_or_interest_total"))
    lines = [
        f"## {SECTION_THE_READ}",
        f"Cash is doing its job. With {cash} in cash-like balances, {liabilities} in liabilities, and {runway_days} days of runway, I would not make this a cash-panic story.",
        f"The better read is capacity and control: reconciled operating burn is {burn}, leaving {capacity} before configured goals.",
        f"The first dollar I would chase is not a lifestyle cut. Fees or interest total {fee_total}, so I would clean up that friction before asking you to shrink broad categories.",
    ]
    status = income.get("status")
    if status:
        lines.append("The main caveat is income continuity: I would verify the current source labels before trusting the forward plan.")
    return "\n\n".join(lines)


def _structured_normal_month(evidence_map: dict[str, dict[str, Any]]) -> str:
    operating = _metric_summary(evidence_map, "monthly_operating_statement")
    money_flow = _metric_summary(evidence_map, "money_flow_baseline")
    floor = _metric_summary(evidence_map, "floor_burn")
    recurring = _metric_summary(evidence_map, "recurring_obligation_calendar")
    rows = [
        ("Average monthly income", _money(operating.get("avg_monthly_income")), "The income base I would plan around."),
        ("Normal spending", _money(operating.get("event_adjusted_normal_spend") or money_flow.get("avg_monthly_spend_after_event_exclusions")), "Trip/event noise is pulled out, so this does not punish a one-off month."),
        ("Fixed floor already visible", _money(operating.get("fixed_floor_visible_in_spend")), "Obligations already sitting inside normal spend."),
        ("Fixed floor gap", _money(operating.get("fixed_floor_gap_not_visible_in_spend")), "The extra floor Mira adds back so burn is not understated."),
        ("Reconciled operating burn", _money(operating.get("reconciled_operating_burn")), "The real monthly hurdle after the fixed monthly floor is respected."),
        ("Visible flexible spend", _money(money_flow.get("flexible_monthly_estimate")), "The part I would tune after the floor is safe."),
        ("Recurring commitments", _money(recurring.get("total_monthly") or floor.get("recurring_monthly")), "Worth a renewal and duplicate check, not panic."),
        ("Room before configured goals", _money(operating.get("capacity_before_configured_goals")), "Planning room, not a finished goal plan."),
    ]
    return "\n".join(
        [
            f"## {SECTION_NORMAL_MONTH}",
            "This is the month I would actually plan around: ordinary income, the fixed monthly floor, normal flexible spend, and what is left before named goals.",
            _markdown_table(("Line", "Amount", "Mira's read"), rows),
            "No active goals are configured, so this is planning capacity before explicit goal targets rather than proof that a specific goal is funded.",
        ]
    )


def _structured_noise_section(evidence_map: dict[str, dict[str, Any]]) -> str:
    cluster_ids = _event_cluster_anchor_ids(evidence_map)
    rows = []
    for evidence_id in cluster_ids[:2]:
        values = _evidence_values(evidence_map, evidence_id)
        start = values.get("travel_window_start") or values.get("activity_window_start") or values.get("window_start")
        end = values.get("activity_window_end") or values.get("travel_window_end") or values.get("window_end")
        amount = _money(values.get("estimated_event_total"))
        merchants = _join_names(values.get("merchant_examples") or [], limit=4)
        rows.append(("Travel/event cluster", f"{start} to {end}", amount, merchants, "Real money, but not proof the normal lifestyle floor changed."))
    if not rows:
        return ""
    return "\n".join(
        [
            f"## {SECTION_NOISE}",
            "This is the money I would separate before judging your habits. It matters, but it should not distort the normal baseline unless it repeats.",
            _markdown_table(("Signal", "Window", "Amount", "Examples", "Mira's read"), rows),
        ]
    )


def _structured_money_section(evidence_map: dict[str, dict[str, Any]]) -> str:
    category_rows = []
    for values in _metric_rows(evidence_map, "category_advisor_ledger", limit=6):
        merchants = _join_top_merchants(values.get("top_merchants") or [], limit=3)
        delta = _money(values.get("recent_vs_prior_monthly_delta"))
        category_rows.append(
            (
                values.get("category") or "Category",
                _money(values.get("monthly_average")),
                delta,
                _money(values.get("avg_ticket_size")),
                merchants,
                _role_label(values.get("spend_role")),
            )
        )
    merchant_rows = []
    for values in _metric_rows(evidence_map, "merchant_lifecycle", limit=6):
        merchant_rows.append(
            (
                _lifecycle_label(values.get("lifecycle_type")),
                values.get("merchant") or "Merchant",
                _money(values.get("total_spend")),
                values.get("category") or "",
                values.get("last_seen") or "",
            )
        )
    transfer = _metric_summary(evidence_map, "external_transfer_pressure")
    lines = [
        f"## {SECTION_MONEY_MAP}",
        "Before I reach for cuts, I want the map: what is structural, what is flexible, what was event-driven, what is private rhythm, and what is just a vendor worth reviewing.",
    ]
    if category_rows:
        lines.append(_markdown_table(("Category", "Monthly avg", "Recent vs prior", "Avg ticket", "Main drivers", "Role"), category_rows))
    if merchant_rows:
        lines.append("Merchant behavior changes the advice: a new spike, a long-running vendor, and a messy label are three different problems.")
        lines.append(_markdown_table(("Type", "Merchant", "Total", "Category", "Last seen"), merchant_rows))
    if transfer.get("net_external_transfer_outflow") is not None:
        lines.append(
            f"I would label external transfers before judging them as lifestyle spending. Incoming external transfers are {_money(transfer.get('incoming_external_transfer_total'))}, outgoing external transfers are {_money(transfer.get('outgoing_external_transfer_total'))}, and net external transfer outflow is {_money(transfer.get('net_external_transfer_outflow'))}; that movement needs a purpose label before it becomes a spending, goal, support, debt, or investing conclusion."
        )
    return "\n\n".join(lines)


def _structured_action_section(action_plan: list[dict[str, Any]]) -> str:
    if not action_plan:
        return ""
    labels = ("First", "Second", "Third", "Fourth", "Fifth", "Sixth")
    lines = [f"## {SECTION_ACTIONS}", "Here is the order I would use, because low-regret fixes should come before painful cuts:"]
    for index, action in enumerate(action_plan[:6]):
        label = labels[min(index, len(labels) - 1)]
        title = _context_safe_text(action.get("title"))
        why = _structured_action_why(action)
        next_step = _context_safe_text(action.get("action"))
        tradeoff = _context_safe_text(action.get("tradeoff"))
        line = f"- **{label}: {title}.** {why} Next: {next_step}"
        if tradeoff:
            line += f" Tradeoff: {tradeoff}"
        lines.append(line)
    return "\n".join(lines)


def _structured_action_why(action: dict[str, Any]) -> str:
    why = _context_safe_text(action.get("why"))
    if why and why.lower() != "this thesis is supported.":
        return why
    thesis_id = str(action.get("thesis_id") or "")
    fallbacks = {
        "avoidable_leakage_first": "This is the cheapest first fix: preventable fees or interest should be handled before lifestyle cuts.",
        "income_continuity_uncertain": "This is the assumption underneath the plan: cash is strong, but forward income still needs confirmation.",
        "goal_capacity_reality": "Capacity only becomes useful after it is assigned to named goals.",
        "fixed_floor_matters": "The floor tells us what has to be protected before flexible spend gets judged.",
        "geico_vendor_review": "This is a vendor review, not a daily behavior problem.",
        "amazon_tune_up": "This is a small-friction tune-up, not the main financial thesis.",
        "alcohol_soft_ceiling": "This is a rhythm-setting issue, not a morality read.",
        "protect_vaping_pause": "This looks like a reduction to protect before looking for new cuts.",
    }
    return fallbacks.get(thesis_id, why)


def _structured_do_not_overcorrect(evidence_map: dict[str, dict[str, Any]]) -> str:
    runway = _metric_summary(evidence_map, "cash_runway")
    position = _metric_summary(evidence_map, "cash_vs_liability_position")
    scenario_id = _first_metric_row_id(evidence_map, "savings_scenarios")
    scenario = _evidence_values(evidence_map, scenario_id) if scenario_id else {}
    lines = [
        f"## {SECTION_DO_NOT_OVERREACT}",
        f"- Do not manufacture cash anxiety. Cash-like balance is {_money(position.get('cash_like_balance'))}, cash runway is {_number(runway.get('cash_runway_days'))} days, and liabilities are {_money(position.get('liability_total'))}.",
        "- Do not let a trip/event cluster become a permanent lifestyle verdict unless you confirm it is repeating.",
        "- Private vaping spend is lower than prior peaks; protect the lower rhythm if sync is complete. The point is preservation, not judgment.",
        "- Alcohol belongs in soft-ceiling territory: set a rhythm if it helps, but do not turn it into a morality read.",
    ]
    if scenario:
        lines.append(f"- Savings scenarios are optional planning sensitivities, not commands: {scenario.get('subject') or 'the top scenario'} has a planning sensitivity of {_money(scenario.get('monthly_effect'))} per month.")
    return "\n".join(lines)


def _structured_verify_section(evidence_map: dict[str, dict[str, Any]]) -> str:
    period = _metric_summary(evidence_map, "advisor_period_reliability")
    data = _metric_summary(evidence_map, "advisor_data_quality_profile")
    recurring = _metric_summary(evidence_map, "recurring_obligation_calendar")
    first_income = period.get("first_income_date")
    latest = "the latest month is partial" if period.get("current_month_partial") else "the latest month appears complete"
    lines = [
        f"## {SECTION_VERIFY}",
        f"- I would anchor trends to the reliable analysis period: it starts at {period.get('analysis_start')} after the first observed income row on {first_income}, and {latest}.",
        "- I would compare recent complete-month cash flow with the trailing view before calling compression structural.",
        "- I would verify income continuity and source labels before trusting forward assumptions.",
        f"- Precision has limits: { _number(data.get('visible_transaction_count')) } visible transactions, { _number(data.get('low_confidence_spend_count')) } low-confidence spending rows, { _number(data.get('recurring_duplicate_row_count')) } recurring duplicate row, and { _number(data.get('investment_holding_count')) } investment holdings are visible in this read.",
        f"- Recurring duplicate candidates are {_number(recurring.get('duplicate_row_count'))}; review duplicates before treating the floor as final.",
        "- Missing goals, budgets, labels, or sync data can change the read, so the recommendation should stay conditional.",
    ]
    return "\n".join(lines)


def build_advisor_read_cards(
    memo: dict[str, Any],
    *,
    evidence_map: dict[str, dict[str, Any]] | None = None,
    delta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build first-class UI cards from validated advisor memo inputs."""
    if not isinstance(memo, dict):
        return []
    existing = memo.get("cards") if isinstance(memo.get("cards"), list) else []
    if existing:
        return _advisor_cards_with_delta([_normalize_advisor_card(card) for card in existing], delta)

    markdown = str(memo.get("memo_markdown") or "")
    actions = memo.get("action_plan") if isinstance(memo.get("action_plan"), list) else build_advisor_ranked_actions(memo)
    normal_rows = _advisor_normal_month_card_rows(evidence_map, markdown)
    money_rows = _advisor_money_map_card_rows(evidence_map, markdown)
    event_rows = _advisor_event_noise_card_rows(evidence_map)
    lever_rows = _advisor_soft_lever_card_rows(evidence_map)
    first_action = _advisor_card_action(actions[0] if actions else None)
    risk_action = _advisor_risk_action(actions, memo)

    cards: list[dict[str, Any]] = [
        {
            "id": "normal_month",
            "title": "Month to plan around",
            "kicker": "Baseline",
            "icon": "calendar_month",
            "summary": (
                "The baseline I would actually use: income, fixed floor, normal flexible spend, recurring commitments, and unassigned room."
                if normal_rows
                else "The stored read has a validated monthly baseline."
            ),
            "rows": normal_rows[:7],
            "action_label": "Walk me through it",
            "followup_type": "normal_month",
            "question": "Walk me through my normal month from Mira's read.",
        },
        {
            "id": "money_map",
            "title": "Money map",
            "kicker": "Money map",
            "icon": "route",
            "summary": (
                "Before cutting anything, separate ordinary living from event noise, private rhythms, and vendors worth reviewing."
                if money_rows
                else _advisor_section_summary(
                    markdown,
                    SECTION_MONEY_MAP,
                    "Where The Money Is Going",
                    "Where Your Money Actually Goes",
                    "Where Money Is Going",
                )
                or "The stored read maps spending by category, merchant, and controllability."
            ),
            "rows": money_rows[:5],
            "action_label": "Find softer cuts",
            "followup_type": "money_map",
            "question": "Walk me through the money map from Mira's read, and what can be reduced without overreacting?",
        },
    ]
    if event_rows:
        cards.append(
            {
                "id": "event_noise",
                "title": "Not the verdict",
                "kicker": "Noise",
                "icon": "filter_alt",
                "summary": "These clusters are real money, but not proof your normal life got more expensive.",
                "rows": event_rows[:3],
                "action_label": "What not to overreact to?",
                "followup_type": "event_noise",
                "question": "What should I not overreact to from Mira's read?",
            }
        )
    if lever_rows:
        cards.append(
            {
                "id": "soft_levers",
                "title": "Low-pain fixes",
                "kicker": "Levers",
                "icon": "tune",
                "summary": "Start where effort is low and regret is low: fees, payment timing, vendors, and small-purchase friction.",
                "rows": lever_rows[:5],
                "action_label": "Find softer cuts",
                "followup_type": "levers",
                "question": "What can I reduce a little without pain from Mira's read?",
            }
        )
    if first_action:
        cards.append(
            {
                "id": "first_move",
                "title": "Do this first",
                "kicker": "Action",
                "icon": "low_priority",
                "summary": first_action["title"],
                "detail": first_action["action"],
                "tradeoff": first_action["tradeoff"],
                "rows": [],
                "action_label": "Why this first?",
                "followup_type": "first_move",
                "question": "What should I do first from Mira's read, and why is it first?",
            }
        )
    if risk_action:
        cards.append(
            {
                "id": "biggest_risk",
                "title": "Assumption to verify",
                "kicker": "Risk",
                "icon": "flag",
                "summary": risk_action["why"] or risk_action["title"],
                "detail": risk_action["action"],
                "rows": [],
                "action_label": "Explain the risk",
                "followup_type": "risk",
                "question": "What is the biggest risk to my goals from Mira's read?",
            }
        )
    cards.append(_advisor_delta_card(delta))
    return [_normalize_advisor_card(card) for card in cards if card.get("summary") or card.get("rows")]


def _advisor_normal_month_card_rows(evidence_map: dict[str, dict[str, Any]] | None, markdown: str) -> list[dict[str, str]]:
    if evidence_map:
        operating = _metric_summary(evidence_map, "monthly_operating_statement")
        money_flow = _metric_summary(evidence_map, "money_flow_baseline")
        recurring = _metric_summary(evidence_map, "recurring_obligation_calendar")
        rows = [
            ("Average monthly income", _money(operating.get("avg_monthly_income")), "The income base I would plan around."),
            ("Normal spending", _money(operating.get("event_adjusted_normal_spend") or money_flow.get("avg_monthly_spend_after_event_exclusions")), "Event-adjusted so trip noise does not become lifestyle drift."),
            ("Reconciled operating burn", _money(operating.get("reconciled_operating_burn")), "The monthly hurdle after the fixed floor is respected."),
            ("Fixed monthly floor", _money(operating.get("fixed_floor_monthly") or money_flow.get("fixed_floor_monthly")), "Cover this before judging flexible spend."),
            ("Visible flexible spend", _money(money_flow.get("flexible_monthly_estimate")), "Tune this after the floor is safe."),
            ("Recurring commitments", _money(recurring.get("total_monthly")), "Review renewals and duplicates before panic."),
            ("Room before configured goals", _money(operating.get("capacity_before_configured_goals")), "Planning room, not a finished goal plan."),
        ]
        return [_advisor_row(label, value, detail) for label, value, detail in rows if value and value != "$0.00"]
    section = _advisor_section_body(markdown, SECTION_NORMAL_MONTH, "The Month I'd Underwrite", "Your Normal Month", "Normal Month")
    return _advisor_markdown_table_rows(section, max_rows=7)


def _advisor_money_map_card_rows(evidence_map: dict[str, dict[str, Any]] | None, markdown: str) -> list[dict[str, str]]:
    if evidence_map:
        rows = []
        seen: set[str] = set()

        def add(values: dict[str, Any]) -> None:
            category = _advisor_card_text(values.get("category") or "Category", max_chars=80)
            if not category or category.lower() in seen:
                return
            amount = _money(values.get("monthly_average"))
            if amount == "$0.00":
                return
            seen.add(category.lower())
            rows.append(_advisor_row(category, amount, _advisor_money_map_detail(values)))

        for values in _metric_rows(evidence_map, "money_flow_baseline", limit=4):
            add(values)
        ledger_rows = _metric_rows(evidence_map, "category_advisor_ledger", limit=18)
        for role in ("event_or_irregular", "private_discretionary", "recurring_or_vendor_review"):
            role_rows = [row for row in ledger_rows if row.get("spend_role") == role]
            if role_rows:
                add(max(role_rows, key=lambda row: _float_value(row.get("monthly_average"))))
        for values in sorted(ledger_rows, key=lambda row: _float_value(row.get("monthly_average")), reverse=True):
            if len(rows) >= 5:
                break
            add(values)
        return rows
    section = _advisor_section_body(
        markdown,
        SECTION_MONEY_MAP,
        "Where The Money Is Going",
        "Where Your Money Actually Goes",
        "Where Money Is Going",
        "Where Money Goes",
    )
    return _advisor_markdown_table_rows(section, max_rows=5)


def _advisor_money_map_detail(values: dict[str, Any]) -> str:
    role = _role_label(values.get("spend_role"))
    merchants = _join_top_merchants(values.get("top_merchants") or [], limit=2)
    delta = _advisor_delta_phrase(values.get("recent_vs_prior_monthly_delta"))
    pieces = [piece for piece in (role, delta, merchants if merchants != "-" else "") if piece]
    return " - ".join(pieces[:3])


def _advisor_delta_phrase(value: Any) -> str:
    amount = _float_value(value)
    if abs(amount) < 0.01:
        return ""
    if amount > 0:
        return f"recent higher by {_money(amount)}"
    return f"recent lower by {_money(abs(amount))}"


def _advisor_event_noise_card_rows(evidence_map: dict[str, dict[str, Any]] | None) -> list[dict[str, str]]:
    if not evidence_map:
        return []
    rows = []
    for values in _metric_rows(evidence_map, "spending_event_clusters", limit=3):
        amount = _money(values.get("estimated_event_total"))
        if amount == "$0.00":
            continue
        start = values.get("travel_window_start") or values.get("activity_window_start") or values.get("window_start")
        end = values.get("activity_window_end") or values.get("travel_window_end") or values.get("window_end")
        window = f"{start} to {end}" if start and end else "Event cluster"
        merchants = _join_names(values.get("merchant_examples") or [], limit=3)
        detail = "Exclude from normal baseline"
        if merchants != "-":
            detail = f"{detail} - {merchants}"
        rows.append(_advisor_row(window, amount, detail))
    return rows


def _advisor_soft_lever_card_rows(evidence_map: dict[str, dict[str, Any]] | None) -> list[dict[str, str]]:
    if not evidence_map:
        return []
    rows = []
    seen: set[str] = set()

    def add(subject: Any, amount: Any, detail: Any) -> None:
        label = _advisor_card_text(subject or "Review candidate", max_chars=80)
        if not label or label.lower() in seen:
            return
        value = _money(amount)
        if value == "$0.00":
            return
        seen.add(label.lower())
        rows.append(_advisor_row(label, value, detail))

    for values in _metric_rows(evidence_map, "avoidable_leakage", limit=3):
        detail = values.get("action") or _leakage_label(values.get("leakage_type"))
        add(values.get("subject") or _leakage_label(values.get("leakage_type")), values.get("measured_amount"), detail)
    for values in _metric_rows(evidence_map, "realistic_trim_levers", limit=6):
        detail = values.get("action") or values.get("tradeoff") or _lever_label(values.get("lever_type"))
        add(values.get("subject") or _lever_label(values.get("lever_type")), values.get("measured_amount"), detail)
    return rows


def _leakage_label(value: Any) -> str:
    mapping = {
        "fee_or_interest": "Fee or interest review",
        "recurring_duplicate_record": "Duplicate recurring review",
    }
    text = str(value or "Leakage review")
    return mapping.get(text, text.replace("_", " ").title())


def _lever_label(value: Any) -> str:
    mapping = {
        "soft_ceiling": "Soft ceiling",
        "protect_pause": "Protect the pause",
        "inspect_fee": "Fee inspection",
        "vendor_review": "Vendor review",
    }
    text = str(value or "Review lever")
    return mapping.get(text, text.replace("_", " ").title())


def _advisor_row(label: Any, value: Any, detail: Any = "") -> dict[str, str]:
    return {
        "label": _advisor_card_text(label, max_chars=80),
        "value": _advisor_card_text(value, max_chars=80),
        "detail": _advisor_card_text(detail, max_chars=160),
    }


def _advisor_card_action(action: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(action, dict):
        return None
    title = _advisor_card_text(action.get("title"), max_chars=140)
    next_step = _advisor_card_text(action.get("action"), max_chars=260)
    if not title and not next_step:
        return None
    return {
        "title": title,
        "why": _advisor_card_text(action.get("why"), max_chars=240),
        "action": next_step,
        "tradeoff": _advisor_card_text(action.get("tradeoff"), max_chars=220),
    }


def _advisor_risk_action(actions: list[dict[str, Any]], memo: dict[str, Any]) -> dict[str, str] | None:
    terms = ("income continuity", "income source", "fixed floor", "fixed monthly floor", "goal capacity", "planning capacity")
    for action in actions:
        text = " ".join(str(action.get(key) or "") for key in ("title", "why", "action")).lower()
        if any(term in text for term in terms):
            return _advisor_card_action(action)
    for thesis in memo.get("theses") or []:
        if not isinstance(thesis, dict):
            continue
        text = " ".join(str(thesis.get(key) or "") for key in ("summary", "paragraph")).lower()
        if any(term in text for term in terms):
            return {
                "title": _advisor_card_text(thesis.get("summary"), max_chars=140),
                "why": _advisor_card_text(thesis.get("summary") or thesis.get("paragraph"), max_chars=300),
                "action": _advisor_card_text(thesis.get("caveat"), max_chars=260),
                "tradeoff": "",
            }
    return None


def _advisor_delta_card(delta: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(delta, dict) and delta:
        rows = []
        for label, values in (
            ("Months", delta.get("touched_months") or []),
            ("Sections", delta.get("changed_sections") or []),
            ("Categories", delta.get("categories") or []),
            ("Merchants", delta.get("merchants") or []),
        ):
            joined = ", ".join(str(item) for item in values if str(item).strip())
            if joined:
                rows.append(_advisor_row(label, joined))
        return {
            "id": "what_changed",
            "title": "What changed",
            "kicker": "Freshness",
            "icon": "published_with_changes",
            "summary": _advisor_card_text(delta.get("headline") or "The stored facts changed since this read.", max_chars=240),
            "detail": _advisor_card_text(delta.get("action") or "", max_chars=260),
            "rows": rows[:4],
            "action_label": "Show the update",
            "followup_type": "changes",
            "question": "What changed since Mira's read?",
        }
    return {
        "id": "what_changed",
        "title": "What changed",
        "kicker": "Freshness",
        "icon": "check_circle",
        "summary": "No stored fact changes since this read.",
        "detail": "",
        "rows": [],
        "action_label": "Check freshness",
        "followup_type": "changes",
        "question": "What changed since Mira's read?",
    }


def _advisor_cards_with_delta(cards: list[dict[str, Any]], delta: dict[str, Any] | None) -> list[dict[str, Any]]:
    normalized = [_normalize_advisor_card(card) for card in cards if isinstance(card, dict)]
    replacement = _normalize_advisor_card(_advisor_delta_card(delta))
    out = []
    replaced = False
    for card in normalized:
        if card.get("id") == "what_changed":
            out.append(replacement)
            replaced = True
        else:
            out.append(card)
    if not replaced:
        out.append(replacement)
    return [card for card in out if card.get("summary") or card.get("rows")]


def _normalize_advisor_card(card: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for row in card.get("rows") or []:
        if not isinstance(row, dict):
            continue
        clean = _advisor_row(row.get("label"), row.get("value"), row.get("detail"))
        if clean["label"]:
            rows.append(clean)
    card_id = _advisor_card_token(card.get("id"), max_chars=48)
    title = _advisor_card_text(card.get("title"), max_chars=80)
    if card_id == "money_map" and title in {"Where the money goes", "Where money goes", "Where Money Goes"}:
        title = "Money map"
    question = _advisor_card_text(card.get("question"), max_chars=180)
    if card_id == "money_map" and question.startswith("Where is my money going from Mira's read"):
        question = "Walk me through the money map from Mira's read, and what can be reduced without overreacting?"
    return {
        "id": card_id,
        "title": title,
        "kicker": _advisor_card_text(card.get("kicker"), max_chars=40),
        "icon": _advisor_card_token(card.get("icon"), max_chars=40),
        "summary": _advisor_card_text(card.get("summary"), max_chars=300),
        "detail": _advisor_card_text(card.get("detail"), max_chars=300),
        "tradeoff": _advisor_card_text(card.get("tradeoff"), max_chars=240),
        "rows": rows[:8],
        "action_label": _advisor_card_text(card.get("action_label"), max_chars=80),
        "followup_type": _advisor_card_token(card.get("followup_type"), max_chars=40),
        "question": question,
    }


def _advisor_card_token(value: Any, *, max_chars: int) -> str:
    text = _context_safe_text(value)
    text = re.sub(r"[^A-Za-z0-9_-]+", "", text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _advisor_card_text(value: Any, *, max_chars: int) -> str:
    text = _context_safe_text(value)
    text = re.sub(r"\brun_sql\b", "finance query", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsafe_finance_query\b", "finance evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSQL\b", "finance query", text)
    text = text.replace("|", " ")
    text = re.sub(r"-{3,}", " ", text)
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0].strip() or text[:max_chars].strip()


def _advisor_section_body(markdown: str, *names: str) -> str:
    wanted = {name.lower() for name in names}
    current = ""
    sections: dict[str, list[str]] = {}
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            current = _advisor_card_text(line[3:], max_chars=100).lower()
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(raw_line)
    for name in wanted:
        if name in sections:
            return "\n".join(sections[name]).strip()
    return ""


def _advisor_section_summary(markdown: str, *names: str, max_chars: int = 260) -> str:
    section = _advisor_section_body(markdown, *names)
    lines = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|"):
            continue
        lines.append(line)
        if len(" ".join(lines)) >= max_chars:
            break
    return _advisor_card_text(" ".join(lines), max_chars=max_chars)


def _advisor_markdown_table_rows(section: str, *, max_rows: int = 5) -> list[dict[str, str]]:
    table_lines = [line.strip() for line in str(section or "").splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 3:
        return []
    header = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(set(cell.replace(" ", "")) <= {"-", ":"} for cell in cells):
            continue
        if len(cells) < len(header):
            cells.extend([""] * (len(header) - len(cells)))
        label = _advisor_card_text(cells[0], max_chars=80)
        value = _advisor_card_text(cells[1], max_chars=80) if len(cells) > 1 else ""
        detail = " · ".join(_advisor_card_text(cell, max_chars=120) for cell in cells[2:4] if _advisor_card_text(cell, max_chars=120))
        if label:
            rows.append(_advisor_row(label, value, detail))
        if len(rows) >= max_rows:
            break
    return rows


def _metric_summary(evidence_map: dict[str, dict[str, Any]], metric: str) -> dict[str, Any]:
    return _evidence_values(evidence_map, f"metric:{metric}:summary").get("summary_numbers") or {}


def _metric_rows(evidence_map: dict[str, dict[str, Any]], metric: str, *, limit: int) -> list[dict[str, Any]]:
    return [_evidence_values(evidence_map, evidence_id) for evidence_id in _first_metric_row_ids(evidence_map, metric, limit=limit)]


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> str:
    safe_headers = [_md_cell(header) for header in headers]
    lines = [
        "| " + " | ".join(safe_headers) + " |",
        "| " + " | ".join("---" for _ in safe_headers) + " |",
    ]
    for row in rows:
        cells = [_md_cell(value) for value in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    text = _context_safe_text(value)
    return text.replace("|", "/") if text else "-"


def _join_top_merchants(values: list[Any], *, limit: int) -> str:
    names = []
    for item in values[:limit]:
        if isinstance(item, dict):
            merchant = item.get("merchant")
            total = item.get("total")
            if merchant and total is not None:
                names.append(f"{merchant} {_money(total)}")
            elif merchant:
                names.append(str(merchant))
    return ", ".join(names) if names else "-"


def _join_names(values: list[Any], *, limit: int) -> str:
    names = [str(value) for value in values[:limit] if str(value or "").strip()]
    return ", ".join(names) if names else "-"


def _role_label(value: Any) -> str:
    mapping = {
        "event_or_irregular": "Event or irregular",
        "flexible_living": "Flexible living",
        "structural_floor": "Structural floor",
        "recurring_or_vendor_review": "Recurring or vendor review",
        "private_discretionary": "Private discretionary",
        "tax_or_irregular": "Tax or irregular",
    }
    text = str(value or "Review")
    return mapping.get(text, text.replace("_", " ").title())


def _lifecycle_label(value: Any) -> str:
    mapping = {
        "top_merchant": "Top merchant",
        "new_since_recent_window": "New in recent window",
        "dormant_since_recent_window": "Dormant since recent window",
        "split_label_group": "Split-label group",
    }
    text = str(value or "Merchant")
    return mapping.get(text, text.replace("_", " ").title())


def build_advisor_ranked_actions(memo: dict[str, Any], *, max_items: int = 6) -> list[dict[str, Any]]:
    """Build a deterministic advisor action plan from validated thesis packets."""
    if not isinstance(memo, dict):
        return []
    theses = [item for item in memo.get("theses") or [] if isinstance(item, dict)]
    by_id = {str(item.get("thesis_id") or ""): item for item in theses}
    actions: list[dict[str, Any]] = []

    def add(
        thesis_id: str,
        *,
        title: str,
        action: str,
        tradeoff: str,
        pain: str = "low",
    ) -> None:
        thesis = by_id.get(thesis_id)
        if not thesis:
            return
        why = _context_safe_text(thesis.get("summary") or thesis.get("paragraph"))
        if not why:
            return
        actions.append(
            {
                "rank": len(actions) + 1,
                "thesis_id": thesis_id,
                "title": title,
                "why": why[:260],
                "action": action,
                "tradeoff": tradeoff,
                "pain": pain,
            }
        )

    add(
        "avoidable_leakage_first",
        title="Fix preventable leakage first",
        action="Inspect fee and interest rows, then set the payment timing or due-date guardrail before broad category cuts or cutting intentional spending.",
        tradeoff="These come before broad category cuts; do not turn a fee cluster into a broad lifestyle verdict.",
    )
    add(
        "income_continuity_uncertain",
        title="Verify income continuity before relying on the plan",
        action="Confirm the current income source labels and whether the recent income stream should be treated as stable.",
        tradeoff="Do not treat liquidity as the whole answer when forward income is still uncertain.",
        pain="medium",
    )
    add(
        "goal_capacity_reality",
        title="Turn planning capacity into named goals",
        action="Assign the available monthly capacity to explicit goals before deciding whether to invest, pay debt faster, or loosen spending.",
        tradeoff="Do not call capacity a funded plan until the goal targets exist.",
    )
    add(
        "fixed_floor_matters",
        title="Anchor the month to the fixed floor",
        action="Verify the fixed commitments that make up the floor, then judge flexible spending only after that floor is covered.",
        tradeoff="Do not compare every month against raw spending without separating structural obligations.",
    )
    add(
        "geico_vendor_review",
        title="Review GEICO like a vendor, not a lifestyle problem",
        action="Quote or compare coverage like-for-like before changing anything.",
        tradeoff="Do not reduce coverage just to make the number smaller.",
    )
    add(
        "amazon_tune_up",
        title="Consolidate Amazon-style small purchases",
        action="Batch or review small purchases before touching categories that affect daily quality of life.",
        tradeoff="Do not treat small-purchase cleanup as the main financial thesis.",
    )
    add(
        "alcohol_soft_ceiling",
        title="Use a soft ceiling for private rhythms",
        action="Set a gentle ceiling or check-in rhythm only after the lower-pain operational fixes are handled.",
        tradeoff="Do not moralize the category or make it the first lever.",
    )
    add(
        "protect_vaping_pause",
        title="Protect private-spend reductions already working",
        action="Keep the lower rhythm intact if sync confirms it before looking for new cuts.",
        tradeoff="Do not overcorrect if the improvement is already happening.",
    )
    return actions[: max(1, min(int(max_items or 6), 8))]


def _attach_ranked_action_plan_section(memo: str, action_plan: list[dict[str, Any]]) -> str:
    text = str(memo or "").strip()
    section = _ranked_action_plan_markdown(action_plan)
    body = _ranked_action_plan_body(action_plan)
    if not text or not section:
        return text
    if "## Ranked Action Plan" in text:
        start = text.find("## Ranked Action Plan")
        after_start = start + len("## Ranked Action Plan")
        match = re.search(r"\n##\s+", text[after_start:])
        end = after_start + match.start() if match else len(text)
        return f"{text[:start].rstrip()}\n\n{section}\n\n{text[end:].lstrip()}".strip()
    action_headings = (f"## {SECTION_ACTIONS}", "## Best Next Moves")
    existing_heading = next((heading for heading in action_headings if heading in text), "")
    if existing_heading and body:
        start = text.find(existing_heading)
        after_start = start + len(existing_heading)
        match = re.search(r"\n##\s+", text[after_start:])
        end = after_start + match.start() if match else len(text)
        current_section = text[start:end].rstrip()
        if "low-regret fixes" in current_section.lower() or "ranked action plan" in current_section.lower():
            return text
        return f"{text[:end].rstrip()}\n\n{body}\n\n{text[end:].lstrip()}".strip()
    return f"{text}\n\n{section}".strip()


def _ranked_action_plan_markdown(action_plan: list[dict[str, Any]]) -> str:
    body = _ranked_action_plan_body(action_plan)
    return f"## {SECTION_ACTIONS}\n{body}".strip() if body else ""


def _ranked_action_plan_body(action_plan: list[dict[str, Any]]) -> str:
    lines = ["Low-regret fixes first, then strategic assumptions:"]
    labels = ("First", "Second", "Third", "Fourth", "Fifth", "Sixth")
    for action in action_plan[:6]:
        title = _context_safe_text(action.get("title"))
        why = _structured_action_why(action)
        next_step = _context_safe_text(action.get("action"))
        tradeoff = _context_safe_text(action.get("tradeoff"))
        if not title or not why or not next_step:
            continue
        rank_index = max(0, min(int(action.get("rank") or len(lines)) - 1, len(labels) - 1))
        line = f"{labels[rank_index]}: {title}. {why} Next: {next_step}"
        if tradeoff:
            line += f" Tradeoff: {tradeoff}"
        lines.append(line)
    return "\n".join(lines).strip() if len(lines) > 1 else ""


def _repair_final_memo_theme_language(memo: str, candidate_theses: list[dict[str, Any]], thesis_ids: list[str]) -> str:
    missing = _missing_memo_theme_markers(memo, thesis_ids)
    for thesis in candidate_theses:
        if not isinstance(thesis, dict) or thesis.get("thesis_id") != "goal_capacity_reality":
            continue
        candidate_text = " ".join(str(thesis.get(field) or "") for field in ("summary", "paragraph", "caveat"))
        if _text_has_no_active_goal_status(candidate_text) and not _text_has_no_active_goal_status(memo):
            missing.append("goal_capacity_reality")
    if not missing:
        return memo
    by_id = {str(thesis.get("thesis_id") or ""): thesis for thesis in candidate_theses if isinstance(thesis, dict)}
    repaired = memo
    for thesis_id in missing:
        thesis = by_id.get(thesis_id)
        if not thesis:
            continue
        addition = _theme_repair_addition(repaired, thesis_id, thesis)
        if not addition:
            continue
        if _normalized_contains(repaired, addition):
            continue
        repaired = _append_memo_paragraph(repaired, _section_for_thesis(thesis_id), addition)
    return clean_memo_text(repaired)


def _theme_repair_addition(memo: str, thesis_id: str, thesis: dict[str, Any]) -> str:
    paragraph = clean_memo_text(thesis.get("paragraph") or thesis.get("summary"))
    if thesis_id == "goal_capacity_reality" and _text_has_goal_capacity(memo) and _text_has_no_active_goal_status(paragraph):
        return "No active goals are configured, so this is planning capacity before explicit goal targets rather than proof that a specific goal is funded."
    if thesis_id == "protect_vaping_pause" and "vaping" in str(memo or "").lower():
        return "The point is the lower rhythm versus prior peaks, not a judgment about the category."
    return paragraph


def _repair_final_memo_anchor_language(memo: str, candidate_theses: list[dict[str, Any]], thesis_ids: list[str]) -> str:
    """Preserve exact advisor anchor facts that the final LLM may compress away."""
    by_id = {str(thesis.get("thesis_id") or ""): thesis for thesis in candidate_theses if isinstance(thesis, dict)}
    repaired = memo
    for thesis_id in thesis_ids:
        if thesis_id not in _ANCHOR_MERGE_THESES:
            continue
        thesis = by_id.get(thesis_id)
        if not thesis:
            continue
        paragraph = clean_memo_text(thesis.get("paragraph") or thesis.get("summary"))
        if not paragraph or not _anchor_fact_missing(repaired, thesis_id, paragraph):
            continue
        addition = _anchor_repair_addition(thesis_id, paragraph)
        if not addition or _normalized_contains(repaired, addition):
            continue
        repaired = _append_memo_paragraph(repaired, _section_for_thesis(thesis_id), addition)
    return clean_memo_text(repaired)


def _anchor_repair_addition(thesis_id: str, paragraph: str) -> str:
    if thesis_id in {"money_map_baseline", "goal_capacity_reality"}:
        burn_amount = _money_after_phrase(paragraph, "reconciled operating burn")
        return f"On the reconciled operating basis, reconciled operating burn is {burn_amount} before goal capacity is assigned." if burn_amount else ""
    if thesis_id == "liquidity_not_primary_risk":
        cash_amount = _money_after_phrase(paragraph, "cash-like balance")
        liability_amount = _money_after_phrase(paragraph, "liabilities")
        if cash_amount and liability_amount:
            return f"cash-like balance is {cash_amount} and liabilities are {liability_amount}, so cash panic is not the main read."
        return f"cash-like balance is {cash_amount}, so cash panic is not the main read." if cash_amount else paragraph
    if thesis_id == "external_transfer_labeling":
        transfer_amount = _money_after_phrase(paragraph, "net external transfer outflow")
        return f"Net external transfer outflow is {transfer_amount}, so external transfers should be labeled before they are judged as lifestyle spending." if transfer_amount else paragraph
    if thesis_id == "savings_scenarios_are_options":
        return paragraph
    if thesis_id == "trip_event_exclusion":
        return paragraph
    if thesis_id in {"category_ledger_matters", "merchant_lifecycle_matters"}:
        return paragraph
    return paragraph


def _anchor_fact_missing(memo: str, thesis_id: str, paragraph: str) -> bool:
    text = " ".join(str(memo or "").lower().split())
    candidate = " ".join(str(paragraph or "").lower().split())
    if thesis_id == "liquidity_not_primary_risk":
        cash_amount = _money_after_phrase(paragraph, "cash-like balance")
        liability_amount = _money_after_phrase(paragraph, "liabilities")
        return bool((cash_amount and cash_amount not in memo) or (liability_amount and liability_amount not in memo))
    if thesis_id in {"money_map_baseline", "goal_capacity_reality"}:
        burn_amount = _money_after_phrase(paragraph, "reconciled operating burn")
        return bool(burn_amount and burn_amount not in memo)
    if thesis_id == "trip_event_exclusion":
        cluster_amounts = _money_tokens(paragraph)
        cluster_dates = _date_tokens(paragraph)
        return bool(cluster_amounts and any(amount not in memo for amount in cluster_amounts)) or bool(
            cluster_dates and any(date_value not in memo for date_value in cluster_dates)
        )
    if thesis_id == "category_ledger_matters":
        return "category ledger" in candidate and "category ledger" not in text
    if thesis_id == "merchant_lifecycle_matters":
        return "merchant lifecycle" in candidate and "merchant lifecycle" not in text
    if thesis_id == "savings_scenarios_are_options":
        return "planning sensitivity" in candidate and "planning sensitivity" not in text
    if thesis_id == "external_transfer_labeling":
        transfer_amount = _money_after_phrase(paragraph, "net external transfer outflow")
        return bool(transfer_amount and transfer_amount not in memo)
    return False


def _money_tokens(text: str) -> list[str]:
    return re.findall(r"\$\d[\d,]*(?:\.\d+)?", str(text or ""))


def _money_after_phrase(text: str, phrase: str) -> str | None:
    pattern = rf"{re.escape(phrase)}[^$]{{0,80}}(\$\d[\d,]*(?:\.\d+)?)"
    match = re.search(pattern, str(text or ""), flags=re.IGNORECASE)
    return match.group(1) if match else None


def _date_tokens(text: str) -> list[str]:
    return re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", str(text or ""))


def _normalized_contains(text: str, needle: str) -> bool:
    haystack = " ".join(str(text or "").lower().split())
    candidate = " ".join(str(needle or "").lower().split())
    return bool(candidate and candidate in haystack)


def _section_for_thesis(thesis_id: str) -> str:
    if thesis_id in {"period_reliability_matters", "cash_flow_compression_matters"}:
        return f"## {SECTION_THE_READ}"
    if thesis_id == "money_map_baseline":
        return f"## {SECTION_MONEY_MAP}"
    if thesis_id in {"category_ledger_matters", "merchant_lifecycle_matters", "external_transfer_labeling", "savings_scenarios_are_options"}:
        return f"## {SECTION_MONEY_MAP}"
    if thesis_id == "goal_capacity_reality":
        return f"## {SECTION_NORMAL_MONTH}"
    if thesis_id in {"liquidity_not_primary_risk", "income_continuity_uncertain"}:
        return f"## {SECTION_THE_READ}"
    if thesis_id in {"trip_event_exclusion", "protect_vaping_pause"}:
        return f"## {SECTION_NOISE}"
    if thesis_id == "fixed_floor_matters":
        return f"## {SECTION_NORMAL_MONTH}"
    if thesis_id in {"avoidable_leakage_first", "fees_inspection_first", "amazon_tune_up", "geico_vendor_review"}:
        return f"## {SECTION_ACTIONS}"
    if thesis_id == "alcohol_soft_ceiling":
        return f"## {SECTION_DO_NOT_OVERREACT}"
    return f"## {SECTION_VERIFY}"


def _append_memo_paragraph(memo: str, heading: str, paragraph: str) -> str:
    text = str(memo or "").strip()
    addition = str(paragraph or "").strip()
    if not addition:
        return text
    if heading not in text:
        return f"{text}\n\n{heading}\n{addition}".strip()
    start = text.find(heading)
    search_from = start + len(heading)
    match = re.search(r"\n##\s+", text[search_from:])
    insert_at = search_from + match.start() if match else len(text)
    before = text[:insert_at].rstrip()
    after = text[insert_at:].lstrip()
    joined = f"{before}\n\n{addition}"
    if after:
        joined = f"{joined}\n\n{after}"
    return joined.strip()


def validate_lens_advisor_memo(payload: dict[str, Any], evidence_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    memo = str(payload.get("memo_markdown") or "").strip()
    theses = payload.get("theses") if isinstance(payload.get("theses"), list) else []
    if len(memo) < ADVISOR_LENS_MIN_MEMO_CHARS:
        failures.append("memo_too_short")
    if not theses:
        failures.append("missing_theses")

    thesis_ids = [str(item.get("thesis_id") or "") for item in theses if isinstance(item, dict)]
    missing_theses = sorted(set(REQUIRED_THESES) - set(thesis_ids))
    unknown_theses = sorted({thesis_id for thesis_id in thesis_ids if thesis_id not in THESIS_CATALOG})
    if missing_theses:
        failures.append("missing_required_thesis")
    if unknown_theses:
        failures.append("unknown_thesis_id")

    invalid_evidence: list[str] = []
    cited_ids: list[str] = []
    for thesis in theses:
        if not isinstance(thesis, dict):
            failures.append("thesis_not_object")
            continue
        evidence_ids = [str(value) for value in thesis.get("evidence_ids") or [] if str(value).strip()]
        if not evidence_ids:
            failures.append(f"{thesis.get('thesis_id')}:missing_evidence")
        for evidence_id in evidence_ids:
            if evidence_id not in evidence_map:
                invalid_evidence.append(evidence_id)
            else:
                cited_ids.append(evidence_id)
        if not str(thesis.get("paragraph") or "").strip():
            failures.append(f"{thesis.get('thesis_id')}:missing_paragraph")
        if not str(thesis.get("caveat") or "").strip():
            failures.append(f"{thesis.get('thesis_id')}:missing_caveat")
    if invalid_evidence:
        failures.append("invalid_evidence_id")

    visible_text = "\n".join(
        [
            memo,
            *[
                " ".join(str(thesis.get(field) or "") for field in ("summary", "paragraph", "caveat"))
                for thesis in theses
                if isinstance(thesis, dict)
            ],
        ]
    )
    lowered = visible_text.lower()
    forbidden_hits = [phrase for phrase in _FORBIDDEN_MEMO_PHRASES if phrase in lowered]
    shame_hits = [phrase for phrase in _SHAMING_PHRASES if phrase in lowered]
    raw_field_hits = _raw_field_name_hits(visible_text)
    loose_approximation_hits = _loose_approximation_hits(visible_text)
    missing_memo_theme_markers = _missing_memo_theme_markers(memo, thesis_ids)
    cited_evidence_items = [evidence_map[eid] for eid in cited_ids if eid in evidence_map]
    goal_capacity_goal_status_missing = (
        "goal_capacity_reality" in thesis_ids
        and _goal_capacity_active_goal_count(cited_evidence_items) == 0
        and not _text_has_no_active_goal_status(memo)
    )
    unsupported = unsupported_numeric_claims(visible_text, cited_evidence_items)
    if forbidden_hits:
        failures.append("forbidden_phrase")
    if shame_hits:
        failures.append("shaming_or_sensitive_moralizing")
    if raw_field_hits:
        failures.append("raw_field_name")
    if loose_approximation_hits:
        failures.append("loose_approximation_phrase")
    if missing_memo_theme_markers:
        failures.append("memo_missing_theme_language")
    if goal_capacity_goal_status_missing:
        failures.append("goal_capacity_goal_status_missing")
    if (
        _BROKEN_NUMERIC_TOKEN_RE.search(visible_text)
        or _CORRUPT_YEAR_TOKEN_RE.search(visible_text)
        or _CORRUPT_ALNUM_TOKEN_RE.search(visible_text)
        or _NON_ASCII_NUMERIC_RE.search(visible_text)
        or _SPLIT_YEAR_PUNCT_RE.search(visible_text)
        or _TRUNCATED_YEAR_TOKEN_RE.search(visible_text)
    ):
        failures.append("broken_numeric_token")
    if unsupported:
        failures.append("unsupported_numeric_claim")

    coverage_count = len(set(thesis_ids) & set(REQUIRED_THESES))
    score = (
        coverage_count * 10
        - len(missing_theses) * 12
        - len(unsupported) * 8
        - len(invalid_evidence) * 8
        - len(forbidden_hits) * 10
        - len(shame_hits) * 10
        - len(raw_field_hits) * 8
        - len(loose_approximation_hits) * 8
        - len(missing_memo_theme_markers) * 8
        - (8 if goal_capacity_goal_status_missing else 0)
    )
    return {
        "ok": not failures,
        "score": score,
        "coverage_count": coverage_count,
        "required_count": len(REQUIRED_THESES),
        "missing_theses": missing_theses,
        "unknown_theses": unknown_theses,
        "invalid_evidence_ids": sorted(set(invalid_evidence)),
        "unsupported_numbers": unsupported[:20],
        "forbidden_phrase_hits": forbidden_hits,
        "shaming_hits": shame_hits,
        "raw_field_name_hits": raw_field_hits,
        "loose_approximation_hits": loose_approximation_hits,
        "missing_memo_theme_markers": missing_memo_theme_markers,
        "goal_capacity_goal_status_missing": goal_capacity_goal_status_missing,
        "failure_reasons": sorted(set(failures)),
    }


def store_lens_advisor_memo(
    *,
    conn,
    profile: str | None,
    payload: dict[str, Any],
    quality: dict[str, Any],
    force: bool = False,
    as_of: str | None = None,
    fact_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not force and not advisor_lens_store_enabled():
        return []
    _ensure_tables(conn)
    scoped_profile = _scope_profile(profile)
    stored_payload = dict(payload or {})
    if not isinstance(stored_payload.get("action_plan"), list):
        stored_payload["action_plan"] = build_advisor_ranked_actions(stored_payload)
    if not isinstance(stored_payload.get("cards"), list):
        stored_payload["cards"] = build_advisor_read_cards(stored_payload)
    snapshot_meta = _store_fact_snapshot_for_memo(
        conn=conn,
        profile=scoped_profile,
        as_of=as_of,
        fact_snapshot=fact_snapshot,
    )
    if snapshot_meta:
        stored_payload["advisor_fact_snapshot"] = snapshot_meta
    fingerprint = _fingerprint_memo(scoped_profile, stored_payload)
    params = (
        scoped_profile,
        stored_payload.get("memo_markdown") or "",
        json.dumps(stored_payload.get("theses") or [], sort_keys=True),
        json.dumps(quality or {}, sort_keys=True),
        json.dumps(stored_payload, sort_keys=True),
        _now(),
        (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7)).isoformat(timespec="seconds") + "Z",
        "active",
        fingerprint,
        ADVISOR_LENS_SYNTHESIS_VERSION,
    )
    conn.execute(
        """
        INSERT INTO mira_advisor_memos (
            profile_id, memo_markdown, thesis_json, quality_json, payload_json,
            generated_at, valid_until, status, fingerprint, version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
            memo_markdown = excluded.memo_markdown,
            thesis_json = excluded.thesis_json,
            quality_json = excluded.quality_json,
            payload_json = excluded.payload_json,
            generated_at = excluded.generated_at,
            valid_until = excluded.valid_until,
            version = excluded.version,
            status = excluded.status
        """,
        params,
    )
    expire_portrait_delta_packets(
        conn,
        profile=scoped_profile,
    )
    return [{**stored_payload, "fingerprint": fingerprint}]


def advisor_lens_memo_delta_status(
    *,
    conn,
    memo: dict[str, Any],
    profile: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    payload = memo.get("payload") if isinstance(memo.get("payload"), dict) else memo
    snapshot_meta = payload.get("advisor_fact_snapshot") if isinstance(payload, dict) else None
    if not isinstance(snapshot_meta, dict) or not snapshot_meta.get("fingerprint"):
        return {"status": "untracked", "reason": "missing_fact_snapshot"}
    scoped_profile = _scope_profile(profile or memo.get("profile_id") or snapshot_meta.get("profile"))
    stored = load_advisor_fact_snapshot(conn, profile=scoped_profile, fingerprint=str(snapshot_meta.get("fingerprint")))
    if not stored or not stored.get("snapshot"):
        return {
            "status": "untracked",
            "reason": "stored_snapshot_not_found",
            "stored_fingerprint": snapshot_meta.get("fingerprint"),
        }
    current = build_advisor_fact_snapshot(conn, profile=scoped_profile, as_of=as_of)
    diff = diff_advisor_fact_snapshots(stored["snapshot"], current)
    packet = build_portrait_delta_packet(diff)
    return {
        "status": packet.get("status"),
        "needs_full_rebuild": bool(packet.get("needs_full_rebuild")),
        "stored_fingerprint": snapshot_meta.get("fingerprint"),
        "current_fingerprint": current.get("fingerprint"),
        "delta_packet": packet,
        "diff": diff,
    }


def run_advisor_lens_portrait_delta(
    *,
    conn,
    profile: str | None = None,
    as_of: str | None = None,
    store: bool = True,
) -> dict[str, Any]:
    """Classify whether the latest stored read can be kept, patched, or rebuilt."""

    memos = list_lens_advisor_memos(conn=conn, profile=profile, limit=1)
    if not memos:
        return {
            "status": "missing_memo",
            "decision": "needs_full_rebuild",
            "reason": "no_stored_advisor_memo",
            "stored_delta_count": 0,
        }
    memo = memos[0]
    delta = advisor_lens_memo_delta_status(conn=conn, memo=memo, profile=profile, as_of=as_of)
    if delta.get("status") == "no_material_change":
        expired = expire_portrait_delta_packets(
            conn,
            profile=_scope_profile(profile or memo.get("profile_id")),
            source_memo_fingerprint=memo.get("fingerprint"),
        )
        return {
            "status": "fresh",
            "decision": "keep_stored_read",
            "delta": delta.get("delta_packet"),
            "stored_delta_count": 0,
            "expired_delta_count": expired,
        }
    if delta.get("status") == "untracked" or delta.get("needs_full_rebuild"):
        expired = expire_portrait_delta_packets(
            conn,
            profile=_scope_profile(profile or memo.get("profile_id")),
            source_memo_fingerprint=memo.get("fingerprint"),
        )
        return {
            "status": "needs_full_rebuild",
            "decision": "queue_full_advisor_synthesis",
            "reason": delta.get("reason") or "snapshot_diff_requires_full_rebuild",
            "delta": delta.get("delta_packet"),
            "stored_delta_count": 0,
            "expired_delta_count": expired,
        }
    stored_delta: dict[str, Any] | None = None
    if store and delta.get("delta_packet"):
        stored_delta = store_portrait_delta_packet(
            conn,
            profile=_scope_profile(profile or memo.get("profile_id")),
            source_memo_fingerprint=memo.get("fingerprint"),
            stored_snapshot_fingerprint=delta.get("stored_fingerprint"),
            current_snapshot_fingerprint=delta.get("current_fingerprint"),
            delta_packet=delta.get("delta_packet") or {},
        )
    return {
        "status": "targeted_delta",
        "decision": "store_targeted_delta" if (stored_delta or {}).get("inserted", False) else "keep_existing_delta",
        "delta": delta.get("delta_packet"),
        "stored_delta_count": 1 if (stored_delta or {}).get("inserted", False) else 0,
        "stored_delta_fingerprint": (stored_delta or {}).get("fingerprint"),
        "duplicate_delta": bool((stored_delta or {}).get("duplicate")),
    }


def _store_fact_snapshot_for_memo(
    *,
    conn,
    profile: str,
    as_of: str | None,
    fact_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        snapshot = fact_snapshot or build_advisor_fact_snapshot(conn, profile=profile, as_of=as_of)
        stored = store_advisor_fact_snapshot(conn, profile=profile, snapshot=snapshot, as_of=as_of)
    except Exception as exc:
        return {
            "status": "error",
            "profile": profile,
            "error": f"{type(exc).__name__}:{exc}",
        }
    return {
        "status": "stored",
        "profile": profile,
        "as_of": stored.get("as_of"),
        "fingerprint": stored.get("fingerprint"),
        "summary": (stored.get("snapshot") or {}).get("summary") or {},
        "version": (stored.get("snapshot") or {}).get("version") or "",
    }


def list_lens_advisor_memos(*, conn, profile: str | None, limit: int = 3) -> list[dict[str, Any]]:
    _ensure_tables(conn)
    rows = conn.execute(
        """
        SELECT *
          FROM mira_advisor_memos
         WHERE profile_id = ?
           AND status = 'active'
           AND (valid_until IS NULL OR valid_until > ?)
         ORDER BY generated_at DESC, id DESC
         LIMIT ?
        """,
        (_scope_profile(profile), _now(), max(1, min(int(limit or 3), 10))),
    ).fetchall()
    return [_row_to_memo(row) for row in rows]


def has_fresh_advisor_lens_memo(
    *,
    conn,
    profile: str | None = None,
    minutes: int | None = None,
) -> bool:
    _ensure_tables(conn)
    window = max(1, int(minutes if minutes is not None else _advisor_lens_min_interval_minutes()))
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
          FROM mira_advisor_memos
         WHERE profile_id = ?
           AND status = 'active'
           AND (valid_until IS NULL OR valid_until > ?)
           AND datetime(generated_at) >= datetime('now', ?)
        """,
        (_scope_profile(profile), _now(), f"-{window} minutes"),
    ).fetchone()
    try:
        return int(row[0] if row is not None else 0) > 0
    except (TypeError, ValueError):
        return False


def build_lens_prompt(bundle: dict[str, Any], lens: dict[str, Any]) -> str:
    body = {
        "lens_id": lens["id"],
        "lens_name": lens["name"],
        "theme_obligations": {
            thesis_id: THESIS_CATALOG[thesis_id]
            for thesis_id in lens.get("theme_obligations", ())
            if thesis_id in THESIS_CATALOG
        },
        "possible_thesis_ids": THESIS_CATALOG,
        "safe_evidence": _compact_bundle_for_prompt(bundle, lens["metrics"]),
    }
    return _prompt_header(
        f"Analyze only this advisor lens: {lens['name']}. Return the lens read and supported thesis packets, not the final memo. Preserve every theme_obligations item that is directly supported by the supplied evidence. Return at most eight supported thesis packets."
    ) + """\n\nLens rules:
- Cover every supplied theme_obligations item when the safe evidence supports it.
- For period reliability, state whether the latest month is partial and which analysis period is reliable.
- For cash-flow compression, compare recent complete months with the trailing view before calling pressure structural.
- For the money map, explain where money normally goes before ranking risks or levers.
- For the category ledger, connect categories to merchant drivers, average tickets, and controllability.
- For merchant lifecycle, distinguish top, new, dormant, and split-label merchants.
- For external transfers, say they need labels before Mira treats them as spending, goals, support, debt, or investing.
- For goal capacity, explain what monthly room remains after reconciled operating burn and configured goal requirements.
- For savings scenarios, call them optional planning sensitivities, not commands.
- For data quality, explain what limits precision without using backend language.
- For avoidable leakage, separate preventable friction from intentional spending.
- For alcohol, call it a soft-ceiling/tuning issue and not a morality read.
- For fees, say whether fees should be inspected before broad category cuts.
- For trip/event spend, say it should be separated from normal baseline.
- For the operating floor, use the phrase "fixed monthly floor".
- Do not copy snake_case field names; paraphrase them into plain English.

Lens evidence JSON:
""" + json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n\nJSON:"


def build_missing_thesis_repair_prompt(bundle: dict[str, Any], missing_thesis_ids: list[str]) -> str:
    metric_names = tuple(
        dict.fromkeys(metric for thesis_id in missing_thesis_ids for metric in _THESIS_DIRECT_METRICS.get(thesis_id, ()))
    )
    body = {
        "lens_id": "missing_thesis_repair",
        "requested_thesis_ids": {thesis_id: THESIS_CATALOG[thesis_id] for thesis_id in missing_thesis_ids if thesis_id in THESIS_CATALOG},
        "safe_evidence": _compact_bundle_for_prompt(bundle, metric_names),
    }
    return _prompt_header(
        "Repair missing advisor thesis packets only. Produce packets only for requested_thesis_ids when directly supported by the supplied safe evidence."
    ) + """\n\nRepair rules:
- Do not write a final memo.
- Return supported_theses only for requested_thesis_ids.
- Every number or date in a thesis summary, paragraph, or caveat must appear in one of that thesis's evidence_ids.
- If you mention multiple event windows, cite evidence for each one.
- If the supplied evidence does not support a requested thesis, omit it.

Repair input JSON:
""" + json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n\nJSON:"


def build_lens_final_prompt(bundle: dict[str, Any], lens_reads: list[dict[str, Any]], candidate_theses: list[dict[str, Any]]) -> str:
    candidate_ids = [str(item.get("thesis_id") or "") for item in candidate_theses if isinstance(item, dict)]
    final_input = {
        "possible_thesis_ids": THESIS_CATALOG,
        "theme_obligations": {thesis_id: THESIS_CATALOG[thesis_id] for thesis_id in candidate_ids if thesis_id in THESIS_CATALOG},
        "validated_candidate_theses": candidate_theses,
        "lens_reads": [
            {
                "lens_id": lens_read.get("lens_id"),
                "missing_or_uncertain": lens_read.get("missing_or_uncertain") or [],
            }
            for lens_read in lens_reads
            if isinstance(lens_read, dict)
        ],
        "evidence_index": _compact_evidence_index(bundle),
    }
    return _prompt_header(
        "Combine the validated candidate theses into one integrated private analyst memo. Include every validated candidate thesis exactly once, keep its evidence IDs, resolve wording conflicts, and do not add facts not present in the candidate theses."
    ) + """\n\nFor this final lens synthesis, return only:
{
  "memo_markdown": "integrated private memo; no evidence IDs in prose",
  "thesis_order": ["the thesis IDs from validated_candidate_theses, each exactly once"],
  "quality_notes": []
}

Do not re-emit the full candidate thesis objects. Python will attach those
validated packets after your memo is parsed.

The memo should be a real private analyst read, not a compressed summary. Use
these markdown sections:
## The Read
## The Month I Would Plan Around
## What I Am Keeping Out Of The Verdict
## The Money Map
## Moves I Would Make First
## What I Would Leave Alone
## What Could Change This Read

Final composer rules:
- Preserve every theme_obligations item in memo_markdown, but do not mention the thesis ID itself.
- Include period reliability when present: reliable analysis period, partial latest month, and why this changes trend confidence.
- Include cash-flow compression when present: recent complete-month pressure versus trailing view.
- Start with the normal money map when money_map_baseline is present: income, normal spend, fixed floor, flexible estimate, top categories/merchants, and event exclusions.
- Include category ledger and merchant lifecycle when present: top categories, merchant drivers, ticket size, new/dormant/split-label patterns, and what is controllable.
- External transfers need labels before they become lifestyle spending, support, investing, debt, or goal movement.
- When goal_capacity_reality is present, include a The Month I Would Plan Around section that states capacity before goals, required monthly goal contribution, and capacity after required goals.
- Savings scenarios are optional planning sensitivities, not commands or moral judgments.
- Data-quality limitations should explain what could change the smaller recommendations.
- Do not call range-wide event exclusions current-month exclusions unless the current-month event-exclusion field supports it.
- For avoidable leakage, explain what looks like fixable friction before recommending any painful lifestyle cuts.
- For alcohol, call it a soft-ceiling/tuning issue and not a morality read.
- For vaping, the caveat is sync completeness; say "pause" only when the safe lever explicitly says pause, otherwise call it a reduction or lower rhythm.
- For fees, say whether fees should be inspected before broad category cuts.
- For trip/event spend, say it should be separated from normal baseline.
- For the operating floor, use the phrase "fixed monthly floor".
- The final ranked action plan is attached by Python from validated thesis packets; do not invent new action items to fill the section.
- Do not copy snake_case field names; paraphrase them into plain English.
""" + "\n\nLens synthesis JSON:\n" + json.dumps(final_input, sort_keys=True, separators=(",", ":")) + "\n\nJSON:"


def _prompt_header(task: str) -> str:
    return f"""You are Mira's offline private analyst. This is a local-only QA artifact, not UI copy.

Task: {task}

Product bar:
- Mira should sound like a top-tier personal financial analyst who has actually studied this user's money.
- Do not narrate dashboards. Form a point of view.
- Distinguish structural facts from temporary noise.
- Name what matters now, what not to overreact to, what to check next, and what caveat could change the read.
- Sensitive local categories may be named in this private memo when the evidence names them, but never shame, moralize, diagnose, or infer motives.
- Python owns math. Use only exact numbers and dates from the provided safe evidence.
- Do not mention SQL, tools, validators, evidence IDs, metric IDs, backend internals, or implementation details in memo_markdown.
- Use dollar signs for money amounts in memo_markdown when the amount is a dollar amount.
- Prefer calm precision over exaggerated adjectives.
- Do not quote internal status strings; paraphrase them in human language.
- Do not copy snake_case field names; paraphrase them into plain English.
- Do not write loose approximations such as "roughly", "approximately", "about $", "around $", "over $", "under $", or "~".

Possible thesis IDs, use only when supported by the evidence:
{json.dumps(THESIS_CATALOG, sort_keys=True)}

Return JSON only.

Hard rules:
- Include every materially supported thesis, especially false alarms and exclusions.
- If money_map_baseline is present, begin the memo with where money normally goes.
- In a final lens synthesis, include every validated_candidate_theses item exactly once unless it is directly contradicted by another candidate.
- Memo_markdown must not contain important claims that are absent from the theses array.
- Every thesis needs at least one valid evidence_id.
- Every number or date in memo_markdown must appear in cited evidence.
- If a practical action comes from a realistic_trim_levers row, cite that row.
- Do not introduce action durations or counts unless the exact number appears in one of the thesis evidence_ids.
- Never put underscores inside numbers.
- Do not turn metric labels into sentences.
- Do not write generic finance tips."""


def build_lens_final_repair_prompt(
    candidate_theses: list[dict[str, Any]],
    prior_payload: dict[str, Any],
    quality: dict[str, Any],
) -> str:
    repair_input = {
        "validated_candidate_theses": candidate_theses,
        "prior_memo_markdown": str((prior_payload or {}).get("memo_markdown") or "")[:5000],
        "quality_failure_reasons": quality.get("failure_reasons") or [],
        "unsupported_numbers": quality.get("unsupported_numbers") or [],
        "raw_field_name_hits": quality.get("raw_field_name_hits") or [],
        "loose_approximation_hits": quality.get("loose_approximation_hits") or [],
        "missing_memo_theme_markers": quality.get("missing_memo_theme_markers") or [],
        "required_thesis_ids": list(REQUIRED_THESES),
    }
    return _prompt_header(
        "Repair the final private analyst memo after validation failure. Keep the same thesis IDs and evidence. Rewrite only memo_markdown and thesis_order."
    ) + """\n\nRepair requirements:
- Use only facts already present in validated_candidate_theses.
- Include every required_thesis_ids item exactly once in thesis_order.
- Copy numeric strings exactly from validated_candidate_theses; do not create new numbers, approximations, ranges, or malformed tokens.
- If a candidate thesis does not contain a number, do not add that number in the memo.
- Do not mention evidence IDs, metric IDs, tool names, SQL, validators, or backend internals.
- Do not copy snake_case field names; paraphrase them into plain English.
- Preserve every required_thesis_ids item already present in validated_candidate_theses.
- Keep a "The Money Map" section when money_map_baseline is present.
- Keep a "The Month I Would Plan Around" section when goal_capacity_reality is present.
- For avoidable leakage, explain fixable friction before painful lifestyle cuts.
- For alcohol, call it a soft-ceiling/tuning issue and not a morality read.
- For vaping, the caveat is sync completeness; say "pause" only when the safe lever explicitly says pause, otherwise call it a reduction or lower rhythm.
- For fees, say whether fees should be inspected before broad category cuts.
- For trip/event spend, say it should be separated from normal baseline.
- For the operating floor, use the phrase "fixed monthly floor".
- Keep the same markdown sections as the prior memo.

Repair input JSON:
""" + json.dumps(repair_input, sort_keys=True, separators=(",", ":")) + "\n\nJSON:"


def _validated_candidate(thesis: dict[str, Any], evidence_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    thesis_id = str(thesis.get("thesis_id") or "").strip()
    if thesis_id not in THESIS_CATALOG:
        return None
    evidence_ids = [str(value) for value in thesis.get("evidence_ids") or [] if str(value).strip()]
    for claim in thesis.get("numeric_claims") or []:
        if not isinstance(claim, dict):
            continue
        evidence_id = str(claim.get("evidence_id") or "").strip()
        if evidence_id and evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
    if thesis_id == "goal_capacity_reality":
        summary_id = "metric:goal_capacity_statement:summary"
        if summary_id in evidence_map and summary_id not in evidence_ids:
            evidence_ids.insert(0, summary_id)
    if not evidence_ids or any(evidence_id not in evidence_map for evidence_id in evidence_ids):
        return None
    memo_text = clean_candidate_text(thesis.get("memo_markdown"))
    summary = clean_candidate_text(thesis.get("summary")) or _first_sentence(memo_text)
    paragraph = clean_candidate_text(thesis.get("paragraph")) or memo_text
    evidence_items = [evidence_map[evidence_id] for evidence_id in evidence_ids]
    caveat = clean_candidate_text(thesis.get("caveat")) or _fallback_candidate_caveat(evidence_items)
    summary, paragraph, caveat = _repair_candidate_text_fields(summary, paragraph, caveat, evidence_items)
    summary, paragraph, caveat = _ensure_thesis_language(thesis_id, summary, paragraph, caveat)
    summary, paragraph, caveat = _ensure_goal_capacity_goal_status(thesis_id, summary, paragraph, caveat, evidence_items)
    if not summary or not paragraph or not caveat:
        return None
    if unsupported_numeric_claims(" ".join((summary, paragraph, caveat)), evidence_items):
        return None
    return {
        "thesis_id": thesis_id,
        "stance": str(thesis.get("stance") or ""),
        "summary": summary,
        "paragraph": paragraph,
        "evidence_ids": evidence_ids[:8],
        "numeric_claims": thesis.get("numeric_claims") if isinstance(thesis.get("numeric_claims"), list) else [],
        "caveat": caveat,
        "confidence": str(thesis.get("confidence") or "medium"),
    }


def unsupported_numeric_claims(text: str, evidence_items: list[dict[str, Any]]) -> list[str]:
    supported = _numbers_from_evidence(evidence_items)
    unsupported = []
    for number in _numbers_from_text(text):
        if number not in supported:
            unsupported.append(number)
    return sorted(set(unsupported), key=lambda value: (len(value), value))


def clean_candidate_text(value: Any) -> str:
    text = str(value or "").replace("\u00a0", " ")
    text = _normalize_split_years(text)
    text = re.sub(r"(?<=\d)\.\s+(?=\d)", ".", text)
    text = re.sub(r"(?<=\d)_(?=\d)", "", text)
    text = text.replace("day-to_day", "day-to-day").replace("day_to_day", "day-to-day")
    text = text.replace("appears materially paused", "is materially lower than prior peaks")
    text = text.replace("appears paused", "is lower than prior peaks")
    text = text.replace("apparent pause", "reduction")
    text = re.sub(r"\b(?:roughly|approximately|about|around|over|under)\s+(\$)", r"\1", text, flags=re.IGNORECASE)
    text = text.replace("~$", "$")
    text = re.sub(r"\(?\b(?:metric|txn):[A-Za-z0-9_:\-.]+\)?", "", text)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    text = _humanize_internal_terms(text)
    text = _drop_corrupt_numeric_sentences(text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _repair_candidate_text_fields(
    summary: str,
    paragraph: str,
    caveat: str,
    evidence_items: list[dict[str, Any]],
) -> tuple[str, str, str]:
    summary = _repair_supported_name_phrases(summary, evidence_items)
    paragraph = _repair_supported_name_phrases(paragraph, evidence_items)
    caveat = _repair_supported_name_phrases(caveat, evidence_items)
    summary = _drop_unsupported_numeric_sentences(summary, evidence_items)
    paragraph = _drop_unsupported_numeric_sentences(paragraph, evidence_items)
    caveat = _drop_unsupported_numeric_sentences(caveat, evidence_items)
    if not summary and paragraph:
        summary = _first_sentence(paragraph)
    if not paragraph and summary:
        paragraph = summary
    if not caveat:
        caveat = _fallback_candidate_caveat(evidence_items)
    return summary, paragraph, caveat


def _ensure_goal_capacity_goal_status(
    thesis_id: str,
    summary: str,
    paragraph: str,
    caveat: str,
    evidence_items: list[dict[str, Any]],
) -> tuple[str, str, str]:
    if thesis_id != "goal_capacity_reality":
        return summary, paragraph, caveat
    active_goals = _goal_capacity_active_goal_count(evidence_items)
    if active_goals != 0:
        return summary, paragraph, caveat
    summary = summary.replace("available for configured goals", "available before explicit goals are configured")
    paragraph = paragraph.replace("available for configured goals", "available before explicit goals are configured")
    caveat = caveat.replace("available for configured goals", "available before explicit goals are configured")
    combined = " ".join((summary, paragraph, caveat))
    if not _text_has_no_active_goal_status(combined):
        paragraph = " ".join(
            piece
            for piece in (
                paragraph,
                "No active goals are configured, so this is planning capacity before explicit goal targets rather than proof that a specific goal is funded.",
            )
            if piece
        ).strip()
    return summary, paragraph, caveat


def _goal_capacity_active_goal_count(evidence_items: list[dict[str, Any]]) -> float | None:
    for item in evidence_items:
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        summary = values.get("summary_numbers") if isinstance(values.get("summary_numbers"), dict) else {}
        if "active_goal_count" in summary:
            return _float_value(summary.get("active_goal_count"))
        if "goal_count" in values:
            return _float_value(values.get("goal_count"))
    return None


def _drop_unsupported_numeric_sentences(text: str, evidence_items: list[dict[str, Any]]) -> str:
    text = str(text or "").strip()
    if not text or not unsupported_numeric_claims(text, evidence_items):
        return text
    pieces = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    kept = [piece.strip() for piece in pieces if piece.strip() and not unsupported_numeric_claims(piece, evidence_items)]
    return " ".join(kept).strip()


def _repair_supported_name_phrases(text: str, evidence_items: list[dict[str, Any]]) -> str:
    text = str(text or "")
    if not text:
        return ""
    supported = _supported_name_phrases(evidence_items)
    if not supported:
        return text
    for phrase in sorted(set(_UPPERCASE_PHRASE_RE.findall(text)), key=len, reverse=True):
        if len(phrase) < 5 or phrase in supported:
            continue
        match = difflib.get_close_matches(phrase, supported, n=1, cutoff=0.86)
        if match:
            text = re.sub(rf"\b{re.escape(phrase)}\b", match[0], text)
    return text


def _supported_name_phrases(evidence_items: list[dict[str, Any]]) -> list[str]:
    phrases: set[str] = set()
    for item in evidence_items:
        for value in _string_values(item):
            for phrase in _UPPERCASE_PHRASE_RE.findall(value):
                if len(phrase) >= 5 and any(ch.isalpha() for ch in phrase):
                    phrases.add(phrase)
    return sorted(phrases)


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for nested in value.values():
            out.extend(_string_values(nested))
        return out
    if isinstance(value, list):
        out = []
        for nested in value:
            out.extend(_string_values(nested))
        return out
    return []


def _first_sentence(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    match = re.search(r"(.+?[.!?])(?:\s|$)", text)
    return (match.group(1) if match else text[:180]).strip()


def _fallback_candidate_caveat(evidence_items: list[dict[str, Any]]) -> str:
    for item in evidence_items:
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        candidates = []
        if isinstance(values.get("caveats"), list):
            candidates.extend(values.get("caveats") or [])
        for key in ("caveat", "confidence_note", "interpretation_caveat"):
            if values.get(key):
                candidates.append(values.get(key))
        for candidate in candidates:
            text = clean_candidate_text(candidate)
            if text:
                return text
    return "This read can change if the cited source data is incomplete, stale, or reclassified."


def clean_memo_text(value: Any) -> str:
    text = clean_candidate_text(value)
    text = _drop_corrupt_numeric_sentences(text)
    replacements = {
        "202026": "2026",
        "20 26": "2026",
        "20\u00a026": "2026",
        "exceptionally resilient": "well covered",
        "exceptionally stable": "stable",
        "exceptionally strong": "strong",
        "massive cash runway": "long cash runway",
        "massive buffer": "large buffer",
        "negligible debt pressure": "low debt pressure",
        "negligible debt": "low debt",
        "if the current data sync is an active obligation or a historical error": "if sync is complete",
        "if current data sync is an active obligation or a historical error": "if sync is complete",
        "sync is an active obligation": "sync is complete",
        "based on fewer and does not include": "based on fewer than three active months and does not include",
        "State the goal capacity reality: monthly room before configured goals, required goal contributions, and capacity after those goals.": "This is planning room before explicit goal targets, not a completed goal plan; once a target exists, compare its required monthly contribution against this capacity.",
        "state the goal capacity reality: monthly room before configured goals, required goal contributions, and capacity after those goals.": "This is planning room before explicit goal targets, not a completed goal plan; once a target exists, compare its required monthly contribution against this capacity.",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
        text = text.replace(source.title(), target)
    text = _humanize_internal_terms(text)
    text = _format_money_amounts(text)
    text = _dedupe_repeated_sentences(text)
    return text


def _dedupe_repeated_sentences(text: str) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for line in str(text or "").splitlines():
        if line.lstrip().startswith("#"):
            lines.append(line)
            continue
        pieces = re.split(r"(?<=[.!?])\s+", line)
        kept: list[str] = []
        for piece in pieces:
            normalized = " ".join(piece.lower().split())
            if normalized and normalized in seen:
                continue
            if normalized:
                seen.add(normalized)
            kept.append(piece)
        lines.append(" ".join(kept).strip())
    return "\n".join(lines).strip()


def _drop_corrupt_numeric_sentences(text: str) -> str:
    lines: list[str] = []
    for line in str(text or "").splitlines():
        if line.lstrip().startswith("#"):
            lines.append(line)
            continue
        pieces = re.split(r"(?<=[.!?])\s+", line)
        kept = [piece for piece in pieces if piece and not _has_corrupt_numeric_token(piece)]
        lines.append(" ".join(kept))
    return "\n".join(lines).strip()


def _has_corrupt_numeric_token(text: str) -> bool:
    return bool(
        _BROKEN_NUMERIC_TOKEN_RE.search(text)
        or _CORRUPT_YEAR_TOKEN_RE.search(text)
        or _CORRUPT_ALNUM_TOKEN_RE.search(text)
        or _NON_ASCII_NUMERIC_RE.search(text)
        or _SPLIT_YEAR_PUNCT_RE.search(text)
        or _TRUNCATED_YEAR_TOKEN_RE.search(text)
    )


def _ensure_thesis_language(thesis_id: str, summary: str, paragraph: str, caveat: str) -> tuple[str, str, str]:
    combined = " ".join((summary, paragraph, caveat)).lower()
    additions: list[str] = []
    if thesis_id == "period_reliability_matters" and not _text_has_period_reliability(combined):
        additions.append("The latest partial month and reliable analysis period should shape every trend read.")
    elif thesis_id == "cash_flow_compression_matters" and not _text_has_cash_flow_compression(combined):
        additions.append("Compare recent complete-month cash flow with the trailing view before deciding whether pressure is structural.")
    elif thesis_id == "money_map_baseline" and not _text_has_money_map(combined):
        additions.append("Start with where money normally goes before ranking risks or levers.")
    elif thesis_id == "category_ledger_matters" and not _text_has_category_ledger(combined):
        additions.append("Use the category ledger to connect categories, merchants, ticket size, and controllability.")
    elif thesis_id == "merchant_lifecycle_matters" and not _text_has_merchant_lifecycle(combined):
        additions.append("Merchant lifecycle should separate top, new, dormant, and split-label merchants before calling something drift.")
    elif thesis_id == "external_transfer_labeling" and not _text_has_external_transfer_labeling(combined):
        additions.append("External transfers should be labeled before they are judged as lifestyle spending.")
    elif thesis_id == "goal_capacity_reality" and not _text_has_goal_capacity(combined):
        additions.append("This is planning room before explicit goal targets, not a completed goal plan; once a target exists, compare its required monthly contribution against this capacity.")
    elif thesis_id == "savings_scenarios_are_options" and not _text_has_savings_scenarios(combined):
        additions.append("Savings scenarios are optional planning sensitivities, not commands.")
    elif thesis_id == "liquidity_not_primary_risk" and not (
        "liquidity" in combined and ("panic" in combined or "primary risk" in combined or "main read" in combined)
    ):
        additions.append("Liquidity is not the main read, so cash panic should not drive the recommendation.")
    elif thesis_id == "fixed_floor_matters" and "fixed monthly floor" not in combined:
        additions.append("The fixed monthly floor should anchor the operating plan.")
    elif thesis_id == "trip_event_exclusion" and not _text_has_trip_exclusion(combined):
        additions.append("Trip/event spend should be separated from the normal lifestyle baseline.")
    elif thesis_id == "avoidable_leakage_first" and not _text_has_avoidable_leakage(combined):
        additions.append("Avoidable leakage should be fixed before painful lifestyle cuts.")
    elif thesis_id == "protect_vaping_pause" and "vaping" in combined:
        if "protect" not in combined:
            additions.append("If sync is complete, protect the vaping reduction rather than treating it as spend to claw back elsewhere.")
        if not _text_has_private_reduction_context(combined):
            additions.append("The point is the lower rhythm versus prior peaks, not a judgment about the category.")
    elif thesis_id == "alcohol_soft_ceiling":
        if "soft ceiling" not in combined and "tuning" not in combined:
            additions.append("Treat alcohol as a soft-ceiling/tuning issue.")
        if "moral" not in combined:
            additions.append("It is not a morality read.")
    elif thesis_id == "fees_inspection_first" and not _text_has_fee_inspection(combined):
        additions.append("Fees should be inspected before broad category cuts.")
    elif thesis_id == "data_quality_limits_precision" and not _text_has_data_quality_limits(combined):
        additions.append("Data quality limits precision in smaller recommendations.")
    if additions:
        paragraph = " ".join(piece for piece in (paragraph, *additions) if piece).strip()
    return summary, paragraph, caveat


def _missing_memo_theme_markers(memo: str, thesis_ids: list[str]) -> list[str]:
    text = " ".join(str(memo or "").lower().split())
    missing: list[str] = []
    for thesis_id in thesis_ids:
        if thesis_id == "period_reliability_matters":
            ok = _text_has_period_reliability(text)
        elif thesis_id == "cash_flow_compression_matters":
            ok = _text_has_cash_flow_compression(text)
        elif thesis_id == "money_map_baseline":
            ok = _text_has_money_map(text)
        elif thesis_id == "category_ledger_matters":
            ok = _text_has_category_ledger(text)
        elif thesis_id == "merchant_lifecycle_matters":
            ok = _text_has_merchant_lifecycle(text)
        elif thesis_id == "external_transfer_labeling":
            ok = _text_has_external_transfer_labeling(text)
        elif thesis_id == "goal_capacity_reality":
            ok = _text_has_goal_capacity(text)
        elif thesis_id == "savings_scenarios_are_options":
            ok = _text_has_savings_scenarios(text)
        elif thesis_id == "liquidity_not_primary_risk":
            ok = "liquidity" in text and ("panic" in text or "primary risk" in text or "main read" in text)
        elif thesis_id == "fixed_floor_matters":
            ok = "fixed monthly floor" in text
        elif thesis_id == "income_continuity_uncertain":
            ok = "income" in text and ("source" in text or "unlabeled" in text or "continuity" in text or "verification" in text)
        elif thesis_id == "trip_event_exclusion":
            ok = _text_has_trip_exclusion(text)
        elif thesis_id == "avoidable_leakage_first":
            ok = _text_has_avoidable_leakage(text)
        elif thesis_id == "protect_vaping_pause":
            ok = (
                "vaping" in text
                and ("protect" in text or "protected" in text)
                and ("sync" in text or "reduction" in text or "lower" in text)
                and _text_has_private_reduction_context(text)
            )
        elif thesis_id == "alcohol_soft_ceiling":
            ok = "alcohol" in text and ("soft ceiling" in text or "soft-ceiling" in text or "tuning" in text) and "moral" in text
        elif thesis_id == "fees_inspection_first":
            ok = _text_has_fee_inspection(text)
        elif thesis_id == "amazon_tune_up":
            ok = "amazon" in text and ("tune" in text or "consolidat" in text or "weekly cart" in text)
        elif thesis_id == "geico_vendor_review":
            ok = "geico" in text and ("vendor" in text or "quote" in text or "comparison" in text)
        elif thesis_id == "data_quality_limits_precision":
            ok = _text_has_data_quality_limits(text)
        elif thesis_id == "missing_data_caveats":
            ok = (
                ("missing" in text or "unconfigured" in text or "lack of active goals" in text or "labels" in text or "sync" in text)
                and (
                    "change the read" in text
                    or "can change" in text
                    or "could change" in text
                    or "could significantly change" in text
                    or "would change" in text
                    or "limited by" in text
                    or "limited to" in text
                )
            )
        else:
            ok = True
        if not ok:
            missing.append(thesis_id)
    return missing


def _text_has_period_reliability(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        ("reliable analysis period" in normalized or "analysis period" in normalized or "period check" in normalized)
        and ("partial" in normalized or "first observed income" in normalized or "first income" in normalized)
    )


def _text_has_cash_flow_compression(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        ("cash flow" in normalized or "cash-flow" in normalized)
        and ("recent" in normalized or "trailing" in normalized or "complete-month" in normalized or "compression" in normalized)
    )


def _text_has_category_ledger(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        ("category ledger" in normalized or "category" in normalized)
        and "merchant" in normalized
        and ("ticket" in normalized or "controll" in normalized or "where the money goes" in normalized)
    )


def _text_has_merchant_lifecycle(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        "merchant" in normalized
        and ("lifecycle" in normalized or "new" in normalized or "dormant" in normalized or "split-label" in normalized)
    )


def _text_has_external_transfer_labeling(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return "external transfer" in normalized and ("label" in normalized or "labeled" in normalized)


def _text_has_savings_scenarios(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        ("savings scenario" in normalized or "savings scenarios" in normalized or "scenario" in normalized)
        and ("optional" in normalized or "sensitivity" in normalized or "not command" in normalized or "not a command" in normalized)
    )


def _text_has_data_quality_limits(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        ("data quality" in normalized or "low-confidence" in normalized or "low confidence" in normalized or "unreviewed" in normalized)
        and ("limit" in normalized or "precision" in normalized)
    )


def _text_has_trip_exclusion(text: str) -> bool:
    return (
        ("trip" in text or "travel" in text or "event" in text)
        and ("baseline" in text or "separate" in text or "subtract" in text or "exclude" in text)
    )


def _text_has_money_map(text: str) -> bool:
    return (
        (
            "money goes" in text
            or "money actually goes" in text
            or "money map" in text
            or "money normally goes" in text
            or "normal month" in text
            or "money is going" in text
            or "where the money is going" in text
            or "where the money goes" in text
            or "where your money" in text
        )
        and ("fixed" in text or "flexible" in text or "category" in text or "merchant" in text)
    )


def _text_has_goal_capacity(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        ("goal capacity" in normalized or ("capacity" in normalized and "goal" in normalized))
        and (
            "monthly room" in normalized
            or "planning room" in normalized
            or "room before" in normalized
            or "before configured goals" in normalized
            or "before explicit goal" in normalized
            or "after required goals" in normalized
        )
    )


def _text_has_no_active_goal_status(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return (
        "no active goals" in normalized
        or "goals are not configured" in normalized
        or "before explicit goals" in normalized
        or "before explicit goal targets" in normalized
    )


def _text_has_avoidable_leakage(text: str) -> bool:
    return (
        ("leakage" in text or "fees" in text or "fee" in text or "interest" in text)
        and ("friction" in text or "preventable" in text or "before painful" in text or "before lifestyle" in text or "before broad" in text)
    )


def _text_has_private_reduction_context(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    markers = (
        "lower than prior",
        "lower than its prior",
        "lower rhythm",
        "prior peak",
        "prior peaks",
        "versus prior",
        "compared with prior",
        "compared to prior",
        "down from",
        "reduction from",
    )
    return any(marker in normalized for marker in markers)


def _text_has_fee_inspection(text: str) -> bool:
    return "fees" in text and ("broad category cuts" in text or ("before" in text and "category cuts" in text))


def _format_money_amounts(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(1)
        if "," in raw:
            return "$" + raw
        try:
            if "." in raw:
                whole, decimal = raw.split(".", 1)
                return "$" + f"{int(whole):,}" + "." + decimal
            return "$" + f"{int(raw):,}"
        except ValueError:
            return "$" + raw

    return re.sub(r"\$(\d{4,}(?:\.\d+)?)", repl, str(text or ""))


def _humanize_internal_terms(text: str) -> str:
    out = str(text or "")
    for source, target in _INTERNAL_TERM_REPLACEMENTS.items():
        out = re.sub(rf"\b{re.escape(source)}\b", target, out)
    for source, target in _TONE_REPLACEMENTS.items():
        out = re.sub(rf"\b{re.escape(source)}\b", target, out, flags=re.IGNORECASE)
    return out


def _raw_field_name_hits(text: str) -> list[str]:
    return sorted(set(_RAW_FIELD_NAME_RE.findall(str(text or ""))))


def _loose_approximation_hits(text: str) -> list[str]:
    return sorted(set(match.group(0).strip() for match in _LOOSE_APPROXIMATION_RE.finditer(str(text or ""))))


def _normalize_split_years(text: str) -> str:
    text = re.sub(r"\b([12])[\s\u00a0]+(\d{3})\b", r"\1\2", text)
    text = re.sub(r"\b((?:19|20)\d)[\s\u00a0]+(\d)\b", r"\1\2", text)
    return re.sub(r"\b((?:19|20))[\s\u00a0]+(\d{2})\b", r"\1\2", text)


def _compact_metric(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "metric": result.get("metric"),
        "basis": result.get("basis"),
        "summary_numbers": result.get("summary_numbers") or {},
        "rows": [_compact_row(row) for row in (result.get("rows") or [])[:12]],
        "caveats": result.get("caveats") or [],
        "evidence_ids": (result.get("evidence_ids") or [])[:20],
    }


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, float):
            out[key] = round(value, 4)
        elif isinstance(value, (int, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = value[:8]
        elif isinstance(value, dict):
            out[key] = {str(k): v for k, v in list(value.items())[:8]}
        else:
            out[key] = str(value)[:240]
    return out


def _compact_bundle_for_prompt(bundle: dict[str, Any], metric_names: tuple[str, ...]) -> dict[str, Any]:
    metrics = bundle.get("metrics") or {}
    selected: dict[str, Any] = {}
    for metric_name in metric_names:
        metric = metrics.get(metric_name)
        if not isinstance(metric, dict):
            continue
        selected[metric_name] = {
            "metric": metric.get("metric") or metric_name,
            "summary_evidence_id": f"metric:{metric_name}:summary",
            "basis": metric.get("basis") or "",
            "summary_numbers": metric.get("summary_numbers") or {},
            "rows": [_row_for_prompt(metric_name, idx, row) for idx, row in enumerate(metric.get("rows") or [], start=1)][:8],
            "caveats": (metric.get("caveats") or [])[:5],
        }
    return {
        "profile": bundle.get("profile") or "",
        "metric_count": len(selected),
        "metrics": selected,
    }


def _row_for_prompt(metric_name: str, idx: int, row: dict[str, Any]) -> dict[str, Any]:
    out = {"evidence_id": f"metric:{metric_name}:{idx}"}
    for key, value in row.items():
        if key == "sample_evidence_ids":
            out[key] = value[:8] if isinstance(value, list) else value
        elif isinstance(value, list):
            out[key] = value[:6]
        elif isinstance(value, dict):
            out[key] = {str(k): v for k, v in list(value.items())[:6]}
        else:
            out[key] = value
    return out


def _compact_evidence_index(bundle: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for metric_name, metric in (bundle.get("metrics") or {}).items():
        if not isinstance(metric, dict):
            continue
        out[metric_name] = {
            "summary_evidence_id": f"metric:{metric_name}:summary",
            "row_evidence_ids": [f"metric:{metric_name}:{idx}" for idx, _row in enumerate((metric.get("rows") or [])[:8], start=1)],
        }
    return out


def _numbers_from_text(text: str) -> set[str]:
    return {_normalize_number(match.group(0)) for match in _NUMERIC_RE.finditer(text or "")}


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


def _quality_score(quality: dict[str, Any]) -> int:
    if not isinstance(quality, dict):
        return -100000
    try:
        return int(quality.get("score") or 0)
    except (TypeError, ValueError):
        return -100000


def _thesis_evidence_score(thesis_id: str, evidence_ids: list[str]) -> int:
    direct_metrics = _THESIS_DIRECT_METRICS.get(thesis_id) or ()
    score = 0
    for evidence_id in evidence_ids:
        for metric in direct_metrics:
            if evidence_id == f"metric:{metric}:summary" or evidence_id.startswith(f"metric:{metric}:"):
                score += 20
        if evidence_id.startswith("txn:"):
            score += 2
    return score


def _complete_json(
    prompt: str,
    *,
    max_tokens: int,
    response_format: dict[str, Any],
    complete_fn: Callable[..., str] | None,
) -> Any:
    if complete_fn is not None:
        raw = complete_fn(prompt, max_tokens, "advisor", response_format=response_format)
    else:
        import llm_client

        raw = llm_client.complete(prompt, max_tokens=max_tokens, purpose="advisor", response_format=response_format)
    return _parse_json(raw)


def _parse_json(raw: str) -> Any:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mira_advisor_memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            memo_markdown TEXT NOT NULL,
            thesis_json TEXT NOT NULL,
            quality_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            valid_until TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            fingerprint TEXT NOT NULL,
            version TEXT NOT NULL,
            UNIQUE(profile_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mira_advisor_memos_profile_status
            ON mira_advisor_memos(profile_id, status, generated_at DESC)
        """
    )


def _row_to_memo(row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"] or "{}")
    return {
        "id": row["id"],
        "profile_id": row["profile_id"],
        "memo_markdown": row["memo_markdown"],
        "theses": json.loads(row["thesis_json"] or "[]"),
        "quality": json.loads(row["quality_json"] or "{}"),
        "payload": payload,
        "action_plan": payload.get("action_plan") if isinstance(payload.get("action_plan"), list) else [],
        "cards": payload.get("cards") if isinstance(payload.get("cards"), list) else [],
        "generated_at": row["generated_at"],
        "valid_until": row["valid_until"],
        "status": row["status"],
        "fingerprint": row["fingerprint"],
        "version": row["version"],
    }


def _fingerprint_memo(profile: str | None, payload: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "profile": _scope_profile(profile),
            "memo": payload.get("memo_markdown") or "",
            "thesis_ids": [item.get("thesis_id") for item in payload.get("theses") or [] if isinstance(item, dict)],
        },
        sort_keys=True,
    )
    return sha1(seed.encode("utf-8")).hexdigest()[:24]


def _bundle_meta(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": bundle.get("version"),
        "metric_count": bundle.get("metric_count"),
        "error_count": len(bundle.get("errors") or []),
    }


def _scope_profile(profile: str | None) -> str:
    return str(profile or "household")


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _advisor_context_relevant(*, question: str, route: dict[str, Any] | None) -> bool:
    route = route if isinstance(route, dict) else {}
    operation = str(route.get("operation") or route.get("intent") or "").strip().lower()
    intent = str(route.get("intent") or "").strip().lower()
    selected_tools = {str(tool or "").strip().lower() for tool in route.get("selected_tools") or []}
    if operation in {"write_preview", "memory_op"} or selected_tools & {"make_chart", "plot_chart", "query_transactions"}:
        return False
    if intent in {
        "finance_priorities",
        "finance_snapshot",
        "savings_capacity",
        "cashflow_forecast",
        "cashflow_shortfall",
        "budget_plan",
        "spending_explain",
        "spending_compare",
    }:
        return True
    text = " ".join(str(question or "").lower().split())
    explicit_markers = (
        "mira's read",
        "miras read",
        "your read",
        "advisor read",
        "advisor memo",
        "private memo",
        "financial portrait",
        "financial read",
        "your analysis",
        "what did you notice",
        "what should i focus",
        "highest leverage",
        "biggest risk",
        "what can i cut",
        "cut down",
        "reduce a bit",
        "not overreact",
    )
    return any(marker in text for marker in explicit_markers)


def _latest_advisor_context_delta(*, conn, profile: str | None, memo: dict[str, Any]) -> dict[str, Any] | None:
    memo_fingerprint = str(memo.get("fingerprint") or "").strip()
    if not memo_fingerprint:
        return None
    try:
        deltas = list_portrait_delta_packets(conn, profile=profile, limit=6)
    except Exception:
        return None
    for delta in deltas:
        if str(delta.get("source_memo_fingerprint") or "").strip() != memo_fingerprint:
            continue
        packet = delta.get("delta_packet") if isinstance(delta.get("delta_packet"), dict) else {}
        if packet.get("status") != "changed":
            continue
        return {**delta, "delta_packet": packet}
    return None


def _advisor_delta_context_lines(delta: dict[str, Any] | None) -> list[str]:
    if not isinstance(delta, dict):
        return []
    packet = delta.get("delta_packet") if isinstance(delta.get("delta_packet"), dict) else {}
    if not packet:
        return []
    lines: list[str] = []
    headline = _context_safe_text(packet.get("headline"))
    action = _context_safe_text(packet.get("action"))
    months = ", ".join(_context_safe_text(item) for item in packet.get("touched_months") or [] if _context_safe_text(item))
    sections = ", ".join(_context_safe_text(item) for item in packet.get("invalidated_sections") or [] if _context_safe_text(item))
    categories: list[str] = []
    for item in packet.get("category_change_summary") or []:
        if not isinstance(item, dict):
            continue
        categories.extend(item.get("added") or [])
        categories.extend(item.get("changed") or [])
        categories.extend(item.get("removed") or [])
    merchants: list[str] = []
    for item in packet.get("merchant_change_summary") or []:
        if not isinstance(item, dict):
            continue
        merchants.extend(item.get("added") or [])
        merchants.extend(item.get("changed") or [])
        merchants.extend(item.get("removed") or [])
    category_text = ", ".join(dict.fromkeys(_context_safe_text(item) for item in categories if _context_safe_text(item)))
    merchant_text = ", ".join(dict.fromkeys(_context_safe_text(item) for item in merchants if _context_safe_text(item)))
    if delta.get("generated_at"):
        lines.append(f"Stored delta generated: {_context_safe_text(delta.get('generated_at'))}")
    if headline:
        lines.append(f"Headline: {headline}")
    if action:
        lines.append(f"Action: {action}")
    if months:
        lines.append(f"Touched months: {months}")
    if sections:
        lines.append(f"Changed sections: {sections}")
    if category_text:
        lines.append(f"Changed categories: {category_text}")
    if merchant_text:
        lines.append(f"Changed merchants: {merchant_text}")
    return [line[:300] for line in lines if line]


def _advisor_context_block(memo: dict[str, Any], *, delta: dict[str, Any] | None = None, max_chars: int | None = None) -> str:
    limit = max(600, int(max_chars if max_chars is not None else _advisor_lens_context_max_chars()))
    memo_text = _context_safe_text(memo.get("memo_markdown"))
    theses = [item for item in memo.get("theses") or [] if isinstance(item, dict)]
    action_plan = build_advisor_ranked_actions(memo, max_items=5)
    thesis_lines: list[str] = []
    for thesis in theses[:10]:
        summary = _context_safe_text(thesis.get("summary"))
        caveat = _context_safe_text(thesis.get("caveat"))
        if not summary:
            continue
        line = f"- {summary}"
        if caveat:
            line += f" Caveat: {caveat}"
        thesis_lines.append(line[:260])
    header = (
        "Stored Mira advisor read (validated background analysis; not live recalculation):\n"
        f"Generated: {memo.get('generated_at') or 'unknown'}\n"
        "Use this only for follow-ups about Mira's read, financial portrait, priorities, tradeoffs, or practical levers. "
        "Do not invent new amounts, dates, merchants, categories, or recommendations beyond this block. "
        "If the user asks for fresh/current exact totals, rely on live Folio evidence instead.\n"
        "Follow-up answer guidance: answer directly with no conversational opener; do not say Hey, TL;DR, no sweat, don't sweat, trimming the fat, or fun outlier. "
        "For focus/risk questions, liquidity is not the main risk; state that cash/liquidity is not the concern, then name income continuity, the fixed monthly floor, and low-pain tune-ups. "
        "For reduce-without-pain questions, say fees, Amazon-style small purchases, and GEICO/vendor review come before broad category cuts. "
        "For travel/event questions, say not to overreact because the event is separated from the normal baseline. "
        "For biggest-risk questions, include the caveat that missing goals, budgets, labels, or stale sync can change the recommendation.\n"
    )
    guidance = (
        "Follow-up answer guidance:\n"
        "- Use 2-3 compact bullets or short sentences. No conversational opener.\n"
        "- Answer directly and skip generic openers or slang such as Hey, highlights reel, TL;DR, no sweat, don't sweat, trimming the fat, or fun outlier.\n"
        "- If the question asks what to focus on, begin with: Cash is not the concern; then name the real focus.\n"
        "- If the question asks both what to focus on and what not to overreact to, include one sentence beginning: Do not overreact to.\n"
        "- If the question asks about a travel/event month, begin with: Do not overreact to the travel month; then explain baseline separation.\n"
        "- For priorities/focus, state in the first sentence that cash/liquidity is not the main risk; income continuity, the fixed monthly floor, and practical tune-ups matter more.\n"
        "- For travel/event spend, explicitly say not to overreact; separate the event from the normal baseline and avoid treating it as lifestyle drift until confirmed.\n"
        "- For reduce-without-pain questions, explicitly say fees, Amazon-style small purchases, and GEICO/vendor review come before broad category cuts. Include the sentence: These come before broad category cuts.\n"
        "- For sensitive private rhythms, use soft-ceiling/tuning language; do not call it a habit, joke about it, moralize it, or say to ignore it.\n"
        "- For biggest-risk questions, lead with income continuity against the fixed monthly floor, then include the caveat that missing goals, budgets, or stale sync can change the recommendation."
    )
    pieces = [header]
    delta_lines = _advisor_delta_context_lines(delta)
    if delta_lines:
        pieces.append(
            "Latest stored delta since this read:\n"
            + "\n".join(delta_lines)
            + "\nUse this for questions like what changed since the read or whether the current-month sections need an update."
        )
    if memo_text:
        pieces.append("Memo excerpt:\n" + memo_text[: max(300, limit // 3)].strip())
    if action_plan:
        action_lines = []
        for action in action_plan:
            title = _context_safe_text(action.get("title"))
            why = _context_safe_text(action.get("why"))
            next_step = _context_safe_text(action.get("action"))
            tradeoff = _context_safe_text(action.get("tradeoff"))
            if title and next_step:
                action_lines.append(
                    f"{action.get('rank')}. {title}: {next_step} Why: {why} Tradeoff: {tradeoff}".strip()
                )
        if action_lines:
            pieces.append("Action order:\n" + "\n".join(action_lines))
    if thesis_lines:
        pieces.append("Validated thesis summaries:\n" + "\n".join(thesis_lines))
    pieces.append(guidance)
    block = "\n\n".join(pieces)
    if len(block) > limit:
        block = block[:limit].rsplit("\n", 1)[0].strip()
    return block


def _context_safe_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"\b(?:metric|txn):[A-Za-z0-9_.:-]+\b", "", text)
    text = re.sub(r"\bevidence_ids?\b", "sources", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _empty_answer_context(reason: str) -> dict[str, Any]:
    return {"block": "", "used": False, "count": 0, "reason": reason}


def _advisor_lens_context_max_chars() -> int:
    try:
        return max(600, int(os.getenv("MIRA_ADVISOR_LENS_CONTEXT_MAX_CHARS", str(ADVISOR_LENS_CONTEXT_MAX_CHARS))))
    except (TypeError, ValueError):
        return ADVISOR_LENS_CONTEXT_MAX_CHARS


def _advisor_lens_post_rewarm_max_tokens() -> int:
    try:
        return max(1, min(int(os.getenv("MIRA_ADVISOR_LENS_POST_REWARM_MAX_TOKENS", "8")), 64))
    except (TypeError, ValueError):
        return 8


def _advisor_lens_post_rewarm_purposes() -> list[str]:
    raw = os.getenv("MIRA_ADVISOR_LENS_POST_REWARM_PURPOSES", "controller")
    allowed = {"controller", "copilot"}
    purposes: list[str] = []
    for item in str(raw or "").split(","):
        purpose = item.strip().lower()
        if purpose in allowed and purpose not in purposes:
            purposes.append(purpose)
    return purposes or ["controller"]


def _compact_rewarm_call(call: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "provider",
        "purpose",
        "model",
        "stream",
        "max_tokens",
        "prompt_chars",
        "output_chars",
        "first_token_ms",
        "wall_ms",
        "load_duration_ms",
        "prompt_eval_duration_ms",
        "eval_duration_ms",
        "total_duration_ms",
        "prompt_eval_count",
        "eval_count",
        "done_reason",
        "error",
    )
    return {key: call.get(key) for key in allowed if key in call}


def _advisor_lens_min_interval_minutes() -> int:
    try:
        return max(1, int(os.getenv("MIRA_ADVISOR_LENS_MIN_INTERVAL_MINUTES", str(ADVISOR_LENS_MIN_INTERVAL_MINUTES))))
    except (TypeError, ValueError):
        return ADVISOR_LENS_MIN_INTERVAL_MINUTES


__all__ = [
    "ADVISOR_LENS_SYNTHESIS_VERSION",
    "REQUIRED_THESES",
    "THESIS_CATALOG",
    "advisor_lens_background_auto_decision",
    "advisor_lens_background_auto_enabled",
    "advisor_lens_answer_context",
    "advisor_lens_context_enabled",
    "advisor_lens_memo_delta_status",
    "advisor_lens_post_rewarm_enabled",
    "advisor_lens_synthesis_enabled",
    "advisor_lens_store_enabled",
    "advisor_lens_ui_enabled",
    "build_lens_evidence_bundle",
    "build_lens_evidence_map",
    "build_advisor_read_cards",
    "build_advisor_ranked_actions",
    "clean_memo_text",
    "compose_lens_final_payload",
    "draft_lens_advisor_memo",
    "has_fresh_advisor_lens_memo",
    "list_lens_advisor_memos",
    "merge_lens_theses",
    "rewarm_chat_after_advisor",
    "run_advisor_lens_background_memo",
    "run_advisor_lens_portrait_delta",
    "run_offline_advisor_lens_synthesis",
    "store_lens_advisor_memo",
    "unsupported_numeric_claims",
    "validate_lens_advisor_memo",
]
