from __future__ import annotations

import copy
import os
import re
import threading
import time
from typing import Any, Iterable

import llm_client

from mira.agentic.schemas import EvidencePacket
from mira.agentic.vnext_answerer import VNextAnswerResult, build_recent_conversation_context
from mira.agentic.vnext_validator import validation_for_general_answer


_FALSE_ENV_VALUES = {"0", "false", "no", "off"}
_READ_ONLY_TABLE_INTENTS = {
    "transaction_lookup",
    "spending_top",
    "spending_breakdown",
    "budget_status",
    "budget_plan",
    "savings_capacity",
    "recurring_summary",
    "recurring_changes",
}
_READ_ONLY_TABLE_OUTPUTS = {"table", "list", "status", "scalar"}
_CHART_INTENTS = {
    "spending_trend",
    "net_worth_trend",
}
_EXPLAIN_COMPARE_INTENTS = {
    "spending_explain",
    "spending_compare",
    "cashflow_forecast",
    "cashflow_shortfall",
    "affordability",
    "net_worth_balance",
    "net_worth_delta",
    "finance_snapshot",
    "finance_priorities",
    "explain_metric",
    "data_health",
    "enrichment_quality",
    "low_confidence_transactions",
    "explain_transaction",
}
_EXPLAIN_COMPARE_OUTPUTS = {"table", "comparison", "status", "scalar", "list"}
_WRITE_PREVIEW_CHANGE_TYPES = {"bulk_recategorize", "create_rule", "set_budget"}
_NONEISH_PLAN_VALUES = {"", "none", "null", "unknown"}
_REWARM_LOCK = threading.Lock()
_REWARM_IN_FLIGHT = False
_REWARM_RUNNING = False
_REWARM_PENDING_TIMER: threading.Timer | None = None
_REWARM_PENDING_ID = ""
_REWARM_CANCELLED_IDS: list[str] = []
_REWARM_LAST_STARTED = 0.0
_REWARM_SEQUENCE = 0
_REWARM_EVENT_LIMIT = 50
_REWARM_STATS: dict[str, Any] = {
    "scheduled_count": 0,
    "started_count": 0,
    "completed_count": 0,
    "skipped_count": 0,
    "error_count": 0,
    "overlap_count": 0,
    "last_event": None,
    "events": [],
}
_CONTROLLER_ACTIVE_COUNT = 0
_CONTROLLER_LAST_ACTIVITY_AT = 0.0


FRONT_CONTROLLER_SYSTEM_PROMPT = """You are Mira, Folio's local-first AI companion.

Choose exactly one protocol. First output token must be CHAT or PLAN. Never
start with answer text; unlabeled answers are invalid.

CHAT
<user-visible reply>

PLAN
route=<finance|memory|write_preview|explain_last>
intent=<none|spending_total|spending_explain|spending_top|spending_breakdown|spending_trend|spending_compare|transaction_lookup|budget_status|budget_plan|savings_capacity|cashflow_forecast|cashflow_shortfall|affordability|recurring_summary|recurring_changes|net_worth_balance|net_worth_trend|net_worth_delta|finance_snapshot|finance_priorities|explain_metric|data_health|enrichment_quality|low_confidence_transactions|explain_transaction|memory_op|write_preview>
subject_kind=<merchant|category|account|metric|transaction|net_worth|self|unknown|none>
subject=<short text or none>
time=<this_month|last_month|ytd|all_time|last_Nd|last_N_months|last_year|custom|month_before_prior|next_month_after_prior|none>
time_a=<YYYY-MM-DD or none>
time_b=<YYYY-MM-DD or none>
discourse_action=<new|follow_up|correction>
output=<scalar|table|list|chart|status|preview|none>
chart_type=<line|bar|donut|none>
sort=<date_desc|amount_desc|none>
amount=<number or none>
change_type=<bulk_recategorize|create_rule|set_budget|none>
target_kind=<category|none>
target=<short text or none>
END

PLAN protocol:
- route is only the lane. Finance concepts such as finance_snapshot,
  finance_priorities, data_health, enrichment_quality, and net_worth_balance
  belong in intent, never in route.
- For route=finance, intent must be a finance intent, not none.
- For set_budget, put the category in subject and the numeric dollar value in
  amount. Do not put the category in target.

Decision:
- CHAT for greetings, broad chat, general knowledge, emotional support, privacy,
  chat-history/meta, natural memory asks, and general capability/access questions
  about what Mira can help with or how Folio data is accessed.
- PLAN when the user asks Mira to inspect, compute, summarize, review, diagnose,
  prioritize, forecast, find, compare, plot, explain, or change their actual
  Folio data. If money/data/action is ambiguous, choose PLAN.
- PLAN route=explain_last when the user asks how/why a prior finance answer was
  produced ("how did you answer that?", "how did you get that?").
- Never do finance math in CHAT. Hidden PLAN text is machine-only and plain.
- User-visible CHAT should sound warm, candid, lightly witty, and useful:
  best friend with receipts. No roasts, finance-only redirects, or "I can assist".

CHAT rules:
- Answer non-finance questions directly; never bounce science/code/travel/etc.
  back to finance. Science/explanation CHAT: 2-5 sentences.
- Bare greetings ("hey", "yo", "hi") are CHAT: one short varied line, no list.
  The first token still must be CHAT; put the greeting reply only after it.
- Capability/access questions are CHAT only when asking about Mira/Folio
  abilities or access in general. Describe user-facing categories only:
  everyday ideas, writing, planning, technology, science/general knowledge,
  decisions, plus Folio balances, accounts, transactions, spending, budgets,
  cash flow, recurring charges, net worth, goals, confidence, and receipts.
  For "what can you help with" style questions, mention broad non-finance help
  before Folio finance help; never answer as if Mira is finance-only.
  "What information do you have/access?" is CHAT unless the user asks you to
  inspect the current values. Do not list private backend tools or run dashboard
  snapshots. If the user asks for the current state, health, priorities, risks,
  or summary of their own Folio data, use PLAN.
- Privacy CHAT: local-first, local models on the user's device; explicit memory
  stores only what the user asks to save.
- Chat-history/meta questions like "where did we leave off?", "what were we
  talking about?", "what did I just ask?", "repeat that", and "summarize this
  chat" are CHAT. Use recent conversation context. Do not turn them into
  finance follow-ups just because the prior chat mentioned spending.
- Natural "remember..." requests are CHAT and must contain `/memory remember`;
  do not say got it, saved, learned, remembered, or keep it in mind unless the
  user actually used a slash command.
- Reactions like "wtf is this", "what is this", "that is wrong", or "I didn't
  ask for that" are CHAT unless asking for a new finance value/list/chart/change.

PLAN mapping:
- "how much did I spend/pay at X" -> finance spending_total scalar, subject
  merchant/category.
- show/recent/latest/biggest transactions -> finance transaction_lookup list;
  biggest/largest sort=amount_desc. "shopping transactions" is category Shopping.
- "when did I get my income last?" -> transaction_lookup, category Income,
  output=list, sort=date_desc.
- why/explain/breakdown/compare spending -> spending_explain, spending_breakdown,
  or spending_compare, output=table. Top merchants/categories -> spending_top.
- spending chart/trend -> spending_trend chart line. Net worth balance/trend/
  change -> net_worth_balance/net_worth_trend/net_worth_delta.
- budget -> budget_status/budget_plan; savings/goals -> savings_capacity;
  cashflow -> cashflow_forecast/cashflow_shortfall; $ affordability ->
  affordability with amount and output=status.
- money patterns/leaks/vices/what kind of spender/how do I save more/what
  would you do with my money -> finance_priorities or savings_capacity,
  output=status.
- dashboard/overview/snapshot/status -> finance_snapshot. account balances/
  current balances -> net_worth_balance. priorities/risks/pay attention/Mira noticed ->
  finance_priorities. data health/import health -> data_health. enrichment
  completeness/quality -> enrichment_quality. low confidence ->
  low_confidence_transactions.
- edits ("move all X to Y", "recategorize", "create rule", "set budget") use
  route=write_preview, intent=write_preview, output=preview. Never apply.
  set budget: change_type=set_budget, subject_kind=category, amount=<number>.
  recategorize/rule: change_type=bulk_recategorize or create_rule,
  subject_kind=merchant, target_kind=category.
- `/memory...` slash commands may use route=memory; other memory wording is CHAT.

Follow-up:
- Latest message wins. If it only changes subject/time, keep missing finance
  context from recent conversation.
- "what about X", "Amazon?", "vaping?", "all time", "last month", "month
  before", "before that" are PLAN when recent finance context exists.
- "month before"/"before that" -> time=month_before_prior.
- Broad words like "my spending", "all spending", "overall spending",
  "expenses", or "all spending man" clear prior merchant/category scope unless
  the current message names one.
- Recent/show transactions with no explicit range -> time=none. Rich known date
  ranges -> time=custom with ISO time_a/time_b. Emotional disclosure alone is
  CHAT; with a concrete Folio-data ask is PLAN.

Examples (emit only CHAT/PLAN output, not the input labels):
Input: hey
CHAT
There you are. Receipts awake, vibes reasonable.

Input: remember I prefer concise answers
CHAT
I can remember that, but use `/memory remember I prefer concise answers` so it
is explicit and inspectable.

Input: what is the Big Bang?
CHAT
The Big Bang was the universe's early hot, dense expansion; space itself was
doing the stretching.

Input: how did you answer that?
PLAN
route=explain_last
intent=none
subject_kind=none
subject=none
time=none
discourse_action=follow_up
output=status
END

Input: how much did I spend at Costco last month?
PLAN
route=finance
intent=spending_total
subject_kind=merchant
subject=Costco
time=last_month
discourse_action=new
output=scalar
END

Input: what about vaping the month before?
PLAN
route=finance
intent=spending_total
subject_kind=category
subject=Vaping
time=month_before_prior
discourse_action=follow_up
output=scalar
END

Input: move Netflix to Entertainment
PLAN
route=write_preview
intent=write_preview
subject_kind=merchant
subject=Netflix
time=none
discourse_action=new
output=preview
change_type=bulk_recategorize
target_kind=category
target=Entertainment
END
"""


def front_controller_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_PROTOCOL_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def chat_fast_lane_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_CHAT_FAST_LANE_ENABLED", "1")).strip().lower() not in _FALSE_ENV_VALUES


def finance_scalar_fast_lane_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_FINANCE_SCALAR_FAST_LANE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def read_only_table_fast_lane_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_READ_ONLY_TABLE_FAST_LANE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def chart_fast_lane_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_CHART_FAST_LANE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def explain_compare_fast_lane_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_EXPLAIN_COMPARE_FAST_LANE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def write_preview_fast_lane_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_WRITE_PREVIEW_FAST_LANE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def explain_last_fast_lane_enabled() -> bool:
    return str(os.getenv("MIRA_EXPLAIN_LAST_FAST_LANE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def front_controller_rewarm_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_REWARM_AFTER_EVIDENCE_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def front_controller_background_drain_enabled() -> bool:
    return str(os.getenv("MIRA_FRONT_CONTROLLER_BACKGROUND_DRAIN_ENABLED", "0")).strip().lower() not in _FALSE_ENV_VALUES


def front_controller_rewarm_max_tokens() -> int:
    try:
        return max(1, min(int(os.getenv("MIRA_FRONT_CONTROLLER_REWARM_MAX_TOKENS", "16")), 64))
    except (TypeError, ValueError):
        return 16


def front_controller_rewarm_delay_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("MIRA_FRONT_CONTROLLER_REWARM_DELAY_MS", "3000")) / 1000.0)
    except (TypeError, ValueError):
        return 3.0


def front_controller_rewarm_min_interval_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("MIRA_FRONT_CONTROLLER_REWARM_MIN_INTERVAL_SECONDS", "8")))
    except (TypeError, ValueError):
        return 8.0


def front_controller_rewarm_min_quiet_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("MIRA_FRONT_CONTROLLER_REWARM_MIN_QUIET_MS", "1200")) / 1000.0)
    except (TypeError, ValueError):
        return 1.2


def mark_front_controller_active() -> None:
    global _CONTROLLER_ACTIVE_COUNT, _CONTROLLER_LAST_ACTIVITY_AT
    with _REWARM_LOCK:
        _CONTROLLER_ACTIVE_COUNT += 1
        _CONTROLLER_LAST_ACTIVITY_AT = time.time()
        if _REWARM_IN_FLIGHT:
            if _REWARM_RUNNING:
                _record_rewarm_event_locked(
                    {
                        "status": "overlap",
                        "reason": "request_arrived_during_rewarm",
                        "running": True,
                        "active_controller_count": _CONTROLLER_ACTIVE_COUNT,
                    }
                )
            else:
                _cancel_pending_rewarm_locked(
                    reason="cancelled_by_user_request",
                    active_controller_count=_CONTROLLER_ACTIVE_COUNT,
                )


def mark_front_controller_inactive() -> None:
    global _CONTROLLER_ACTIVE_COUNT, _CONTROLLER_LAST_ACTIVITY_AT
    with _REWARM_LOCK:
        _CONTROLLER_ACTIVE_COUNT = max(0, _CONTROLLER_ACTIVE_COUNT - 1)
        _CONTROLLER_LAST_ACTIVITY_AT = time.time()


def front_controller_max_tokens() -> int:
    try:
        return max(64, int(os.getenv("MIRA_FRONT_CONTROLLER_MAX_TOKENS", "900")))
    except (TypeError, ValueError):
        return 900


def _next_rewarm_id_locked() -> str:
    global _REWARM_SEQUENCE
    _REWARM_SEQUENCE += 1
    return f"rewarm_{_REWARM_SEQUENCE}"


def _remember_cancelled_rewarm_locked(rewarm_id: str) -> None:
    if not rewarm_id:
        return
    _REWARM_CANCELLED_IDS.append(rewarm_id)
    del _REWARM_CANCELLED_IDS[:-_REWARM_EVENT_LIMIT]


def _cancel_pending_rewarm_locked(*, reason: str, **extra: Any) -> bool:
    global _REWARM_IN_FLIGHT, _REWARM_PENDING_TIMER, _REWARM_PENDING_ID
    if not _REWARM_IN_FLIGHT or _REWARM_RUNNING or not _REWARM_PENDING_ID:
        return False
    rewarm_id = _REWARM_PENDING_ID
    timer = _REWARM_PENDING_TIMER
    if timer is not None:
        timer.cancel()
    _remember_cancelled_rewarm_locked(rewarm_id)
    _record_rewarm_event_locked(
        {
            "status": "skipped",
            "rewarm_id": rewarm_id,
            "reason": reason,
            **extra,
        }
    )
    _REWARM_PENDING_TIMER = None
    _REWARM_PENDING_ID = ""
    _REWARM_IN_FLIGHT = False
    return True


def _record_rewarm_event_locked(event: dict[str, Any]) -> dict[str, Any]:
    status = str(event.get("status") or "unknown")
    recorded = {
        "at": round(time.time(), 3),
        **event,
        "status": status,
    }
    if status == "scheduled":
        _REWARM_STATS["scheduled_count"] = int(_REWARM_STATS.get("scheduled_count") or 0) + 1
    elif status == "started":
        _REWARM_STATS["started_count"] = int(_REWARM_STATS.get("started_count") or 0) + 1
    elif status == "completed":
        _REWARM_STATS["completed_count"] = int(_REWARM_STATS.get("completed_count") or 0) + 1
    elif status == "skipped":
        _REWARM_STATS["skipped_count"] = int(_REWARM_STATS.get("skipped_count") or 0) + 1
    elif status == "error":
        _REWARM_STATS["error_count"] = int(_REWARM_STATS.get("error_count") or 0) + 1
    elif status == "overlap":
        _REWARM_STATS["overlap_count"] = int(_REWARM_STATS.get("overlap_count") or 0) + 1
    events = _REWARM_STATS.setdefault("events", [])
    if isinstance(events, list):
        events.append(recorded)
        del events[:-_REWARM_EVENT_LIMIT]
    _REWARM_STATS["last_event"] = recorded
    return recorded


def _compact_rewarm_llm_call(call: dict[str, Any]) -> dict[str, Any]:
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
    return {key: copy.deepcopy(call.get(key)) for key in allowed if key in call}


def front_controller_rewarm_stats_snapshot() -> dict[str, Any]:
    with _REWARM_LOCK:
        return {
            "enabled": front_controller_rewarm_enabled(),
            "delay_ms": round(front_controller_rewarm_delay_seconds() * 1000, 2),
            "max_tokens": front_controller_rewarm_max_tokens(),
            "min_interval_seconds": front_controller_rewarm_min_interval_seconds(),
            "min_quiet_seconds": front_controller_rewarm_min_quiet_seconds(),
            "in_flight": bool(_REWARM_IN_FLIGHT),
            "running": bool(_REWARM_RUNNING),
            "pending": bool(_REWARM_PENDING_ID),
            "pending_rewarm_id": _REWARM_PENDING_ID or None,
            "active_controller_count": _CONTROLLER_ACTIVE_COUNT,
            **copy.deepcopy(_REWARM_STATS),
        }


def reset_front_controller_rewarm_stats() -> None:
    global _REWARM_IN_FLIGHT, _REWARM_RUNNING, _REWARM_PENDING_TIMER, _REWARM_PENDING_ID, _REWARM_LAST_STARTED, _REWARM_SEQUENCE, _CONTROLLER_ACTIVE_COUNT, _CONTROLLER_LAST_ACTIVITY_AT
    with _REWARM_LOCK:
        if _REWARM_PENDING_TIMER is not None:
            _REWARM_PENDING_TIMER.cancel()
        _REWARM_IN_FLIGHT = False
        _REWARM_RUNNING = False
        _REWARM_PENDING_TIMER = None
        _REWARM_PENDING_ID = ""
        _REWARM_CANCELLED_IDS.clear()
        _REWARM_LAST_STARTED = 0.0
        _REWARM_SEQUENCE = 0
        _CONTROLLER_ACTIVE_COUNT = 0
        _CONTROLLER_LAST_ACTIVITY_AT = 0.0
        _REWARM_STATS.clear()
        _REWARM_STATS.update(
            {
                "scheduled_count": 0,
                "started_count": 0,
                "completed_count": 0,
                "skipped_count": 0,
                "error_count": 0,
                "overlap_count": 0,
                "last_event": None,
                "events": [],
            }
        )


def schedule_front_controller_rewarm(
    *,
    question: str,
    history: list[dict] | None = None,
    reason: str = "after_evidence_answer",
) -> dict[str, Any]:
    """Warm the controller prompt off the user-visible path.

    The result is advisory telemetry only. The rewarm never supplies a user
    answer and never changes routing; it simply drains a tiny controller request
    so Ollama can restore the front-controller prompt residency after an
    evidence-answer prompt.
    """

    if not front_controller_rewarm_enabled():
        return {"scheduled": False, "reason": "disabled"}
    if not front_controller_enabled() or not chat_fast_lane_enabled():
        return {"scheduled": False, "reason": "front_controller_disabled"}

    now = time.time()
    min_interval = front_controller_rewarm_min_interval_seconds()
    delay = front_controller_rewarm_delay_seconds()
    max_tokens = front_controller_rewarm_max_tokens()
    with _REWARM_LOCK:
        global _REWARM_IN_FLIGHT, _REWARM_PENDING_TIMER, _REWARM_PENDING_ID
        if _CONTROLLER_ACTIVE_COUNT > 0:
            _record_rewarm_event_locked({"status": "skipped", "reason": "controller_active"})
            return {"scheduled": False, "reason": "controller_active"}
        if _REWARM_RUNNING:
            _record_rewarm_event_locked({"status": "skipped", "reason": "running"})
            return {"scheduled": False, "reason": "running"}
        if _REWARM_IN_FLIGHT:
            _cancel_pending_rewarm_locked(reason="superseded_by_new_schedule")
        if min_interval and now - _REWARM_LAST_STARTED < min_interval:
            _record_rewarm_event_locked(
                {
                    "status": "skipped",
                    "reason": "throttled",
                    "age_ms": round((now - _REWARM_LAST_STARTED) * 1000, 2),
                    "min_interval_seconds": min_interval,
                }
            )
            return {"scheduled": False, "reason": "throttled"}
        rewarm_id = _next_rewarm_id_locked()
        timer = threading.Timer(
            delay,
            _run_front_controller_rewarm,
            kwargs={
                "rewarm_id": rewarm_id,
                "question": str(question or ""),
                "history": list(history or []),
                "max_tokens": max_tokens,
                "scheduled_at": now,
            },
        )
        timer.daemon = True
        _REWARM_IN_FLIGHT = True
        _REWARM_PENDING_ID = rewarm_id
        _REWARM_PENDING_TIMER = timer
        _record_rewarm_event_locked(
            {
                "status": "scheduled",
                "rewarm_id": rewarm_id,
                "reason": reason,
                "delay_ms": round(delay * 1000, 2),
                "max_tokens": max_tokens,
            }
        )
    timer.start()
    return {
        "scheduled": True,
        "rewarm_id": rewarm_id,
        "reason": reason,
        "delay_ms": round(delay * 1000, 2),
        "max_tokens": max_tokens,
    }


def _run_front_controller_rewarm(*, rewarm_id: str, question: str, history: list[dict] | None, max_tokens: int, scheduled_at: float) -> None:
    global _REWARM_IN_FLIGHT, _REWARM_RUNNING, _REWARM_PENDING_TIMER, _REWARM_PENDING_ID, _REWARM_LAST_STARTED
    started = time.perf_counter()
    trace_token = None
    calls: list[dict[str, Any]] = []
    error = ""
    ran = False
    try:
        with _REWARM_LOCK:
            if rewarm_id in _REWARM_CANCELLED_IDS:
                return
            if _REWARM_PENDING_ID and _REWARM_PENDING_ID != rewarm_id:
                return
            if _CONTROLLER_ACTIVE_COUNT > 0:
                _record_rewarm_event_locked(
                    {
                        "status": "skipped",
                        "rewarm_id": rewarm_id,
                        "reason": "controller_active_at_start",
                    }
                )
                _REWARM_IN_FLIGHT = False
                return
            if _CONTROLLER_LAST_ACTIVITY_AT and _CONTROLLER_LAST_ACTIVITY_AT > scheduled_at:
                _record_rewarm_event_locked(
                    {
                        "status": "skipped",
                        "rewarm_id": rewarm_id,
                        "reason": "stale_after_user_activity",
                        "age_ms": round((time.time() - scheduled_at) * 1000, 2),
                    }
                )
                _REWARM_IN_FLIGHT = False
                _REWARM_PENDING_TIMER = None
                _REWARM_PENDING_ID = ""
                return
            min_quiet = front_controller_rewarm_min_quiet_seconds()
            quiet_for = time.time() - _CONTROLLER_LAST_ACTIVITY_AT if _CONTROLLER_LAST_ACTIVITY_AT else None
            if quiet_for is not None and min_quiet and quiet_for < min_quiet:
                _record_rewarm_event_locked(
                    {
                        "status": "skipped",
                        "rewarm_id": rewarm_id,
                        "reason": "not_idle",
                        "quiet_ms": round(quiet_for * 1000, 2),
                        "min_quiet_ms": round(min_quiet * 1000, 2),
                    }
                )
                _REWARM_IN_FLIGHT = False
                return
            _REWARM_RUNNING = True
            _REWARM_PENDING_TIMER = None
            _REWARM_LAST_STARTED = time.time()
            _record_rewarm_event_locked(
                {
                    "status": "started",
                    "rewarm_id": rewarm_id,
                    "max_tokens": max_tokens,
                }
            )
        ran = True
        trace_token = llm_client.start_trace()
        chunks = llm_client.chat_with_tools_stream(
            messages=[{"role": "user", "content": build_front_controller_user_prompt(question, history)}],
            tools=[],
            system=FRONT_CONTROLLER_SYSTEM_PROMPT,
            max_tokens=max_tokens,
            purpose="controller",
        )
        for _kind, _payload in chunks:
            pass
    except Exception as exc:
        error = str(exc)
    finally:
        if trace_token is not None:
            try:
                calls = llm_client.finish_trace(trace_token)
            except Exception:
                calls = []
        wall_ms = round((time.perf_counter() - started) * 1000, 2)
        with _REWARM_LOCK:
            if ran:
                compact_calls = [_compact_rewarm_llm_call(call) for call in calls if isinstance(call, dict)]
                if error:
                    _record_rewarm_event_locked(
                        {
                            "status": "error",
                            "rewarm_id": rewarm_id,
                            "reason": "exception",
                            "error": error,
                            "wall_ms": wall_ms,
                            "llm_call_count": len(compact_calls),
                            "llm_calls": compact_calls,
                        }
                    )
                else:
                    _record_rewarm_event_locked(
                        {
                            "status": "completed",
                            "rewarm_id": rewarm_id,
                            "wall_ms": wall_ms,
                            "llm_call_count": len(compact_calls),
                            "llm_calls": compact_calls,
                        }
                    )
            _REWARM_RUNNING = False
            if not _REWARM_PENDING_ID or _REWARM_PENDING_ID == rewarm_id:
                _REWARM_PENDING_ID = ""
                _REWARM_PENDING_TIMER = None
                _REWARM_IN_FLIGHT = False


def build_front_controller_user_prompt(question: str, history: list[dict] | None = None) -> str:
    context = build_recent_conversation_context(history, limit=4, max_chars_per_turn=180)
    context_block = (
        "\n\nRecent visible conversation for follow-up context:\n"
        + context
        + "\nUse this only to resolve omitted follow-up details. The current user question wins for subject, time, intent, and output."
        if context
        else ""
    )
    return "Current user question:\n" + str(question or "") + context_block


def detect_protocol_mode(buffer: str) -> str:
    text = str(buffer or "").lstrip()
    upper = text.upper()
    if upper.startswith("CHAT"):
        return "CHAT"
    if upper.startswith("PLAN"):
        return "PLAN"
    if len(text) >= 12 or "\n" in text:
        return "INVALID"
    return ""


def strip_chat_prefix(buffer: str) -> str:
    text = str(buffer or "").lstrip()
    if text.upper().startswith("CHAT"):
        return text[4:].lstrip("\r\n ")
    return ""


def parse_plan_fields(buffer: str) -> dict[str, str]:
    text = str(buffer or "").lstrip()
    if not text.upper().startswith("PLAN"):
        return {}
    body = text[4:]
    fields: dict[str, str] = {}
    for raw_line in body.splitlines(keepends=True):
        complete_line = raw_line.endswith(("\n", "\r")) or raw_line.strip().upper() == "END"
        if not complete_line:
            continue
        line = raw_line.strip()
        if not line:
            continue
        if line.upper() == "END":
            break
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if key in {"route", "intent", "subject_kind", "subject", "time", "time_a", "time_b", "output", "chart_type", "sort", "discourse_action", "amount", "purpose", "change_type", "target_kind", "target"}:
            fields[key] = value.strip().strip(",;")
    return fields


def scalar_plan_ineligible(fields: dict[str, str]) -> bool:
    route = str(fields.get("route") or "").strip().lower()
    if route and route != "finance":
        return True
    intent = str(fields.get("intent") or "").strip().lower()
    if intent and intent != "spending_total":
        return True
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    if subject_kind and subject_kind not in {"merchant", "category"}:
        return True
    output = str(fields.get("output") or "").strip().lower()
    if output and output != "scalar":
        return True
    return False


def scalar_plan_ready(fields: dict[str, str]) -> bool:
    if scalar_plan_ineligible(fields):
        return False
    required = ("route", "intent", "subject_kind", "subject", "time", "output")
    if not all(str(fields.get(key) or "").strip() for key in required):
        return False
    subject = str(fields.get("subject") or "").strip().lower()
    time_value = str(fields.get("time") or "").strip().lower()
    if time_value == "custom" and not str(fields.get("time_a") or "").strip():
        return False
    return subject not in {"none", "null", "unknown"} and time_value not in {"none", "null"}


def scalar_plan_repair_candidate(fields: dict[str, str]) -> bool:
    if scalar_plan_ineligible(fields):
        return False
    route = str(fields.get("route") or "").strip().lower()
    intent = str(fields.get("intent") or "").strip().lower()
    output = str(fields.get("output") or "").strip().lower()
    if route != "finance" or intent != "spending_total" or output != "scalar":
        return False
    return bool(str(fields.get("subject") or fields.get("time") or "").strip())


def read_only_table_plan_ineligible(fields: dict[str, str]) -> bool:
    route = str(fields.get("route") or "").strip().lower()
    if route and route != "finance":
        return True
    intent = str(fields.get("intent") or "").strip().lower()
    if intent and intent not in _READ_ONLY_TABLE_INTENTS:
        return True
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    if subject_kind and subject_kind not in {"merchant", "category", "account", "metric", "transaction", "unknown", "none"}:
        return True
    subject = str(fields.get("subject") or "").strip().lower()
    if subject_kind in {"none", "unknown"} and subject not in _NONEISH_PLAN_VALUES:
        return True
    if intent == "transaction_lookup" and subject not in _NONEISH_PLAN_VALUES:
        if subject_kind not in {"merchant", "category", "account", "transaction"}:
            return True
    output = str(fields.get("output") or "").strip().lower()
    if output and output not in _READ_ONLY_TABLE_OUTPUTS:
        return True
    return False


def read_only_table_plan_ready(fields: dict[str, str]) -> bool:
    if read_only_table_plan_ineligible(fields):
        return False
    required = ("route", "intent", "subject_kind", "time", "output")
    if not all(str(fields.get(key) or "").strip() for key in required):
        return False
    if not _subject_field_ready(fields):
        return False
    time_value = str(fields.get("time") or "").strip().lower()
    if time_value == "custom" and not str(fields.get("time_a") or "").strip():
        return False
    return True


def chart_plan_ineligible(fields: dict[str, str]) -> bool:
    route = str(fields.get("route") or "").strip().lower()
    if route and route != "finance":
        return True
    intent = str(fields.get("intent") or "").strip().lower()
    if intent and intent not in _CHART_INTENTS:
        return True
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    if subject_kind and subject_kind not in {"category", "metric", "net_worth", "unknown", "none"}:
        return True
    subject = str(fields.get("subject") or "").strip().lower()
    if subject_kind in {"none", "unknown"} and subject not in _NONEISH_PLAN_VALUES:
        return True
    output = str(fields.get("output") or "").strip().lower()
    if output and output != "chart":
        return True
    chart_type = str(fields.get("chart_type") or "").strip().lower()
    if chart_type and chart_type not in {"line", "bar", "donut", "pie", "none"}:
        return True
    return False


def chart_plan_ready(fields: dict[str, str]) -> bool:
    if chart_plan_ineligible(fields):
        return False
    required = ("route", "intent", "subject_kind", "time", "output")
    if not all(str(fields.get(key) or "").strip() for key in required):
        return False
    if not _subject_field_ready(fields):
        return False
    time_value = str(fields.get("time") or "").strip().lower()
    if time_value == "custom" and not str(fields.get("time_a") or "").strip():
        return False
    return True


def explain_compare_plan_ineligible(fields: dict[str, str]) -> bool:
    route = str(fields.get("route") or "").strip().lower()
    if route and route != "finance":
        return True
    intent = str(fields.get("intent") or "").strip().lower()
    if intent and intent not in _EXPLAIN_COMPARE_INTENTS:
        return True
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    if subject_kind and subject_kind not in {"merchant", "category", "account", "metric", "transaction", "net_worth", "self", "unknown", "none"}:
        return True
    subject = str(fields.get("subject") or "").strip().lower()
    if subject_kind in {"none", "unknown"} and subject not in _NONEISH_PLAN_VALUES:
        return True
    output = str(fields.get("output") or "").strip().lower()
    if output and output not in _EXPLAIN_COMPARE_OUTPUTS:
        return True
    return False


def explain_compare_plan_ready(fields: dict[str, str]) -> bool:
    if explain_compare_plan_ineligible(fields):
        return False
    required = ("route", "intent", "subject_kind", "time", "output")
    if not all(str(fields.get(key) or "").strip() for key in required):
        return False
    if not _subject_field_ready(fields):
        return False
    time_value = str(fields.get("time") or "").strip().lower()
    if time_value == "custom" and not str(fields.get("time_a") or "").strip():
        return False
    return True


def write_preview_plan_ineligible(fields: dict[str, str]) -> bool:
    route = str(fields.get("route") or "").strip().lower()
    if route and route != "write_preview":
        return True
    intent = str(fields.get("intent") or "").strip().lower()
    if intent and intent != "write_preview":
        return True
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    if subject_kind and subject_kind not in {"merchant", "category", "account", "transaction", "unknown", "none"}:
        return True
    output = str(fields.get("output") or "").strip().lower()
    if output and output != "preview":
        return True
    change_type = str(fields.get("change_type") or "").strip().lower()
    if change_type and change_type not in _WRITE_PREVIEW_CHANGE_TYPES | {"none"}:
        return True
    target_kind = str(fields.get("target_kind") or "").strip().lower()
    if target_kind and target_kind not in {"category", "none"}:
        return True
    return False


def write_preview_plan_ready(fields: dict[str, str]) -> bool:
    if write_preview_plan_ineligible(fields):
        return False
    required = ("route", "intent", "subject_kind", "output", "change_type")
    if not all(str(fields.get(key) or "").strip() for key in required):
        return False
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    subject = str(fields.get("subject") or "").strip().lower()
    target_kind = str(fields.get("target_kind") or "").strip().lower()
    target = str(fields.get("target") or "").strip().lower()
    change_type = str(fields.get("change_type") or "").strip().lower()
    if change_type in {"bulk_recategorize", "create_rule"}:
        return (
            subject_kind == "merchant"
            and subject not in _NONEISH_PLAN_VALUES
            and target_kind == "category"
            and target not in _NONEISH_PLAN_VALUES
        )
    if change_type == "set_budget":
        amount = str(fields.get("amount") or "").strip().lower()
        return (
            subject_kind == "category"
            and subject not in _NONEISH_PLAN_VALUES
            and amount not in _NONEISH_PLAN_VALUES
        )
    return False


def explain_last_plan_ineligible(fields: dict[str, str]) -> bool:
    route = str(fields.get("route") or "").strip().lower()
    if route and route != "explain_last":
        return True
    return False


def explain_last_plan_ready(fields: dict[str, str]) -> bool:
    return not explain_last_plan_ineligible(fields) and str(fields.get("route") or "").strip().lower() == "explain_last"


def front_controller_plan_ready(fields: dict[str, str]) -> bool:
    return (
        finance_scalar_fast_lane_enabled() and scalar_plan_ready(fields)
    ) or (
        read_only_table_fast_lane_enabled() and read_only_table_plan_ready(fields)
    ) or (
        chart_fast_lane_enabled() and chart_plan_ready(fields)
    ) or (
        explain_compare_fast_lane_enabled() and explain_compare_plan_ready(fields)
    ) or (
        write_preview_fast_lane_enabled() and write_preview_plan_ready(fields)
    ) or (
        explain_last_fast_lane_enabled() and explain_last_plan_ready(fields)
    )


def front_controller_plan_ineligible(fields: dict[str, str]) -> bool:
    scalar_possible = finance_scalar_fast_lane_enabled() and not scalar_plan_ineligible(fields)
    read_only_possible = read_only_table_fast_lane_enabled() and not read_only_table_plan_ineligible(fields)
    chart_possible = chart_fast_lane_enabled() and not chart_plan_ineligible(fields)
    explain_compare_possible = explain_compare_fast_lane_enabled() and not explain_compare_plan_ineligible(fields)
    write_preview_possible = write_preview_fast_lane_enabled() and not write_preview_plan_ineligible(fields)
    explain_last_possible = explain_last_fast_lane_enabled() and not explain_last_plan_ineligible(fields)
    return not (
        scalar_possible
        or read_only_possible
        or chart_possible
        or explain_compare_possible
        or write_preview_possible
        or explain_last_possible
    )


def _subject_field_ready(fields: dict[str, str]) -> bool:
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    intent = str(fields.get("intent") or "").strip().lower()
    subject = str(fields.get("subject") or "").strip().lower()
    if intent == "spending_top" and subject_kind in {"merchant", "category"} and subject in _NONEISH_PLAN_VALUES:
        return True
    if intent == "net_worth_balance" and subject_kind in {"account", "net_worth"} and subject in _NONEISH_PLAN_VALUES:
        return True
    if subject_kind in {"none", "unknown", "net_worth", "self"}:
        return True
    return bool(str(fields.get("subject") or "").strip())


def plan_fields_to_selector_decision(fields: dict[str, str]) -> dict[str, Any]:
    route = str(fields.get("route") or "finance").strip().lower()
    if route not in {"finance", "memory", "write_preview", "explain_last"}:
        route = "finance"
    subject_text = str(fields.get("subject") or "").strip()
    subject_kind = str(fields.get("subject_kind") or "").strip().lower()
    subject = {
        "kind": subject_kind,
        "text": None if subject_text.lower() in {"", "none", "null"} else subject_text,
    }
    intent = str(fields.get("intent") or "spending_total").strip().lower()
    time_value = str(fields.get("time") or "").strip().lower()
    time_a = str(fields.get("time_a") or "").strip() or None
    time_b = str(fields.get("time_b") or "").strip() or None
    if re.match(r"^\d{4}-\d{2}$", time_value):
        time_a = f"{time_value}-01"
        time_b = None
        time_value = "custom"
    output = str(fields.get("output") or "scalar").strip().lower()
    chart_type = str(fields.get("chart_type") or "").strip().lower()
    if output == "chart" and chart_type in {"", "none", "null", "unknown"}:
        chart_type = "line"
    if chart_type == "pie":
        chart_type = "donut"
    if chart_type in {"none", "null", "unknown"}:
        chart_type = ""
    sort_text = str(fields.get("sort") or "").strip().lower()
    if sort_text in {"none", "null", "unknown"}:
        sort_text = ""
    amount_text = str(fields.get("amount") or "").strip()
    purpose_text = str(fields.get("purpose") or "").strip()
    change_type_text = str(fields.get("change_type") or "").strip()
    target_kind = str(fields.get("target_kind") or "").strip().lower()
    target_text = str(fields.get("target") or "").strip()
    payload: dict[str, Any] = {}
    if amount_text.lower() not in {"", "none", "null", "unknown"}:
        try:
            payload["amount"] = float(amount_text.replace("$", "").replace(",", ""))
        except ValueError:
            payload["amount"] = amount_text
    if purpose_text.lower() not in {"", "none", "null", "unknown"}:
        payload["purpose"] = purpose_text
    if change_type_text.lower() not in {"", "none", "null", "unknown"}:
        payload["change_type"] = change_type_text
    if (
        route == "write_preview"
        and subject_kind in {"merchant", "category", "account", "transaction"}
        and subject_text.lower() not in {"", "none", "null", "unknown"}
    ):
        payload.setdefault(subject_kind, subject_text)
    if target_kind == "category" and target_text.lower() not in {"", "none", "null", "unknown"}:
        payload["category"] = target_text
    discourse_action = str(fields.get("discourse_action") or "new").strip().lower()
    if discourse_action not in {"new", "follow_up", "correction"}:
        discourse_action = "new"
    decision = {
        "route": route,
        "intent": intent,
        "subject": subject,
        "time": time_value,
        "time_a": time_a,
        "time_b": time_b,
        "output": output,
        "chart_type": chart_type or None,
        "payload": payload,
        "details": payload,
        "discourse_action": discourse_action,
        "intent_frame": {
            "route": route,
            "intent": intent,
            "subject": subject,
            "time": time_value,
            "time_a": time_a,
            "time_b": time_b,
            "output": output,
            "chart_type": chart_type or None,
            "discourse_action": discourse_action,
            "answer": "",
        },
        "intent_frame_source": "front_controller_plan",
        "front_controller_plan": True,
    }
    if sort_text:
        decision["sort"] = sort_text
    return decision


def iter_front_controller_stream(
    *,
    question: str,
    history: list[dict] | None = None,
    stream: Iterable[tuple[str, Any]] | None = None,
    stream_chat_tokens: bool = True,
) -> Iterable[dict[str, Any]]:
    """Yield front-controller stream events.

    Returns a final private event:
    - {"type": "_front_controller_result", "handled": bool, ...}
    """
    if not front_controller_enabled() or not chat_fast_lane_enabled():
        yield {"type": "_front_controller_result", "handled": False, "reason": "disabled"}
        return

    started = time.perf_counter()
    messages = [{"role": "user", "content": build_front_controller_user_prompt(question, history)}]
    chunks = (
        stream
        if stream is not None
        else llm_client.chat_with_tools_stream(
            messages=messages,
            tools=[],
            system=FRONT_CONTROLLER_SYSTEM_PROMPT,
            max_tokens=front_controller_max_tokens(),
            purpose="controller",
        )
    )
    buffer = ""
    answer_parts: list[str] = []
    mode = ""
    yielded_chat = False
    fallback_reason = ""
    fallback_plan_fields: dict[str, str] = {}
    first_text_ms: float | None = None
    mode_detect_ms: float | None = None

    def controller_metrics() -> dict[str, Any]:
        seen_chars = len(buffer) + sum(len(part) for part in answer_parts)
        return {
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "front_controller_first_text_ms": round(first_text_ms, 2) if first_text_ms is not None else None,
            "front_controller_mode_detect_ms": round(mode_detect_ms, 2) if mode_detect_ms is not None else None,
            "front_controller_seen_chars": seen_chars,
        }

    for kind, payload in chunks:
        if kind == "stop":
            break
        if kind != "text":
            fallback_reason = f"unexpected_{kind}"
            break
        text = str(payload or "")
        if not text:
            continue
        if first_text_ms is None:
            first_text_ms = (time.perf_counter() - started) * 1000
        if not mode:
            buffer += text
            mode = detect_protocol_mode(buffer)
            if mode and mode_detect_ms is None:
                mode_detect_ms = (time.perf_counter() - started) * 1000
            if mode == "PLAN":
                if (
                    not finance_scalar_fast_lane_enabled()
                    and not read_only_table_fast_lane_enabled()
                    and not chart_fast_lane_enabled()
                    and not explain_compare_fast_lane_enabled()
                    and not write_preview_fast_lane_enabled()
                    and not explain_last_fast_lane_enabled()
                ):
                    fallback_reason = "plan_fallback"
                    break
                fields = _repair_plan_fields_from_question(parse_plan_fields(buffer), question)
                if front_controller_plan_ineligible(fields):
                    fallback_plan_fields = fields
                    fallback_reason = "plan_ineligible"
                    if _plan_buffer_complete(buffer):
                        break
                if front_controller_plan_ready(fields):
                    _finish_plan_stream_after_dispatch(chunks)
                    yield {
                        "type": "_front_controller_result",
                        "handled": True,
                        "mode": "PLAN",
                        "plan_fields": fields,
                        **controller_metrics(),
                    }
                    return
            if mode == "INVALID":
                fallback_reason = "invalid_protocol"
                break
            if mode == "CHAT":
                first_text = strip_chat_prefix(buffer)
                if first_text:
                    answer_parts.append(first_text)
                    yielded_chat = True
                    if stream_chat_tokens:
                        yield {"type": "token", "text": first_text}
            continue

        if mode == "CHAT":
            output_text = text if yielded_chat else text.lstrip("\r\n ")
            if output_text:
                answer_parts.append(output_text)
                yielded_chat = True
                if stream_chat_tokens:
                    yield {"type": "token", "text": output_text}
        elif mode == "PLAN":
            buffer += text
            fields = _repair_plan_fields_from_question(parse_plan_fields(buffer), question)
            if front_controller_plan_ineligible(fields):
                fallback_plan_fields = fields
                fallback_reason = "plan_ineligible"
                if _plan_buffer_complete(buffer):
                    break
            if front_controller_plan_ready(fields):
                _finish_plan_stream_after_dispatch(chunks)
                yield {
                    "type": "_front_controller_result",
                    "handled": True,
                    "mode": "PLAN",
                    "plan_fields": fields,
                    **controller_metrics(),
                }
                return

    if mode == "PLAN":
        fields = _repair_plan_fields_from_question(parse_plan_fields(buffer), question)
        if scalar_plan_repair_candidate(fields):
            _finish_plan_stream_after_dispatch(chunks)
            yield {
                "type": "_front_controller_result",
                "handled": True,
                "mode": "PLAN",
                "plan_fields": fields,
                **controller_metrics(),
            }
            return

    if mode == "CHAT" and yielded_chat:
        answer = "".join(answer_parts).strip()
        yield {
            "type": "_front_controller_result",
            "handled": True,
            "mode": "CHAT",
            "answer": answer,
            **controller_metrics(),
        }
        return

    _close_stream_if_possible(chunks)
    yield {
        "type": "_front_controller_result",
        "handled": False,
        "mode": mode,
        "reason": fallback_reason or "no_chat_answer",
        "plan_fields": fallback_plan_fields or (parse_plan_fields(buffer) if mode == "PLAN" else {}),
        **controller_metrics(),
    }


def _repair_plan_fields_from_question(fields: dict[str, str], question: str) -> dict[str, str]:
    """Fill mechanical slots only after the LLM has selected a concrete PLAN."""
    if not fields:
        return fields
    route = str(fields.get("route") or "").strip().lower()
    change_type = str(fields.get("change_type") or "").strip().lower()
    if route == "write_preview" and change_type == "set_budget":
        amount = str(fields.get("amount") or "").strip().lower()
        if amount in _NONEISH_PLAN_VALUES:
            match = re.search(r"(?:\$|usd\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", question, flags=re.I)
            if match:
                repaired = dict(fields)
                repaired["amount"] = match.group(1).replace(",", "")
                return repaired
    return fields


def _close_stream_if_possible(stream: Iterable[tuple[str, Any]] | None) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _plan_buffer_complete(buffer: str) -> bool:
    return bool(re.search(r"(?:^|\n)\s*END\s*$", str(buffer or ""), flags=re.I))


def _finish_plan_stream_after_dispatch(stream: Iterable[tuple[str, Any]] | None) -> None:
    if not front_controller_background_drain_enabled():
        _close_stream_if_possible(stream)
        return
    _drain_stream_in_background(stream)


def _drain_stream_in_background(stream: Iterable[tuple[str, Any]] | None) -> None:
    if stream is None:
        return

    def drain() -> None:
        try:
            for _ in stream:
                pass
        except Exception:
            _close_stream_if_possible(stream)

    thread = threading.Thread(target=drain, name="mira-front-controller-drain", daemon=True)
    thread.start()


def front_controller_done_event(
    *,
    question: str,
    answer: str,
    latency_ms: float,
    controller_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validation_for_general_answer(question=question, history=None)
    evidence = EvidencePacket(question=question)
    answer_result = VNextAnswerResult(
        answer=answer,
        path="front_controller_chat",
        llm_calls=1,
        raw=answer,
        max_tokens=front_controller_max_tokens(),
    )
    metrics = controller_metrics if isinstance(controller_metrics, dict) else {}
    trace = {
        "runtime": "agentic_vnext",
        "stage": "front_controller",
        "status": "chat_fast_lane",
        "answer_path": "front_controller_chat",
        "front_controller_enabled": True,
        "front_controller_latency_ms": latency_ms,
    }
    for key in (
        "front_controller_first_text_ms",
        "front_controller_mode_detect_ms",
        "front_controller_seen_chars",
    ):
        if metrics.get(key) is not None:
            trace[key] = metrics.get(key)
    route = {
        "runtime": "agentic_vnext",
        "controller_act": "front_controller_chat",
        "intent": "chat",
        "confidence": 1.0,
        "operation": "general_answer",
        "selected_tools": [],
        "tool_plan": [],
        "grounded_entities": [],
        "validation": validation.to_dict(),
        "llm_calls": 0,
        "legacy_router_used": False,
        "trace": trace,
    }
    return {
        "route": route,
        "validation": validation,
        "evidence": evidence,
        "answer_result": answer_result,
    }


__all__ = [
    "FRONT_CONTROLLER_SYSTEM_PROMPT",
    "build_front_controller_user_prompt",
    "chart_fast_lane_enabled",
    "chart_plan_ready",
    "chat_fast_lane_enabled",
    "detect_protocol_mode",
    "explain_compare_fast_lane_enabled",
    "explain_compare_plan_ready",
    "explain_last_fast_lane_enabled",
    "explain_last_plan_ready",
    "finance_scalar_fast_lane_enabled",
    "front_controller_done_event",
    "front_controller_enabled",
    "front_controller_background_drain_enabled",
    "front_controller_max_tokens",
    "front_controller_rewarm_enabled",
    "front_controller_rewarm_max_tokens",
    "front_controller_rewarm_min_quiet_seconds",
    "front_controller_rewarm_stats_snapshot",
    "iter_front_controller_stream",
    "mark_front_controller_active",
    "mark_front_controller_inactive",
    "parse_plan_fields",
    "plan_fields_to_selector_decision",
    "read_only_table_fast_lane_enabled",
    "read_only_table_plan_ready",
    "reset_front_controller_rewarm_stats",
    "schedule_front_controller_rewarm",
    "scalar_plan_repair_candidate",
    "scalar_plan_ready",
    "strip_chat_prefix",
    "write_preview_fast_lane_enabled",
    "write_preview_plan_ready",
]
