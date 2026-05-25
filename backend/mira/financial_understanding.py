"""Evidence-backed financial understanding store for Mira.

Phase 25 is deliberately off the chat hot path. This module turns the existing
background evidence bundle into compact, profile-scoped read-model facts that
later phases can use for lifestyle-aware answers and cards.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha1
from typing import Any, Callable

from mira.background_evidence import build_background_evidence_bundle, bundle_json_bytes


STORE_VERSION = "mira_financial_understanding_v1"
DEFAULT_MAX_FACTS = int(os.getenv("MIRA_FINANCIAL_UNDERSTANDING_MAX_FACTS", "8"))
LLM_MAX_TOKENS = int(os.getenv("MIRA_FINANCIAL_UNDERSTANDING_MAX_TOKENS", "900"))

FACT_FAMILIES = {"lifestyle_profile", "friction_map", "operating_plan"}
SUBJECT_TYPES = {"profile", "category", "merchant", "subscription", "account", "cashflow"}
CONFIDENCE_STATES = {"high", "medium", "low"}
SENSITIVITY_STATES = {"low", "medium", "high"}
STATUS_STATES = {"active", "stale", "dismissed"}

_PRIVATE_TERMS = (
    "run_sql",
    "sql",
    "private tool",
    "backend tool",
    "tool registry",
    "evidence_id",
    "evidence id",
    "deterministic",
    "raw_description",
    "account_number",
    "routing_number",
)
_SHAMING_TERMS = (
    "addict",
    "bad habit",
    "failure",
    "irresponsible",
    "reckless",
    "shame",
    "you failed",
    "you messed up",
)
_NUMERIC_RE = re.compile(r"(?<![A-Za-z])(?:\$?\d[\d,]*(?:\.\d+)?%?)(?![A-Za-z])")

_PROMPT = """You are Mira's offline financial understanding analyst and personal-finance advisor. You run off the chat path.

You have access only to the deterministic evidence bundle below. Treat it as trusted finance-tool output.

Your job is to build an accurate spending persona and practical financial read:
- what the user's lifestyle appears to be from the data;
- where money leaks or creates friction;
- what blocks a better savings rate;
- what operating guardrails would improve the month;
- which improvement themes are most likely to matter.

Hard rules:
- Use ONLY facts present in the evidence bundle.
- Do not run SQL, request tools, infer from missing data, or inspect schema.
- Do not compute new totals, averages, deltas, dates, balances, or percentages.
- Every fact must cite evidence_id values from the bundle.
- Do not create action candidates, direct write suggestions, or instructions to move/cancel/edit anything.
- Be honest and specific, but never shaming or moralizing.
- Suppress weak, obvious, stale, or non-actionable observations.
- Visible summaries must be compact and free of internal source names.
- You may suggest improvement themes only as read-only guidance grounded in cited evidence.

Return JSON only:
{{
  "facts": [
    {{
      "fact_family": "lifestyle_profile|friction_map|operating_plan",
      "kind": "short enum",
      "subject_type": "profile|category|merchant|subscription|account|cashflow",
      "subject_key": "canonical key",
      "summary": "one compact sentence",
      "numbers": {{}},
      "traits": ["short_enum"],
      "improvement_theme": "optional compact read-only theme",
      "time_scope": "current_month|last_90d|last_6_months|next_30d",
      "confidence": "high|medium|low",
      "sensitivity": "low|medium|high",
      "valid_until": "YYYY-MM-DD",
      "evidence_ids": ["..."],
      "suppress_reason": ""
    }}
  ]
}}

Evidence bundle JSON:
{bundle_json}

JSON:"""


def run_financial_understanding(
    *,
    profile: str | None = None,
    conn: sqlite3.Connection | None = None,
    bundle: dict[str, Any] | None = None,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Build, validate, and store Phase 25 facts.

    This does not schedule itself and does not alter chat prompts.
    """

    if not _enabled():
        return {"status": "disabled", "stored_count": 0, "rejected_count": 0}

    if conn is None:
        from database import get_db

        with get_db() as c:
            return run_financial_understanding(profile=profile, conn=c, bundle=bundle, complete_fn=complete_fn)

    _ensure_tables(conn)
    started = time.perf_counter()
    started_at = _now_iso()
    evidence_bundle = bundle or build_background_evidence_bundle(profile=profile, conn=conn)

    deterministic = derive_deterministic_facts(evidence_bundle, max_facts=DEFAULT_MAX_FACTS)
    llm_result = draft_financial_understanding_facts(evidence_bundle, complete_fn=complete_fn)
    proposed = [*deterministic, *(llm_result.get("facts") or [])]
    validation = validate_financial_facts(evidence_bundle, proposed, max_facts=DEFAULT_MAX_FACTS)
    stored = store_financial_facts(conn=conn, profile=profile, facts=validation["accepted"], bundle=evidence_bundle)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    status = "stored" if stored else "no_accepted_facts"
    if llm_result.get("status") in {"disabled", "llm_unavailable", "llm_error"} and deterministic:
        status = "stored_deterministic"
    run_id = log_understanding_run(
        conn=conn,
        profile=profile,
        started_at=started_at,
        input_bundle_version=str(evidence_bundle.get("version") or ""),
        fact_count=len(stored),
        rejected_count=len(validation["rejected"]) + len(llm_result.get("rejected") or []),
        latency_ms=latency_ms,
        status=status,
        error=";".join(llm_result.get("errors") or []),
    )
    return {
        "status": status,
        "run_id": run_id,
        "stored_count": len(stored),
        "rejected_count": len(validation["rejected"]) + len(llm_result.get("rejected") or []),
        "deterministic_count": len(deterministic),
        "llm_count": len(llm_result.get("facts") or []),
        "llm_status": llm_result.get("status") or "disabled",
        "bundle_meta": _bundle_meta(evidence_bundle),
        "latency_ms": latency_ms,
    }


def derive_deterministic_facts(bundle: dict[str, Any], *, max_facts: int = DEFAULT_MAX_FACTS) -> list[dict[str, Any]]:
    """Create conservative facts directly from deterministic evidence."""

    facts: list[dict[str, Any]] = []
    as_of = _bundle_date(bundle)
    by_kind = _facts_by_kind(bundle)

    month_summary = _first(by_kind, "dashboard_month_summary")
    if month_summary:
        values = _values(month_summary)
        income = _num(values.get("income"))
        expenses = _num(values.get("expenses"))
        net = _num(values.get("net"))
        savings_rate = _num(values.get("savings_rate"))
        if savings_rate == 0 and income:
            savings_rate = round((income - expenses) / income, 4)
        traits = ["positive_cashflow" if net >= 0 else "negative_cashflow"]
        if savings_rate >= 0.2:
            traits.append("strong_savings_rate")
        elif savings_rate < 0:
            traits.append("savings_pressure")
        facts.append(
            _fact(
                bundle,
                fact_family="lifestyle_profile",
                kind="savings_rate_pattern",
                subject_type="profile",
                subject_key="profile",
                summary="Recent cash flow is part of Mira's lifestyle read.",
                numbers={"income": income, "expenses": expenses, "net": net, "savings_rate": savings_rate},
                traits=traits,
                evidence_ids=[month_summary["evidence_id"]],
                confidence=month_summary.get("confidence") or "high",
                valid_until=_valid_until(as_of, "lifestyle_profile"),
                time_scope="current_month",
            )
        )

    recurring = _first(by_kind, "recurring_summary")
    if recurring:
        values = _values(recurring)
        recurring_total = _num(values.get("total_monthly"))
        income = _num(_values(month_summary).get("income")) if month_summary else 0.0
        ratio = round(recurring_total / income, 4) if income > 0 else 0.0
        facts.append(
            _fact(
                bundle,
                fact_family="lifestyle_profile",
                kind="recurring_obligation_baseline",
                subject_type="subscription",
                subject_key="recurring_baseline",
                summary="Recurring obligations are a visible part of the monthly baseline.",
                numbers={"recurring_monthly": recurring_total, "income": income, "recurring_to_income_ratio": ratio},
                traits=["recurring_baseline"],
                evidence_ids=[recurring["evidence_id"]],
                confidence=recurring.get("confidence") or "medium",
                valid_until=_valid_until(as_of, "lifestyle_profile"),
                improvement_theme="review_recurring_baseline",
                time_scope="last_90d",
            )
        )

    plan = _first(by_kind, "plan_snapshot")
    if plan:
        values = _values(plan)
        facts.append(
            _fact(
                bundle,
                fact_family="operating_plan",
                kind="variable_spend_guardrail",
                subject_type="cashflow",
                subject_key=str(values.get("month") or "current_month"),
                summary="The current plan can anchor a weekly spending guardrail.",
                numbers=_pick_numbers(values, ("safe_to_spend", "safe_to_spend_limit", "safe_to_spend_spent", "remaining", "active_goal_count")),
                traits=["guardrail_available"],
                evidence_ids=[plan["evidence_id"]],
                confidence=plan.get("confidence") or "medium",
                valid_until=_valid_until(as_of, "operating_plan"),
                improvement_theme="use_variable_spend_guardrail",
                time_scope="current_month",
            )
        )

    cashflow = _first(by_kind, "cashflow_shortfall")
    if cashflow:
        values = _values(cashflow)
        if values.get("has_shortfall_risk") or values.get("projected_low_point"):
            facts.append(
                _fact(
                    bundle,
                    fact_family="operating_plan",
                    kind="cash_low_point_radar",
                    subject_type="cashflow",
                    subject_key="forecast_low_point",
                    summary="Cash-flow forecast should be watched before extra spending.",
                    numbers=_pick_numbers(values, ("has_shortfall_risk",)),
                    traits=["cashflow_radar"],
                    evidence_ids=[cashflow["evidence_id"]],
                    confidence=cashflow.get("confidence") or "medium",
                    sensitivity="medium",
                    valid_until=_valid_until(as_of, "operating_plan"),
                    improvement_theme="check_cash_low_point",
                    time_scope="next_30d",
                )
            )

    for category in sorted(by_kind.get("category_current_spend", []), key=lambda f: _num(_values(f).get("total")), reverse=True)[:2]:
        values = _values(category)
        name = str(values.get("category") or "").strip()
        if not name:
            continue
        facts.append(
            _fact(
                bundle,
                fact_family="friction_map",
                kind="category_concentration",
                subject_type="category",
                subject_key=_key(name),
                summary=f"{_safe_subject(name)} is one of the current major spending areas.",
                numbers=_pick_numbers(values, ("total", "gross", "refunds", "percent")),
                traits=["category_concentration"],
                evidence_ids=[category["evidence_id"]],
                confidence=category.get("confidence") or "high",
                sensitivity=_sensitivity_for_subject(name),
                valid_until=_valid_until(as_of, "friction_map"),
                improvement_theme="review_category_concentration",
                time_scope="current_month",
            )
        )

    for merchant in sorted(by_kind.get("merchant_current_spend", []), key=lambda f: _num(_values(f).get("transaction_count")), reverse=True)[:2]:
        values = _values(merchant)
        count = _num(values.get("transaction_count"))
        name = str(values.get("name") or "").strip()
        if not name or count < 2:
            continue
        facts.append(
            _fact(
                bundle,
                fact_family="friction_map",
                kind="merchant_repeat",
                subject_type="merchant",
                subject_key=_key(name),
                summary=f"{_safe_subject(name)} is a repeated merchant in the current data.",
                numbers=_pick_numbers(values, ("total_spent", "transaction_count")),
                traits=["merchant_repeat"],
                evidence_ids=[merchant["evidence_id"]],
                confidence=merchant.get("confidence") or "medium",
                sensitivity=_sensitivity_for_subject(name),
                valid_until=_valid_until(as_of, "friction_map"),
                improvement_theme="review_repeat_merchant",
                time_scope="current_month",
            )
        )

    return facts[: max(0, max_facts)]


def draft_financial_understanding_facts(
    bundle: dict[str, Any],
    *,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Ask the optional background LLM for additional structured facts."""

    if not _llm_enabled() and complete_fn is None:
        return {"status": "disabled", "facts": [], "rejected": [], "errors": []}
    if not (bundle.get("facts") or bundle.get("candidate_signals")):
        return {"status": "no_evidence", "facts": [], "rejected": [], "errors": []}
    prompt = build_financial_understanding_prompt(bundle)
    try:
        if complete_fn is not None:
            raw = complete_fn(prompt, LLM_MAX_TOKENS, "copilot", response_format="json")
        else:
            import llm_client

            if not llm_client.is_available():
                return {"status": "llm_unavailable", "facts": [], "rejected": [], "errors": []}
            raw = llm_client.complete(
                prompt,
                max_tokens=LLM_MAX_TOKENS,
                purpose="copilot",
                response_format="json",
            )
    except Exception as exc:
        return {"status": "llm_error", "facts": [], "rejected": [], "errors": [f"{type(exc).__name__}: {exc}"]}
    parsed = parse_financial_understanding_output(raw, bundle)
    parsed["status"] = "ok" if parsed["facts"] else "no_facts"
    return parsed


def build_financial_understanding_prompt(bundle: dict[str, Any]) -> str:
    compact_json = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _PROMPT.format(bundle_json=compact_json)


def parse_financial_understanding_output(raw: str, bundle: dict[str, Any]) -> dict[str, Any]:
    rejected: list[dict[str, Any]] = []
    try:
        payload = json.loads(raw or "{}")
    except Exception as exc:
        return {"facts": [], "rejected": [], "errors": [f"invalid_json:{type(exc).__name__}"], "raw_excerpt": str(raw or "")[:240]}
    raw_facts = payload.get("facts") if isinstance(payload, dict) else payload
    if not isinstance(raw_facts, list):
        return {"facts": [], "rejected": [{"reason": "facts_not_list"}], "errors": []}

    facts: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_facts[: DEFAULT_MAX_FACTS]):
        if not isinstance(item, dict):
            rejected.append({"index": idx, "reason": "fact_not_object"})
            continue
        suppress = _clean_text(item.get("suppress_reason"), 220)
        if suppress:
            rejected.append({"index": idx, "reason": f"suppressed:{suppress}"})
            continue
        fact = _clean_llm_fact(item, bundle)
        facts.append(fact)
    validation = validate_financial_facts(bundle, facts, max_facts=DEFAULT_MAX_FACTS)
    return {"facts": validation["accepted"], "rejected": [*rejected, *validation["rejected"]], "errors": []}


def validate_financial_facts(bundle: dict[str, Any], facts: list[dict[str, Any]], *, max_facts: int = DEFAULT_MAX_FACTS) -> dict[str, Any]:
    evidence_ids = _evidence_ids(bundle)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for idx, fact in enumerate(facts[: max(0, max_facts)]):
        reason = _fact_rejection_reason(fact, evidence_ids)
        if reason:
            rejected.append({"index": idx, "reason": reason, "kind": fact.get("kind") or ""})
            continue
        accepted.append(_normalize_fact(fact, bundle))
    return {"accepted": accepted, "rejected": rejected}


def store_financial_facts(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    facts: list[dict[str, Any]],
    bundle: dict[str, Any],
) -> list[dict[str, Any]]:
    _ensure_tables(conn)
    scope = _profile_scope(profile)
    stored: list[dict[str, Any]] = []
    fingerprints: list[str] = []
    for fact in facts:
        normalized = _normalize_fact(fact, bundle)
        normalized["fingerprint"] = _fingerprint(scope, normalized)
        fingerprint = normalized["fingerprint"]
        fingerprints.append(fingerprint)
        conn.execute(
            """
            INSERT INTO mira_financial_facts (
                profile_id, fact_family, kind, subject_type, subject_key, summary,
                numbers_json, traits_json, evidence_json, confidence, sensitivity,
                valid_until, status, fingerprint, generated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, datetime('now'))
            ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
                summary = excluded.summary,
                numbers_json = excluded.numbers_json,
                traits_json = excluded.traits_json,
                evidence_json = excluded.evidence_json,
                confidence = excluded.confidence,
                sensitivity = excluded.sensitivity,
                valid_until = excluded.valid_until,
                status = 'active',
                generated_at = datetime('now')
            """,
            (
                scope,
                normalized["fact_family"],
                normalized["kind"],
                normalized["subject_type"],
                normalized["subject_key"],
                normalized["summary"],
                json.dumps(normalized.get("numbers") or {}, ensure_ascii=True, sort_keys=True),
                json.dumps(normalized.get("traits") or [], ensure_ascii=True, sort_keys=True),
                json.dumps(normalized.get("evidence") or {}, ensure_ascii=True, sort_keys=True),
                normalized["confidence"],
                normalized["sensitivity"],
                normalized.get("valid_until"),
                fingerprint,
            ),
        )
        stored.append(normalized)
    if fingerprints:
        placeholders = ",".join("?" for _ in fingerprints)
        conn.execute(
            f"""
            UPDATE mira_financial_facts
               SET status = 'stale'
             WHERE profile_id = ?
               AND status = 'active'
               AND fingerprint NOT IN ({placeholders})
            """,
            [scope, *fingerprints],
        )
    conn.commit()
    return stored


def list_financial_facts(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    include_stale: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    _ensure_tables(conn)
    scope = _profile_scope(profile)
    where = "profile_id = ?"
    params: list[Any] = [scope]
    if not include_stale:
        where += " AND status = 'active'"
    rows = conn.execute(
        f"""
        SELECT *
          FROM mira_financial_facts
         WHERE {where}
         ORDER BY generated_at DESC, id DESC
         LIMIT ?
        """,
        [*params, max(1, int(limit))],
    ).fetchall()
    return [_public_fact(dict(row)) for row in rows]


def log_understanding_run(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    started_at: str,
    input_bundle_version: str,
    fact_count: int,
    rejected_count: int,
    latency_ms: float,
    status: str,
    error: str = "",
    run_type: str = "manual",
    model_name: str = "",
) -> int:
    _ensure_tables(conn)
    scope = _profile_scope(profile)
    cur = conn.execute(
        """
        INSERT INTO mira_financial_understanding_runs (
            profile_id, run_type, started_at, finished_at, input_bundle_version,
            fact_count, rejected_count, model_name, latency_ms, status, error
        )
        VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
        """,
        (scope, run_type, started_at, input_bundle_version, fact_count, rejected_count, model_name, latency_ms, status, error[:500]),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_understanding_run(conn: sqlite3.Connection, profile: str | None = None) -> dict[str, Any] | None:
    _ensure_tables(conn)
    row = conn.execute(
        """
        SELECT *
          FROM mira_financial_understanding_runs
         WHERE profile_id = ?
         ORDER BY started_at DESC, id DESC
         LIMIT 1
        """,
        (_profile_scope(profile),),
    ).fetchone()
    return dict(row) if row else None


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_financial_facts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id      TEXT DEFAULT NULL,
            fact_family     TEXT NOT NULL CHECK(fact_family IN ('lifestyle_profile', 'friction_map', 'operating_plan')),
            kind            TEXT NOT NULL,
            subject_type    TEXT NOT NULL CHECK(subject_type IN ('profile', 'category', 'merchant', 'subscription', 'account', 'cashflow')),
            subject_key     TEXT NOT NULL DEFAULT '',
            summary         TEXT NOT NULL,
            numbers_json    TEXT NOT NULL DEFAULT '{}',
            traits_json     TEXT NOT NULL DEFAULT '[]',
            evidence_json   TEXT NOT NULL DEFAULT '{}',
            confidence      TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
            sensitivity     TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
            generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            valid_until     TEXT DEFAULT NULL,
            status          TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'stale', 'dismissed')),
            fingerprint     TEXT NOT NULL,
            UNIQUE(profile_id, fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_financial_facts_profile_status
            ON mira_financial_facts(profile_id, status, fact_family, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_financial_facts_subject
            ON mira_financial_facts(profile_id, subject_type, subject_key, status);
        CREATE INDEX IF NOT EXISTS idx_mira_financial_facts_valid_until
            ON mira_financial_facts(valid_until) WHERE valid_until IS NOT NULL AND status = 'active';

        CREATE TABLE IF NOT EXISTS mira_financial_understanding_runs (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id              TEXT DEFAULT NULL,
            run_type                TEXT NOT NULL DEFAULT 'manual',
            started_at              TEXT NOT NULL,
            finished_at             TEXT DEFAULT NULL,
            input_bundle_version    TEXT NOT NULL DEFAULT '',
            fact_count              INTEGER NOT NULL DEFAULT 0,
            rejected_count          INTEGER NOT NULL DEFAULT 0,
            model_name              TEXT NOT NULL DEFAULT '',
            latency_ms              REAL NOT NULL DEFAULT 0,
            status                  TEXT NOT NULL DEFAULT 'ok',
            error                   TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_mira_financial_understanding_runs_profile
            ON mira_financial_understanding_runs(profile_id, started_at DESC);
        """
    )


def _fact(
    bundle: dict[str, Any],
    *,
    fact_family: str,
    kind: str,
    subject_type: str,
    subject_key: str,
    summary: str,
    numbers: dict[str, Any],
    traits: list[str],
    evidence_ids: list[str],
    confidence: str,
    valid_until: str,
    sensitivity: str = "low",
    improvement_theme: str = "",
    time_scope: str = "",
) -> dict[str, Any]:
    return {
        "fact_family": fact_family,
        "kind": kind,
        "subject_type": subject_type,
        "subject_key": subject_key,
        "summary": summary,
        "numbers": numbers,
        "traits": traits,
        "evidence": {
            "bundle_version": bundle.get("version") or "",
            "evidence_ids": evidence_ids,
            "improvement_theme": improvement_theme,
            "time_scope": time_scope,
        },
        "confidence": _confidence(confidence),
        "sensitivity": _sensitivity(sensitivity),
        "valid_until": valid_until,
    }


def _clean_llm_fact(item: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    evidence_ids = [str(v).strip() for v in (item.get("evidence_ids") or []) if str(v).strip()][:6]
    return _fact(
        bundle,
        fact_family=str(item.get("fact_family") or "").strip(),
        kind=_enumish(item.get("kind")),
        subject_type=str(item.get("subject_type") or "").strip(),
        subject_key=_key(item.get("subject_key") or item.get("kind") or "profile"),
        summary=_clean_text(item.get("summary"), 220),
        numbers=_scalar_dict(item.get("numbers") if isinstance(item.get("numbers"), dict) else {}),
        traits=_traits(item.get("traits")),
        evidence_ids=evidence_ids,
        confidence=str(item.get("confidence") or "medium"),
        sensitivity=str(item.get("sensitivity") or "low"),
        valid_until=str(item.get("valid_until") or _valid_until(_bundle_date(bundle), str(item.get("fact_family") or ""))),
        improvement_theme=_enumish(item.get("improvement_theme")),
        time_scope=_enumish(item.get("time_scope")),
    )


def _normalize_fact(fact: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    evidence = fact.get("evidence") if isinstance(fact.get("evidence"), dict) else {}
    evidence_ids = evidence.get("evidence_ids") if isinstance(evidence.get("evidence_ids"), list) else fact.get("evidence_ids")
    evidence_ids = [str(v).strip() for v in (evidence_ids or []) if str(v).strip()]
    normalized = {
        "fact_family": str(fact.get("fact_family") or "").strip(),
        "kind": _enumish(fact.get("kind")),
        "subject_type": str(fact.get("subject_type") or "").strip(),
        "subject_key": _key(fact.get("subject_key") or fact.get("kind") or "profile"),
        "summary": _clean_text(fact.get("summary"), 240),
        "numbers": _scalar_dict(fact.get("numbers") if isinstance(fact.get("numbers"), dict) else {}),
        "traits": _traits(fact.get("traits")),
        "evidence": {
            "bundle_version": bundle.get("version") or evidence.get("bundle_version") or "",
            "evidence_ids": evidence_ids,
            "improvement_theme": _enumish(evidence.get("improvement_theme") or fact.get("improvement_theme")),
            "time_scope": _enumish(evidence.get("time_scope") or fact.get("time_scope")),
        },
        "confidence": _confidence(fact.get("confidence")),
        "sensitivity": _sensitivity(fact.get("sensitivity")),
        "valid_until": str(fact.get("valid_until") or _valid_until(_bundle_date(bundle), str(fact.get("fact_family") or ""))),
    }
    normalized["fingerprint"] = _fingerprint(str(bundle.get("profile_scope") or ""), normalized)
    return normalized


def _fact_rejection_reason(fact: dict[str, Any], evidence_ids: set[str]) -> str:
    family = str(fact.get("fact_family") or "")
    subject_type = str(fact.get("subject_type") or "")
    summary = str(fact.get("summary") or "")
    if family not in FACT_FAMILIES:
        return "invalid_fact_family"
    if subject_type not in SUBJECT_TYPES:
        return "invalid_subject_type"
    if not fact.get("kind") or not summary:
        return "missing_kind_or_summary"
    evidence = fact.get("evidence") if isinstance(fact.get("evidence"), dict) else {}
    ids = evidence.get("evidence_ids") if isinstance(evidence.get("evidence_ids"), list) else fact.get("evidence_ids")
    ids = [str(v).strip() for v in (ids or []) if str(v).strip()]
    if not ids:
        return "missing_evidence_ids"
    if any(value not in evidence_ids for value in ids):
        return "unknown_evidence_id"
    lowered = " ".join([summary, str(evidence.get("improvement_theme") or "")]).lower()
    if any(term in lowered for term in _PRIVATE_TERMS):
        return "internal_or_private_term"
    if any(term in lowered for term in _SHAMING_TERMS):
        return "shaming_language"
    cited_numbers = _numbers_from_values([fact.get("numbers") or {}])
    unsupported = [n for n in _numbers_from_text(summary) if n not in cited_numbers]
    if unsupported:
        return "unsupported_numeric_claim"
    if _word_count(summary) > 28:
        return "summary_too_long"
    return ""


def _public_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "numbers": _json_load(row.get("numbers_json"), {}),
        "traits": _json_load(row.get("traits_json"), []),
        "evidence": _json_load(row.get("evidence_json"), {}),
    }


def _facts_by_kind(bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for fact in bundle.get("facts") or []:
        if isinstance(fact, dict):
            out.setdefault(str(fact.get("kind") or ""), []).append(fact)
    return out


def _first(by_kind: dict[str, list[dict[str, Any]]], kind: str) -> dict[str, Any] | None:
    rows = by_kind.get(kind) or []
    return rows[0] if rows else None


def _values(fact: dict[str, Any] | None) -> dict[str, Any]:
    values = fact.get("values") if isinstance(fact, dict) and isinstance(fact.get("values"), dict) else {}
    return values


def _pick_numbers(values: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = round(float(value), 4)
    return out


def _scalar_dict(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            out[_enumish(key)] = value
        elif isinstance(value, (int, float)):
            out[_enumish(key)] = round(float(value), 4)
        elif isinstance(value, str) and len(value) <= 80:
            out[_enumish(key)] = value
    return out


def _traits(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out = []
    for value in values[:8]:
        text = _enumish(value)
        if text and text not in out:
            out.append(text)
    return out


def _evidence_ids(bundle: dict[str, Any]) -> set[str]:
    ids = set()
    for item in [*(bundle.get("facts") or []), *(bundle.get("candidate_signals") or [])]:
        if isinstance(item, dict) and item.get("evidence_id"):
            ids.add(str(item["evidence_id"]))
    return ids


def _bundle_date(bundle: dict[str, Any]) -> date:
    raw = ((bundle.get("periods") if isinstance(bundle.get("periods"), dict) else {}) or {}).get("as_of")
    try:
        return datetime.fromisoformat(str(raw)).date()
    except Exception:
        return date.today()


def _valid_until(as_of: date, family: str) -> str:
    if family == "operating_plan":
        return (as_of + timedelta(days=14)).isoformat()
    if family == "friction_map":
        return (as_of + timedelta(days=45)).isoformat()
    return (as_of + timedelta(days=90)).isoformat()


def _fingerprint(scope: str, fact: dict[str, Any]) -> str:
    evidence = fact.get("evidence") if isinstance(fact.get("evidence"), dict) else {}
    seed = json.dumps(
        {
            "scope": scope,
            "family": fact.get("fact_family"),
            "kind": fact.get("kind"),
            "subject_type": fact.get("subject_type"),
            "subject_key": fact.get("subject_key"),
            "evidence_ids": evidence.get("evidence_ids") or [],
        },
        sort_keys=True,
    )
    return f"{scope}:financial_understanding:{sha1(seed.encode('utf-8')).hexdigest()[:16]}"


def _profile_scope(profile: str | None) -> str:
    return str(profile or "household").strip() or "household"


def _enabled() -> bool:
    return os.getenv("MIRA_FINANCIAL_UNDERSTANDING_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _llm_enabled() -> bool:
    return os.getenv("MIRA_FINANCIAL_UNDERSTANDING_LLM_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


def _confidence(value: Any) -> str:
    text = str(value or "medium").strip().lower()
    return text if text in CONFIDENCE_STATES else "medium"


def _sensitivity(value: Any) -> str:
    text = str(value or "low").strip().lower()
    return text if text in SENSITIVITY_STATES else "low"


def _sensitivity_for_subject(value: str) -> str:
    lowered = value.lower()
    if any(term in lowered for term in ("vaping", "tobacco", "alcohol", "gambling", "medical", "debt")):
        return "high"
    if any(term in lowered for term in ("rent", "housing", "loan", "insurance")):
        return "medium"
    return "low"


def _safe_subject(value: str) -> str:
    text = " ".join(str(value or "").split())[:70]
    return text or "This area"


def _key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:80] or "profile"


def _enumish(value: Any) -> str:
    return _key(value)


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _num(value: Any) -> float:
    try:
        return round(float(value or 0), 4)
    except (TypeError, ValueError):
        return 0.0


def _numbers_from_text(text: str) -> set[str]:
    return {_normalize_number(match.group(0)) for match in _NUMERIC_RE.finditer(text or "")}


def _numbers_from_values(values: list[Any]) -> set[str]:
    numbers: set[str] = set()
    for value in values:
        numbers.update(_numbers_from_text(json.dumps(value, sort_keys=True)))
    return numbers


def _normalize_number(value: str) -> str:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _json_load(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return default


def _word_count(value: Any) -> int:
    return len(str(value or "").split())


def _bundle_meta(bundle: dict[str, Any]) -> dict[str, Any]:
    meta = bundle.get("meta") if isinstance(bundle.get("meta"), dict) else {}
    return {
        "version": bundle.get("version") or "",
        "fact_count": int(meta.get("fact_count") or len(bundle.get("facts") or [])),
        "candidate_signal_count": int(meta.get("candidate_signal_count") or len(bundle.get("candidate_signals") or [])),
        "json_bytes": int(meta.get("json_bytes") or bundle_json_bytes(bundle)),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
