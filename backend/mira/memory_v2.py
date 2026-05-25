from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any, Callable


MEMORY_TYPES = {
    "preference",
    "goal",
    "constraint",
    "stressor",
    "commitment",
    "rejected_advice",
    "coaching_state",
    "identity_fact",
    "tone_preference",
}
SENSITIVITIES = {"low", "medium", "high"}
EXACT_FINANCE_INTENTS = {"spending", "transactions", "chart", "drilldown"}
EXACT_FINANCE_ACTIONS = {
    "SpendTotal",
    "TransactionSearch",
    "MonthlyTrend",
    "NetWorthTrend",
    "CompareSpend",
    "ExplainLastAnswer",
}
EXACT_FINANCE_OPERATIONS = {
    "category_total",
    "merchant_total",
    "list_transactions",
    "find_transactions",
    "current_vs_previous",
    "current_vs_average",
    "monthly_spending_chart",
    "net_worth_chart",
    "explain_grounding",
}
EXACT_FINANCE_TOOLS = {
    "get_merchant_spend",
    "get_category_spend",
    "get_transactions",
    "get_transactions_for_merchant",
    "find_transactions",
    "get_monthly_spending_trend",
    "get_net_worth_trend",
    "compare_periods",
}
MEMORY_MANAGEMENT_OPERATIONS = {
    "manage_memory",
    "remember_user_context",
    "retrieve_relevant_memories",
    "update_memory",
    "forget_memory",
    "list_mira_memories",
}
MEMORY_RETRIEVAL_TYPES = {
    "affordability_coaching": ("goal", "constraint", "commitment", "tone_preference", "stressor"),
    "goal_followup": ("goal", "commitment", "constraint"),
    "casual_persona": ("tone_preference", "preference"),
    "memory_management": tuple(sorted(MEMORY_TYPES)),
}
MEMORY_RETRIEVAL_CAPS = {
    "affordability_coaching": 3,
    "goal_followup": 3,
    "casual_persona": 2,
    "memory_management": 12,
    "exact_finance": 0,
    "none": 0,
}
PROMPT_CONTEXT_MEMORY_TYPES = {"preference", "tone_preference", "goal", "constraint", "commitment"}
SUGGESTABLE_MEMORY_TYPES = {"preference", "tone_preference", "goal", "constraint", "commitment", "stressor"}
MEMORY_SCOUT_MAX_TOKENS = int(os.getenv("MIRA_MEMORY_SCOUT_MAX_TOKENS", "220"))
MEMORY_COMMAND_NORMALIZER_MAX_TOKENS = int(os.getenv("MIRA_MEMORY_COMMAND_NORMALIZER_MAX_TOKENS", "220"))
SESSION_SUMMARY_MAX_TOKENS = int(os.getenv("MIRA_SESSION_SUMMARY_MAX_TOKENS", "320"))
SESSION_SUMMARY_CONTEXT_MAX_TOKENS = int(os.getenv("MIRA_SESSION_SUMMARY_CONTEXT_MAX_TOKENS", "80"))
SESSION_SUMMARY_MAX_TURNS = int(os.getenv("MIRA_SESSION_SUMMARY_MAX_TURNS", "12"))
SESSION_SUMMARY_MIN_USER_TURNS = int(os.getenv("MIRA_SESSION_SUMMARY_MIN_USER_TURNS", "2"))
SESSION_SUMMARY_IDLE_SECONDS = float(os.getenv("MIRA_SESSION_SUMMARY_IDLE_SECONDS", "90"))
_FALSE_ENV_VALUES = {"0", "false", "no", "off"}
_SESSION_SUMMARY_TIMERS: dict[str, tuple[str, threading.Timer]] = {}
_SESSION_SUMMARY_LOCK = threading.Lock()

_MEMORY_SCOUT_PROMPT = """You are Mira's memory scout. You run after Mira has answered, off the visible answer path.

Decide whether Mira should OFFER to remember something from the latest user message.

Rules:
- The user must directly state durable personal context in the latest message.
- Good candidates: conversation preferences, goals, constraints, commitments, stable preferences, non-sensitive recurring context.
- Do not infer facts from the assistant answer.
- Do not suggest ordinary lookups, one-off tasks, finance totals, transactions, balances, merchants/stores, categories, write commands, or chart requests.
- Do not suggest sensitive stressors such as rent stress, debt stress, health, medical, job loss, family issues, or eviction. Explicit remember requests are handled by another path.
- Never save automatically. Your job is only to decide whether to show a confirmation card.
- The `text` must be the memory itself, in third person. Good: "User prefers concise answers." Bad: "User stated a preference for concise answers."
- Set `finance_related` true if the candidate is about Folio data, spending, balances, transactions, merchants, stores, categories, budgets, bills, or raw financial facts.
- Set `sensitive_topic` true if the candidate is about debt stress, rent stress, health, medical, job loss, income shocks, family issues, eviction, or other private distress.

Return one JSON object only:
{{
  "should_suggest": true|false,
  "memory_type": "preference|tone_preference|goal|constraint|commitment|stressor",
  "text": "User ...",
  "topic": "short-topic",
  "reason": "short reason for the confirmation card",
  "evidence": "short quote from the user",
  "sensitivity": "low|medium|high",
  "finance_related": true|false,
  "sensitive_topic": true|false
}}

If no suggestion is warranted, return:
{{"should_suggest": false}}

Latest user message:
{question}

Mira answer:
{answer}

JSON:"""

_MEMORY_COMMAND_NORMALIZER_PROMPT = """Normalize this explicit `/memory remember` body into one durable memory record.

Rules: JSON only. `text` must start with "User " or "User's ". Reject one-off tasks/lookups. Set `finance_related` true for Folio data, spending, balances, transactions, merchants, categories, bills, budgets, or raw finance facts. Sensitive explicit memories are allowed; mark them sensitivity="high" and sensitive_topic=true.

JSON shape:
{{
  "ok": true|false,
  "memory_type": "preference|tone_preference|goal|constraint|stressor|commitment|identity_fact",
  "text": "User ...",
  "topic": "short-topic",
  "sensitivity": "low|medium|high",
  "finance_related": true|false,
  "sensitive_topic": true|false,
  "reason": "short reason if ok=false"
}}

Command body:
{text}

JSON:"""

_SESSION_SUMMARY_PROMPT = """You are Mira's off-path session summarizer. You run only after the user has gone idle.

Summarize durable conversational continuity from the transcript. Do not write a transcript.

Rules:
- JSON only.
- Store only compact context that could help Mira continue naturally later.
- Do not include raw transaction rows, balances, account numbers, exact spend totals, passwords, secrets, or private identifiers.
- Do not turn live finance facts into memory. Finance facts belong to Folio tools.
- Stress/sensitivity signals must be sparse and only when the user explicitly stated concern, stress, anxiety, pressure, or a sensitive constraint.
- Prefer "should_store": false for pure one-off lookups, greetings, generic Q&A, or sessions with no continuity value.
- Keep every string short and plain.

Return exactly this JSON shape:
{{
  "should_store": true|false,
  "summary": "one sentence, no raw finance facts",
  "topics": ["short topic"],
  "user_goals": ["goal explicitly discussed"],
  "unresolved_followups": ["follow-up Mira could ask about later"],
  "preferences_seen": ["durable preference explicitly stated"],
  "stress_or_sensitivity_signals": ["sparse explicit signal"],
  "evidence_refs": ["turn ids or short refs only"],
  "confidence": 0.0
}}

Transcript:
{transcript}

JSON:"""

_STOPWORDS = {
    "a", "about", "am", "and", "are", "at", "be", "can", "doing", "for",
    "from", "how", "i", "im", "in", "is", "it", "keep", "me", "my", "of",
    "on", "or", "remember", "save", "saved", "saving", "that", "the", "this",
    "to", "trying", "user", "want", "wants", "what", "with", "you",
}
_SENSITIVE_TERMS = {
    "anxious", "anxiety", "debt", "medical", "health", "mental", "job", "income",
    "layoff", "laid", "family", "support", "rent", "eviction", "stress", "stressed",
}
_DERIVABLE_FINANCE_RE = re.compile(
    r"\b(?:spent|spend|paid|charged|transaction|transactions|balance|net worth|costco|merchant)\b",
    re.I,
)
_AMOUNT_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")
_EXACT_FINANCE_QUESTION_RE = re.compile(
    r"\b(?:how much did i spend|how much have i spent|show(?: me)? transactions?|"
    r"list transactions?|latest transaction|find transactions?|compare .+ (?:vs|versus) .+|"
    r"chart|plot|graph|net worth)\b",
    re.I,
)
_AFFORDABILITY_RE = re.compile(
    r"\b(?:can i afford|afford another|afford to|should i buy|should i spend|"
    r"can i spend|help me spend less|spend less|budget advice|financial advice|"
    r"what should i do|coach(?:ing)?|advice)\b",
    re.I,
)
_GOAL_FOLLOWUP_RE = re.compile(
    r"\b(?:goal|goals|on track|track for|pacing|pace|how am i doing|still on track)\b",
    re.I,
)
_CASUAL_PERSONA_RE = re.compile(
    r"\b(?:talk to me|tone|joke|jokes|roast|tease|serious|short answers?|concise|"
    r"like i asked|hey mira|hi mira|hey girl|hello mira)\b",
    re.I,
)
_STYLE_TOPIC_TERMS = {
    "answer", "answers", "reply", "replies", "tone", "style", "serious",
    "short", "concise", "brief", "joke", "jokes", "roast", "tease",
}
_DOMAIN_TOPIC_TERMS = {
    "budget", "coffee", "debt", "dining", "family", "groceries", "health",
    "house", "income", "job", "medical", "rent", "subscriptions", "support",
}
_TOPIC_HINT_STOPWORDS = _STOPWORDS | {
    "advice", "afford", "another", "budget", "buy", "cap", "compare",
    "current", "did", "doing", "find", "goal", "goals", "help", "last",
    "less", "list", "month", "monthly", "much", "show", "should", "spend",
    "spending", "spent", "still", "track", "under", "versus", "vs", "week",
    "year",
}


def remember_user_context(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    text: str,
    memory_type: str | None = None,
    topic: str | None = None,
    source_summary: str = "",
    source_conversation_id: int | None = None,
    source_turn_id: str | None = None,
    pinned: bool = False,
    expires_at: str | None = None,
    consent: str = "explicit",
) -> dict[str, Any]:
    candidate = extract_memory_candidate(text, memory_type=memory_type, topic=topic)
    if not candidate:
        return {"saved": False, "reason": "No durable user memory detected."}
    memory_id = create_memory(
        conn=conn,
        profile=profile,
        source_conversation_id=source_conversation_id,
        source_turn_id=source_turn_id,
        source_summary=source_summary,
        pinned=pinned,
        expires_at=expires_at,
        consent=consent,
        **candidate,
    )
    memory = get_memory(conn, memory_id, profile)
    stated_intent = None
    if memory:
        try:
            from mira.stated_intents import maybe_create_stated_intent_from_memory

            stated_intent = maybe_create_stated_intent_from_memory(conn=conn, profile=profile, memory=memory)
        except Exception:
            stated_intent = None
    result = {
        "saved": True,
        "memory": memory,
        "memory_trace": trace_for_memories([memory] if memory else [], allowed=True, reason="explicit_memory_write"),
    }
    if stated_intent:
        result["stated_intent"] = stated_intent
    return result


def extract_memory_candidate(text: str, memory_type: str | None = None, topic: str | None = None) -> dict[str, Any] | None:
    original = " ".join((text or "").strip().split())
    if not original:
        return None
    lowered = _normalize_for_match(original)
    explicit = bool(re.search(r"\b(?:remember(?: that)?|keep in mind|save this|save that)\b", lowered))
    body = re.sub(r"^(?:please\s+)?(?:remember(?: that)?|keep in mind(?: that)?|save this|save that)\s+", "", original, flags=re.I).strip(" .")

    inferred_type = memory_type if memory_type in MEMORY_TYPES else None
    if not inferred_type:
        inferred_type = _infer_memory_type(lowered)
    if not inferred_type:
        return None

    if _looks_like_rejected_finance_fact(lowered, inferred_type, explicit):
        return None
    if inferred_type == "stressor" and not _first_person_stated(lowered):
        return None

    normalized = _normalize_memory_text(body or original, inferred_type)
    if not normalized:
        return None
    inferred_topic = (topic or _infer_topic(lowered, normalized)).strip().lower()
    sensitivity = _infer_sensitivity(lowered, inferred_type, inferred_topic)
    confidence = 1.0 if explicit else 0.86
    return {
        "memory_type": inferred_type,
        "topic": inferred_topic,
        "normalized_text": normalized,
        "original_text": original,
        "sensitivity": sensitivity,
        "confidence": confidence,
    }


def suggest_memory_candidate(
    *,
    text: str,
    answer: str = "",
    route: dict[str, Any] | None = None,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any] | None:
    """Ask a local LLM scout whether to offer a user-confirmed memory."""
    original = " ".join((text or "").strip().split())
    if not original:
        return None
    if not _memory_scout_route_allowed(route):
        return None

    prompt = _MEMORY_SCOUT_PROMPT.format(
        question=original[:1600],
        answer=" ".join(str(answer or "").split())[:2400],
    )
    try:
        if complete_fn is not None:
            raw = complete_fn(prompt, MEMORY_SCOUT_MAX_TOKENS, "controller", response_format="json")
        else:
            import llm_client

            if not llm_client.is_available():
                return None
            raw = llm_client.complete(
                prompt,
                max_tokens=MEMORY_SCOUT_MAX_TOKENS,
                purpose="controller",
                response_format="json",
            )
    except Exception:
        return None

    return _candidate_from_memory_scout_output(raw, source_text=original)


def normalize_explicit_memory_command(
    text: str,
    *,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Normalize an explicit `/memory remember ...` body with a local LLM."""
    original = " ".join((text or "").strip().split())
    if not original:
        return {"ok": False, "reason": "memory text is required"}

    prompt = _MEMORY_COMMAND_NORMALIZER_PROMPT.format(text=original[:1600])
    try:
        if complete_fn is not None:
            raw = complete_fn(prompt, MEMORY_COMMAND_NORMALIZER_MAX_TOKENS, "controller", response_format="json")
        else:
            import llm_client

            if not llm_client.is_available():
                return {"ok": False, "reason": "local model is unavailable"}
            raw = llm_client.complete(
                prompt,
                max_tokens=MEMORY_COMMAND_NORMALIZER_MAX_TOKENS,
                purpose="controller",
                response_format="json",
            )
    except Exception:
        return {"ok": False, "reason": "local model could not normalize that memory"}

    parsed = _parse_json_object(raw)
    if not parsed or not bool(parsed.get("ok")):
        return {"ok": False, "reason": _short_reason((parsed or {}).get("reason")) or "that does not look like durable memory"}

    memory_type = str(parsed.get("memory_type") or "").strip()
    if memory_type not in MEMORY_TYPES:
        return {"ok": False, "reason": "unsupported memory type"}
    normalized = " ".join(str(parsed.get("text") or "").strip().split())
    if not normalized or len(normalized) > 240:
        return {"ok": False, "reason": "memory text is empty or too long"}
    if not normalized.lower().startswith(("user ", "user's ")):
        return {"ok": False, "reason": "memory text was not normalized safely"}
    finance_related = _scout_bool(parsed.get("finance_related"))
    if finance_related is not False:
        return {"ok": False, "reason": "I will not save raw Folio or finance facts as memory."}
    sensitive_topic = _scout_bool(parsed.get("sensitive_topic"))
    if sensitive_topic is None:
        return {"ok": False, "reason": "missing memory sensitivity field"}
    sensitivity = str(parsed.get("sensitivity") or "low").strip().lower()
    if sensitivity not in SENSITIVITIES:
        return {"ok": False, "reason": "invalid memory sensitivity"}
    return {
        "ok": True,
        "text": normalized,
        "memory_type": memory_type,
        "topic": _safe_scout_topic(parsed.get("topic")),
        "sensitivity": sensitivity,
        "sensitive_topic": sensitive_topic,
        "finance_related": False,
        "original_text": original,
    }


def session_summaries_enabled() -> bool:
    return str(os.getenv("MIRA_SESSION_SUMMARIES_ENABLED", "1")).strip().lower() not in _FALSE_ENV_VALUES


def schedule_session_summary_after_idle(
    *,
    profile: str | None,
    history: list[dict[str, Any]] | None,
    latest_question: str,
    latest_answer: str,
    delay_seconds: float | None = None,
    _complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Debounce an off-path session summary run for the active profile.

    This function must stay non-blocking for chat. The actual LLM summarizer
    runs later in a background timer and stores a compact, inspectable row.
    """
    if not session_summaries_enabled():
        return {"scheduled": False, "reason": "session_summaries_disabled"}
    turns = _session_turns_from_history(
        history=history,
        latest_question=latest_question,
        latest_answer=latest_answer,
    )
    user_turns = sum(1 for turn in turns if turn.get("role") == "user")
    if user_turns < max(1, SESSION_SUMMARY_MIN_USER_TURNS):
        return {"scheduled": False, "reason": "not_enough_user_turns", "user_turns": user_turns}
    key = profile or "household"
    delay = max(1.0, float(delay_seconds if delay_seconds is not None else SESSION_SUMMARY_IDLE_SECONDS))
    run_id = hashlib.sha256(f"{key}:{datetime.now().isoformat()}".encode("utf-8")).hexdigest()[:16]
    with _SESSION_SUMMARY_LOCK:
        previous = _SESSION_SUMMARY_TIMERS.pop(key, None)
        if previous is not None:
            previous[1].cancel()
        timer = threading.Timer(
            delay,
            _run_scheduled_session_summary,
            kwargs={"profile": profile, "turns": turns, "run_id": run_id, "complete_fn": _complete_fn},
        )
        timer.daemon = True
        _SESSION_SUMMARY_TIMERS[key] = (run_id, timer)
        timer.start()
    return {"scheduled": True, "delay_seconds": delay, "turns": len(turns)}


def _run_scheduled_session_summary(
    *,
    profile: str | None,
    turns: list[dict[str, Any]],
    run_id: str,
    complete_fn: Callable[..., str] | None = None,
) -> None:
    key = profile or "household"
    try:
        summary = summarize_session_turns(turns, complete_fn=complete_fn)
        if not summary or not summary.get("should_store"):
            return
        from database import get_db

        with get_db() as conn:
            create_session_summary(conn=conn, profile=profile, summary=summary)
    except Exception:
        return
    finally:
        with _SESSION_SUMMARY_LOCK:
            current = _SESSION_SUMMARY_TIMERS.get(key)
            if current is not None and current[0] == run_id:
                _SESSION_SUMMARY_TIMERS.pop(key, None)


def summarize_session_turns(
    turns: list[dict[str, Any]],
    *,
    complete_fn: Callable[..., str] | None = None,
) -> dict[str, Any] | None:
    transcript = _session_transcript_for_prompt(turns)
    if not transcript:
        return None
    prompt = _SESSION_SUMMARY_PROMPT.format(transcript=transcript)
    try:
        if complete_fn is not None:
            raw = complete_fn(prompt, SESSION_SUMMARY_MAX_TOKENS, "controller", response_format="json")
        else:
            import llm_client

            if not llm_client.is_available():
                return None
            raw = llm_client.complete(
                prompt,
                max_tokens=SESSION_SUMMARY_MAX_TOKENS,
                purpose="controller",
                response_format="json",
            )
    except Exception:
        return None
    parsed = _parse_json_object(raw)
    if not parsed:
        return None
    return _safe_session_summary(parsed, turns=turns)


def create_session_summary(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    summary: dict[str, Any],
    source_turn_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    safe = _safe_session_summary(summary, turns=[])
    if not safe or not safe.get("should_store"):
        return None
    session_key = _session_summary_key(safe, profile)
    turn_ids = source_turn_ids or list(safe.get("source_turn_ids") or [])
    existing = conn.execute(
        """
        SELECT id
        FROM mira_session_summaries
        WHERE ((? IS NULL AND profile_id IS NULL) OR profile_id = ?)
          AND session_key = ?
        LIMIT 1
        """,
        (profile, profile, session_key),
    ).fetchone()
    if existing:
        summary_id = int(existing["id"])
        conn.execute(
            """
            UPDATE mira_session_summaries
            SET summary_text = ?,
                topics_json = ?,
                user_goals_json = ?,
                unresolved_followups_json = ?,
                preferences_seen_json = ?,
                stress_signals_json = ?,
                evidence_refs_json = ?,
                source_turn_ids_json = ?,
                confidence = ?,
                metadata_json = ?,
                status = 'active',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                safe["summary"],
                json.dumps(safe["topics"], sort_keys=True),
                json.dumps(safe["user_goals"], sort_keys=True),
                json.dumps(safe["unresolved_followups"], sort_keys=True),
                json.dumps(safe["preferences_seen"], sort_keys=True),
                json.dumps(safe["stress_or_sensitivity_signals"], sort_keys=True),
                json.dumps(safe["evidence_refs"], sort_keys=True),
                json.dumps(turn_ids, sort_keys=True),
                safe["confidence"],
                json.dumps(metadata or {}, sort_keys=True),
                summary_id,
            ),
        )
        return get_session_summary(conn, summary_id, profile)
    cursor = conn.execute(
        """
        INSERT INTO mira_session_summaries (
            profile_id, scope, session_key, summary_text, topics_json,
            user_goals_json, unresolved_followups_json, preferences_seen_json,
            stress_signals_json, evidence_refs_json, source_turn_ids_json,
            confidence, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile,
            "household" if profile in (None, "household") else "profile",
            session_key,
            safe["summary"],
            json.dumps(safe["topics"], sort_keys=True),
            json.dumps(safe["user_goals"], sort_keys=True),
            json.dumps(safe["unresolved_followups"], sort_keys=True),
            json.dumps(safe["preferences_seen"], sort_keys=True),
            json.dumps(safe["stress_or_sensitivity_signals"], sort_keys=True),
            json.dumps(safe["evidence_refs"], sort_keys=True),
            json.dumps(turn_ids, sort_keys=True),
            safe["confidence"],
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )
    row_id = cursor.lastrowid
    return get_session_summary(conn, int(row_id), profile) if row_id else None


def get_session_summary(conn: sqlite3.Connection, summary_id: int, profile: str | None) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM mira_session_summaries
        WHERE id = ? AND (? IS NULL OR profile_id = ? OR profile_id IS NULL OR scope = 'household')
        """,
        (summary_id, profile, profile),
    ).fetchone()
    return _public_session_summary(dict(row)) if row else None


def list_session_summaries(
    conn: sqlite3.Connection,
    profile: str | None,
    *,
    include_inactive: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where = ["(? IS NULL OR profile_id = ? OR profile_id IS NULL OR scope = 'household')"]
    params: list[Any] = [profile, profile]
    if not include_inactive:
        where.append("status = 'active'")
    params.append(max(1, min(int(limit or 50), 200)))
    rows = conn.execute(
        f"""
        SELECT *
        FROM mira_session_summaries
        WHERE {' AND '.join(where)}
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_public_session_summary(dict(row)) for row in rows]


def update_session_summary(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    summary_id: int,
    summary_text: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    before = get_session_summary(conn, summary_id, profile)
    if not before:
        return None
    updates = ["updated_at = datetime('now')"]
    params: list[Any] = []
    if summary_text is not None:
        text = _safe_summary_text(summary_text, limit=260)
        if not text:
            raise ValueError("summary_text is required")
        updates.append("summary_text = ?")
        params.append(text)
    if status is not None:
        if status not in {"active", "deleted"}:
            raise ValueError("status must be active or deleted")
        updates.append("status = ?")
        params.append(status)
    params.extend([summary_id, profile, profile])
    conn.execute(
        f"""
        UPDATE mira_session_summaries
        SET {', '.join(updates)}
        WHERE id = ? AND (? IS NULL OR profile_id = ? OR profile_id IS NULL OR scope = 'household')
        """,
        params,
    )
    return get_session_summary(conn, summary_id, profile)


def delete_session_summary(*, conn: sqlite3.Connection, profile: str | None, summary_id: int) -> bool:
    updated = update_session_summary(conn=conn, profile=profile, summary_id=summary_id, status="deleted")
    return bool(updated)


def retrieve_relevant_session_summaries(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    question: str,
    route: dict[str, Any] | None = None,
    limit: int = 2,
    force: bool = False,
) -> dict[str, Any]:
    retrieval = classify_memory_retrieval_intent(question, route, force=force)
    if not bool(retrieval.get("allowed")):
        return {
            "summaries": [],
            "session_summary_trace": _session_summary_trace(
                [],
                allowed=False,
                reason=str(retrieval.get("reason") or "session_summary_not_relevant"),
                retrieval=retrieval,
            ),
            "compact_session_summaries": _compact_session_summary_packet(
                [],
                allowed=False,
                reason=str(retrieval.get("reason") or "session_summary_not_relevant"),
                retrieval=retrieval,
            ),
        }

    candidates = list_session_summaries(conn, profile, limit=100)
    ranked = _rank_session_summaries(candidates, question, retrieval)
    selected = ranked[: max(1, min(int(limit or 2), 4))]
    if selected:
        ids = [int(item["id"]) for item in selected]
        conn.execute(
            f"UPDATE mira_session_summaries SET last_used_at = datetime('now') WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        )
    reason = str(retrieval.get("reason") or "session_summary_retrieval")
    return {
        "summaries": selected,
        "session_summary_trace": _session_summary_trace(selected, allowed=True, reason=reason, retrieval=retrieval),
        "compact_session_summaries": _compact_session_summary_packet(selected, allowed=True, reason=reason, retrieval=retrieval),
    }


def session_summary_prompt_context_from_packet(
    packet: dict[str, Any] | None,
    *,
    max_tokens: int = SESSION_SUMMARY_CONTEXT_MAX_TOKENS,
) -> dict[str, Any]:
    if not isinstance(packet, dict):
        return {"block": "", "used": False, "count": 0, "reason": "no_session_summary_packet"}
    reason = str(packet.get("reason") or "")
    if not packet.get("allowed"):
        return {"block": "", "used": False, "count": 0, "reason": reason or "session_summary_not_allowed"}
    lines: list[str] = []
    used: list[dict[str, Any]] = []
    for item in packet.get("items") or []:
        if not isinstance(item, dict):
            continue
        pieces = [str(item.get("summary") or "").strip()]
        goals = [str(value).strip() for value in item.get("user_goals") or [] if str(value).strip()]
        followups = [str(value).strip() for value in item.get("unresolved_followups") or [] if str(value).strip()]
        if goals:
            pieces.append("Goal: " + goals[0])
        if followups:
            pieces.append("Open follow-up: " + followups[0])
        line = "; ".join(piece for piece in pieces if piece)
        if not line:
            continue
        candidate_lines = [*lines, f"- {line[:220].rstrip(' ,;:')}"]
        block = "Recent session continuity:\n" + "\n".join(candidate_lines)
        if _estimated_tokens(block) > max(1, int(max_tokens or SESSION_SUMMARY_CONTEXT_MAX_TOKENS)):
            break
        lines = candidate_lines
        used.append({"id": item.get("id"), "confidence": item.get("confidence")})
        if len(lines) >= 2:
            break
    if not lines:
        return {"block": "", "used": False, "count": 0, "reason": reason or "no_prompt_safe_session_summaries", "allowed": True}
    return {
        "block": "Recent session continuity:\n" + "\n".join(lines),
        "used": True,
        "count": len(lines),
        "reason": reason or "session_summary_context",
        "items": used,
        "allowed": True,
    }


def merge_answer_contexts(*contexts: dict[str, Any], max_tokens: int = 140) -> dict[str, Any]:
    blocks: list[str] = []
    count = 0
    reasons: list[str] = []
    items: list[dict[str, Any]] = []
    for context in contexts:
        if not isinstance(context, dict):
            continue
        block = str(context.get("block") or "").strip()
        if not block:
            if context.get("reason"):
                reasons.append(str(context.get("reason")))
            continue
        candidate = "\n\n".join([*blocks, block])
        if _estimated_tokens(candidate) > max(1, int(max_tokens or 140)):
            continue
        blocks.append(block)
        count += max(0, int(context.get("count") or 0))
        if context.get("reason"):
            reasons.append(str(context.get("reason")))
        if isinstance(context.get("items"), list):
            items.extend(item for item in context["items"] if isinstance(item, dict))
    if not blocks:
        return {"block": "", "used": False, "count": 0, "reason": "|".join(reasons) or "no_memory_context"}
    return {
        "block": "\n\n".join(blocks),
        "used": True,
        "count": count,
        "reason": "|".join(dict.fromkeys(reasons)) or "memory_context",
        "items": items,
    }


def _memory_scout_route_allowed(route: dict[str, Any] | None) -> bool:
    if _route_is_memory_management(route):
        return False
    if _route_is_exact_finance(route):
        return False
    return True


def _candidate_from_memory_scout_output(raw: str, *, source_text: str) -> dict[str, Any] | None:
    parsed = _parse_json_object(raw)
    if not parsed or not bool(parsed.get("should_suggest")):
        return None
    memory_type = str(parsed.get("memory_type") or parsed.get("type") or "").strip()
    if memory_type not in SUGGESTABLE_MEMORY_TYPES:
        return None
    sensitivity = str(parsed.get("sensitivity") or "low").strip().lower()
    if sensitivity == "high":
        return None
    normalized = " ".join(str(parsed.get("text") or "").strip().split())
    if not normalized or len(normalized) > 220:
        return None
    if not normalized.lower().startswith(("user ", "user's ")):
        return None
    finance_related = _scout_bool(parsed.get("finance_related"))
    sensitive_topic = _scout_bool(parsed.get("sensitive_topic"))
    if finance_related is not False or sensitive_topic is not False:
        return None
    evidence = _short_user_quote(str(parsed.get("evidence") or source_text))
    return {
        "text": normalized,
        "type": memory_type,
        "memory_type": memory_type,
        "topic": _safe_scout_topic(parsed.get("topic")),
        "reason": _short_reason(parsed.get("reason")) or _suggestion_reason(memory_type),
        "evidence": evidence,
        "sensitivity": sensitivity if sensitivity in SENSITIVITIES else "low",
        "finance_related": False,
        "sensitive_topic": False,
        "confidence": 0.82,
    }


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"```[a-zA-Z]*\n?", "", text).strip("`\n ")
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def _scout_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def create_memory(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    memory_type: str,
    topic: str,
    normalized_text: str,
    original_text: str = "",
    source_summary: str = "",
    sensitivity: str = "low",
    confidence: float = 1.0,
    source_conversation_id: int | None = None,
    source_turn_id: str | None = None,
    pinned: bool = False,
    expires_at: str | None = None,
    consent: str = "explicit",
    metadata: dict[str, Any] | None = None,
) -> int:
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"memory_type must be one of {sorted(MEMORY_TYPES)}")
    if sensitivity not in SENSITIVITIES:
        raise ValueError("sensitivity must be low, medium, or high")
    text = " ".join((normalized_text or "").strip().split())
    if not text:
        raise ValueError("normalized_text is required")
    existing = _find_duplicate(conn, profile, memory_type, text)
    if existing:
        return existing
    cursor = conn.execute(
        """
        INSERT INTO mira_memories (
            profile_id, scope, memory_type, topic, normalized_text, original_text,
            source_summary, sensitivity, confidence, source_conversation_id,
            source_turn_id, pinned, expires_at, consent, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile,
            "household" if profile in (None, "household") else "profile",
            memory_type,
            (topic or "").strip().lower(),
            text,
            original_text or text,
            source_summary,
            sensitivity,
            max(0.0, min(float(confidence), 1.0)),
            source_conversation_id,
            source_turn_id,
            1 if pinned else 0,
            expires_at,
            consent or "explicit",
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )
    memory_id = int(cursor.lastrowid)
    _log_event(conn, memory_id, profile, "created", after=get_memory(conn, memory_id, profile), source_turn_id=source_turn_id)
    return memory_id


def list_memories(
    conn: sqlite3.Connection,
    profile: str | None,
    *,
    include_inactive: bool = False,
    include_expired: bool = False,
    memory_type: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where = ["(? IS NULL OR profile_id = ? OR profile_id IS NULL OR scope = 'household')"]
    params: list[Any] = [profile, profile]
    if not include_inactive:
        where.append("status = 'active'")
    if not include_expired:
        where.append("(pinned = 1 OR expires_at IS NULL OR expires_at > datetime('now'))")
    if memory_type:
        where.append("memory_type = ?")
        params.append(memory_type)
    params.append(max(1, min(int(limit), 500)))
    rows = conn.execute(
        f"""
        SELECT *
        FROM mira_memories
        WHERE {' AND '.join(where)}
        ORDER BY pinned DESC, updated_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_public_memory(dict(row)) for row in rows]


def get_memory(conn: sqlite3.Connection, memory_id: int, profile: str | None) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM mira_memories
        WHERE id = ? AND (? IS NULL OR profile_id = ? OR profile_id IS NULL OR scope = 'household')
        """,
        (memory_id, profile, profile),
    ).fetchone()
    return _public_memory(dict(row)) if row else None


def update_memory(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    memory_id: int,
    normalized_text: str | None = None,
    memory_type: str | None = None,
    topic: str | None = None,
    sensitivity: str | None = None,
    confidence: float | None = None,
    pinned: bool | None = None,
    expires_at: str | None = None,
    status: str | None = None,
    source_turn_id: str | None = None,
) -> dict[str, Any] | None:
    before = get_memory(conn, memory_id, profile)
    if not before:
        return None
    updates: list[str] = ["updated_at = datetime('now')"]
    params: list[Any] = []
    if normalized_text is not None:
        updates.append("normalized_text = ?")
        params.append(" ".join(normalized_text.strip().split()))
    if memory_type is not None:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"memory_type must be one of {sorted(MEMORY_TYPES)}")
        updates.append("memory_type = ?")
        params.append(memory_type)
    if topic is not None:
        updates.append("topic = ?")
        params.append(topic.strip().lower())
    if sensitivity is not None:
        if sensitivity not in SENSITIVITIES:
            raise ValueError("sensitivity must be low, medium, or high")
        updates.append("sensitivity = ?")
        params.append(sensitivity)
    if confidence is not None:
        updates.append("confidence = ?")
        params.append(max(0.0, min(float(confidence), 1.0)))
    if pinned is not None:
        updates.append("pinned = ?")
        params.append(1 if pinned else 0)
    if expires_at is not None:
        updates.append("expires_at = ?")
        params.append(expires_at or None)
    if status is not None:
        if status not in {"active", "superseded", "deleted", "rejected"}:
            raise ValueError("status must be active, superseded, deleted, or rejected")
        updates.append("status = ?")
        params.append(status)
    params.extend([memory_id, profile, profile])
    conn.execute(
        f"""
        UPDATE mira_memories
        SET {', '.join(updates)}
        WHERE id = ? AND (? IS NULL OR profile_id = ? OR profile_id IS NULL OR scope = 'household')
        """,
        params,
    )
    after = get_memory(conn, memory_id, profile)
    _log_event(conn, memory_id, profile, "updated", before=before, after=after, source_turn_id=source_turn_id)
    return after


def forget_memory(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    memory_id: int | None = None,
    topic: str | None = None,
    text: str | None = None,
    source_turn_id: str | None = None,
) -> dict[str, Any]:
    candidates = list_memories(conn, profile, limit=200)
    target: dict[str, Any] | None = None
    if memory_id is not None:
        target = get_memory(conn, memory_id, profile)
    elif topic or text:
        query = " ".join([topic or "", text or ""]).strip()
        ranked = _rank_memories(candidates, query)
        target = ranked[0] if ranked else None
    else:
        return {"forgot": False, "reason": "Which memory should I remove?"}
    if not target:
        return {"forgot": False, "reason": "No matching active memory found."}
    before = target
    conn.execute(
        """
        UPDATE mira_memories
        SET status = 'deleted', updated_at = datetime('now')
        WHERE id = ?
        """,
        (target["id"],),
    )
    after = get_memory(conn, int(target["id"]), profile)
    _log_event(conn, int(target["id"]), profile, "deleted", before=before, after=after, source_turn_id=source_turn_id)
    return {"forgot": True, "memory": before}


def retrieve_relevant_memories(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    question: str,
    route: dict[str, Any] | None = None,
    limit: int = 5,
    include_expired: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    retrieval = classify_memory_retrieval_intent(question, route, force=force)
    allowed = bool(retrieval.get("allowed"))
    reason = str(retrieval.get("reason") or "")
    if not allowed:
        trace = trace_for_memories(
            [],
            allowed=False,
            reason=reason,
            intent=str(retrieval.get("intent") or "none"),
            candidate_count=0,
            allowed_types=retrieval.get("allowed_types") or [],
            topic_hints=retrieval.get("topic_hints") or [],
        )
        packet = compact_memory_packet(
            [],
            question=question,
            route=route,
            allowed=False,
            reason=reason,
            excluded_count=0,
            retrieval=retrieval,
        )
        return {"memories": [], "memory_trace": trace, "compact_memory": packet, "compact_memory_trace": packet}

    explicit = str(retrieval.get("intent") or "") == "memory_management"
    memories = list_memories(
        conn,
        profile,
        include_expired=bool(include_expired and explicit),
        limit=200,
    )
    retrieval = {**retrieval, "candidate_count": len(memories)}
    ranked, excluded_reasons = _rank_memory_candidates(memories, question, retrieval)
    requested_limit = max(1, min(int(limit or 1), 12))
    intent_cap = max(1, min(int(retrieval.get("max_items") or requested_limit), 12))
    selected = ranked[: min(requested_limit, intent_cap)]
    excluded_count = max(0, len(memories) - len(selected))
    if len(ranked) > len(selected):
        excluded_reasons["not_selected_after_cap"] = excluded_reasons.get("not_selected_after_cap", 0) + (len(ranked) - len(selected))
    if selected:
        ids = [int(item["id"]) for item in selected]
        conn.execute(
            f"UPDATE mira_memories SET last_used_at = datetime('now') WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        )
    trace = trace_for_memories(
        selected,
        allowed=True,
        reason=reason,
        intent=str(retrieval.get("intent") or "none"),
        excluded_count=excluded_count,
        candidate_count=len(memories),
        excluded_reasons=excluded_reasons,
        allowed_types=retrieval.get("allowed_types") or [],
        topic_hints=retrieval.get("topic_hints") or [],
    )
    packet = compact_memory_packet(
        selected,
        question=question,
        route=route,
        allowed=True,
        reason=reason,
        excluded_count=excluded_count,
        retrieval=retrieval,
    )
    return {"memories": selected, "memory_trace": trace, "compact_memory": packet, "compact_memory_trace": packet}


def classify_memory_retrieval_intent(
    question: str,
    route: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    route = route or {}
    q = " ".join((question or "").strip().split())
    lowered = _normalize_for_match(q)
    topic_hints = _topic_hints(q, route)

    def result(intent: str, allowed: bool, reason: str, allowed_types: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        types = list(allowed_types or MEMORY_RETRIEVAL_TYPES.get(intent, ()))
        return {
            "intent": intent,
            "allowed": bool(allowed),
            "reason": reason,
            "allowed_types": types,
            "topic_hints": topic_hints,
            "max_items": int(MEMORY_RETRIEVAL_CAPS.get(intent, 0)),
        }

    if force or _route_is_memory_management(route) or _looks_like_explicit_memory_request(lowered):
        return result("memory_management", True, "explicit_memory_request")
    if _route_is_exact_finance(route) or _looks_like_exact_finance_question(lowered):
        return result("exact_finance", False, "exact_finance_query")
    if _route_is_affordability(route) or _AFFORDABILITY_RE.search(lowered):
        return result("affordability_coaching", True, "affordability question")
    if _route_is_goal_followup(route) or _GOAL_FOLLOWUP_RE.search(lowered):
        return result("goal_followup", True, "goal follow-up")
    if _route_is_casual_chat(route) and _CASUAL_PERSONA_RE.search(lowered):
        return result("casual_persona", True, "casual/persona preference")
    return result("none", False, "memory_not_relevant")


def retrieval_allowed(question: str, route: dict[str, Any] | None = None, *, force: bool = False) -> tuple[bool, str]:
    classified = classify_memory_retrieval_intent(question, route, force=force)
    return bool(classified.get("allowed")), str(classified.get("reason") or "")


def _route_action(route: dict[str, Any] | None) -> dict[str, Any]:
    action = (route or {}).get("domain_action") if isinstance(route, dict) else None
    return action if isinstance(action, dict) else {}


def _route_is_memory_management(route: dict[str, Any] | None) -> bool:
    route = route or {}
    action = _route_action(route)
    intent = str(route.get("intent") or "").lower()
    operation = str(route.get("operation") or "").lower()
    tool_name = str(route.get("tool_name") or "").lower()
    selected_tools = route.get("selected_tools") if isinstance(route.get("selected_tools"), list) else []
    return (
        intent == "memory"
        or str(action.get("name") or "") == "Memory"
        or operation in MEMORY_MANAGEMENT_OPERATIONS
        or tool_name in MEMORY_MANAGEMENT_OPERATIONS
        or any(str(name or "").lower() in MEMORY_MANAGEMENT_OPERATIONS for name in selected_tools)
    )


def _route_is_exact_finance(route: dict[str, Any] | None) -> bool:
    route = route or {}
    action = _route_action(route)
    action_name = str(action.get("name") or "")
    intent = str(route.get("intent") or "").lower()
    operation = str(route.get("operation") or "").lower()
    tool_name = str(route.get("tool_name") or "").lower()
    return (
        intent in EXACT_FINANCE_INTENTS
        or action_name in EXACT_FINANCE_ACTIONS
        or operation in EXACT_FINANCE_OPERATIONS
        or tool_name in EXACT_FINANCE_TOOLS
    )


def _route_is_affordability(route: dict[str, Any] | None) -> bool:
    route = route or {}
    action = _route_action(route)
    return (
        str(action.get("name") or "") == "Affordability"
        or str(route.get("operation") or "").lower() == "affordability"
        or str(route.get("tool_name") or "").lower() == "check_affordability"
    )


def _route_is_goal_followup(route: dict[str, Any] | None) -> bool:
    route = route or {}
    action = _route_action(route)
    operation = str(route.get("operation") or "").lower()
    return str(action.get("name") or "") == "BudgetStatus" or operation in {"on_track", "budget_status"}


def _route_is_casual_chat(route: dict[str, Any] | None) -> bool:
    route = route or {}
    action = _route_action(route)
    intent = str(route.get("intent") or "").lower()
    return intent in {"", "chat"} or str(action.get("name") or "") == "GeneralChat"


def _looks_like_explicit_memory_request(lowered: str) -> bool:
    return bool(
        re.search(
            r"\b(?:remember(?: that)?|forget(?: that| this)?|what do you remember|"
            r"what do you know about me|list(?: my)? memories|show(?: my)? memories|"
            r"update my .+memory|change my .+memory|memory|memories)\b",
            lowered or "",
        )
    )


def _looks_like_exact_finance_question(lowered: str) -> bool:
    if _EXACT_FINANCE_QUESTION_RE.search(lowered or ""):
        return True
    tokens = set(_tokens(lowered or ""))
    if {"how", "much"} <= tokens and tokens & {"spent", "spend", "paid"}:
        return True
    if tokens & {"transactions", "transaction"} and tokens & {"show", "list", "find"}:
        return True
    return False


def _topic_hints(question: str, route: dict[str, Any] | None) -> list[str]:
    hints: list[str] = []

    def add(value: Any) -> None:
        text = " ".join(str(value or "").strip().lower().split())
        if not text:
            return
        for candidate in (text, *_tokens(text)):
            normalized = _canonical_topic(candidate)
            if normalized and not normalized.isdigit() and normalized not in hints and normalized not in _TOPIC_HINT_STOPWORDS:
                hints.append(normalized)

    args = (route or {}).get("args") if isinstance((route or {}).get("args"), dict) else {}
    for key in ("category", "merchant", "subject", "purpose"):
        add(args.get(key))
    action = _route_action(route)
    slots = action.get("validated_slots") if isinstance(action.get("validated_slots"), dict) else {}
    for key in ("category", "merchant", "subject", "purpose"):
        add(slots.get(key))

    lowered = _normalize_for_match(question)
    for token in _tokens(lowered):
        canonical = _canonical_topic(token)
        if canonical and not canonical.isdigit() and canonical not in hints and canonical not in _TOPIC_HINT_STOPWORDS:
            hints.append(canonical)
    for match in re.finditer(r"\b(?:about|on|for|toward|towards)\s+([a-z0-9 &'-]{2,40})", lowered):
        phrase = " ".join(token for token in _tokens(match.group(1)) if not token.isdigit() and token not in _TOPIC_HINT_STOPWORDS)
        if phrase:
            add(phrase)
    return hints[:8]


def _canonical_topic(value: str) -> str:
    token = " ".join(str(value or "").lower().split()).strip(" .,:;!?")
    if not token:
        return ""
    aliases = {
        "restaurants": "dining",
        "restaurant": "dining",
        "food": "dining",
        "food dining": "dining",
        "food & dining": "dining",
        "home": "house",
        "housing": "house",
        "mortgage": "house",
        "downpayment": "house",
        "loan": "debt",
        "loans": "debt",
        "credit": "debt",
        "cards": "debt",
        "card": "debt",
        "grocery": "groceries",
        "summary": "summaries",
        "answer": "answers",
        "reply": "answers",
        "replies": "answers",
        "jokes": "joke",
        "roasts": "roast",
    }
    compact = re.sub(r"[^a-z0-9]+", " ", token).strip()
    return aliases.get(token) or aliases.get(compact) or compact


def trace_for_memories(
    memories: list[dict[str, Any]],
    *,
    allowed: bool,
    reason: str,
    intent: str = "",
    excluded_count: int = 0,
    candidate_count: int = 0,
    excluded_reasons: dict[str, int] | None = None,
    allowed_types: list[str] | tuple[str, ...] | None = None,
    topic_hints: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    items = []
    for item in memories:
        items.append(
            {
                "id": item.get("id"),
                "type": item.get("memory_type"),
                "memory_type": item.get("memory_type"),
                "topic": item.get("topic"),
                "sensitivity": item.get("sensitivity"),
                "confidence": item.get("confidence"),
                "pinned": bool(item.get("pinned")),
                "status": item.get("status"),
            }
        )
    return {
        "version": 2,
        "allowed": bool(allowed),
        "used": bool(items),
        "intent": intent,
        "reason": reason,
        "used_count": len(items),
        "excluded_count": max(0, int(excluded_count or 0)),
        "candidate_count": max(0, int(candidate_count or len(memories) or 0)),
        "excluded_reasons": dict(excluded_reasons or {}),
        "allowed_types": list(allowed_types or []),
        "topic_hints": list(topic_hints or []),
        "used_memory_ids": [item["id"] for item in items if item.get("id") is not None],
        "sensitive_used": any(item.get("sensitivity") == "high" for item in items),
        "items": items,
    }


def context_block(memories: list[dict[str, Any]]) -> str:
    packet = compact_memory_packet(memories, question="", route=None, allowed=bool(memories), reason="prompt_context")
    return context_block_from_packet(packet)


def context_block_from_packet(packet: dict[str, Any] | None) -> str:
    if not isinstance(packet, dict) or not packet.get("items"):
        return ""
    return "Compact relevant Mira memory packet:\n" + json.dumps(packet, ensure_ascii=True, sort_keys=True)


def answer_prompt_context_from_packet(packet: dict[str, Any] | None, *, max_tokens: int = 80) -> dict[str, Any]:
    """Return a tiny, prompt-safe memory block for answer style and durable goals."""
    if not isinstance(packet, dict):
        return {"block": "", "used": False, "count": 0, "reason": "no_memory_packet"}
    reason = str(packet.get("reason") or "")
    if not packet.get("allowed"):
        return {"block": "", "used": False, "count": 0, "reason": reason or "memory_not_allowed"}

    lines: list[str] = []
    used_items: list[dict[str, Any]] = []
    for item in packet.get("items") or []:
        if not isinstance(item, dict):
            continue
        memory_type = str(item.get("type") or "")
        if memory_type not in PROMPT_CONTEXT_MEMORY_TYPES:
            continue
        if str(item.get("sensitivity") or "") == "sensitive":
            continue
        summary = _prompt_safe_memory_summary(item.get("summary"))
        if not summary:
            continue
        candidate_lines = [*lines, f"- {summary}"]
        block = "Relevant user preferences:\n" + "\n".join(candidate_lines)
        if _estimated_tokens(block) > max(1, int(max_tokens or 80)):
            break
        lines = candidate_lines
        used_items.append(
            {
                "id": item.get("id"),
                "type": memory_type,
                "topic": item.get("topic"),
                "confidence": item.get("confidence"),
            }
        )
        if len(lines) >= 3:
            break

    if not lines:
        return {
            "block": "",
            "used": False,
            "count": 0,
            "reason": reason or "no_prompt_safe_memories",
            "allowed": True,
        }
    return {
        "block": "Relevant user preferences:\n" + "\n".join(lines),
        "used": True,
        "count": len(lines),
        "reason": reason or "memory_context",
        "items": used_items,
        "allowed": True,
    }


def compact_memory_packet(
    memories: list[dict[str, Any]],
    *,
    question: str,
    route: dict[str, Any] | None,
    allowed: bool,
    reason: str,
    excluded_count: int = 0,
    retrieval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in memories[:12]:
        items.append(
            {
                "id": str(item.get("id")),
                "type": item.get("memory_type"),
                "topic": item.get("topic") or "general",
                "summary": _memory_summary(item),
                "confidence": _confidence_label(item.get("confidence")),
                "sensitivity": _packet_sensitivity(item.get("sensitivity")),
            }
        )
    return {
        "version": 1,
        "used": bool(allowed and items),
        "allowed": bool(allowed),
        "intent": str((retrieval or {}).get("intent") or _memory_intent(route)),
        "reason": reason,
        "items": items,
        "excluded_count": max(0, int(excluded_count or 0)),
        "candidate_count": max(0, int((retrieval or {}).get("candidate_count") or len(memories) or 0)),
        "allowed_types": list((retrieval or {}).get("allowed_types") or []),
        "topic_hints": list((retrieval or {}).get("topic_hints") or []),
        "sensitive_used": any(item.get("sensitivity") == "high" for item in memories),
    }


def affordability_constraint_context(
    memories: list[dict[str, Any]],
    *,
    category: str,
    amount: float,
) -> dict[str, Any]:
    """Return compact affordability memory context; raw memory text stays internal."""
    conflicts: list[str] = []
    used: list[dict[str, Any]] = []
    category_lower = (category or "").lower()
    category_topics = {_canonical_topic(category_lower), *(_canonical_topic(token) for token in _tokens(category_lower))}
    category_topics = {token for token in category_topics if token}
    for memory in memories:
        memory_type = str(memory.get("memory_type") or "")
        if memory_type not in {"goal", "constraint", "commitment"}:
            continue
        summary = _memory_summary(memory)
        used.append(
            {
                "id": memory.get("id"),
                "type": memory_type,
                "topic": memory.get("topic") or "general",
                "summary": summary,
                "confidence": _confidence_label(memory.get("confidence")),
                "sensitivity": _packet_sensitivity(memory.get("sensitivity")),
            }
        )
        text = str(memory.get("normalized_text") or memory.get("original_text") or "")
        cap = _amount_from_text(text)
        if category_topics and category_topics & _memory_topic_tokens(memory) and cap is not None and float(amount or 0) > cap:
            conflicts.append(f"it conflicts with your saved {category} cap context")
        elif any(token in text.lower() for token in ("save", "saving", "debt", "house", "emergency")):
            if float(amount or 0) >= 100:
                conflicts.append("it works against a saved goal or constraint")
    return {"used_memories": used, "conflicts": conflicts}


def parse_memory_command(question: str) -> dict[str, Any] | None:
    q = " ".join((question or "").strip().split())
    lowered = _normalize_for_match(q)
    if not q:
        return None
    if re.search(r"\b(?:what do you remember about me|what do you know about me|list(?: my)? memories|show(?: my)? memories)\b", lowered):
        return {"operation": "list_mira_memories", "args": {}}
    m = re.search(r"\bforget(?: that)?\s+(.+)$", q, re.I)
    if m and m.group(1).strip(" .").lower() not in {"that", "this"}:
        return {"operation": "forget_memory", "args": {"text": m.group(1).strip(" .")}}
    if re.search(r"\b(?:forget that|forget this|dont remember this|don't remember this|delete that memory|remove that memory)\b", lowered):
        return {"operation": "forget_memory", "args": {}}
    if re.search(r"\b(?:that'?s not true anymore|not true anymore)\b", q, re.I):
        return {"operation": "forget_memory", "args": {"text": q}}
    m = re.search(r"\b(?:update my|change my)\b(.+)$", q, re.I)
    if m:
        return {"operation": "update_memory", "args": {"text": m.group(1).strip(" ."), "original": q}}
    if re.search(r"\b(?:remember(?: that)?|keep in mind|save this|save that|i prefer|i'm trying|i am trying|i want|i'm anxious|i am anxious|don't joke|dont joke|don't roast|dont roast)\b", lowered):
        return {"operation": "remember_user_context", "args": {"text": q}}
    return None


def answer_for_memory_tool(operation: str, result: dict[str, Any]) -> str:
    if operation == "list_mira_memories":
        items = result.get("items") or result.get("memories") or []
        if not items:
            return "I don't have any Mira memories saved for you yet."
        lines = ["Here's what I have saved:"]
        for item in items[:12]:
            lines.append(f"- {item.get('normalized_text')}")
        return "\n".join(lines)
    if operation == "forget_memory":
        if result.get("forgot"):
            return "Got it. I removed that memory."
        return result.get("reason") or "I couldn't find a matching memory to remove."
    if operation == "update_memory":
        if result.get("updated"):
            memory = result.get("memory") or {}
            return f"Updated: {memory.get('normalized_text')}"
        return result.get("reason") or "I couldn't find a matching memory to update."
    if result.get("saved"):
        return "Got it. I'll keep that in mind."
    return result.get("reason") or "I did not save that as memory."


def _session_turns_from_history(
    *,
    history: list[dict[str, Any]] | None,
    latest_question: str,
    latest_answer: str,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _safe_summary_text(turn.get("content") or turn.get("answer") or "", limit=800)
        if content:
            turns.append({"role": role, "content": content, "turn_id": str(turn.get("id") or turn.get("turn_id") or "")})
    question = _safe_summary_text(latest_question, limit=800)
    answer = _safe_summary_text(latest_answer, limit=1000)
    if question:
        turns.append({"role": "user", "content": question, "turn_id": "latest_user"})
    if answer:
        turns.append({"role": "assistant", "content": answer, "turn_id": "latest_assistant"})
    return turns[-max(2, SESSION_SUMMARY_MAX_TURNS * 2):]


def _session_transcript_for_prompt(turns: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for idx, turn in enumerate((turns or [])[-max(2, SESSION_SUMMARY_MAX_TURNS * 2):], start=1):
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _safe_summary_text(turn.get("content") or "", limit=900)
        if not content:
            continue
        lines.append(f"T{idx} {role}: {content}")
    return "\n".join(lines)[:7000]


def _safe_session_summary(payload: dict[str, Any], *, turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    should_store = bool(payload.get("should_store"))
    summary = _safe_summary_text(payload.get("summary") or payload.get("summary_text") or "", limit=260)
    topics = _safe_summary_list(payload.get("topics"), limit=6, item_limit=48)
    user_goals = _safe_summary_list(payload.get("user_goals"), limit=4, item_limit=120)
    followups = _safe_summary_list(payload.get("unresolved_followups"), limit=4, item_limit=120)
    preferences = _safe_summary_list(payload.get("preferences_seen"), limit=4, item_limit=120)
    stress = _safe_stress_signals(payload.get("stress_or_sensitivity_signals") or payload.get("stress_signals"))
    evidence_refs = _safe_summary_list(payload.get("evidence_refs"), limit=6, item_limit=72)
    if not should_store:
        return {
            "should_store": False,
            "summary": summary,
            "topics": topics,
            "user_goals": user_goals,
            "unresolved_followups": followups,
            "preferences_seen": preferences,
            "stress_or_sensitivity_signals": stress,
            "evidence_refs": evidence_refs,
            "confidence": _bounded_confidence(payload.get("confidence"), default=0.0),
            "source_turn_ids": _source_turn_ids(turns),
        }
    if not summary:
        return None
    if _summary_contains_disallowed_finance_fact(summary):
        return None
    cleaned_lists = [topics, user_goals, followups, preferences, stress]
    if not any(cleaned_lists):
        return None
    return {
        "should_store": True,
        "summary": summary,
        "topics": topics,
        "user_goals": user_goals,
        "unresolved_followups": followups,
        "preferences_seen": preferences,
        "stress_or_sensitivity_signals": stress,
        "evidence_refs": evidence_refs,
        "confidence": _bounded_confidence(payload.get("confidence"), default=0.75),
        "source_turn_ids": _source_turn_ids(turns),
    }


def _safe_summary_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    text = re.sub(r"<[^>]{1,80}>", "", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip(" ,;:") + "..."


def _safe_summary_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    items: list[str] = []
    for raw in raw_items:
        item = _safe_summary_text(raw, limit=item_limit)
        if not item:
            continue
        if _summary_contains_secret_like_text(item) or _summary_contains_disallowed_finance_fact(item):
            continue
        if item not in items:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def _safe_stress_signals(value: Any) -> list[str]:
    signals = _safe_summary_list(value, limit=2, item_limit=120)
    allowed: list[str] = []
    for item in signals:
        lowered = _normalize_for_match(item)
        if set(_tokens(lowered)) & _SENSITIVE_TERMS or re.search(r"\b(?:stress|stressed|anxious|worried|pressure|concerned)\b", lowered):
            allowed.append(item)
    return allowed


def _summary_contains_disallowed_finance_fact(text: str) -> bool:
    lowered = _normalize_for_match(text)
    if _AMOUNT_RE.search(text) and _DERIVABLE_FINANCE_RE.search(lowered):
        return True
    if re.search(r"\b(?:account number|routing number|ssn|password|passcode|secret)\b", lowered):
        return True
    return False


def _summary_contains_secret_like_text(text: str) -> bool:
    lowered = _normalize_for_match(text)
    if re.search(r"\b(?:ssn|password|passcode|routing number|account number|secret)\b", lowered):
        return True
    return bool(re.search(r"\b(?:account|acct|card)\s+(?:ending\s+)?(?:in\s+)?[x*\- ]*\d{3,}\b", lowered))


def _bounded_confidence(value: Any, *, default: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return max(0.0, min(confidence, 1.0))


def _source_turn_ids(turns: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for turn in turns or []:
        turn_id = str((turn or {}).get("turn_id") or "").strip()
        if turn_id and turn_id not in ids:
            ids.append(turn_id)
    return ids[:12]


def _session_summary_key(summary: dict[str, Any], profile: str | None) -> str:
    payload = {
        "profile": profile or "household",
        "summary": summary.get("summary") or "",
        "topics": summary.get("topics") or [],
        "goals": summary.get("user_goals") or [],
        "followups": summary.get("unresolved_followups") or [],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _public_session_summary(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for db_key, public_key in (
        ("topics_json", "topics"),
        ("user_goals_json", "user_goals"),
        ("unresolved_followups_json", "unresolved_followups"),
        ("preferences_seen_json", "preferences_seen"),
        ("stress_signals_json", "stress_or_sensitivity_signals"),
        ("evidence_refs_json", "evidence_refs"),
        ("source_turn_ids_json", "source_turn_ids"),
    ):
        try:
            out[public_key] = json.loads(out.get(db_key) or "[]")
        except Exception:
            out[public_key] = []
        out.pop(db_key, None)
    try:
        out["metadata"] = json.loads(out.get("metadata_json") or "{}")
    except Exception:
        out["metadata"] = {}
    out.pop("metadata_json", None)
    try:
        out["confidence"] = float(out.get("confidence") or 0)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    return out


def _rank_session_summaries(
    summaries: list[dict[str, Any]],
    question: str,
    retrieval: dict[str, Any],
) -> list[dict[str, Any]]:
    query_tokens = {token for token in _tokens(question) if token not in _STOPWORDS}
    topic_hints = set(str(item).lower() for item in retrieval.get("topic_hints") or [] if item)
    intent = str(retrieval.get("intent") or "")
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for item in summaries:
        if str(item.get("status") or "active") != "active":
            continue
        text = " ".join(
            [
                str(item.get("summary_text") or ""),
                " ".join(item.get("topics") or []),
                " ".join(item.get("user_goals") or []),
                " ".join(item.get("unresolved_followups") or []),
                " ".join(item.get("preferences_seen") or []),
            ]
        )
        tokens = {token for token in _tokens(text) if token not in _STOPWORDS}
        score = float(len(query_tokens & tokens))
        item_topics = {_canonical_topic(topic) for topic in item.get("topics") or []}
        score += 5.0 * len(topic_hints & item_topics)
        if intent in {"goal_followup", "affordability_coaching"} and (item.get("user_goals") or item.get("unresolved_followups")):
            score += 2.0
        if intent == "casual_persona" and item.get("preferences_seen"):
            score += 1.5
        try:
            score += max(0.0, min(float(item.get("confidence") or 0), 1.0))
        except (TypeError, ValueError):
            pass
        if score <= 0 and intent != "memory_management":
            continue
        ranked.append((score, int(item.get("id") or 0), item))
    ranked.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return [item for _score, _id, item in ranked]


def _session_summary_trace(
    summaries: list[dict[str, Any]],
    *,
    allowed: bool,
    reason: str,
    retrieval: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": 1,
        "allowed": bool(allowed),
        "used": bool(summaries),
        "reason": reason,
        "intent": str(retrieval.get("intent") or "none"),
        "used_count": len(summaries),
        "used_summary_ids": [item.get("id") for item in summaries if item.get("id") is not None],
        "topic_hints": list(retrieval.get("topic_hints") or []),
    }


def _compact_session_summary_packet(
    summaries: list[dict[str, Any]],
    *,
    allowed: bool,
    reason: str,
    retrieval: dict[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in summaries[:4]:
        items.append(
            {
                "id": str(item.get("id")),
                "summary": _safe_summary_text(item.get("summary_text"), limit=220),
                "topics": list(item.get("topics") or [])[:4],
                "user_goals": list(item.get("user_goals") or [])[:2],
                "unresolved_followups": list(item.get("unresolved_followups") or [])[:2],
                "preferences_seen": list(item.get("preferences_seen") or [])[:2],
                "confidence": _confidence_label(item.get("confidence")),
            }
        )
    return {
        "version": 1,
        "allowed": bool(allowed),
        "used": bool(allowed and items),
        "reason": reason,
        "intent": str(retrieval.get("intent") or "none"),
        "items": items,
        "topic_hints": list(retrieval.get("topic_hints") or []),
    }


def _find_duplicate(conn: sqlite3.Connection, profile: str | None, memory_type: str, normalized_text: str) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM mira_memories
        WHERE (? IS NULL OR profile_id = ? OR profile_id IS NULL OR scope = 'household')
          AND memory_type = ?
          AND LOWER(normalized_text) = LOWER(?)
          AND status = 'active'
        LIMIT 1
        """,
        (profile, profile, memory_type, normalized_text),
    ).fetchone()
    return int(row["id"]) if row else None


def _infer_memory_type(lowered: str) -> str | None:
    if re.search(r"\b(?:don't|dont|do not|never)\s+(?:joke|roast|tease)\b", lowered):
        return "tone_preference"
    if re.search(r"\b(?:prefer|prefers|preference|like|likes)\b.*\b(?:short|concise|brief|serious|tone|answers?|replies|no jokes?)\b", lowered):
        return "tone_preference"
    if re.search(r"\b(?:prefer|prefers|preference|like|likes|dislike|dislikes|hate|hates)\b", lowered):
        return "preference"
    if re.search(r"\b(?:anxious|worried|stressed|stress|concerned)\b", lowered):
        return "stressor"
    if re.search(r"\b(?:under|below|cap|limit|keep .+ under|avoid|must|need to)\b", lowered):
        return "constraint"
    if re.search(r"\b(?:trying to|working on|committed to)\b", lowered):
        return "commitment"
    if re.search(r"\b(?:goal|save for|saving for|want to save|i want)\b", lowered):
        return "goal"
    if re.search(r"\b(?:i am|i'm|my job|my family|i work)\b", lowered):
        return "identity_fact"
    return None


def _normalize_memory_text(text: str, memory_type: str) -> str:
    cleaned = " ".join((text or "").strip(" .").split())
    if not cleaned:
        return ""
    lowered = _normalize_for_match(cleaned)
    cleaned = re.sub(r"^(?:that\s+)?", "", cleaned, flags=re.I).strip()
    if memory_type == "tone_preference" and re.search(r"\b(?:don't|dont|do not|never)\s+(?:joke|roast|tease)\b", lowered):
        topic = _topic_after_about(cleaned) or _infer_topic(lowered, cleaned)
        verb = "jokes" if "joke" in lowered else "roasts"
        return f"User does not want {verb} about {topic}."
    first_person = _third_person_memory_text(cleaned)
    if first_person:
        return first_person.rstrip(".") + "."
    return cleaned[0].upper() + cleaned[1:] + "."


def _third_person_memory_text(text: str) -> str:
    cleaned = " ".join((text or "").strip().split())
    patterns = (
        (r"^i\s+want\s+to\s+", "User wants to "),
        (r"^i\s+prefer\s+", "User prefers "),
        (r"^i\s+like\s+", "User likes "),
        (r"^i\s+dislike\s+", "User dislikes "),
        (r"^i\s+hate\s+", "User hates "),
        (r"^i(?:'m| am)\s+trying\s+to\s+", "User is trying to "),
        (r"^i(?:'m| am)\s+working\s+on\s+", "User is working on "),
        (r"^i(?:'m| am)\s+committed\s+to\s+", "User is committed to "),
        (r"^i(?:'m| am)\s+anxious\s+about\s+", "User is anxious about "),
        (r"^i(?:'m| am)\s+worried\s+about\s+", "User is worried about "),
        (r"^i(?:'m| am)\s+stressed\s+about\s+", "User is stressed about "),
        (r"^we\s+want\s+to\s+", "User's household wants to "),
        (r"^we(?:'re| are)\s+trying\s+to\s+", "User's household is trying to "),
        (r"^our\s+", "User's household "),
        (r"^my\s+", "User's "),
    )
    for pattern, replacement in patterns:
        if re.search(pattern, cleaned, re.I):
            return re.sub(pattern, replacement, cleaned, count=1, flags=re.I)
    if cleaned.lower().startswith(("i ", "i'm ", "i am ", "we ")):
        return "User " + cleaned[0].lower() + cleaned[1:]
    return ""


def _looks_like_rejected_finance_fact(lowered: str, memory_type: str, explicit: bool) -> bool:
    if memory_type in {"constraint", "goal"} and re.search(r"\b(?:under|below|cap|limit|save|saving)\b", lowered):
        return False
    if explicit and not re.search(r"\b(?:spent|paid|transaction|balance|net worth)\b", lowered):
        return False
    if _DERIVABLE_FINANCE_RE.search(lowered) and _AMOUNT_RE.search(lowered):
        return True
    if re.search(r"\b(?:how much|latest transaction|last transaction)\b", lowered):
        return True
    return False


def _safe_scout_topic(value: Any) -> str:
    topic = _canonical_topic(str(value or "").strip())[:48]
    return topic or "general"


def _short_reason(value: Any) -> str:
    reason = " ".join(str(value or "").strip().split())
    if len(reason) <= 140:
        return reason
    return reason[:137].rstrip() + "..."


def _suggestion_reason(memory_type: str) -> str:
    if memory_type == "tone_preference":
        return "You stated a durable conversation preference."
    if memory_type == "preference":
        return "You stated a durable preference."
    if memory_type in {"goal", "constraint", "commitment"}:
        return "You stated a durable goal or constraint."
    if memory_type == "stressor":
        return "You stated a recurring concern that could help Mira respond better."
    return "You stated durable context that could help Mira respond better."


def _short_user_quote(text: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    if len(cleaned) <= 140:
        return cleaned
    return cleaned[:137].rstrip() + "..."


def _first_person_stated(lowered: str) -> bool:
    return bool(re.search(r"\b(?:i|im|i'm|i am|my|we|our)\b", lowered))


def _infer_sensitivity(lowered: str, memory_type: str, topic: str) -> str:
    if memory_type == "stressor":
        return "high" if set(_tokens(lowered)) & _SENSITIVE_TERMS else "medium"
    if topic in _SENSITIVE_TERMS or set(_tokens(lowered)) & _SENSITIVE_TERMS:
        return "high"
    if memory_type in {"constraint", "rejected_advice", "coaching_state"}:
        return "medium"
    return "low"


def _infer_topic(lowered: str, normalized: str) -> str:
    about = _topic_after_about(normalized)
    if about:
        return about.lower()
    tokens = [token for token in _tokens(lowered or normalized) if token not in _STOPWORDS]
    for token in tokens:
        if token in {"dining", "debt", "coffee", "house", "summaries", "summary", "budget", "rent"}:
            return "weekly summaries" if token in {"summaries", "summary"} else token
    return tokens[-1] if tokens else "general"


def _topic_after_about(text: str) -> str:
    match = re.search(r"\babout\s+(.+?)(?:[.!?]|$)", text, re.I)
    if not match:
        return ""
    return " ".join(match.group(1).strip().split()[:4]).strip(" .")


def _rank_memories(memories: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    retrieval = {
        "intent": "memory_management",
        "allowed_types": list(MEMORY_TYPES),
        "topic_hints": _topic_hints(query, None),
        "max_items": 12,
    }
    ranked, _excluded = _rank_memory_candidates(memories, query, retrieval)
    return ranked


def _rank_memory_candidates(
    memories: list[dict[str, Any]],
    query: str,
    retrieval: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    query_tokens = {token for token in _tokens(query) if token not in _STOPWORDS}
    topic_hints = set(str(item).lower() for item in retrieval.get("topic_hints") or [] if item)
    allowed_types = set(str(item) for item in retrieval.get("allowed_types") or [])
    intent = str(retrieval.get("intent") or "none")
    excluded: dict[str, int] = {}
    ranked: list[tuple[float, int, dict[str, Any]]] = []

    def reject(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for item in memories:
        memory_type = str(item.get("memory_type") or "")
        if allowed_types and memory_type not in allowed_types:
            reject("type_not_allowed")
            continue
        if str(item.get("status") or "active") != "active":
            reject("inactive")
            continue
        if _memory_is_expired(item) and not item.get("pinned") and intent != "memory_management":
            reject("expired")
            continue
        if item.get("sensitivity") == "high" and not _sensitive_memory_relevant(item, query_tokens, topic_hints, intent):
            reject("sensitive_not_relevant")
            continue
        if intent != "memory_management" and not _memory_topic_relevant(item, topic_hints, query_tokens, intent):
            reject("topic_mismatch")
            continue

        score = _memory_relevance_score(item, query_tokens, topic_hints, intent)
        if score <= 0 and intent != "memory_management":
            reject("no_relevance")
            continue
        ranked.append((score, int(item.get("id") or 0), item))

    ranked.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    return [item for _score, _id, item in ranked], excluded


def _memory_relevance_score(
    item: dict[str, Any],
    query_tokens: set[str],
    topic_hints: set[str],
    intent: str,
) -> float:
    memory_type = str(item.get("memory_type") or "")
    topic_tokens = _memory_topic_tokens(item)
    text_tokens = {
        token for token in _tokens(f"{item.get('topic') or ''} {item.get('normalized_text') or ''}")
        if token not in _STOPWORDS
    }
    overlap = len(query_tokens & text_tokens)
    topic_overlap = len(topic_hints & topic_tokens)
    score = float(overlap)
    if topic_overlap:
        score += 5.0 + topic_overlap
    if str(item.get("topic") or "").lower() in topic_hints:
        score += 3.0
    if memory_type in {"goal", "constraint", "commitment"}:
        if intent in {"affordability_coaching", "goal_followup"}:
            score += 2.0
        if query_tokens & {"afford", "budget", "goal", "goals", "track", "pace", "saving", "save", "spend"}:
            score += 1.5
    if memory_type == "stressor":
        if query_tokens & {"advice", "debt", "anxious", "worried", "stress", "stressed"}:
            score += 2.5
    if memory_type in {"tone_preference", "preference"}:
        if _style_memory_relevant(item, query_tokens, topic_hints, intent):
            score += 3.0
        elif query_tokens & {"joke", "roast", "tone", "serious", "short", "concise", "summaries"}:
            score += 1.5
    if item.get("pinned"):
        score += 0.5
    try:
        score += max(0.0, min(float(item.get("confidence") or 0), 1.0))
    except (TypeError, ValueError):
        pass
    if _memory_is_expired(item):
        score -= 0.25
    if intent == "memory_management" and score <= 0:
        score = 0.1
    return score


def _memory_topic_relevant(
    item: dict[str, Any],
    topic_hints: set[str],
    query_tokens: set[str],
    intent: str,
) -> bool:
    memory_type = str(item.get("memory_type") or "")
    memory_topics = _memory_topic_tokens(item)
    if memory_topics & topic_hints:
        return True
    if memory_type in {"tone_preference", "preference"}:
        return _style_memory_relevant(item, query_tokens, topic_hints, intent)
    return bool(memory_topics & query_tokens & _DOMAIN_TOPIC_TERMS)


def _memory_topic_tokens(item: dict[str, Any]) -> set[str]:
    topic = str(item.get("topic") or "").lower()
    tokens = {_canonical_topic(topic)} if topic and _canonical_topic(topic) not in _TOPIC_HINT_STOPWORDS else set()
    tokens.update(_canonical_topic(token) for token in _tokens(topic) if _canonical_topic(token) not in _TOPIC_HINT_STOPWORDS)
    text = str(item.get("normalized_text") or "")
    for token in _tokens(text):
        canonical = _canonical_topic(token)
        if canonical in _DOMAIN_TOPIC_TERMS or canonical in _STYLE_TOPIC_TERMS:
            tokens.add(canonical)
    return {token for token in tokens if token}


def _style_memory_relevant(
    item: dict[str, Any],
    query_tokens: set[str],
    topic_hints: set[str],
    intent: str,
) -> bool:
    memory_type = str(item.get("memory_type") or "")
    if memory_type not in {"tone_preference", "preference"}:
        return False
    tokens = _memory_topic_tokens(item)
    text_tokens = set(_tokens(str(item.get("normalized_text") or "")))
    domain_topics = tokens & _DOMAIN_TOPIC_TERMS
    query_topics = (query_tokens | topic_hints) & _DOMAIN_TOPIC_TERMS
    style_request = bool((query_tokens | topic_hints) & _STYLE_TOPIC_TERMS)
    style_overlap = bool((tokens | text_tokens) & _STYLE_TOPIC_TERMS)
    if domain_topics:
        return bool(domain_topics & query_topics) and (style_overlap or style_request or intent != "casual_persona")
    if query_tokens & _STYLE_TOPIC_TERMS:
        return style_overlap or bool((tokens | text_tokens) & query_tokens)
    if topic_hints & _STYLE_TOPIC_TERMS:
        return style_overlap
    return intent == "casual_persona" and style_overlap


def _sensitive_memory_relevant(
    item: dict[str, Any],
    query_tokens: set[str],
    topic_hints: set[str],
    intent: str,
) -> bool:
    tokens = _memory_topic_tokens(item)
    sensitive_tokens = tokens & _SENSITIVE_TERMS
    if sensitive_tokens and (sensitive_tokens & (query_tokens | topic_hints)):
        return True
    if tokens & topic_hints:
        return True
    return bool(query_tokens & {"debt", "anxious", "worried", "stress", "stressed"} and tokens & query_tokens)


def _memory_is_expired(item: dict[str, Any]) -> bool:
    raw = str(item.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
    return expires <= now


def _memory_intent(route: dict[str, Any] | None) -> str:
    route = route or {}
    operation = str(route.get("operation") or "").strip()
    intent = str(route.get("intent") or "").strip()
    if operation:
        return operation
    return intent or "unknown"


def _packet_sensitivity(value: Any) -> str:
    raw = str(value or "low").lower()
    if raw == "high":
        return "sensitive"
    if raw == "medium":
        return "caution"
    return "normal"


def _confidence_label(value: Any) -> str:
    try:
        confidence = float(value or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence >= 0.9:
        return "high"
    if confidence >= 0.7:
        return "medium"
    return "low"


def _memory_summary(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get("normalized_text") or "").strip().split())
    memory_type = str(item.get("memory_type") or "memory")
    topic = str(item.get("topic") or "general").strip()
    if not text:
        return f"{memory_type.replace('_', ' ').title()} about {topic}."

    summary = text.rstrip(".")
    summary = re.sub(r"^User\s+", "", summary, flags=re.I)
    summary = re.sub(r"^does not want\s+", "Does not want ", summary, flags=re.I)
    summary = re.sub(r"^wants\s+", "Wants ", summary, flags=re.I)
    summary = re.sub(r"^is\s+", "Is ", summary, flags=re.I)
    summary = re.sub(r"^prefers\s+", "Prefers ", summary, flags=re.I)
    summary = re.sub(r"^user\s+", "", summary, flags=re.I)
    if summary and summary[0].islower():
        summary = summary[0].upper() + summary[1:]
    if not summary:
        summary = f"{memory_type.replace('_', ' ').title()} about {topic}"
    return summary[:220].rstrip(" ,;:") + "."


def _prompt_safe_memory_summary(value: Any) -> str:
    summary = " ".join(str(value or "").strip().split())
    if not summary:
        return ""
    if _DERIVABLE_FINANCE_RE.search(summary) and _AMOUNT_RE.search(summary):
        return ""
    return summary[:180].rstrip(" ,;:")


def _estimated_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _amount_from_text(text: str) -> float | None:
    match = _AMOUNT_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(0).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _normalize_for_match(text))


def _normalize_for_match(text: str) -> str:
    return (
        (text or "")
        .lower()
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def _public_memory(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    try:
        row["metadata"] = json.loads(row.get("metadata_json") or "{}")
    except Exception:
        row["metadata"] = {}
    row.pop("metadata_json", None)
    row["pinned"] = bool(row.get("pinned"))
    try:
        row["confidence"] = float(row.get("confidence") or 0)
    except (TypeError, ValueError):
        row["confidence"] = 0.0
    return row


def _log_event(
    conn: sqlite3.Connection,
    memory_id: int,
    profile: str | None,
    event_type: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
    source_turn_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO mira_memory_events (memory_id, profile_id, event_type, before_json, after_json, reason, source_turn_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            profile,
            event_type,
            json.dumps(before or {}, sort_keys=True, default=str),
            json.dumps(after or {}, sort_keys=True, default=str),
            reason,
            source_turn_id,
        ),
    )
