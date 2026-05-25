"""Offline Mira analyst drafts over deterministic background evidence."""

from __future__ import annotations

import json
import os
import re
from hashlib import sha1
from typing import Any, Callable

import proactive_insights
from mira.background_evidence import build_background_evidence_bundle, bundle_json_bytes


CLAIM_VALIDATOR_VERSION = "mira_background_claim_validator_v1"
BACKGROUND_ANALYST_MAX_TOKENS = int(os.getenv("MIRA_BACKGROUND_ANALYST_MAX_TOKENS", "900"))
BACKGROUND_ANALYST_MAX_DRAFTS = int(os.getenv("MIRA_BACKGROUND_ANALYST_MAX_DRAFTS", "3"))
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}
_ALLOWED_TONES = {"heads_up", "calm", "encouraging", "celebrate", "check_in"}
_PRIVATE_SURFACE_TERMS = ("run_sql", "sql query", "private tool", "backend tool", "tool registry")
_VISIBLE_INTERNAL_TERMS = (
    "background_mira_analyst",
    "deterministic",
    "evidence bundle",
    "evidence id",
    "evidence_id",
    "proactive_insights",
    "signal.",
    "validator",
)
_ALARMIST_TERMS = (
    "catastrophe",
    "disaster",
    "doomed",
    "failure",
    "irresponsible",
    "reckless",
    "shame",
    "terrible",
    "you failed",
    "you messed up",
)
_NUMERIC_CLAIM_RE = re.compile(r"(?<![A-Za-z])(?:\$?\d[\d,]*(?:\.\d+)?%?)(?![A-Za-z])")
_VISIBLE_EVIDENCE_REF_RE = re.compile(
    r"\s*\((?:signal|fact|goal|merchant|plan|category|recurring|scheduled|cashflow)[A-Za-z0-9_.:-]*\)",
    re.IGNORECASE,
)
_VISIBLE_DOTTED_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_:-]*\.[A-Za-z0-9_.:-]+\b")

_ANALYST_PROMPT = """You are Mira's background analyst. You run off the chat path.

Your job: read a deterministic evidence bundle and draft up to {max_drafts} proactive insight candidates that feel useful, warm, specific, and a little alive.

Mira voice:
- concise, observant, lightly witty when the topic is not stressful;
- no corporate report voice, no robotic source narration;
- no jokes about debt, rent, overdrafts, shortfalls, or financial stress;
- no emojis, no scolding, no moralizing.
- Prefer natural titles like "Amazon Prime may be doubled up",
  "Housing is outrunning the plan", or "Subscription week is getting crowded".
- Avoid report words like "detected", "identified", "projection shows",
  "scheduled to occur", or "deterministic forecast".

Surface only cards that are actionable, surprising, timely, or reassuring.
Suppress bland summaries the user would already know from the dashboard.

Hard rules:
- Use ONLY facts present in the bundle.
- Do not run SQL, ask for tools, or infer from missing data.
- Do not compute new totals, deltas, averages, due dates, balances, or confidence.
- Every draft must cite one or more `evidence_id` values from the bundle.
- If a fact is not clearly supported, suppress it instead of guessing.
- Be calm and non-alarmist. No shame, scolding, or moralizing.
- Do not put evidence IDs, source names, tool names, "deterministic", "bundle",
  "validator", or internal terms in visible title/body/action copy.
- Visible copy must be compact: title <= 9 words, body <= 34 words,
  recommended_action <= 20 words.
- Drafts are not user-visible yet; a validator will check them later.

Return JSON only:
{{
  "drafts": [
    {{
      "title": "short title, <= 9 words",
      "body": "one short sentence, grounded in cited evidence",
      "why_it_matters": "short reason this is worth surfacing",
      "recommended_action": "small next step, no write action",
      "tone": "heads_up|calm|encouraging|celebrate|check_in",
      "confidence": "high|medium|low",
      "evidence_ids": ["evidence.id"],
      "suppress_reason": ""
    }}
  ]
}}

If nothing is worth surfacing, return {{"drafts":[]}}.

Evidence bundle JSON:
{bundle_json}

JSON:"""


def draft_background_insights(
    *,
    profile: str | None = None,
    conn=None,
    bundle: dict[str, Any] | None = None,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Return background analyst drafts without storing or showing them."""
    if not _background_analyst_enabled():
        return _empty_result(status="disabled", bundle=bundle)

    evidence_bundle = bundle or build_background_evidence_bundle(profile=profile, conn=conn)
    if not (evidence_bundle.get("facts") or evidence_bundle.get("candidate_signals")):
        return _empty_result(status="no_evidence", bundle=evidence_bundle)

    prompt = build_background_analyst_prompt(evidence_bundle)
    try:
        if complete_fn is not None:
            raw = complete_fn(prompt, BACKGROUND_ANALYST_MAX_TOKENS, "copilot", response_format="json")
        else:
            import llm_client

            if not llm_client.is_available():
                return _empty_result(status="llm_unavailable", bundle=evidence_bundle)
            raw = llm_client.complete(
                prompt,
                max_tokens=BACKGROUND_ANALYST_MAX_TOKENS,
                purpose="copilot",
                response_format="json",
            )
    except Exception as exc:
        return _empty_result(status="llm_error", bundle=evidence_bundle, errors=[f"{type(exc).__name__}: {exc}"])

    parsed = parse_background_analyst_output(raw, evidence_bundle)
    parsed["status"] = "ok" if parsed["drafts"] else "no_drafts"
    parsed["bundle_meta"] = _bundle_meta(evidence_bundle)
    return parsed


def build_background_analyst_prompt(bundle: dict[str, Any]) -> str:
    compact_json = json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _ANALYST_PROMPT.format(
        max_drafts=max(1, BACKGROUND_ANALYST_MAX_DRAFTS),
        bundle_json=compact_json,
    )


def parse_background_analyst_output(raw: str, bundle: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    rejected: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    try:
        payload = json.loads(raw or "{}")
    except Exception as exc:
        return {
            "drafts": [],
            "suppressed": [],
            "rejected": [],
            "errors": [f"invalid_json:{type(exc).__name__}"],
            "raw_excerpt": str(raw or "")[:240],
        }
    raw_drafts = payload.get("drafts") if isinstance(payload, dict) else payload
    if not isinstance(raw_drafts, list):
        return {"drafts": [], "suppressed": [], "rejected": [], "errors": ["drafts_not_list"]}

    evidence_ids = _bundle_evidence_ids(bundle)
    drafts: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_drafts[: max(1, BACKGROUND_ANALYST_MAX_DRAFTS)]):
        if not isinstance(item, dict):
            rejected.append({"index": idx, "reason": "draft_not_object"})
            continue
        suppress_reason = _clean_text(item.get("suppress_reason"), limit=220)
        if suppress_reason:
            suppressed.append({"index": idx, "reason": suppress_reason})
            continue
        cleaned = _clean_draft(item)
        reason = _draft_rejection_reason(cleaned, evidence_ids)
        if reason:
            rejected.append({"index": idx, "reason": reason, "title": cleaned.get("title") or ""})
            continue
        drafts.append(cleaned)

    return {"drafts": drafts, "suppressed": suppressed, "rejected": rejected, "errors": errors}


def validate_background_drafts(bundle: dict[str, Any], drafts: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate draft claims against deterministic bundle evidence."""
    evidence_by_id = _bundle_evidence_by_id(bundle)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for idx, draft in enumerate(drafts):
        if not isinstance(draft, dict):
            rejected.append({"index": idx, "reason": "draft_not_object"})
            continue
        reason = _claim_rejection_reason(draft, evidence_by_id)
        if reason:
            rejected.append({"index": idx, "reason": reason, "title": draft.get("title") or ""})
            continue
        accepted.append(_draft_to_insight(bundle, draft, evidence_by_id))
    return {"accepted": accepted, "rejected": rejected}


def store_background_analyst_insights(
    *,
    profile: str | None = None,
    conn=None,
    bundle: dict[str, Any] | None = None,
    analyst_result: dict[str, Any] | None = None,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Validate and store background analyst drafts. Does not schedule itself."""
    if not _background_analyst_store_enabled():
        return {"status": "store_disabled", "stored_count": 0, "accepted": [], "rejected": []}
    evidence_bundle = bundle or build_background_evidence_bundle(profile=profile, conn=conn)
    result = analyst_result or draft_background_insights(
        profile=profile,
        conn=conn,
        bundle=evidence_bundle,
        complete_fn=complete_fn,
    )
    validation = validate_background_drafts(evidence_bundle, result.get("drafts") or [])
    if conn is not None:
        for insight in validation["accepted"]:
            proactive_insights.upsert_insight(profile, insight, conn=conn)
    else:
        for insight in validation["accepted"]:
            proactive_insights.upsert_insight(profile, insight)
    return {
        "status": "stored" if validation["accepted"] else "no_accepted_drafts",
        "stored_count": len(validation["accepted"]),
        "accepted": validation["accepted"],
        "rejected": [*(result.get("rejected") or []), *validation["rejected"]],
        "suppressed": result.get("suppressed") or [],
        "errors": result.get("errors") or [],
        "bundle_meta": _bundle_meta(evidence_bundle),
    }


def run_background_mira_analysis(
    *,
    profile: str | None = None,
    conn=None,
    bundle: dict[str, Any] | None = None,
    force: bool = False,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Run the offline analyst once, with a freshness gate around LLM calls."""
    if conn is not None:
        return _run_background_mira_analysis(
            profile=profile,
            conn=conn,
            bundle=bundle,
            force=force,
            complete_fn=complete_fn,
        )

    from database import get_db

    with get_db() as c:
        return _run_background_mira_analysis(
            profile=profile,
            conn=c,
            bundle=bundle,
            force=force,
            complete_fn=complete_fn,
        )


def has_fresh_background_analyst_insight(
    *,
    profile: str | None = None,
    conn=None,
    minutes: int | None = None,
) -> bool:
    """Return true when an active analyst card is fresh enough to reuse."""
    if conn is not None:
        return _has_fresh_background_analyst_insight(conn, profile, minutes=minutes)

    from database import get_db

    with get_db() as c:
        return _has_fresh_background_analyst_insight(c, profile, minutes=minutes)


def background_analyst_auto_decision(
    *,
    profile: str | None = None,
    conn=None,
    minutes: int | None = None,
) -> dict[str, Any]:
    """Return whether an automatic background analyst run should be queued."""
    if not _background_analyst_auto_enabled():
        return {"should_queue": False, "reason": "auto_disabled"}
    if not _background_analyst_enabled():
        return {"should_queue": False, "reason": "analyst_disabled"}
    if not _background_analyst_store_enabled():
        return {"should_queue": False, "reason": "store_disabled"}
    if has_fresh_background_analyst_insight(profile=profile, conn=conn, minutes=minutes):
        if _financial_understanding_needs_refresh(conn=conn, profile=profile):
            return {
                "should_queue": True,
                "reason": "financial_understanding_missing",
                "min_interval_minutes": int(minutes if minutes is not None else _background_analyst_min_interval_minutes()),
            }
        if _advisor_cases_background_auto_enabled() and _advisor_cases_need_refresh(conn=conn, profile=profile):
            return {
                "should_queue": True,
                "reason": "advisor_cases_missing",
                "min_interval_minutes": int(minutes if minutes is not None else _background_analyst_min_interval_minutes()),
            }
        if _advisor_lens_background_auto_enabled() and _advisor_lens_need_refresh(conn=conn, profile=profile):
            return {
                "should_queue": True,
                "reason": "advisor_lens_missing",
                "min_interval_minutes": int(minutes if minutes is not None else _background_analyst_min_interval_minutes()),
            }
        return {"should_queue": False, "reason": "fresh_cache"}
    return {
        "should_queue": True,
        "reason": "stale_or_missing",
        "min_interval_minutes": int(minutes if minutes is not None else _background_analyst_min_interval_minutes()),
    }


def _run_background_mira_analysis(
    *,
    profile: str | None,
    conn,
    bundle: dict[str, Any] | None,
    force: bool,
    complete_fn: Callable[..., str] | None,
) -> dict[str, Any]:
    if not _background_analyst_enabled():
        return {"status": "disabled", "stored_count": 0, "fresh_cache": False}
    evidence_bundle = bundle or build_background_evidence_bundle(profile=profile, conn=conn)
    if not force and _has_fresh_background_analyst_insight(conn, profile):
        return {
            "status": "fresh_cache",
            "stored_count": 0,
            "fresh_cache": True,
            "financial_understanding": _run_financial_understanding_sidecar(
                profile=profile,
                conn=conn,
                bundle=evidence_bundle,
            ),
            "advisor_cases": _run_advisor_cases_sidecar(profile=profile, conn=conn),
            "advisor_lens_memo": _run_advisor_lens_sidecar(profile=profile, conn=conn, complete_fn=complete_fn),
        }
    result = store_background_analyst_insights(profile=profile, conn=conn, bundle=evidence_bundle, complete_fn=complete_fn)
    result["financial_understanding"] = _run_financial_understanding_sidecar(
        profile=profile,
        conn=conn,
        bundle=evidence_bundle,
    )
    result["advisor_cases"] = _run_advisor_cases_sidecar(profile=profile, conn=conn)
    result["advisor_lens_memo"] = _run_advisor_lens_sidecar(profile=profile, conn=conn, complete_fn=complete_fn)
    result["fresh_cache"] = False
    return result


def _run_financial_understanding_sidecar(*, profile: str | None, conn, bundle: dict[str, Any]) -> dict[str, Any]:
    """Populate Mira's financial-understanding read model from the same background bundle."""

    try:
        from mira.financial_understanding import run_financial_understanding

        return run_financial_understanding(profile=profile, conn=conn, bundle=bundle)
    except Exception as exc:
        return {"status": "error", "stored_count": 0, "rejected_count": 0, "error": f"{type(exc).__name__}: {exc}"}


def _financial_understanding_needs_refresh(*, conn, profile: str | None) -> bool:
    """Return true when the advisor read model has no active facts yet."""

    try:
        from mira.financial_understanding import list_financial_facts

        if conn is None:
            from database import get_db

            with get_db() as c:
                return len(list_financial_facts(conn=c, profile=profile, limit=1)) == 0
        return len(list_financial_facts(conn=conn, profile=profile, limit=1)) == 0
    except Exception:
        return False


def _run_advisor_cases_sidecar(*, profile: str | None, conn) -> dict[str, Any]:
    """Populate Mira's advisor cards from the safe finance query layer."""

    try:
        from mira.advisor_cases import refresh_advisor_cases

        return refresh_advisor_cases(profile=profile, conn=conn)
    except Exception as exc:
        return {"status": "error", "stored_count": 0, "case_count": 0, "error": f"{type(exc).__name__}: {exc}"}


def _advisor_cases_need_refresh(*, conn, profile: str | None) -> bool:
    try:
        from mira.advisor_cases import advisor_cases_need_refresh

        if conn is None:
            from database import get_db

            with get_db() as c:
                return advisor_cases_need_refresh(conn=c, profile=profile)
        return advisor_cases_need_refresh(conn=conn, profile=profile)
    except Exception:
        return False


def _run_advisor_lens_sidecar(*, profile: str | None, conn, complete_fn: Callable[..., str] | None) -> dict[str, Any]:
    """Populate the private advisor memo store from the same background cadence."""

    try:
        from mira.advisor_lens_synthesis import run_advisor_lens_background_memo

        return run_advisor_lens_background_memo(profile=profile, conn=conn, complete_fn=complete_fn)
    except Exception as exc:
        return {"status": "error", "stored_count": 0, "fresh_cache": False, "error": f"{type(exc).__name__}: {exc}"}


def _advisor_lens_need_refresh(*, conn, profile: str | None) -> bool:
    try:
        from mira.advisor_lens_synthesis import advisor_lens_background_auto_decision

        if conn is None:
            from database import get_db

            with get_db() as c:
                return bool(advisor_lens_background_auto_decision(conn=c, profile=profile).get("should_queue"))
        return bool(advisor_lens_background_auto_decision(conn=conn, profile=profile).get("should_queue"))
    except Exception:
        return False


def _advisor_lens_background_auto_enabled() -> bool:
    try:
        from mira.advisor_lens_synthesis import advisor_lens_background_auto_enabled

        return advisor_lens_background_auto_enabled()
    except Exception:
        return False


def _advisor_cases_background_auto_enabled() -> bool:
    return os.getenv("MIRA_ADVISOR_BACKGROUND_AUTO_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


def _clean_draft(item: dict[str, Any]) -> dict[str, Any]:
    confidence = str(item.get("confidence") or "medium").lower()
    tone = str(item.get("tone") or "calm").lower()
    evidence_ids = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
    return {
        "title": _clean_visible_text(item.get("title"), limit=90),
        "body": _clean_visible_text(item.get("body"), limit=420),
        "why_it_matters": _clean_text(item.get("why_it_matters"), limit=260),
        "recommended_action": _clean_visible_text(item.get("recommended_action"), limit=260),
        "tone": tone if tone in _ALLOWED_TONES else "calm",
        "confidence": confidence if confidence in _ALLOWED_CONFIDENCE else "medium",
        "evidence_ids": [str(value).strip() for value in evidence_ids if str(value).strip()][:5],
        "source": "background_mira_analyst",
    }


def _draft_rejection_reason(draft: dict[str, Any], evidence_ids: set[str]) -> str:
    if not draft.get("title") or not draft.get("body"):
        return "missing_title_or_body"
    ids = draft.get("evidence_ids") or []
    if not ids:
        return "missing_evidence_ids"
    unknown = [value for value in ids if value not in evidence_ids]
    if unknown:
        return "unknown_evidence_id"
    text = " ".join(str(draft.get(key) or "") for key in ("title", "body", "why_it_matters", "recommended_action")).lower()
    if any(term in text for term in _PRIVATE_SURFACE_TERMS):
        return "private_surface_leak"
    visible_text = " ".join(str(draft.get(key) or "") for key in ("title", "body", "recommended_action")).lower()
    if any(term in visible_text for term in _VISIBLE_INTERNAL_TERMS):
        return "visible_internal_term"
    if _word_count(draft.get("title")) > 12 or _word_count(draft.get("body")) > 45 or _word_count(draft.get("recommended_action")) > 26:
        return "visible_copy_too_long"
    return ""


def _claim_rejection_reason(draft: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> str:
    base_reason = _draft_rejection_reason(draft, set(evidence_by_id.keys()))
    if base_reason:
        return base_reason
    text = _draft_text(draft)
    lowered = text.lower()
    if any(term in lowered for term in _ALARMIST_TERMS):
        return "alarmist_or_shaming_tone"
    cited_numbers = _numbers_from_values([evidence_by_id[eid] for eid in draft.get("evidence_ids") or []])
    unsupported = [number for number in _numbers_from_text(text) if number not in cited_numbers]
    if unsupported:
        return "unsupported_numeric_claim"
    return ""


def _draft_to_insight(bundle: dict[str, Any], draft: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    evidence_ids = list(draft.get("evidence_ids") or [])
    scope = bundle.get("profile_scope") or "household"
    fingerprint_seed = "|".join([scope, draft.get("title") or "", *evidence_ids])
    cited = [
        {
            "evidence_id": evidence_id,
            "kind": evidence_by_id[evidence_id].get("kind"),
            "source": evidence_by_id[evidence_id].get("source"),
            "confidence": evidence_by_id[evidence_id].get("confidence"),
            "values": evidence_by_id[evidence_id].get("values"),
        }
        for evidence_id in evidence_ids
    ]
    return {
        "kind": "background_mira_analyst",
        "insight_type": "background_mira_analyst",
        "title": draft.get("title") or "",
        "body": draft.get("body") or "",
        "severity": "info",
        "priority": 24 if draft.get("confidence") == "high" else 36,
        "confidence": draft.get("confidence") or "medium",
        "recommended_action": draft.get("recommended_action") or "",
        "fingerprint": f"{scope}:background_mira_analyst:{sha1(fingerprint_seed.encode('utf-8')).hexdigest()[:16]}",
        "assumptions": [],
        "evidence": {
            "source": "background_mira_analyst",
            "validator_version": CLAIM_VALIDATOR_VERSION,
            "bundle_version": bundle.get("version") or "",
            "evidence_ids": evidence_ids,
            "cited_evidence": cited,
            "why_it_matters": draft.get("why_it_matters") or "",
            "tone": draft.get("tone") or "calm",
        },
    }


def _bundle_evidence_by_id(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in [*(bundle.get("facts") or []), *(bundle.get("candidate_signals") or [])]:
        if isinstance(item, dict) and item.get("evidence_id"):
            out[str(item["evidence_id"])] = item
    return out


def _bundle_evidence_ids(bundle: dict[str, Any]) -> set[str]:
    ids = set()
    for item in [*(bundle.get("facts") or []), *(bundle.get("candidate_signals") or [])]:
        evidence_id = item.get("evidence_id") if isinstance(item, dict) else None
        if evidence_id:
            ids.add(str(evidence_id))
    return ids


def _draft_text(draft: dict[str, Any]) -> str:
    return " ".join(str(draft.get(key) or "") for key in ("title", "body", "why_it_matters", "recommended_action"))


def _numbers_from_text(text: str) -> set[str]:
    return {_normalize_number(match.group(0)) for match in _NUMERIC_CLAIM_RE.finditer(text or "")}


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


def _clean_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _clean_visible_text(value: Any, *, limit: int) -> str:
    text = _clean_text(value, limit=limit)
    text = _VISIBLE_EVIDENCE_REF_RE.sub("", text)
    text = _VISIBLE_DOTTED_TOKEN_RE.sub("", text)
    text = re.sub(r"\s*\((?:\s|,)*\)", "", text)
    text = re.sub(r"\bdeterministic\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bevidence bundle\b", "data", text, flags=re.IGNORECASE)
    text = re.sub(r"\bevidence ids?\b", "sources", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(from|via|using|according to)\s*\.?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+([.,;:])", r"\1", text)
    return _clean_text(text, limit=limit)


def _word_count(value: Any) -> int:
    return len(str(value or "").split())


def _empty_result(status: str, bundle: dict[str, Any] | None, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "drafts": [],
        "suppressed": [],
        "rejected": [],
        "errors": errors or [],
        "bundle_meta": _bundle_meta(bundle or {}),
    }


def _bundle_meta(bundle: dict[str, Any]) -> dict[str, Any]:
    meta = bundle.get("meta") if isinstance(bundle.get("meta"), dict) else {}
    return {
        "version": bundle.get("version") or "",
        "fact_count": int(meta.get("fact_count") or len(bundle.get("facts") or [])),
        "candidate_signal_count": int(meta.get("candidate_signal_count") or len(bundle.get("candidate_signals") or [])),
        "evidence_ref_count": int(meta.get("evidence_ref_count") or len(_bundle_evidence_ids(bundle))),
        "json_bytes": int(meta.get("json_bytes") or bundle_json_bytes(bundle)),
    }


def _background_analyst_enabled() -> bool:
    return os.getenv("MIRA_BACKGROUND_ANALYST_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _background_analyst_store_enabled() -> bool:
    return os.getenv("MIRA_BACKGROUND_ANALYST_STORE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def _background_analyst_auto_enabled() -> bool:
    return os.getenv("MIRA_BACKGROUND_ANALYST_AUTO_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


def _background_analyst_min_interval_minutes() -> int:
    try:
        return max(1, int(os.getenv("MIRA_BACKGROUND_ANALYST_MIN_INTERVAL_MINUTES", "360")))
    except (TypeError, ValueError):
        return 360


def _has_fresh_background_analyst_insight(conn, profile: str | None, minutes: int | None = None) -> bool:
    scope = proactive_insights._scope_profile(profile)
    window = max(1, int(minutes if minutes is not None else _background_analyst_min_interval_minutes()))
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
          FROM proactive_insights
         WHERE profile_id = ?
           AND kind = 'background_mira_analyst'
           AND status = 'active'
           AND (valid_until IS NULL OR date(valid_until) >= date('now'))
           AND datetime(generated_at) >= datetime('now', ?)
        """,
        (scope, f"-{window} minutes"),
    ).fetchone()
    try:
        return int(row[0] if row is not None else 0) > 0
    except (TypeError, ValueError):
        return False
