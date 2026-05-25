"""
Copilot agent loop. Provider-agnostic tool-use via llm_client.chat_with_tools.

- System prompt pre-loads a live orientation block so cheap questions resolve
  in a single LLM turn with zero tool calls.
- Up to MAX_ITERATIONS rounds of tool calls, then forced final answer.
- Request-scoped cache dedups identical tool+args within a run.
- Optional conversation history: previous user turns + assistant final answers.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

import llm_client
from copilot_context import build_copilot_context
from copilot_tools import TOOL_REGISTRY, execute_tool
from database import get_db
import memory
from mira.grounding import resolve_category_name
from range_parser import chart_months, words as range_words

logger = logging.getLogger(__name__)


class _RetiredPersonaShim:
    @staticmethod
    def core_voice_guide() -> str:
        return "Answer in a concise, grounded Folio voice."

    @staticmethod
    def persona_prompt_block() -> str:
        return ""


persona = _RetiredPersonaShim()


def compose_persona_answer(answer: str, **_: Any) -> str:
    return answer


def guard_finance_numbers(answer: str, **_: Any) -> str:
    return answer


def guard_no_tool_finance_answer(answer: str, **_: Any) -> str:
    return answer

MAX_ITERATIONS = 3
TOOL_RESULT_PAYLOAD_LIMIT = 12000  # chars
HISTORY_TURN_LIMIT = 6              # last N user/assistant pairs

SUMMARY_TOOLS = (
    "get_month_summary",
    "get_top_categories",
    "get_top_merchants",
    "get_recurring_summary",
    "get_summary",
    "get_account_balances",
)
DRILLDOWN_TOOLS = (
    "get_category_spend",
    "get_merchant_spend",
    "get_transactions",
    "get_transactions_for_merchant",
    "get_top_categories",
    "get_top_merchants",
)
WRITE_TOOLS = (
    "preview_bulk_recategorize",
    "preview_create_rule",
    "preview_rename_merchant",
    "preview_set_budget",
    "preview_create_goal",
    "preview_update_goal_target",
    "preview_mark_goal_funded",
    "preview_set_transaction_note",
    "preview_set_transaction_tags",
    "preview_mark_reviewed",
    "preview_bulk_mark_reviewed",
    "preview_update_manual_account_balance",
    "preview_split_transaction",
    "preview_confirm_recurring_obligation",
    "preview_dismiss_recurring_obligation",
    "preview_cancel_recurring",
    "preview_restore_recurring",
)
CHART_TOOLS = ("plot_chart",)
TREND_CHART_TOOLS = (
    "get_monthly_spending_trend",
    "get_net_worth_trend",
    "get_top_categories",
    "get_category_spend",
    "get_top_merchants",
    "plot_chart",
)
SQL_SCHEMA_ON_DEMAND = """SQL schema notes for run_sql:
- Internal/debug only. Normal Mira routing must use specialized grounded tools instead of run_sql.
- Read transactions through transactions_visible. Spending filter: amount < 0, category NOT IN ('Savings Transfer','Personal Transfer','Credit Card Payment','Income','Credits & Refunds'), and expense_type not internal/household transfer.
- Useful tables: transactions_visible(id, account_id, profile_id, date, description, amount, category, merchant_name, is_excluded, expense_type), accounts(id, profile_id, account_name, account_type, current_balance), categories(name, expense_type), category_rules(pattern, category), merchants(clean_name, category, total_spent), net_worth_history(date, profile_id, total_assets, total_owed, net_worth), saved_insights(question, answer, kind, pinned).
- profile='household' means omit a profile filter.
"""

CHART_RECIPE_ON_DEMAND = """Chart recipe:
- To chart net worth: call get_net_worth_trend, then plot_chart(type='line') using date/month labels and net_worth values.
- To chart spending over months: call get_monthly_spending_trend, then plot_chart(type='line') using labels and values.
- To chart top categories/merchants: fetch the data first, then plot_chart(type='bar' or 'donut').
"""

# Keys in tool results that typically carry user-displayable list data,
# ordered by preference (first match wins).
_DISPLAY_LIST_KEYS = (
    "transactions",  # get_transactions_for_merchant, get_transactions inner
    "data",          # get_transactions (paginated shape: {data, total_count})
    "recent",        # get_category_spend / get_merchant_spend recent txns
    "merchants",     # get_top_merchants, get_merchant_spend matched_merchants
    "matched_merchants",
    "categories",    # get_top_categories, get_category_breakdown
    "items",         # get_recurring_summary
    "accounts",      # get_account_balances
    "rows",          # internal SQL debug
    "series",        # get_net_worth_trend
    "rules",         # get_category_rules
    "insights",      # search_saved_insights
)


def _uniq_tools(*groups: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for name in group:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
    return ordered


def _looks_financial(question: str) -> bool:
    q = question.lower()
    terms = (
        "spend", "spent", "income", "budget", "merchant", "category", "transaction",
        "subscription", "recurring", "cash", "balance", "account", "net worth",
        "saving", "savings", "month", "week", "ytd", "money", "card", "payment",
        "rent", "grocery", "groceries", "restaurant", "dining", "watch", "runway",
    )
    return "$" in q or any(t in q for t in terms)


def _is_watch_question(question: str) -> bool:
    q = question.lower().strip()
    has_watch = any(p in q for p in ("what should i watch", "watch this month", "watch out", "keep an eye", "anything concerning", "anything i should worry"))
    has_period = any(p in q for p in ("this month", "current month", "month", "right now", "lately"))
    return has_watch and has_period


def _history_text(history: list[dict] | None, limit: int = 4) -> str:
    if not history:
        return ""
    parts: list[str] = []
    older = history[:-limit] if len(history) > limit else []
    older_user_notes = []
    for turn in older:
        if turn.get("role") != "user":
            continue
        content = " ".join((turn.get("content") or "").split())
        if content:
            older_user_notes.append(content[:140])
    if older_user_notes:
        parts.append("Earlier user asks: " + " | ".join(older_user_notes[-3:]))
    for turn in history[-limit:]:
        content = (turn.get("content") or "").strip()
        if content:
            parts.append(content)
    return "\n".join(parts)


def _extract_month_count(question: str, history: list[dict] | None = None) -> int:
    context = f"{_history_text(history)}\n{question}".lower()
    return chart_months(context, fallback=6)


def _extract_spending_category(question: str, history: list[dict] | None = None) -> str | None:
    context = f"{_history_text(history)}\n{question}".lower()
    grounded = resolve_category_name(context)
    if grounded:
        return grounded
    known = _extract_known_category_from_text(context)
    if known:
        return known
    return None


def _normalize_subject_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _extract_known_category_from_text(text: str) -> str | None:
    normalized = f" {_normalize_subject_text(text)} "
    if not normalized.strip():
        return None
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT name FROM categories WHERE COALESCE(name, '') != ''").fetchall()
    except Exception:
        return None
    candidates: list[str] = []
    for row in rows:
        name = row["name"] if hasattr(row, "keys") else row[0]
        if not name:
            continue
        norm_name = _normalize_subject_text(str(name))
        if norm_name and f" {norm_name} " in normalized:
            candidates.append(str(name))
    if not candidates:
        return None
    return max(candidates, key=len)


def _extract_chart_spending_category(question: str, history: list[dict] | None = None) -> str | None:
    q = (question or "").lower()
    if re.search(r"\b(all categories|all spending|overall|total spending|total expenses?|financial expenses?)\b", q):
        return None
    current_category = _extract_spending_category(question, None)
    if current_category:
        return current_category
    if history and re.search(r"\b(that|those|same|every month|monthly|chart|graph|plot|trend)\b", q):
        return _extract_spending_category(_history_text(history, limit=6), None)
    return None


def _extra_prompt_for_tools(tool_names: list[str]) -> str:
    parts = []
    if "run_sql" in tool_names:
        parts.append(SQL_SCHEMA_ON_DEMAND)
    if "plot_chart" in tool_names:
        parts.append(CHART_RECIPE_ON_DEMAND)
    return "\n".join(parts)


def _fast_watch_system(profile: str | None) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return (
        "Your name is Mira. You're the user's Folio companion. "
        f"{persona.core_voice_guide()} "
        "Summarize the provided live finance data in 2-4 warm, direct sentences. "
        "Call out the biggest current-month watch item, cite concrete numbers, "
        "and do not invent data. No markdown table. "
        f"Today is {today}. Active profile: {profile or 'household'}."
    )


def _summarize_tool_payload(result: Any, *, key: str, limit: int = 5) -> Any:
    if not isinstance(result, dict):
        return result
    cloned = dict(result)
    value = cloned.get(key)
    if isinstance(value, list):
        cloned[key] = value[:limit]
    return cloned


def _execute_fast_watch_tools(profile: str | None, cache: dict) -> tuple[dict[str, Any], list[dict]]:
    payload: dict[str, Any] = {}
    trace: list[dict] = []
    calls = [
        ("get_recurring_summary", {"limit": 5}, "recurring", "items"),
        ("get_top_merchants", {"range": "current_month", "limit": 5}, "top_merchants", "merchants"),
        ("get_top_categories", {"range": "current_month", "limit": 5}, "top_categories", "categories"),
    ]
    for name, args, payload_key, list_key in calls:
        start = datetime.now()
        result = execute_tool(name, args, profile, cache=cache)
        duration_ms = int((datetime.now() - start).total_seconds() * 1000)
        trace.append({"name": name, "args": args, "duration_ms": duration_ms})
        payload[payload_key] = _summarize_tool_payload(result, key=list_key)
    return payload, trace


def _build_monthly_spending_chart(question: str, profile: str | None, history: list[dict] | None = None, cache: dict | None = None) -> tuple[str, dict, list[dict], dict]:
    cache = cache if cache is not None else {}
    months = _extract_month_count(question, history)
    category = _extract_chart_spending_category(question, history)
    trend_args = {"months": months}
    if category:
        trend_args["category"] = category

    trace: list[dict] = []
    start = datetime.now()
    trend = execute_tool("get_monthly_spending_trend", trend_args, profile, cache=cache)
    trace.append({
        "name": "get_monthly_spending_trend",
        "args": trend_args,
        "duration_ms": int((datetime.now() - start).total_seconds() * 1000),
    })

    labels = trend.get("labels", []) if isinstance(trend, dict) else []
    values = trend.get("values", []) if isinstance(trend, dict) else []
    subject = f"{category} spending" if category else "spending"
    chart_args = {
        "type": "line",
        "title": f"{subject.title()} - Last {months} Months",
        "series_name": subject.title(),
        "labels": labels,
        "values": values,
        "unit": "currency",
    }
    start = datetime.now()
    chart = execute_tool("plot_chart", chart_args, profile, cache=cache)
    trace.append({
        "name": "plot_chart",
        "args": chart_args,
        "duration_ms": int((datetime.now() - start).total_seconds() * 1000),
    })

    if values:
        latest = values[-1]
        peak_idx = max(range(len(values)), key=lambda i: values[i])
        answer = (
            f"Done — here's your {subject} trend for the last {months} months. "
            f"Latest month is ${latest:,.2f}; peak was {labels[peak_idx]} at ${values[peak_idx]:,.2f}."
        )
    else:
        answer = f"I couldn't find {subject} data for the last {months} months."
    return answer, trend if isinstance(trend, dict) else {}, trace, chart if isinstance(chart, dict) else {}


def _latest_monthly_points(series: list[dict]) -> tuple[list[str], list[float], list[dict]]:
    by_month: dict[str, dict] = {}
    for point in series:
        date = str(point.get("date") or point.get("month") or "")
        if not date:
            continue
        month = date[:7]
        by_month[month] = point
    labels = sorted(by_month)
    values: list[float] = []
    rows: list[dict] = []
    for month in labels:
        point = by_month[month]
        raw = point.get("net_worth", point.get("value", point.get("total", 0)))
        try:
            value = round(float(raw or 0), 2)
        except (TypeError, ValueError):
            value = 0.0
        values.append(value)
        rows.append({"month": month, "net_worth": value})
    return labels, values, rows


def _build_net_worth_chart(profile: str | None, cache: dict | None = None) -> tuple[str, dict, list[dict], dict]:
    cache = cache if cache is not None else {}
    trace: list[dict] = []
    args = {"interval": "monthly", "limit": 24}
    start = datetime.now()
    trend = execute_tool("get_net_worth_trend", args, profile, cache=cache)
    trace.append({
        "name": "get_net_worth_trend",
        "args": args,
        "duration_ms": int((datetime.now() - start).total_seconds() * 1000),
    })

    raw_series = trend.get("series", []) if isinstance(trend, dict) else []
    labels, values, rows = _latest_monthly_points(raw_series)
    chart_args = {
        "type": "line",
        "title": "Net Worth Trend",
        "series_name": "Net Worth",
        "labels": labels,
        "values": values,
        "unit": "currency",
    }
    start = datetime.now()
    chart = execute_tool("plot_chart", chart_args, profile, cache=cache)
    trace.append({
        "name": "plot_chart",
        "args": chart_args,
        "duration_ms": int((datetime.now() - start).total_seconds() * 1000),
    })

    if values:
        answer = f"Done — here's your net worth trend. Latest month is ${values[-1]:,.2f}."
    else:
        answer = "I couldn't find net worth history to chart."
    trend_payload = {"series": rows, "labels": labels, "values": values}
    return answer, trend_payload, trace, chart if isinstance(chart, dict) else {}


def _can_execute_chart_plan(tool_names: list[str]) -> str | None:
    tools = set(tool_names)
    if {"get_net_worth_trend", "plot_chart"}.issubset(tools):
        return "net_worth"
    if {"get_monthly_spending_trend", "plot_chart"}.issubset(tools):
        return "spending"
    return None


def _execute_chart_plan(kind: str, question: str, profile: str | None, history: list[dict] | None, cache: dict) -> tuple[str, dict, list[dict], dict]:
    if kind == "net_worth":
        return _build_net_worth_chart(profile, cache)
    return _build_monthly_spending_chart(question, profile, history, cache)


def _finalize_answer(
    *,
    question: str,
    profile: str | None,
    raw_answer: str,
    trace: list[dict],
    cache: dict,
    iterations: int,
    run_detector: bool,
    route: dict | None = None,
    number_guard: bool = False,
    memory_trace: dict | None = None,
) -> dict:
    display_rows, display_source = _extract_display_data(trace, cache, profile)
    if number_guard:
        raw_answer = guard_finance_numbers(raw_answer, route=route, trace=trace, cache=cache, profile=profile)
    raw_answer = guard_no_tool_finance_answer(
        raw_answer,
        route=route,
        trace=trace,
        provenance_context_available=bool((route or {}).get("provenance_context_available")),
    )
    raw_answer = compose_persona_answer(
        raw_answer,
        question=question,
        route=route,
        trace=trace,
        cache=cache,
        profile=profile,
        memory_trace=memory_trace,
    )
    cleaned_answer, agent_props_raw, observations_logged, proposals_created = _persist_agent_tags(
        raw_answer=raw_answer, profile=profile,
    )
    if run_detector:
        detector_props = _persist_detector_signals(
            user_question=question,
            cleaned_answer=cleaned_answer or raw_answer,
            profile=profile,
            agent_proposals_raw=agent_props_raw,
        )
        proposals_created.extend(detector_props)
    answer_text = _fallback_when_empty(cleaned_answer, bool(proposals_created))
    return {
        "answer": answer_text,
        "tool_trace": trace,
        "iterations": iterations,
        "error": None,
        "data": display_rows,
        "data_source": display_source,
        "memory_proposals": proposals_created,
        "memory_observations": observations_logged,
    }


def _extract_display_data(trace: list[dict], tool_cache: dict, profile: str | None) -> tuple[list[dict] | None, str | None]:
    """
    Scan the tool trace back-to-front for the most recent list-shaped result.
    Returns (rows, source_tool_name) — rows for inline table render, or (None, None).
    """
    for call in reversed(trace):
        name = call.get("name")
        args = call.get("args") or {}
        cache_key = (name, json.dumps(args, sort_keys=True, default=str), profile)
        result = tool_cache.get(cache_key)
        if not isinstance(result, dict):
            continue
        for key in _DISPLAY_LIST_KEYS:
            value = result.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[:50], name
    return None, None


SYSTEM_PROMPT = """TOOL DISCIPLINE
- If you need a tool, emit no visible prose before the tool call.
- Treat the latest user message as authoritative. Use prior turns only for pronouns or explicit follow-ups, never to reuse a previous merchant/category/chart type when the latest message names a new one.
- The orientation block is only a truncated preview. For any specific time period, merchant, category, account balance, transaction list, write, or chart, call an appropriate tool.
- Time windows: current_month, last_month, this_week, last_week, last_7d/30d/90d/180d/365d, ytd, last_year, all, or YYYY-MM. Never reuse old turn numbers for a changed time window.
- Use specialized grounded tools for finance reads. Raw SQL is reserved for internal debug tooling.
- Specialized tools match dashboard aggregation. If a result disagrees with the UI, trust the dashboard and say you'll double-check.

FINANCE SEMANTICS
- Spending excludes Savings Transfer, Personal Transfer, Credit Card Payment, Income, Credits & Refunds, and internal/household transfers.
- Negative amount = money out. Positive amount = income/refund.
- Net category spend subtracts refunds. Merchant identity uses merchant_name, falling back to description.
- "Watch / keep an eye on" means financial concerns here. "Park my money" means where to hold cash. "Burn rate/runway" means spending velocity.

WRITES
- You may preview data changes only when the user explicitly asks to change data.
- Use the narrow preview_* write tools for categories, rules, merchant names, budgets, goals, transaction metadata/review/splits, manual account balances, and recurring-charge state. The UI shows Confirm/Cancel from the returned confirmation ID.
- After a preview, summarize the change briefly and stop.

CHARTS
- Use plot_chart only after fetching the underlying numbers. One chart max.
- line = trends, bar = comparisons, donut = composition. Skip charts for single-number answers.
- For monthly spending charts, call get_monthly_spending_trend, then call plot_chart with its labels and values. Use the user's requested months; if omitted, default to 6 months.

PERSISTENT MEMORY
- The orientation may include persistent memory about the user. Use it as background; do not restate it verbatim.
- At the very end only, after visible prose, you may emit hidden tags that the app strips:
  <observation theme="short_kebab_case">one-line note</observation>
  <memory_proposal section="identity|preferences|goals|concerns|open_questions" confidence="stated" evidence="quote or reason">one-line entry</memory_proposal>
- Use memory_proposal only for explicit user-stated enduring facts, preferences, commitments, goals, or contradictions. Most turns produce no tags.
"""


_OBSERVATION_THEME_TO_SECTION: dict[str, str] = {
    "runway_anxiety": "concerns",
    "response_length_pushback": "preferences",
}


def _section_for_observation(theme: str) -> str:
    """Best-effort mapping from observation theme → memory section. Defaults to concerns."""
    return _OBSERVATION_THEME_TO_SECTION.get(theme, "concerns")


def _persist_agent_tags(
    *,
    raw_answer: str,
    profile: str | None,
    source_conversation_id: int | None = None,
) -> tuple[str, list[dict], list[dict], list[dict]]:
    """
    FAST phase. Parses <observation>/<memory_proposal> tags out of the agent's
    reply, persists what's there, runs the cheap threshold check on observations.
    No LLM call — runs in the streaming critical path.

    Returns (cleaned_answer, agent_proposals_raw, observations_logged, proposals_created).
    `agent_proposals_raw` is needed by the slow phase to dedup detector output against.
    """
    cleaned, observations, agent_proposals = memory.parse_agent_memory_tags(raw_answer)
    obs_logged: list[dict] = []
    props_created: list[dict] = []

    if not observations and not agent_proposals:
        return cleaned, agent_proposals, obs_logged, props_created

    try:
        with get_db() as conn:
            for obs in observations:
                try:
                    obs_id = memory.log_observation(
                        profile=profile,
                        theme=obs["theme"],
                        note=obs["note"],
                        source_conversation_id=source_conversation_id,
                        conn=conn,
                    )
                    obs_logged.append({"id": obs_id, **obs})
                    promoted = memory.maybe_promote_observation(
                        profile=profile,
                        theme=obs["theme"],
                        section=_section_for_observation(obs["theme"]),
                        body=obs["note"],
                        source_conversation_id=source_conversation_id,
                        conn=conn,
                    )
                    if promoted:
                        promo = memory.get_proposal(promoted, conn) or {}
                        props_created.append(promo)
                except Exception:
                    logger.debug("failed to log observation %s", obs, exc_info=True)

            for prop in agent_proposals:
                try:
                    pid = memory.create_proposal(
                        profile=profile,
                        section=prop["section"],
                        body=prop["body"],
                        confidence=prop["confidence"],
                        evidence=prop.get("evidence", ""),
                        theme=prop.get("theme"),
                        source="agent",
                        source_conversation_id=source_conversation_id,
                        conn=conn,
                    )
                    created = memory.get_proposal(pid, conn) or {}
                    props_created.append(created)
                except Exception:
                    logger.debug("failed to create agent proposal %s", prop, exc_info=True)
    except Exception:
        logger.exception("agent-tag persistence failed")

    return cleaned, agent_proposals, obs_logged, props_created


def _persist_detector_signals(
    *,
    user_question: str,
    cleaned_answer: str,
    profile: str | None,
    agent_proposals_raw: list[dict],
    source_conversation_id: int | None = None,
) -> list[dict]:
    """
    SLOW phase. Runs the dedicated post-turn detector and persists any
    proposals it produces (deduplicated against agent-emitted ones).
    Safe to call after the streaming `done` event has been sent.
    """
    if not should_run_memory_detector(user_question, cleaned_answer):
        return []
    detected = memory.detect_memory_signals(user_question, cleaned_answer or "")
    if not detected:
        return []

    existing_bodies = {p.get("body", "").strip().lower() for p in agent_proposals_raw if p.get("body")}
    new_proposals: list[dict] = []
    try:
        with get_db() as conn:
            for sig in detected:
                if sig["body"].strip().lower() in existing_bodies:
                    continue
                try:
                    pid = memory.create_proposal(
                        profile=profile,
                        section=sig["section"],
                        body=sig["body"],
                        confidence=sig["confidence"],
                        evidence=sig.get("evidence", ""),
                        theme=None,
                        source="agent",
                        source_conversation_id=source_conversation_id,
                        conn=conn,
                    )
                    created = memory.get_proposal(pid, conn) or {}
                    new_proposals.append(created)
                except Exception:
                    logger.debug("failed to persist detector signal %s", sig, exc_info=True)
    except Exception:
        logger.exception("detector persistence failed")
    return new_proposals


def _has_phrase(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    if len(phrase) > len(tokens):
        return False
    width = len(phrase)
    return any(tuple(tokens[idx:idx + width]) == phrase for idx in range(len(tokens) - width + 1))


def should_run_memory_detector(user_question: str, cleaned_answer: str = "") -> bool:
    tokens = range_words(user_question)
    if not tokens:
        return False
    token_set = set(tokens)

    transient_terms = {
        "spend", "spent", "transaction", "transactions", "chart", "plot", "graph",
        "recategorize", "rename", "rule", "category", "merchant", "costco",
        "latest", "last", "month", "months", "week", "weeks", "ytd",
        "code", "python", "javascript", "sql",
    }
    if token_set & transient_terms and not ({"remember", "prefer", "preference", "goal", "trying", "want"} & token_set):
        return False

    first_person = {"i", "im", "i'm", "me", "my", "mine", "we", "our"} & token_set
    memory_terms = {
        "remember", "always", "never", "prefer", "preference", "goal", "trying",
        "working", "building", "planning", "worried", "concerned", "hate", "love",
        "like", "dislike", "avoid", "focus",
    }
    explicit_memory = _has_phrase(tokens, ("remember", "that")) or _has_phrase(tokens, ("keep", "in", "mind"))
    enduring_statement = bool(first_person and (token_set & memory_terms))
    answer_hint = "<memory_proposal" in (cleaned_answer or "") or "<observation" in (cleaned_answer or "")
    return explicit_memory or enduring_statement or answer_hint


def _fallback_when_empty(cleaned: str, has_signals: bool) -> str:
    """
    The agent occasionally emits a reply that's entirely tags + whitespace, leaving
    nothing visible after cleanup. If we have a proposal to show, give the user a
    short anchor sentence. Otherwise admit we have nothing.
    """
    stripped = (cleaned or "").strip()
    if stripped:
        return stripped
    if has_signals:
        return "Got it — I noted that."
    return "I didn't land on a confident answer this turn."


def _truncate_for_model(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=True, default=str)
    if len(text) > TOOL_RESULT_PAYLOAD_LIMIT:
        return text[:TOOL_RESULT_PAYLOAD_LIMIT] + "...[truncated]"
    return text


def _build_system_prompt(profile: str | None, tool_names: list[str] | None = None) -> str:
    def _build_orientation() -> str:
        try:
            with get_db() as conn:
                return build_copilot_context(profile, conn)
        except Exception:
            logger.exception("Failed to build orientation block")
            return ""

    try:
        import copilot_cache
        orientation = copilot_cache.get_prompt_fragment("orientation", copilot_cache.make_key(profile), _build_orientation)
    except Exception:
        orientation = _build_orientation()

    now = datetime.now().strftime("%Y-%m-%d")
    header = f"Today is {now}. Active profile: {profile or 'household'}."
    tool_context = _extra_prompt_for_tools(tool_names or [])
    blocks = [persona.persona_prompt_block(), SYSTEM_PROMPT, header, tool_context]
    if orientation:
        blocks.append(orientation)
    return "\n\n".join(block for block in blocks if block)


def _normalize_history(history: list[dict] | None) -> list[dict]:
    """Keep only user/assistant turns with text content. Drop tool traces."""
    if not history:
        return []
    cleaned = []
    for turn in history[-HISTORY_TURN_LIMIT * 2:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def _dispatcher_enabled() -> bool:
    return True


def _selected_schema_tokens(tool_names: list[str] | tuple[str, ...]) -> int:
    return 0


def _planned_tools_for_route(route: dict) -> list[str]:
    selected = route.get("selected_tools") if isinstance(route, dict) else None
    return [str(tool) for tool in selected] if isinstance(selected, list) else []


def route_question(
    question: str,
    history: list[dict] | None = None,
    forced_intent: str | None = None,
    profile: str | None = None,
) -> dict:
    from copilot_agents.dispatcher import route_question as classify_route

    return classify_route(question, history, forced_intent=forced_intent, profile=profile)


def run_agent(question: str, profile: str | None, history: list[dict] | None = None, forced_intent: str | None = None) -> dict:
    from copilot_agents.dispatcher import run_agent as run_dispatcher_agent

    dispatched = run_dispatcher_agent(question, profile, history, forced_intent=forced_intent)
    if dispatched is not None:
        return dispatched
    return {
        "answer": "I could not route that request cleanly.",
        "tool_trace": [],
        "iterations": 0,
        "error": "dispatcher returned no result",
        "data": None,
        "data_source": None,
    }


def run_agent_stream(question: str, profile: str | None, history: list[dict] | None = None, forced_intent: str | None = None):
    """
    Streaming variant of run_agent. Yields event dicts suitable for SSE:
      {type: 'status', message: str}
      {type: 'tool_call', name, args}
      {type: 'tool_result', name, duration_ms}
      {type: 'token', text}                      # incremental text from current turn
      {type: 'reset_text'}                       # emitted before a new turn that may supersede
      {type: 'done', answer, data, data_source, tool_trace, iterations}
      {type: 'error', message}
    """
    from copilot_agents.dispatcher import run_agent_stream as run_dispatcher_stream

    trace_token = llm_client.start_trace()
    try:
        dispatched = run_dispatcher_stream(question, profile, history, forced_intent=forced_intent)
        if dispatched is not None:
            for event in dispatched:
                if isinstance(event, dict) and event.get("type") == "done":
                    event = dict(event)
                    trace = dict(event.get("trace") or {})
                    llm_calls = llm_client.current_trace_calls()
                    trace["llm_calls"] = llm_calls
                    trace["llm_call_count"] = len(llm_calls)
                    trace["llm_call_summary"] = _summarize_llm_calls_for_trace(llm_calls)
                    event["trace"] = trace
                yield event
            return
        yield {"type": "error", "message": "Mira could not route that request cleanly."}
    finally:
        llm_client.finish_trace(trace_token)


def _summarize_llm_calls_for_trace(calls: list[dict]) -> dict:
    by_purpose: dict[str, int] = {}
    for call in calls or []:
        purpose = str(call.get("purpose") or "unknown")
        by_purpose[purpose] = by_purpose.get(purpose, 0) + 1
    return {
        "count": len(calls or []),
        "by_purpose": by_purpose,
    }


def _legacy_tool_names() -> list[str]:
    preferred = _uniq_tools(
        SUMMARY_TOOLS,
        DRILLDOWN_TOOLS,
        TREND_CHART_TOOLS,
        WRITE_TOOLS,
        ("get_category_breakdown", "get_dashboard_bundle", "get_net_worth_delta", "get_category_rules", "search_saved_insights"),
    )
    return [name for name in preferred if name in TOOL_REGISTRY]


def _run_legacy_escape_hatch(question: str, profile: str | None, history: list[dict] | None = None) -> dict:
    return {
        "answer": "The legacy Mira escape hatch has been retired. Mira vNext is the active copilot runtime.",
        "tool_trace": [],
        "iterations": 0,
        "error": "legacy escape hatch retired",
        "data": None,
        "data_source": None,
        "route": {
            "intent": "legacy_retired",
            "shortcut": "env_escape_hatch",
            "selected_tools": [],
            "tool_schema_tokens_est": 0,
        },
    }


def _run_legacy_escape_hatch_stream(question: str, profile: str | None, history: list[dict] | None = None):
    yield {
        "type": "error",
        "message": "The legacy Mira escape hatch has been retired. Mira vNext is the active copilot runtime.",
        "intent": "legacy_retired",
        "shortcut": "env_escape_hatch",
    }
