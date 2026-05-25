"""
main.py
FastAPI backend for Folio personal finance tracker.
"""

from pathlib import Path as FilePath
from datetime import date, datetime, timedelta
from threading import Lock
import csv
import io
import json
import re

from fastapi import FastAPI, Depends, HTTPException, Query, BackgroundTasks, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, StrictInt, ValidationError
from auth import verify_api_key, rate_limit_middleware
import bank
from bank import validate_teller_config, close_all_clients
import os

from log_config import get_logger, setup_logging
from sync_status import start_sync, finish_sync, get_sync_status, update_phase

# Ensure logging is configured before anything else
setup_logging()

logger = get_logger(__name__)

_mira_background_job_lock = Lock()
_mira_background_jobs: dict[str, dict] = {}
_mira_advisor_read_job_lock = Lock()
_mira_advisor_read_jobs: dict[str, dict] = {}
_mira_money_outlook_job_lock = Lock()
_mira_money_outlook_jobs: dict[str, dict] = {}

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


DEMO_MODE = _env_flag("DEMO_MODE", False)
_receipt_flag = os.getenv("RECEIPT_INTELLIGENCE_ENABLED")
RECEIPT_INTELLIGENCE_ENABLED = (
    (_receipt_flag.strip().lower() in _TRUE_ENV_VALUES)
    if _receipt_flag is not None
    else not DEMO_MODE
)

from data_manager import (
    get_data, fetch_fresh_data, fetch_simplefin_data,
    update_transaction_category,
    bulk_mark_transactions_reviewed,
    add_category, deactivate_category, get_categories, get_categories_meta, get_category_rules,
    get_accounts_filtered, get_transactions_paginated,
    get_summary_data, get_monthly_analytics_data,
    get_category_analytics_data, get_merchant_insights_data,
    get_net_worth_series_data, get_dashboard_bundle_data,
    update_category_parent, update_category_rule,
    get_copilot_conversations, clear_copilot_conversations, delete_copilot_conversation, get_data_browser_rows,
    log_copilot_conversation, prepare_copilot_history_record, prune_copilot_conversations,
    get_category_budgets, update_category_budget,
    get_goals, upsert_goal, delete_goal,
    get_review_queue_data,
    get_merchant_directory, update_merchant_directory_entry,
    update_transaction_excluded, update_transaction_metadata,
    get_transaction_splits, replace_transaction_splits,
    create_manual_account, update_manual_account, deactivate_manual_account,
    update_account_payment_details,
    get_data_health_summary,
    get_scheduled_transactions_data,
    get_cash_flow_forecast_data,
    create_month_explanation,
    get_investments_summary_data,
    upsert_investment_holding,
    delete_investment_holding,
    get_backup_status_data,
    create_backup_export_data,
    get_transactions_for_merchant,
    get_category_rule_impact,
    explain_category_assignment, find_merchants_missing_category,
    bulk_recategorize_preview, preview_rule_creation,
    rename_merchant_variants, repair_polluted_merchant_categories,
)
from categorizer import get_active_categories
from categorization_backends import resolve_categorization_backend
from database import init_db, get_db, get_db_session, close_thread_local_connection
from local_llm import (
    get_catalog_response,
    get_status_response,
    update_settings as update_local_llm_settings,
    get_frontend_flags,
    install_model as install_local_llm_model,
    schedule_prewarm_selected_model,
)

def schedule_prewarm_chat_prompt(*args, **kwargs) -> bool:
    return False

# CORS origins from env, with dev defaults
_cors_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000",
).split(",")

from fastapi import APIRouter

# ── Health check (no auth — used by Docker healthcheck) ──
# This is a separate mini-app mounted BEFORE the main app processes
# global dependencies. FastAPI's sub-application mounting ensures
# the health endpoint is completely independent of the main app's auth.
_health_app = FastAPI(title="Health", openapi_url=None)


@_health_app.get("/health")
async def health_check():
    """Health check endpoint for Docker. No auth required."""
    return {"status": "ok"}


app = FastAPI(
    title="Folio API",
    version="3.1.0",
    dependencies=[Depends(verify_api_key)],
)

# Mount health as a sub-application — bypasses all main app middleware and deps
app.mount("/healthz", _health_app)

if os.getenv("MERCURY_MIRA_EXPERIMENT", "").strip().lower() in {"1", "true", "yes", "on"}:
    from mira.mercury_adapter import router as mercury_mira_router
    app.include_router(mercury_mira_router)


# Also keep a convenience redirect so /health works too
@app.get("/health", include_in_schema=False, dependencies=[])
async def health_redirect():
    """
    Convenience health endpoint on the main app.
    NOTE: FastAPI's dependencies=[] at decorator level does NOT override
    app-level global deps. For truly unauthenticated health checks,
    Docker should use /healthz/health. This endpoint exists only for
    manual testing by developers who include the API key header.
    """
    return {"status": "ok"}


@app.on_event("startup")
def startup():
    if not DEMO_MODE:
        validate_teller_config()
    init_db()
    # These were previously auto-called at database.py import time.
    # Moved here for explicit, single-point initialization.
    from database import sync_subscription_seeds, sync_enrichment_cache_from_seeds
    sync_subscription_seeds()
    sync_enrichment_cache_from_seeds()
    # Repair legacy non-spending misclassifications before the UI reads totals.
    from data_manager import (
        repair_non_spending_transaction_categories,
        repair_polluted_merchant_categories,
        repair_cc_income_misclassifications,
        reclassify_transfers,
    )
    repair_non_spending_transaction_categories()
    repair_polluted_merchant_categories()
    repair_cc_income_misclassifications()
    reclassify_transfers()
    schedule_prewarm_selected_model("controller")
    schedule_prewarm_selected_model("copilot")
    schedule_prewarm_chat_prompt()
    if not DEMO_MODE:
        from auto_sync import start_auto_sync_scheduler
        start_auto_sync_scheduler(on_success=_after_auto_sync_success)


@app.on_event("shutdown")
def shutdown():
    """Close any remaining thread-local DB connections and Teller clients on server shutdown."""
    try:
        from auto_sync import stop_auto_sync_scheduler
        stop_auto_sync_scheduler()
    except Exception:
        logger.debug("Auto sync scheduler shutdown skipped", exc_info=True)
    close_thread_local_connection()
    close_all_clients()


# Rate limiting middleware (must be added before CORS)
app.middleware("http")(rate_limit_middleware)

# [FIX M1] Trusted Host — blocks DNS rebinding attacks
# Only requests with Host header matching these values are accepted
_trusted_hosts = os.getenv(
    "TRUSTED_HOSTS", "*" if DEMO_MODE else "localhost,127.0.0.1,backend"
).split(",")
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[h.strip() for h in _trusted_hosts],
)

# [FIX M3] CORS — restricted methods and headers (configurable origins via env)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Categories excluded from spending calculations
TRANSFER_CATEGORIES = {"Savings Transfer", "Personal Transfer", "Credit Card Payment"}
DIRECT_CASHFLOW_CATEGORIES = {"Cash Withdrawal", "Cash Deposit", "Investment Transfer"}
NON_SPENDING_CATEGORIES = TRANSFER_CATEGORIES | DIRECT_CASHFLOW_CATEGORIES | {"Income", "Credits & Refunds"}

# ── Profile helpers ──────────────────────────────────────────────
def _normalize_profile_whitespace(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _canonicalize_profile_id(value: str | None) -> str:
    return _normalize_profile_whitespace(value).lower()


def _titleize_profile_name(value: str | None) -> str:
    normalized = _normalize_profile_whitespace(value)
    return normalized.title() if normalized else ""


def _invalidate_copilot_cache() -> None:
    try:
        import copilot_cache
        copilot_cache.invalidate_all()
    except Exception:
        logger.debug("Copilot cache invalidation skipped", exc_info=True)


def _after_auto_sync_success(data: dict) -> None:
    _invalidate_copilot_cache()
    try:
        background_refresh = _maybe_queue_mira_background_refresh(
            background_tasks=None,
            profile=None,
            reason="auto_sync_completed",
        )
        with get_db() as conn:
            money_outlook_refresh = _maybe_queue_mira_money_outlook_refresh(
                background_tasks=None,
                conn=conn,
                profile=None,
                reason="auto_sync_completed",
            )
        logger.info(
            "Auto sync post-refresh complete: accounts=%s transactions=%s background=%s money_outlook=%s",
            len(data.get("accounts", [])) if isinstance(data, dict) else None,
            len(data.get("transactions", [])) if isinstance(data, dict) else None,
            background_refresh.get("status") or background_refresh.get("queued"),
            money_outlook_refresh.get("reason"),
        )
    except Exception:
        logger.debug("Auto sync post-refresh skipped", exc_info=True)


def _display_name_from_profile_id(profile_id: str) -> str:
    return _titleize_profile_name(profile_id) or "Primary"


def _load_profile_rows(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT id, display_name
           FROM profiles
           WHERE TRIM(COALESCE(id, '')) != ''
             AND LOWER(TRIM(id)) != 'household'
           ORDER BY LOWER(COALESCE(display_name, id)), LOWER(id)"""
    ).fetchall()
    return [dict(r) for r in rows]


def _get_profile_list(conn) -> list[dict]:
    rows = _load_profile_rows(conn)
    profiles = [
        {
            "id": row["id"],
            "name": (row.get("display_name") or "").strip() or _display_name_from_profile_id(row["id"]),
        }
        for row in rows
    ]
    if len(profiles) > 1:
        profiles.append({"id": "household", "name": "Household"})
    return profiles


def _load_valid_profiles(conn) -> set[str]:
    return {row["id"] for row in _load_profile_rows(conn)}


def _ensure_profile(profile_value: str, display_name: str | None = None, conn=None) -> dict:
    canonical_id = _canonicalize_profile_id(profile_value)
    if not canonical_id:
        raise ValueError("Profile is required.")
    if canonical_id == "household":
        raise ValueError("'household' is reserved and cannot be used as a profile.")

    desired_display = _titleize_profile_name(display_name) or _display_name_from_profile_id(canonical_id)

    def _upsert(target_conn):
        existing = target_conn.execute(
            "SELECT id, display_name FROM profiles WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        if existing:
            current_display = (existing["display_name"] or "").strip()
            if not current_display or current_display.lower() == canonical_id:
                target_conn.execute(
                    "UPDATE profiles SET display_name = ? WHERE id = ?",
                    (desired_display, canonical_id),
                )
                current_display = desired_display
            return {
                "id": canonical_id,
                "display_name": current_display or desired_display,
                "created": False,
            }

        target_conn.execute(
            """INSERT INTO profiles (id, display_name, is_default)
               VALUES (?, ?, ?)""",
            (canonical_id, desired_display, 1 if canonical_id == "primary" else 0),
        )
        return {"id": canonical_id, "display_name": desired_display, "created": True}

    if conn is not None:
        return _upsert(conn)

    with get_db() as target_conn:
        return _upsert(target_conn)


def _filter_by_profile(items: list[dict], profile: str | None) -> list[dict]:
    """
    Filter a list of dicts (transactions or accounts) by profile.
    - None or 'household' → return all
    - specific name → filter to that profile only
    """
    if not profile or profile == "household":
        return items
    return [item for item in items if item.get("profile") == profile]


# [FIX M4] Profile validation — reject unknown profile names
_VALID_PROFILES: set[str] | None = None


def validate_profile(profile: str | None = Query(None)) -> str | None:
    global _VALID_PROFILES
    normalized = _canonicalize_profile_id(profile)
    if not normalized:
        return None
    if normalized == "household":
        return "household"
    if _VALID_PROFILES is None:
        with get_db() as conn:
            _VALID_PROFILES = _load_valid_profiles(conn) | {"household"}
    if normalized not in _VALID_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown profile: '{profile}'. Valid profiles: {sorted(_VALID_PROFILES)}",
        )
    return normalized


def _invalidate_profile_cache():
    """Reset cached profile validation results after profile-affecting changes."""
    global _VALID_PROFILES
    _VALID_PROFILES = None


def _require_live_mode(detail: str = "This action is disabled in demo mode.") -> None:
    if DEMO_MODE:
        raise HTTPException(status_code=403, detail=detail)


def _require_receipts_enabled() -> None:
    if not RECEIPT_INTELLIGENCE_ENABLED:
        raise HTTPException(status_code=404, detail="Receipt intelligence is disabled.")


def _mira_agentic_runtime_payload() -> dict:
    return {
        "miraAgenticEnabled": True,
        "miraAgenticRuntime": "vnext",
    }


def _mira_enabled() -> bool:
    explicit = os.getenv("MIRA_ENABLED")
    if explicit is not None and explicit.strip():
        return explicit.strip().lower() in _TRUE_ENV_VALUES

    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower() or "ollama"
    if provider == "llamacpp":
        return bool(os.getenv("LLAMACPP_BASE_URL", "").strip())
    if provider == "ollama":
        return bool(os.getenv("OLLAMA_BASE_URL", "").strip() and os.getenv("OLLAMA_MODEL_COPILOT", "").strip())
    return False


def _categorization_status_payload(*, preload_distilbert: bool = False) -> dict:
    backend = resolve_categorization_backend()
    payload = {
        "backend": backend,
        "localLlmCategorization": backend == "local_llm",
        "distilbertCategorization": backend == "distilbert",
        "rulesOnlyCategorization": backend == "rules_only",
    }
    if backend == "distilbert":
        try:
            from distilbert_categorizer import get_runtime_status

            payload["distilbert"] = get_runtime_status(preload=preload_distilbert)
        except Exception as exc:
            payload["distilbert"] = {
                "available": False,
                "warnings": [str(exc)],
            }
    return payload


def _app_config_payload(db=None) -> dict:
    advisor_read_ui_enabled = False
    advisor_read_context_enabled = False
    advisor_read_generation_enabled = False
    financial_feedback_loop_enabled = False
    stated_intent_memory_enabled = False
    habit_streaks_enabled_flag = False
    monthly_retrospective_enabled_flag = False
    money_outlook_enabled_flag = False
    safe_to_spend_enabled_flag = False
    cash_low_point_radar_enabled_flag = False
    try:
        from mira.advisor_lens_synthesis import (
            advisor_lens_context_enabled,
            advisor_lens_store_enabled,
            advisor_lens_synthesis_enabled,
            advisor_lens_ui_enabled,
        )

        advisor_read_ui_enabled = advisor_lens_ui_enabled()
        advisor_read_context_enabled = advisor_lens_context_enabled()
        advisor_read_generation_enabled = advisor_lens_synthesis_enabled() and advisor_lens_store_enabled()
    except Exception as exc:
        logger.debug("Failed to load advisor read UI flag: %s", exc)
    try:
        from mira.financial_feedback import financial_feedback_loop_enabled as _feedback_enabled

        financial_feedback_loop_enabled = _feedback_enabled()
    except Exception as exc:
        logger.debug("Failed to load financial feedback flag: %s", exc)
    try:
        from mira.stated_intents import stated_intent_memory_enabled as _stated_intent_enabled

        stated_intent_memory_enabled = _stated_intent_enabled()
    except Exception as exc:
        logger.debug("Failed to load stated intent memory flag: %s", exc)
    try:
        from mira.habit_streaks import habit_streaks_enabled as _habit_streaks_enabled

        habit_streaks_enabled_flag = _habit_streaks_enabled()
    except Exception as exc:
        logger.debug("Failed to load habit streaks flag: %s", exc)
    try:
        from mira.monthly_retrospectives import monthly_retrospective_enabled as _monthly_retrospective_enabled

        monthly_retrospective_enabled_flag = _monthly_retrospective_enabled()
    except Exception as exc:
        logger.debug("Failed to load monthly retrospective flag: %s", exc)
    try:
        from mira.money_outlook import (
            cash_low_point_radar_enabled,
            money_outlook_enabled,
            safe_to_spend_enabled,
        )

        money_outlook_enabled_flag = money_outlook_enabled()
        safe_to_spend_enabled_flag = safe_to_spend_enabled()
        cash_low_point_radar_enabled_flag = cash_low_point_radar_enabled()
    except Exception as exc:
        logger.debug("Failed to load money outlook flags: %s", exc)
    payload = {
        "demoMode": DEMO_MODE,
        "bankLinkingEnabled": not DEMO_MODE,
        "manualSyncEnabled": not DEMO_MODE,
        "demoPersistence": "ephemeral" if DEMO_MODE else "persistent",
        "receiptIntelligenceEnabled": RECEIPT_INTELLIGENCE_ENABLED,
        "miraEnabled": _mira_enabled(),
        "miraAdvisorReadUiEnabled": advisor_read_ui_enabled,
        "miraAdvisorReadContextEnabled": advisor_read_context_enabled,
        "miraAdvisorReadGenerationEnabled": advisor_read_generation_enabled,
        "miraFinancialFeedbackLoopEnabled": financial_feedback_loop_enabled,
        "miraStatedIntentMemoryEnabled": stated_intent_memory_enabled,
        "miraHabitStreaksEnabled": habit_streaks_enabled_flag,
        "miraMonthlyRetrospectiveEnabled": monthly_retrospective_enabled_flag,
        "miraMoneyOutlookEnabled": money_outlook_enabled_flag,
        "miraSafeToSpendEnabled": safe_to_spend_enabled_flag,
        "miraCashLowPointRadarEnabled": cash_low_point_radar_enabled_flag,
        "categorization": _categorization_status_payload(preload_distilbert=False),
        **_mira_agentic_runtime_payload(),
    }
    try:
        payload.update(get_frontend_flags(db))
    except Exception as exc:
        logger.debug("Failed to load local LLM frontend flags: %s", exc)
    return payload


_ADVISOR_PUBLIC_REF_RE = re.compile(r"\b(?:metric|txn):[A-Za-z0-9_.:-]+\b")


def _advisor_public_text(value, *, max_chars: int) -> str:
    text = str(value or "")
    text = _ADVISOR_PUBLIC_REF_RE.sub("", text)
    text = re.sub(r"\bevidence_ids?\b", "sources", text, flags=re.IGNORECASE)
    text = re.sub(r"\brun_sql\b", "finance query", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsafe_finance_query\b", "finance evidence", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSQL\b", "finance query", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) <= max_chars:
        return text
    trimmed = text[:max_chars].rsplit(" ", 1)[0].strip()
    return trimmed or text[:max_chars].strip()


def _advisor_public_text_list(values, *, max_items: int = 8, max_chars: int = 80) -> list[str]:
    out: list[str] = []
    for value in values or []:
        clean = _advisor_public_text(value, max_chars=max_chars)
        if clean and clean not in out:
            out.append(clean)
        if len(out) >= max_items:
            break
    return out


def _public_advisor_delta_payload(delta: dict | None, memo: dict | None = None) -> dict | None:
    if not isinstance(delta, dict):
        return None
    if memo and delta.get("source_memo_fingerprint") and memo.get("fingerprint"):
        if delta.get("source_memo_fingerprint") != memo.get("fingerprint"):
            return None
    packet = delta.get("delta_packet") if isinstance(delta.get("delta_packet"), dict) else {}
    if not packet or packet.get("status") not in {"changed", "fresh"}:
        return None
    headline = _advisor_public_text(packet.get("headline"), max_chars=180)
    action = _advisor_public_text(packet.get("action"), max_chars=220)
    sections = _advisor_public_text_list(packet.get("invalidated_sections"), max_items=8, max_chars=80)
    months = _advisor_public_text_list(packet.get("touched_months"), max_items=8, max_chars=16)
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
    public_delta = {
        "generated_at": delta.get("generated_at"),
        "status": _advisor_public_text(packet.get("status"), max_chars=24),
        "headline": headline,
        "action": action,
        "touched_months": months,
        "changed_sections": sections,
        "categories": _advisor_public_text_list(categories, max_items=8, max_chars=80),
        "merchants": _advisor_public_text_list(merchants, max_items=8, max_chars=80),
        "needs_full_rebuild": bool(packet.get("needs_full_rebuild")),
    }
    if not headline and not action and not months and not sections:
        return None
    return public_delta


def _latest_public_advisor_delta(*, conn, profile: str | None, memo: dict | None) -> dict | None:
    if not memo:
        return None
    try:
        from mira.advisor_fact_snapshot import list_portrait_delta_packets

        deltas = list_portrait_delta_packets(conn, profile=profile, limit=6)
    except Exception as exc:
        logger.debug("Failed to load advisor portrait delta: %s", exc)
        return None
    for delta in deltas:
        public_delta = _public_advisor_delta_payload(delta, memo=memo)
        if public_delta:
            return public_delta
    return None


def _advisor_read_feedback_by_card(*, conn, profile: str | None, cards: list[dict]) -> dict[str, dict]:
    try:
        from mira.financial_feedback import feedback_effect_summary, financial_feedback_loop_enabled
    except Exception:
        return {}
    if not financial_feedback_loop_enabled():
        return {}
    feedback: dict[str, dict] = {}
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("id") or "").strip()
        if not card_id:
            continue
        summary = feedback_effect_summary(conn=conn, profile=profile, target_type="advisor_card", target_id=card_id)
        if summary.get("count"):
            feedback[card_id] = summary
    return feedback


def _attach_public_advisor_feedback(public_memo: dict | None, *, conn, profile: str | None) -> dict | None:
    if not isinstance(public_memo, dict):
        return public_memo
    cards = public_memo.get("cards") if isinstance(public_memo.get("cards"), list) else []
    feedback_by_card = _advisor_read_feedback_by_card(conn=conn, profile=profile, cards=cards)
    if not feedback_by_card:
        return public_memo
    for card in cards:
        if not isinstance(card, dict):
            continue
        card_feedback = feedback_by_card.get(str(card.get("id") or ""))
        if card_feedback:
            card["feedback"] = {
                "count": card_feedback.get("count") or 0,
                "effects": card_feedback.get("effects") or {},
                "feedback_types": card_feedback.get("feedback_types") or {},
                "safe_summaries": card_feedback.get("safe_summaries") or [],
            }
    return public_memo


def _public_advisor_read_payload(memo: dict, *, delta: dict | None = None, feedback_by_card: dict[str, dict] | None = None) -> dict:
    from mira.advisor_lens_synthesis import build_advisor_ranked_actions, build_advisor_read_cards

    quality = memo.get("quality") if isinstance(memo.get("quality"), dict) else {}
    stored_payload = memo.get("payload") if isinstance(memo.get("payload"), dict) else {}
    public_delta = (
        delta
        if isinstance(delta, dict) and "changed_sections" in delta and "delta_packet" not in delta
        else _public_advisor_delta_payload(delta, memo=memo)
    )
    public_theses = []
    for thesis in memo.get("theses") or []:
        if not isinstance(thesis, dict):
            continue
        summary = _advisor_public_text(thesis.get("summary"), max_chars=360)
        paragraph = _advisor_public_text(thesis.get("paragraph"), max_chars=520)
        caveat = _advisor_public_text(thesis.get("caveat"), max_chars=260)
        if not summary and not paragraph:
            continue
        public_theses.append(
            {
                "summary": summary,
                "paragraph": paragraph,
                "caveat": caveat,
                "confidence": _advisor_public_text(thesis.get("confidence") or "medium", max_chars=32),
            }
        )
    action_plan = []
    raw_actions = stored_payload.get("action_plan") if isinstance(stored_payload.get("action_plan"), list) and stored_payload.get("action_plan") else None
    if raw_actions is None:
        raw_actions = memo.get("action_plan") if isinstance(memo.get("action_plan"), list) and memo.get("action_plan") else None
    if raw_actions is None:
        raw_actions = build_advisor_ranked_actions({"memo_markdown": memo.get("memo_markdown"), "theses": memo.get("theses") or []})
    for action in raw_actions:
        if not isinstance(action, dict):
            continue
        action_plan.append(
            {
                "rank": action.get("rank"),
                "title": _advisor_public_text(action.get("title"), max_chars=140),
                "why": _advisor_public_text(action.get("why"), max_chars=320),
                "action": _advisor_public_text(action.get("action"), max_chars=320),
                "tradeoff": _advisor_public_text(action.get("tradeoff"), max_chars=260),
                "pain": _advisor_public_text(action.get("pain"), max_chars=32),
            }
        )
    public_memo = {
        "id": memo.get("id"),
        "generated_at": memo.get("generated_at"),
        "valid_until": memo.get("valid_until"),
        "version": memo.get("version"),
        "memo_markdown": _advisor_public_text(memo.get("memo_markdown"), max_chars=9000),
        "theses": public_theses[:12],
        "action_plan": action_plan[:6],
        "delta": public_delta,
        "quality": {
            "ok": bool(quality.get("ok")),
            "score": quality.get("score"),
            "coverage_count": quality.get("coverage_count"),
            "required_count": quality.get("required_count"),
        },
    }
    stored_cards = stored_payload.get("cards") if isinstance(stored_payload.get("cards"), list) else None
    if stored_cards is None:
        stored_cards = memo.get("cards") if isinstance(memo.get("cards"), list) else None
    public_memo["cards"] = build_advisor_read_cards({**public_memo, "cards": stored_cards or []}, delta=public_delta)
    if feedback_by_card:
        for card in public_memo["cards"]:
            if not isinstance(card, dict):
                continue
            card_feedback = feedback_by_card.get(str(card.get("id") or ""))
            if card_feedback:
                card["feedback"] = {
                    "count": card_feedback.get("count") or 0,
                    "effects": card_feedback.get("effects") or {},
                    "feedback_types": card_feedback.get("feedback_types") or {},
                    "safe_summaries": card_feedback.get("safe_summaries") or [],
                }
    return public_memo


_ADVISOR_FOLLOWUP_TYPES = {
    "focus",
    "levers",
    "risk",
    "changes",
    "normal_month",
    "money_map",
    "event_noise",
    "first_move",
    "general",
}
_ADVISOR_FOLLOWUP_GENERIC_PHRASES = (
    "that's a big",
    "financial co-pilot",
    "local-first companion",
    "designed to help",
    "generally, you should",
    "daily fluctuations",
    "spending habits",
    "if you want me to dig",
    "if you want to dig",
    "just let me know what you're curious",
    "what you're curious about",
    "simple number",
    "small tweaks could make a big difference",
    "just data points",
    "prioritization is a bit limited",
    "no active financial-understanding facts",
)
_ADVISOR_FOLLOWUP_FORBIDDEN_PHRASES = (
    "[object object]",
    "dashboard snapshot",
    "available for your review",
    "safe_finance_query",
    "run_sql",
    "sql",
    "query layer",
    "validator",
    "backend",
    "evidence_id",
    "evidence id",
    "metric:",
    "txn:",
)
_ADVISOR_FOLLOWUP_NUMERIC_RE = re.compile(r"(?<![A-Za-z])\$?\d[\d,]*(?:\.\d+)?%?(?![A-Za-z])")


def _advisor_read_followup_kind(value: str | None) -> str:
    kind = str(value or "general").strip().lower().replace("-", "_")
    return kind if kind in _ADVISOR_FOLLOWUP_TYPES else "general"


def _advisor_followup_number_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _ADVISOR_FOLLOWUP_NUMERIC_RE.findall(str(text or "")):
        normalized = raw.replace("$", "").replace(",", "").strip()
        if normalized.endswith("%"):
            normalized = normalized[:-1]
        if normalized:
            tokens.add(normalized)
    return tokens


def _advisor_read_public_number_text(public_memo: dict | None) -> str:
    if not isinstance(public_memo, dict):
        return ""
    parts = [str(public_memo.get("memo_markdown") or "")]
    for thesis in public_memo.get("theses") or []:
        if not isinstance(thesis, dict):
            continue
        parts.extend(
            [
                str(thesis.get("summary") or ""),
                str(thesis.get("paragraph") or ""),
                str(thesis.get("caveat") or ""),
            ]
        )
    for action in public_memo.get("action_plan") or []:
        if not isinstance(action, dict):
            continue
        parts.extend(
            [
                str(action.get("title") or ""),
                str(action.get("why") or ""),
                str(action.get("action") or ""),
                str(action.get("tradeoff") or ""),
            ]
        )
    for card in public_memo.get("cards") or []:
        if not isinstance(card, dict):
            continue
        parts.extend(
            [
                str(card.get("title") or ""),
                str(card.get("summary") or ""),
                str(card.get("detail") or ""),
                str(card.get("tradeoff") or ""),
            ]
        )
        for row in card.get("rows") or []:
            if not isinstance(row, dict):
                continue
            parts.extend(
                [
                    str(row.get("label") or ""),
                    str(row.get("value") or ""),
                    str(row.get("detail") or ""),
                ]
            )
    delta = public_memo.get("delta") if isinstance(public_memo.get("delta"), dict) else {}
    if delta:
        parts.extend(
            [
                str(delta.get("headline") or ""),
                str(delta.get("action") or ""),
                " ".join(str(item) for item in delta.get("touched_months") or []),
                " ".join(str(item) for item in delta.get("changed_sections") or []),
                " ".join(str(item) for item in delta.get("categories") or []),
                " ".join(str(item) for item in delta.get("merchants") or []),
            ]
        )
    return "\n".join(parts)


def _advisor_question_asks_for_actions(question: str) -> bool:
    lowered = " ".join(str(question or "").lower().split())
    markers = (
        "what should i fix",
        "fix first",
        "do first",
        "next move",
        "next step",
        "action plan",
        "rank",
        "priority",
        "prioritize",
        "where should i start",
    )
    return any(marker in lowered for marker in markers)


_ADVISOR_STRATEGIC_RISK_TERMS = (
    "income continuity",
    "income source",
    "income stream",
    "source label",
    "source labels",
    "forward income",
    "fixed monthly floor",
    "fixed floor",
    "operating floor",
    "floor burn",
    "goal capacity",
    "planning capacity",
    "configured goals",
    "explicit goals",
    "goal targets",
    "reconciled operating burn",
)
_ADVISOR_FIRST_ACTION_TERMS = (
    "fee",
    "fees",
    "interest",
    "leakage",
    "amazon",
    "geico",
    "vendor",
    "recurring",
    "subscription",
)


def _advisor_has_strategic_risk_anchor(text: str) -> bool:
    lowered = " ".join(str(text or "").lower().split())
    return any(term in lowered for term in _ADVISOR_STRATEGIC_RISK_TERMS)


def _advisor_followup_sentences(text: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", str(text or "").strip())
        if sentence.strip()
    ]


def _advisor_pick_sentences(text: str, terms: tuple[str, ...], *, limit: int = 2) -> list[str]:
    picked: list[str] = []
    seen: set[str] = set()
    for sentence in _advisor_followup_sentences(text):
        lowered = sentence.lower()
        if not any(term in lowered for term in terms):
            continue
        normalized = " ".join(lowered.split())
        if normalized in seen:
            continue
        seen.add(normalized)
        picked.append(_advisor_public_text(sentence, max_chars=320))
        if len(picked) >= limit:
            break
    return picked


def _advisor_action_sentence(action: dict) -> str:
    title = str(action.get("title") or "").strip()
    why = str(action.get("why") or "").strip()
    next_step = str(action.get("action") or "").strip()
    parts = []
    if title:
        parts.append(title)
    if why:
        parts.append(why)
    if next_step:
        parts.append(f"Next: {next_step}")
    return _advisor_public_text(". ".join(parts), max_chars=520)


def _advisor_normalize_strategic_risk_text(text: str) -> str:
    cleaned = _advisor_public_text(text, max_chars=760)
    if not cleaned:
        return cleaned
    lowered = cleaned.lower()
    if "fixed monthly floor" in lowered:
        return cleaned
    if "fixed floor" in lowered:
        return re.sub(r"\bfixed floor\b", "fixed monthly floor", cleaned, flags=re.IGNORECASE)
    if "income continuity" in lowered and "floor" not in lowered:
        return cleaned.rstrip(".") + " against the fixed monthly floor."
    return cleaned


def _advisor_read_followup_spine(public_memo: dict) -> dict[str, str]:
    actions = [item for item in public_memo.get("action_plan") or [] if isinstance(item, dict)]
    full_text = _advisor_read_public_number_text(public_memo)
    first_action = _advisor_action_sentence(actions[0]) if actions else ""
    lever_lines = []
    for action in actions:
        line = _advisor_action_sentence(action)
        if not line:
            continue
        lowered = line.lower()
        if any(term in lowered for term in _ADVISOR_FIRST_ACTION_TERMS):
            lever_lines.append(line)
        if len(lever_lines) >= 3:
            break
    risk_lines: list[str] = []
    for terms in (
        (
            "income continuity",
            "income source",
            "income stream",
            "source label",
            "source labels",
            "forward income",
        ),
        (
            "fixed monthly floor",
            "fixed floor",
            "operating floor",
            "floor burn",
        ),
        (
            "goal capacity",
            "planning capacity",
            "configured goals",
            "explicit goals",
            "goal targets",
            "reconciled operating burn",
        ),
    ):
        for sentence in _advisor_pick_sentences(full_text, terms, limit=1):
            if sentence not in risk_lines:
                risk_lines.append(sentence)
        if len(risk_lines) >= 3:
            break
    overreact_lines = _advisor_pick_sentences(
        full_text,
        (
            "do not overreact",
            "not overreact",
            "do not treat",
            "not the concern",
            "not the main",
            "broad lifestyle verdict",
            "travel/event",
            "travel month",
        ),
        limit=2,
    )
    return {
        "first_action": first_action or "Use the ranked action plan in order.",
        "low_pain_levers": " ".join(lever_lines) or first_action or "Inspect low-pain operational levers before broad category cuts.",
        "strategic_risk": _advisor_normalize_strategic_risk_text(" ".join(risk_lines))
        or "Income continuity against the fixed monthly floor is the strategic risk to verify before relying on the plan.",
        "do_not_overreact": " ".join(overreact_lines)
        or "Do not turn one noisy month or first-action cleanup into a broad lifestyle verdict.",
    }


def _advisor_read_followup_prompt(
    *,
    public_memo: dict,
    question: str,
    followup_type: str,
    history: list[dict] | None = None,
) -> str:
    theses = public_memo.get("theses") if isinstance(public_memo.get("theses"), list) else []
    action_plan = public_memo.get("action_plan") if isinstance(public_memo.get("action_plan"), list) else []
    spine = _advisor_read_followup_spine(public_memo)
    action_lines = []
    for action in action_plan[:6]:
        if not isinstance(action, dict):
            continue
        title = str(action.get("title") or "").strip()
        why = str(action.get("why") or "").strip()
        next_step = str(action.get("action") or "").strip()
        tradeoff = str(action.get("tradeoff") or "").strip()
        if not title or not next_step:
            continue
        line = f"{action.get('rank')}. {title}: {next_step}"
        if why:
            line += f" Why: {why}"
        if tradeoff:
            line += f" Tradeoff: {tradeoff}"
        action_lines.append(line[:800])
    thesis_lines = []
    for idx, thesis in enumerate(theses[:8], start=1):
        if not isinstance(thesis, dict):
            continue
        summary = str(thesis.get("summary") or "").strip()
        paragraph = str(thesis.get("paragraph") or "").strip()
        caveat = str(thesis.get("caveat") or "").strip()
        if not summary and not paragraph:
            continue
        line = f"{idx}. {summary or paragraph}"
        if paragraph and paragraph != summary:
            line += f" {paragraph}"
        if caveat:
            line += f" Caveat: {caveat}"
        thesis_lines.append(line[:700])
    delta = public_memo.get("delta") if isinstance(public_memo.get("delta"), dict) else {}
    delta_lines: list[str] = []
    if delta:
        headline = str(delta.get("headline") or "").strip()
        action = str(delta.get("action") or "").strip()
        touched_months = ", ".join(str(item) for item in (delta.get("touched_months") or []) if str(item).strip())
        sections = ", ".join(str(item) for item in (delta.get("changed_sections") or []) if str(item).strip())
        categories = ", ".join(str(item) for item in (delta.get("categories") or []) if str(item).strip())
        merchants = ", ".join(str(item) for item in (delta.get("merchants") or []) if str(item).strip())
        if headline:
            delta_lines.append(f"Headline: {headline}")
        if action:
            delta_lines.append(f"Action: {action}")
        if touched_months:
            delta_lines.append(f"Touched months: {touched_months}")
        if sections:
            delta_lines.append(f"Changed read sections: {sections}")
        if categories:
            delta_lines.append(f"Changed categories: {categories}")
        if merchants:
            delta_lines.append(f"Changed merchants: {merchants}")
    card_lines: list[str] = []
    for card in (public_memo.get("cards") or [])[:6]:
        if not isinstance(card, dict):
            continue
        title = str(card.get("title") or "").strip()
        summary = str(card.get("summary") or "").strip()
        detail = str(card.get("detail") or "").strip()
        rows = []
        for row in (card.get("rows") or [])[:4]:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            value = str(row.get("value") or "").strip()
            row_detail = str(row.get("detail") or "").strip()
            if label:
                rows.append(f"{label}: {value}{f' ({row_detail})' if row_detail else ''}")
        line = f"{title}: {summary}"
        if detail:
            line += f" {detail}"
        if rows:
            line += " Rows: " + "; ".join(rows)
        card_lines.append(line[:900])
    memo_excerpt = str(public_memo.get("memo_markdown") or "").strip()[:5000]
    recent_lines: list[str] = []
    for turn in (history or [])[-6:]:
        if not isinstance(turn, dict):
            continue
        role = "User" if str(turn.get("role") or "").lower() == "user" else "Mira"
        content = _advisor_public_text(turn.get("content"), max_chars=700)
        if content:
            recent_lines.append(f"{role}: {content}")
    return f"""
You are Mira, answering a follow-up about a validated stored financial portrait.
Use only the stored read below. Do not run tools, do not invent new facts, and do not give generic finance advice.

User question:
{question}

Follow-up type:
{followup_type}

Recent advisor-read conversation:
{chr(10).join(recent_lines) if recent_lines else "(none)"}

Required behavior:
- Answer directly from the stored read in 2-4 short plain-text paragraphs.
- Name the concrete thesis, the reason it matters, what not to overreact to, and the practical next move.
- If the type is focus, start with the real focus from the read and include what not to overreact to.
- If the type is levers, name the low-pain levers from the read before broad category cuts.
- If the type is risk, lead with the largest strategic risk from the read, not the first low-pain action unless those are the same thing. You may mention the first action after you distinguish it from the strategic risk.
- If the type is changes, answer from the stored delta first; explain what changed, what sections it affects, and whether the core read still stands.
- If the type is normal_month, explain the monthly baseline table from the stored read: income, normal spend, fixed floor, flexible spend, recurring commitments, and capacity.
- If the type is money_map, explain where the money is going using the stored money-map card and distinguish normal pattern from event noise.
- If the type is event_noise, explain what to keep out of the verdict and why it should not become the lifestyle baseline.
- If the type is first_move, answer from the first ranked action and say why it comes before broader cuts.
- If the user asks what to fix first, use the ranked action plan in order.
- Do not use Markdown bullets, numbered lists, headings, bold text, or asterisks.
- Never mention SQL, tools, evidence IDs, validators, backend internals, or dashboard snapshots.
- Never say you cannot answer when the stored read contains the answer.
- Avoid generic openers like "That's a big question" or "Generally".

Advisor distinction spine:
First low-pain action: {spine["first_action"]}
Largest strategic risk: {spine["strategic_risk"]}
Low-pain levers: {spine["low_pain_levers"]}
What not to overreact to: {spine["do_not_overreact"]}

Ranked action plan:
{chr(10).join(action_lines) if action_lines else "(none)"}

Stored delta since the read:
{chr(10).join(delta_lines) if delta_lines else "(none)"}

Advisor read cards:
{chr(10).join(card_lines) if card_lines else "(none)"}

Validated thesis summaries:
{chr(10).join(thesis_lines) if thesis_lines else "(none)"}

Stored read excerpt:
{memo_excerpt}
""".strip()


def _advisor_read_followup_reject_reasons(
    answer: str,
    followup_type: str,
    public_memo: dict | None = None,
    question: str | None = None,
) -> list[str]:
    text = " ".join(str(answer or "").split())
    lowered = text.lower()
    reasons: list[str] = []
    if len(text) < 60:
        reasons.append("too_short")
    for phrase in _ADVISOR_FOLLOWUP_GENERIC_PHRASES:
        if phrase in lowered:
            reasons.append(f"generic:{phrase}")
    for phrase in _ADVISOR_FOLLOWUP_FORBIDDEN_PHRASES:
        if phrase in lowered:
            reasons.append(f"forbidden:{phrase}")
    if "**" in text or re.search(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", str(answer or "")):
        reasons.append("markdown_formatting")
    if public_memo is not None:
        supported_numbers = _advisor_followup_number_tokens(_advisor_read_public_number_text(public_memo))
        unsupported_numbers = _advisor_followup_number_tokens(text) - supported_numbers
        if unsupported_numbers:
            reasons.append("unsupported_numeric_claim")
    if re.search(r"\bdays_\d+_\d+\b", lowered):
        reasons.append("raw_bucket")
    kind = _advisor_read_followup_kind(followup_type)
    if kind == "focus" and not ("cash" in lowered and ("income" in lowered or "fixed" in lowered or "floor" in lowered)):
        reasons.append("missing_focus_thesis")
    if kind == "levers" and not any(term in lowered for term in ("fee", "amazon", "geico", "vendor", "recurring", "subscription", "soft ceiling", "rhythm", "private")):
        reasons.append("missing_lever_thesis")
    if kind == "event_noise" and not (
        ("travel" in lowered or "event" in lowered or "trip" in lowered)
        and ("baseline" in lowered or "overreact" in lowered or "verdict" in lowered)
    ):
        reasons.append("missing_event_noise_thesis")
    if kind == "risk":
        if not _advisor_has_strategic_risk_anchor(lowered):
            reasons.append("missing_risk_thesis")
        first_clause = lowered[:260]
        if any(term in first_clause for term in _ADVISOR_FIRST_ACTION_TERMS) and not _advisor_has_strategic_risk_anchor(first_clause):
            reasons.append("risk_confuses_first_action")
    if kind == "changes":
        has_delta = bool(public_memo and isinstance(public_memo.get("delta"), dict) and public_memo.get("delta"))
        if has_delta and not any(term in lowered for term in ("changed", "update", "current month", "since the read", "still stand")):
            reasons.append("missing_delta_change")
    if _advisor_question_asks_for_actions(question or ""):
        has_action_language = (
            ("first" in lowered or "start" in lowered or "priority" in lowered)
            and any(term in lowered for term in ("fee", "leakage", "income", "geico", "amazon", "capacity", "goal"))
        )
        if not has_action_language:
            reasons.append("missing_ranked_action")
    return sorted(set(reasons))


def _advisor_read_followup_fallback(public_memo: dict, followup_type: str) -> str:
    kind = _advisor_read_followup_kind(followup_type)
    theses = [item for item in public_memo.get("theses") or [] if isinstance(item, dict)]
    actions = [item for item in public_memo.get("action_plan") or [] if isinstance(item, dict)]
    cards = {str(item.get("id") or ""): item for item in public_memo.get("cards") or [] if isinstance(item, dict)}
    spine = _advisor_read_followup_spine(public_memo)
    caveats = [str(item.get("caveat") or "").strip() for item in theses if str(item.get("caveat") or "").strip()]
    caveat = caveats[0] if caveats else "That priority can change if sync, goals, budgets, or income labeling are incomplete."
    ranked = []
    for action in actions[:3]:
        title = str(action.get("title") or "").strip()
        why = str(action.get("why") or "").strip()
        next_step = str(action.get("action") or "").strip()
        tradeoff = str(action.get("tradeoff") or "").strip()
        if title and next_step:
            ranked.append((title, why, next_step, tradeoff))
    if kind == "normal_month":
        card = cards.get("normal_month") or {}
        rows = []
        for row in (card.get("rows") or [])[:6]:
            label = str(row.get("label") or "").strip()
            value = str(row.get("value") or "").strip()
            detail = str(row.get("detail") or "").strip()
            if label and value:
                rows.append(f"{label} is {value}{f'; {detail}' if detail else ''}")
        if rows:
            return (
                "The normal-month read is the planning baseline, not a raw spending recap. "
                + ". ".join(rows[:4])
                + ".\n\n"
                "The useful point is sequence: cover the fixed floor first, then judge flexible spend, recurring commitments, and goal capacity. Do not pull trip or event noise into the lifestyle baseline unless it repeats.\n\n"
                f"Next move: verify the fixed commitments and goal targets before treating the remaining capacity as spendable. Caveat: {caveat}"
            )
    if kind == "money_map":
        card = cards.get("money_map") or {}
        rows = []
        for row in (card.get("rows") or [])[:5]:
            label = str(row.get("label") or "").strip()
            value = str(row.get("value") or "").strip()
            detail = str(row.get("detail") or "").strip()
            if label:
                rows.append(f"{label}{f' averages {value}' if value else ''}{f'; driven by {detail}' if detail else ''}")
        if rows:
            return (
                "The money-map read is about controllability, not just category size. "
                + ". ".join(rows[:4])
                + ".\n\n"
                "Start with reviewable leakage, recurring/vendor items, and small-purchase cleanup before broad cuts. Do not treat travel or one-off event clusters as normal lifestyle drift unless the pattern repeats.\n\n"
                f"Next move: pick one low-pain review item first, then revisit flexible categories after that cleanup. Caveat: {caveat}"
            )
    if kind == "event_noise":
        card = cards.get("event_noise") or {}
        rows = []
        for row in (card.get("rows") or [])[:3]:
            label = str(row.get("label") or "").strip()
            value = str(row.get("value") or "").strip()
            detail = str(row.get("detail") or "").strip().rstrip(".")
            if label:
                rows.append(f"{label}{f' was {value}' if value else ''}{f'; {detail}' if detail else ''}")
        if rows:
            return (
                "The thing not to overreact to is event noise: "
                + ". ".join(rows)
                + ".\n\n"
                "That belongs outside the normal lifestyle baseline until it repeats. The point is not to ignore it; it is to avoid using a trip or one-off event as proof your everyday operating floor changed.\n\n"
                f"Next move: keep those clusters excluded when judging normal spend, then inspect the recurring and lower-pain levers separately. Caveat: {caveat}"
            )
    if kind == "first_move":
        if ranked:
            first = ranked[0]
            answer = f"First move: {first[0]}. {first[2]} The reason it comes first is: {first[1]}"
            if first[3]:
                answer += f"\n\nDo not overreact here: {first[3]}"
            if len(ranked) > 1:
                answer += f"\n\nAfter that, move to {ranked[1][0].lower()}; that is the next dependency once the first cleanup is handled."
            return answer
    if ranked and kind == "general":
        first = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        answer = f"First: {first[0]}. {first[2]} The reason is: {first[1]}"
        if second:
            answer += f"\n\nSecond: {second[0]}. {second[2]}"
        if first[3]:
            answer += f"\n\nDo not overreact here: {first[3]}"
            return answer
    delta = public_memo.get("delta") if isinstance(public_memo.get("delta"), dict) else {}
    if kind == "changes":
        if delta:
            headline = str(delta.get("headline") or "The stored facts changed since this read.").strip()
            action = str(delta.get("action") or "Use the changed facts to update the affected sections before making a new decision.").strip()
            months = ", ".join(str(item) for item in delta.get("touched_months") or [] if str(item).strip())
            sections = ", ".join(str(item) for item in delta.get("changed_sections") or [] if str(item).strip())
            subjects = ", ".join(
                str(item)
                for item in (delta.get("categories") or []) + (delta.get("merchants") or [])
                if str(item).strip()
            )
            answer = f"{headline} The core read can still stand, but this update should be checked before acting on the current month."
            if months or sections:
                answer += f"\n\nIt touches {months or 'the latest period'} and affects {sections or 'the current read sections'}."
            if subjects:
                answer += f"\n\nThe visible changed subjects are {subjects}. {action}"
            else:
                answer += f"\n\n{action}"
            return answer
        return (
            "I do not have a stored change packet after this read, so I would treat the stored portrait as the latest validated read for now.\n\n"
            "If you want a fresh rebuild, use Fresh read; otherwise I can answer from the current stored portrait without pretending it has new facts."
        )
    if kind == "levers":
        card = cards.get("soft_levers") or {}
        rows = []
        for row in (card.get("rows") or [])[:5]:
            label = str(row.get("label") or "").strip()
            value = str(row.get("value") or "").strip()
            detail = str(row.get("detail") or "").strip().rstrip(".")
            if label:
                rows.append(f"{label}{f' at {value}' if value else ''}{f': {detail}' if detail else ''}")
        if rows:
            return (
                "The lower-pain levers from the read are: "
                + ". ".join(rows[:4])
                + ".\n\n"
                "These come before broad cuts because they are timing, vendor, recurring, or rhythm cleanups rather than a demand to shrink everything you enjoy.\n\n"
                f"Next move: fix or verify the first review item, then decide whether any flexible category still needs a real trim. Caveat: {caveat}"
            )
        if ranked:
            first = ranked[0]
            extra = ranked[1] if len(ranked) > 1 else None
            answer = f"Start with {first[0].lower()}: {first[2]} The reason is: {first[1]}"
            if extra:
                answer += f"\n\nThen look at {extra[0].lower()}: {extra[2]}"
            answer += "\n\nDo not turn this into broad cuts before the low-pain cleanup is done."
            return answer
        return (
            "Start with the tune-ups before broad cuts: fees, Amazon-style small purchases, recurring charges, and vendor reviews are the lower-pain places to inspect first.\n\n"
            "Do not overcorrect from one noisy month or a private rhythm that is already improving; the read treats those as tuning signals, not a character verdict.\n\n"
            f"Next move: check the repeat charges and vendor pricing first, then only trim broader categories if the pattern survives that cleanup. Caveat: {caveat}"
        )
    if kind == "risk":
        first_action = spine.get("first_action") or "the first ranked action"
        strategic_risk = spine.get("strategic_risk") or "income continuity against the fixed monthly floor"
        do_not_overreact = spine.get("do_not_overreact") or "Do not turn a noisy month into a broad lifestyle verdict."
        return (
            f"The biggest strategic risk in the read: {strategic_risk}\n\n"
            f"That is different from the first low-pain action: {first_action} Use that cleanup first, but do not mistake it for the whole risk picture.\n\n"
            f"Do not overreact here: {do_not_overreact}\n\n"
            f"Next move: verify income-source labels, fixed obligations, and goal targets before making bigger tradeoffs. Caveat: {caveat}"
        )
    if kind == "focus":
        return (
            "Cash is not the concern; the useful focus is whether income continuity comfortably supports the fixed monthly floor once one-offs and travel/event spend are separated from the baseline.\n\n"
            "Do not overreact to a noisy month before isolating what was temporary. The sharper move is to protect the parts already improving and inspect fees, recurring charges, and vendor pricing first.\n\n"
            f"Next move: verify income source labels and the operating floor, then decide what actually needs tuning. Caveat: {caveat}"
        )
    summaries = [str(item.get("summary") or "").strip() for item in theses if str(item.get("summary") or "").strip()]
    if summaries:
        return "From the stored read, the main point is: " + " ".join(summaries[:3])
    return "Mira has a stored financial read, but it does not contain enough validated detail to answer this follow-up cleanly."


def _compose_advisor_read_followup(
    *,
    memo: dict,
    question: str,
    followup_type: str,
    history: list[dict] | None = None,
    delta: dict | None = None,
    complete_fn=None,
) -> dict:
    import llm_client

    kind = _advisor_read_followup_kind(followup_type)
    public_memo = _public_advisor_read_payload(memo, delta=delta)
    prompt = _advisor_read_followup_prompt(
        public_memo=public_memo,
        question=question,
        followup_type=kind,
        history=history,
    )
    raw = ""
    error = None
    try:
        fn = complete_fn or llm_client.complete
        raw = str(fn(prompt, max_tokens=420, purpose="copilot") or "").strip()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    reject_reasons = _advisor_read_followup_reject_reasons(raw, kind, public_memo=public_memo, question=question)
    used_fallback = bool(error or reject_reasons)
    answer = _advisor_read_followup_fallback(public_memo, kind) if used_fallback else raw
    final_reject_reasons = _advisor_read_followup_reject_reasons(answer, kind, public_memo=public_memo, question=question)
    return {
        "answer": answer,
        "followup_type": kind,
        "used_fallback": used_fallback,
        "reject_reasons": reject_reasons,
        "final_reject_reasons": final_reject_reasons,
        "llm_error": error,
        "memo_id": public_memo.get("id"),
        "memo_generated_at": public_memo.get("generated_at"),
        "delta": public_memo.get("delta"),
    }


def _advisor_read_generation_enabled() -> bool:
    try:
        from mira.advisor_lens_synthesis import advisor_lens_store_enabled, advisor_lens_synthesis_enabled

        return advisor_lens_synthesis_enabled() and advisor_lens_store_enabled()
    except Exception as exc:
        logger.debug("Failed to load advisor read generation flags: %s", exc)
        return False


# ── Models ──


class CategoryUpdate(BaseModel):
    category: str
    one_off: bool = False


class TransactionExcludeUpdate(BaseModel):
    is_excluded: bool


class CopilotRequest(BaseModel):
    question: str
    history: list[dict] | None = None


class AdvisorReadFollowupRequest(BaseModel):
    question: str
    followup_type: str = "general"
    history: list[dict] | None = None


class MiraFinancialFeedbackRequest(BaseModel):
    feedback_type: str
    target_type: str = "advisor_read"
    target_id: str = ""
    fact_id: StrictInt | None = None
    insight_id: StrictInt | None = None
    subject_type: str = "profile"
    subject_key: str = ""
    correction_text: str = ""
    normalized_effect: str = ""
    safe_summary: str = ""
    sensitivity: str = ""
    source: str = "chat"
    expires_at: str = ""
    metadata: dict | None = None


class MiraStatedIntentCreateRequest(BaseModel):
    subject_type: str
    subject_key: str
    intent_kind: str = "monitor"
    target_text: str
    subject_label: str = ""
    baseline_scope: str = ""
    feedback_state: str = "neutral"


class MiraStatedIntentUpdateRequest(BaseModel):
    target_text: str | None = None
    status: str | None = None
    feedback_state: str | None = None


class SaveInsightRequest(BaseModel):
    question: str
    answer: str
    kind: str = "insight"
    source_conversation_id: int | None = None


class MonthExplanationRequest(BaseModel):
    month: str
    use_llm: bool = True
    save: bool = True


class MemoryEntryCreate(BaseModel):
    section: str
    body: str
    confidence: str = "stated"
    evidence: str = ""


class MemoryEntryUpdate(BaseModel):
    body: str
    evidence: str | None = None


class MemoryProposalAccept(BaseModel):
    body: str | None = None
    section: str | None = None


class MiraMemoryUpdate(BaseModel):
    normalized_text: str | None = None
    memory_type: str | None = None
    topic: str | None = None
    sensitivity: str | None = None
    confidence: float | None = None
    pinned: bool | None = None
    expires_at: str | None = None
    status: str | None = None


class MiraMemoryCreate(BaseModel):
    text: str
    memory_type: str | None = None
    topic: str | None = None
    source_summary: str = ""
    source_turn_id: str | None = None
    pinned: bool = False
    expires_at: str | None = None


class MiraSessionSummaryUpdate(BaseModel):
    summary_text: str | None = None
    status: str | None = None


class LocalLlmSettingsUpdate(BaseModel):
    llm_provider: str | None = None
    preset: str | None = None
    categorize_model: str | None = None
    controller_model: str | None = None
    copilot_model: str | None = None
    categorize_batch_size: int | None = None
    inter_batch_delay_ms: int | None = None
    low_power_mode: bool | None = None
    expert_mode: bool | None = None


class LocalLlmInstallRequest(BaseModel):
    model: str


class ReceiptItemUpdateRequest(BaseModel):
    items: list[dict]
    store_name: str | None = None
    receipt_date: str | None = None


# ── Helper Functions ──


def _is_expense(tx: dict) -> bool:
    """True if transaction is a real spending expense."""
    amount = float(tx.get("amount", 0))
    cat = tx.get("category", "Other")
    return amount < 0 and cat not in NON_SPENDING_CATEGORIES


def _is_income(tx: dict) -> bool:
    """True if transaction is income."""
    amount = float(tx.get("amount", 0))
    cat = tx.get("category", "Other")
    return cat == "Income" and amount > 0


def _is_refund(tx: dict) -> bool:
    """
    True if transaction is a refund.
    Positive amount, not income, not a transfer/savings.
    """
    amount = float(tx.get("amount", 0))
    cat = tx.get("category", "Other")
    return amount > 0 and (cat == "Credits & Refunds" or cat not in NON_SPENDING_CATEGORIES)


def _is_savings(tx: dict) -> bool:
    """True if transaction is a savings transfer."""
    return tx.get("category") == "Savings Transfer"


# ── Routes ──


@app.get("/api/profiles")
def profiles(db=Depends(get_db_session)):
    """Return available profile names for the frontend toggle."""
    return _get_profile_list(db)


@app.get("/api/app-config")
def app_config(db=Depends(get_db_session)):
    """Frontend-safe runtime flags for demo/public deployments."""
    return _app_config_payload(db)


@app.get("/api/local-llm/catalog")
def local_llm_catalog(db=Depends(get_db_session)):
    return get_catalog_response(db)


@app.get("/api/categorization/status")
def categorization_status():
    return _categorization_status_payload(preload_distilbert=False)


@app.get("/api/local-llm/status")
def local_llm_status(db=Depends(get_db_session)):
    schedule_prewarm_selected_model("controller", db)
    schedule_prewarm_selected_model("copilot", db)
    schedule_prewarm_chat_prompt()
    return get_status_response(db)


@app.patch("/api/local-llm/settings")
def patch_local_llm_settings(body: LocalLlmSettingsUpdate, db=Depends(get_db_session)):
    payload = {}
    if body.llm_provider is not None:
        payload["llm_provider"] = body.llm_provider
    if body.preset is not None:
        payload["local_ai_profile"] = body.preset
    if body.categorize_model is not None:
        payload["categorize_model"] = body.categorize_model
    if body.controller_model is not None:
        payload["controller_model"] = body.controller_model
    if body.copilot_model is not None:
        payload["copilot_model"] = body.copilot_model
    if body.categorize_batch_size is not None:
        payload["categorize_batch_size"] = body.categorize_batch_size
    if body.inter_batch_delay_ms is not None:
        payload["inter_batch_delay_ms"] = body.inter_batch_delay_ms
    if body.low_power_mode is not None:
        payload["low_power_mode"] = body.low_power_mode
    if body.expert_mode is not None:
        payload["expert_mode"] = body.expert_mode

    try:
        update_local_llm_settings(db, payload)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if body.controller_model is not None or body.llm_provider is not None:
        schedule_prewarm_selected_model("controller", db, force=True)
    if body.copilot_model is not None or body.llm_provider is not None:
        schedule_prewarm_selected_model("copilot", db, force=True)
        schedule_prewarm_chat_prompt(force=True)

    return {
        "status": get_status_response(db),
        "config": _app_config_payload(db),
    }


@app.post("/api/local-llm/install")
def post_local_llm_install(body: LocalLlmInstallRequest, db=Depends(get_db_session)):
    try:
        result = install_local_llm_model(body.model, db)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        **result,
        "config": _app_config_payload(db),
    }


class AccountPaymentDetailsPayload(BaseModel):
    usual_due_day: StrictInt | None = None


@app.get("/api/accounts")
def accounts(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_accounts_filtered(profile=profile, conn=db)


@app.patch("/api/accounts/{account_id}/payment-details")
def update_account_payment_details_endpoint(account_id: str, body: AccountPaymentDetailsPayload, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    try:
        account = update_account_payment_details(account_id, body.model_dump(), profile=profile, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not account:
        raise HTTPException(status_code=404, detail="Account not found.")
    return {"status": "updated", "account": account}


@app.get("/api/transactions")
def transactions(
    month: str | None = Query(None, description="YYYY-MM"),
    category: str | None = Query(None),
    account: str | None = Query(None),
    search: str | None = Query(None),
    reviewed: bool | None = Query(None),
    profile: str | None = Depends(validate_profile),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db=Depends(get_db_session),
):
    return get_transactions_paginated(
        month=month,
        category=category,
        account=account,
        search=search,
        reviewed=reviewed,
        profile=profile,
        limit=limit,
        offset=offset,
        conn=db,
    )


@app.get("/api/transactions/review-queue")
def transaction_review_queue(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_review_queue_data(profile=profile, conn=db)


@app.post("/api/transactions/bulk-review")
def bulk_review_transactions(
    month: str | None = Query(None, description="YYYY-MM"),
    category: str | None = Query(None),
    account: str | None = Query(None),
    search: str | None = Query(None),
    reviewed: bool | None = Query(None),
    target_reviewed: bool = Query(True),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    return {
        "status": "updated",
        **bulk_mark_transactions_reviewed(
            month=month,
            category=category,
            account=account,
            search=search,
            reviewed=reviewed,
            target_reviewed=target_reviewed,
            profile=profile,
            conn=db,
        ),
    }


@app.get("/api/transactions/export")
def export_transactions(
    month: str | None = Query(None, description="YYYY-MM"),
    category: str | None = Query(None),
    account: str | None = Query(None),
    search: str | None = Query(None),
    reviewed: bool | None = Query(None),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    rows = []
    offset = 0
    total_count = None
    while True:
        result = get_transactions_paginated(
            month=month,
            category=category,
            account=account,
            search=search,
            reviewed=reviewed,
            profile=profile,
            limit=1000,
            offset=offset,
            conn=db,
        )
        page = result.get("data", [])
        rows.extend(page)
        total_count = result.get("total_count", len(rows))
        if not page or len(rows) >= total_count:
            break
        offset += len(page)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "date", "description", "merchant_display_name", "account_name",
            "amount", "category", "reviewed", "notes", "tags", "profile",
        ],
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({
            **row,
            "reviewed": "yes" if row.get("reviewed") else "no",
            "tags": ", ".join(row.get("tags") or []),
        })
    suffix = month or datetime.utcnow().strftime("%Y-%m-%d")
    return {
        "filename": f"folio-transactions-{suffix}.csv",
        "csv": output.getvalue(),
        "row_count": len(rows),
        "total_count": total_count if total_count is not None else len(rows),
    }


@app.post("/api/receipts/parse")
async def parse_receipt(
    file: UploadFile = File(...),
    profile: str | None = Query(None),
    db=Depends(get_db_session),
):
    _require_receipts_enabled()
    validated_profile = validate_profile(profile)
    content_type = (file.content_type or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Upload a receipt image.")
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Receipt image is empty.")
    if len(image_bytes) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Receipt image must be under 12 MB.")

    try:
        from receipts import create_draft_receipt, parse_receipt_image
        parsed, parser_model = await run_in_threadpool(parse_receipt_image, image_bytes, file.content_type)
        return create_draft_receipt(db, validated_profile, parsed, parser_model)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("Receipt parsing failed")
        raise HTTPException(status_code=502, detail=f"Receipt parsing failed: {exc}")


@app.get("/api/receipts")
def receipt_list(
    status: str | None = Query(None),
    limit: int = Query(12, ge=1, le=50),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    _require_receipts_enabled()
    from receipts import list_receipts
    statuses = [part.strip().lower() for part in (status or "").split(",") if part.strip()]
    invalid = [part for part in statuses if part not in {"draft", "approved", "discarded"}]
    if invalid:
        raise HTTPException(status_code=400, detail="Receipt status must be draft, approved, or discarded.")
    return list_receipts(db, profile, statuses or None, limit)


@app.get("/api/receipts/comparisons")
def receipt_comparisons(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    _require_receipts_enabled()
    from receipts import get_comparisons
    return get_comparisons(db, profile)


@app.get("/api/receipts/{receipt_id}")
def receipt_detail(receipt_id: int, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    _require_receipts_enabled()
    from receipts import get_receipt
    try:
        return get_receipt(db, receipt_id, profile)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.patch("/api/receipts/{receipt_id}/items")
def patch_receipt_items(
    receipt_id: int,
    body: ReceiptItemUpdateRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    _require_receipts_enabled()
    from receipts import ReceiptDraftMetadataUpdate, ReceiptItemUpdate, update_receipt_items
    try:
        items = [ReceiptItemUpdate.model_validate(item) for item in body.items]
        metadata = ReceiptDraftMetadataUpdate.model_validate({
            "store_name": body.store_name,
            "receipt_date": body.receipt_date,
        })
        return update_receipt_items(db, receipt_id, profile, items, metadata)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/receipts/{receipt_id}/approve")
def approve_receipt(receipt_id: int, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    _require_receipts_enabled()
    from receipts import set_receipt_status
    try:
        return set_receipt_status(db, receipt_id, profile, "approved")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/receipts/{receipt_id}/discard")
def discard_receipt(receipt_id: int, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    _require_receipts_enabled()
    from receipts import set_receipt_status
    try:
        return set_receipt_status(db, receipt_id, profile, "discarded")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.patch("/api/transactions/{tx_id}/category")
def update_category(tx_id: str, body: CategoryUpdate, db=Depends(get_db_session)):
    active_cats = get_active_categories()
    # Allow new categories — they'll be auto-created
    # Only reject empty strings
    if not body.category or not body.category.strip():
        raise HTTPException(
            status_code=400,
            detail="Category cannot be empty.",
        )
    result = update_transaction_category(tx_id, body.category, one_off=body.one_off)
    if not result:
        raise HTTPException(status_code=404, detail="Transaction not found")
    _invalidate_copilot_cache()
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        tx_id=tx_id,
        reason="transaction_category_changed",
    )

    response = {"status": "updated", "tx_id": tx_id, "category": body.category}

    if isinstance(result, dict):
        response["retroactive_count"] = result.get("retroactive_count", 0)
        # Enhancement 7: Pass through subscription prompt signal
        if result.get("subscription_prompt"):
            response["subscription_prompt"] = True
            response["merchant"] = result.get("merchant", "")
            response["amount"] = result.get("amount", 0.0)
            response["transaction_id"] = result.get("transaction_id", tx_id)

    return response


@app.patch("/api/transactions/{tx_id}/exclude")
def update_transaction_exclusion(tx_id: str, body: TransactionExcludeUpdate, db=Depends(get_db_session)):
    result = update_transaction_excluded(tx_id=tx_id, is_excluded=body.is_excluded, conn=db)
    if not result:
        raise HTTPException(status_code=404, detail="Transaction not found")
    _invalidate_copilot_cache()
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        tx_id=tx_id,
        reason="transaction_exclusion_changed",
    )
    return {"status": "updated", "transaction": result}


@app.get("/api/categories")
def categories():
    return get_active_categories()


class NewCategory(BaseModel):
    name: str


class CategoryDeactivateBody(BaseModel):
    replacement_category: str | None = None


@app.post("/api/categories")
def create_category(body: NewCategory):
    """Add a new user-defined category."""
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="Category name cannot be empty.")
    success = add_category(body.name.strip())
    if not success:
        raise HTTPException(status_code=409, detail="Category already exists.")
    _invalidate_copilot_cache()
    return {"status": "created", "category": body.name.strip()}


@app.delete("/api/categories/{category_name}")
def delete_category(category_name: str, body: CategoryDeactivateBody | None = None, db=Depends(get_db_session)):
    """Soft-delete a user-defined category, optionally moving references first."""
    try:
        result = deactivate_category(
            category_name,
            replacement_category=body.replacement_category if body else None,
            conn=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result:
        raise HTTPException(status_code=404, detail="Category not found.")
    _invalidate_copilot_cache()
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        all_profiles=True,
        reason="category_deleted",
    )
    return {"status": "deleted", **result}


class ExpenseTypeUpdate(BaseModel):
    expense_type: str


class CategoryParentUpdate(BaseModel):
    parent_category: str | None = None


@app.patch("/api/categories/{category_name}/expense-type")
def update_expense_type(category_name: str, body: ExpenseTypeUpdate, db=Depends(get_db_session)):
    """Update a category's expense_type classification (fixed/variable)."""
    if body.expense_type not in ("fixed", "variable"):
        raise HTTPException(
            status_code=400,
            detail="expense_type must be 'fixed' or 'variable'.",
        )
    row = db.execute(
        "SELECT name, expense_type FROM categories WHERE name = ? AND is_active = 1",
        (category_name,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Category not found.")
    # Don't allow toggling non_expense categories
    if row[1] == "non_expense":
        raise HTTPException(
            status_code=400,
            detail="Cannot change expense type of transfer/income categories.",
        )
    db.execute(
        "UPDATE categories SET expense_type = ?, expense_type_source = 'user' WHERE name = ?",
        (body.expense_type, category_name),
    )
    _invalidate_copilot_cache()
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        all_profiles=True,
        reason="category_expense_type_changed",
    )
    return {
        "status": "updated",
        "category": category_name,
        "expense_type": body.expense_type,
    }


@app.get("/api/categories/meta")
def categories_meta():
    return get_categories_meta()


@app.patch("/api/categories/{category_name}/parent")
def update_category_parent_endpoint(category_name: str, body: CategoryParentUpdate, db=Depends(get_db_session)):
    try:
        result = update_category_parent(category_name, body.parent_category, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result:
        raise HTTPException(status_code=404, detail="Category not found.")
    _invalidate_copilot_cache()

    return {"status": "updated", "category": result}


@app.get("/api/category-rules")
def list_category_rules(source: str | None = Query(None)):
    """List category rules, optionally filtered by source ('user' or 'system')."""
    return get_category_rules(source)


class CategoryRuleUpdate(BaseModel):
    category: str | None = None
    priority: int | None = None
    is_active: bool | None = None


@app.patch("/api/category-rules/{rule_id}")
def update_category_rule_endpoint(rule_id: int, body: CategoryRuleUpdate, db=Depends(get_db_session)):
    try:
        result = update_category_rule(
            rule_id=rule_id,
            category=body.category,
            priority=body.priority,
            is_active=body.is_active,
            conn=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result:
        raise HTTPException(status_code=404, detail="Rule not found.")

    _invalidate_copilot_cache()
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        all_profiles=True,
        reason="category_rule_changed",
    )
    return {"status": "updated", "rule": result}


@app.get("/api/category-rules/{rule_id}/impact")
def category_rule_impact(
    rule_id: int,
    profile: str | None = Depends(validate_profile),
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db_session),
):
    result = get_category_rule_impact(rule_id=rule_id, profile=profile, limit=limit, conn=db)
    if not result:
        raise HTTPException(status_code=404, detail="Rule not found.")
    return result


@app.get("/api/analytics/monthly")
def monthly_analytics(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_monthly_analytics_data(profile=profile, conn=db)


@app.get("/api/analytics/categories")
def category_analytics(month: str | None = Query(None), profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_category_analytics_data(month=month, profile=profile, conn=db)

@app.get("/api/summary")
def summary(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_summary_data(profile=profile, conn=db)

@app.get("/api/merchants")
def merchant_insights(month: str | None = Query(None), profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    """Merchant-level spending breakdown from Trove-enriched data."""
    return get_merchant_insights_data(month=month, profile=profile, conn=db)


@app.get("/api/analytics/recurring")
def get_recurring_transactions(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    """
    Return recurring / subscription data from the recurring obligation model.
    Detection runs incrementally after each sync (see data_manager.fetch_fresh_data)
    and still maintain legacy merchant subscription fields as a compatibility cache.
    Use POST /api/subscriptions/redetect for a manual full refresh.

    Response includes items, events, and dismissed arrays for the frontend bundle.
    """
    from data_manager import get_recurring_from_db

    try:
        result = get_recurring_from_db(profile=profile, conn=db)
        # If no items stored yet (first load before any sync), fall back to live detection
        if not result["items"] and result["active_count"] == 0:
            from recurring import RecurringDetector, write_detection_results_to_db
            data = get_data()
            txns = data["transactions"]
            txns = _filter_by_profile(txns, profile)
            if txns:
                detector = RecurringDetector(get_db_conn=get_db)
                detection = detector.detect(transactions=txns, profile=profile, generate_events=True)
                write_detection_results_to_db(
                    get_db_conn=get_db,
                    items=detection["items"],
                    events=detection.get("events", []),
                    profile=profile,
                )
                result = get_recurring_from_db(profile=profile, conn=db)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Subscription user feedback ───────────────────────────────────

class SubscriptionConfirm(BaseModel):
    merchant: str
    pattern: str | None = None
    frequency_hint: str = "monthly"
    category: str = "Subscriptions"


class SubscriptionDismiss(BaseModel):
    merchant: str
    pattern: str | None = None


@app.post("/api/subscriptions/confirm")
def confirm_subscription(body: SubscriptionConfirm, profile: str | None = Query(None), db=Depends(get_db_session)):
    """
    User confirms a detected recurring charge as a subscription.
    Creates a user-sourced seed in the subscription_seeds table.
    """
    from recurring_obligations import canonical_key as _recurring_canonical_key, record_feedback as _record_feedback

    pattern = (body.pattern or body.merchant).upper().strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="Merchant or pattern required.")

    created_by = profile or "household"

    existing = db.execute(
        """SELECT id FROM subscription_seeds
           WHERE pattern = ? AND source = 'user' AND created_by = ?""",
        (pattern, created_by),
    ).fetchone()

    if existing:
        db.execute(
            """UPDATE subscription_seeds
               SET name = ?, frequency_hint = ?, category = ?, is_active = 1
               WHERE id = ?""",
            (body.merchant, body.frequency_hint, body.category, existing[0]),
        )
    else:
        db.execute(
            """INSERT INTO subscription_seeds
               (name, pattern, frequency_hint, category, source, created_by)
               VALUES (?, ?, ?, ?, 'user', ?)""",
            (body.merchant, pattern, body.frequency_hint, body.category, created_by),
        )

    merchant_key = _recurring_canonical_key(body.merchant)
    if merchant_key:
        db.execute(
            """UPDATE recurring_obligations
               SET state = 'confirmed',
                   source = CASE WHEN source = 'user' THEN source ELSE 'user_confirmed' END,
                   confidence_score = MAX(confidence_score, 100),
                   confidence_label = 'user',
                   last_user_action_at = datetime('now'),
                   updated_at = datetime('now')
               WHERE profile_id = ? AND merchant_key = ?""",
            (created_by, merchant_key),
        )
        _record_feedback(
            db,
            merchant=body.merchant,
            profile_id=created_by,
            feedback_type="confirmed",
            scope="merchant",
            payload={
                "pattern": pattern,
                "frequency_hint": body.frequency_hint,
                "category": body.category,
            },
        )

    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=created_by,
        reason="recurring_subscription_confirmed",
    )
    return {"status": "confirmed", "merchant": body.merchant, "pattern": pattern}


@app.post("/api/subscriptions/dismiss")
def dismiss_subscription(body: SubscriptionDismiss, profile: str | None = Query(None), db=Depends(get_db_session)):
    """
    User dismisses a false positive — marks the pattern as inactive for this user.
    Also records in dismissed_recurring table for Enhancement 2.
    If it's a system seed, we create a user-level suppression entry.
    """
    from recurring_obligations import record_feedback as _record_feedback

    pattern = (body.pattern or body.merchant).upper().strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="Merchant or pattern required.")

    created_by = profile or "household"

    existing = db.execute(
        """SELECT id FROM subscription_seeds
           WHERE pattern = ? AND source = 'user' AND created_by = ?""",
        (pattern, created_by),
    ).fetchone()

    if existing:
        db.execute(
            "UPDATE subscription_seeds SET is_active = 0 WHERE id = ?",
            (existing[0],),
        )
    else:
        db.execute(
            """INSERT INTO subscription_seeds
               (name, pattern, frequency_hint, category, source, created_by, is_active)
               VALUES (?, ?, 'monthly', 'Dismissed', 'user', ?, 0)""",
            (body.merchant, pattern, created_by),
        )

    # Also record in dismissed_recurring table (Enhancement 2)
    db.execute(
        """INSERT OR IGNORE INTO dismissed_recurring
           (merchant_name, profile_id)
           VALUES (?, ?)""",
        (body.merchant, created_by),
    )
    _record_feedback(
        db,
        merchant=body.merchant,
        profile_id=created_by,
        feedback_type="dismissed",
        scope="merchant",
        payload={"pattern": pattern},
    )

    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=created_by,
        reason="recurring_subscription_dismissed",
    )
    return {"status": "dismissed", "merchant": body.merchant, "pattern": pattern}


# ── Subscription management (Enhancements 1-4, 6) ────────────────────────

class SubscriptionDeclare(BaseModel):
    merchant: str
    amount: float
    frequency: str = "monthly"
    category: str = "Subscriptions"
    expected_day: int | None = None
    profile: str | None = None


class SubscriptionAmountReviewDismiss(BaseModel):
    merchant: str
    suggested_amount: float
    latest_date: str
    profile: str | None = None


@app.post("/api/subscriptions/declare")
def declare_subscription_endpoint(body: SubscriptionDeclare, db=Depends(get_db_session)):
    """
    User explicitly declares a transaction as a recurring subscription.
    Layer 0 — always appears in results with confidence = 'user'.
    """
    from data_manager import declare_subscription

    if not body.merchant or not body.merchant.strip():
        raise HTTPException(status_code=400, detail="Merchant name required.")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive.")
    if body.frequency not in ("monthly", "quarterly", "semi_annual", "annual"):
        raise HTTPException(status_code=400, detail="Frequency must be monthly, quarterly, semi_annual, or annual.")
    if body.expected_day is not None and not (1 <= int(body.expected_day) <= 31):
        raise HTTPException(status_code=400, detail="Expected day must be between 1 and 31.")

    profile = body.profile or "household"
    result = declare_subscription(
        merchant=body.merchant.strip(),
        amount=body.amount,
        frequency=body.frequency,
        profile=profile,
        category=body.category or "Subscriptions",
        expected_day=body.expected_day,
    )
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile,
        reason="recurring_subscription_declared",
    )
    return {"status": "ok", "message": "Subscription declared", "subscription": result}


@app.post("/api/subscriptions/amount-review/dismiss")
def dismiss_subscription_amount_review(body: SubscriptionAmountReviewDismiss, db=Depends(get_db_session)):
    """
    Suppress the current expected-amount suggestion for a user-declared recurring bill.
    A newer latest charge or materially different suggested amount can surface again.
    """
    if not body.merchant or not body.merchant.strip():
        raise HTTPException(status_code=400, detail="Merchant name required.")
    if body.suggested_amount <= 0:
        raise HTTPException(status_code=400, detail="Suggested amount must be positive.")
    if not body.latest_date or not body.latest_date.strip():
        raise HTTPException(status_code=400, detail="Latest date required.")

    from recurring_obligations import canonical_key as _recurring_canonical_key, record_feedback as _record_feedback

    profile_id = body.profile or "household"
    merchant = body.merchant.strip()
    result = db.execute(
        """UPDATE user_declared_subscriptions
           SET amount_review_dismissed_amount = ?,
               amount_review_dismissed_latest_date = ?,
               amount_review_dismissed_at = datetime('now'),
               updated_at = datetime('now')
           WHERE profile_id = ?
             AND is_active = 1
             AND (merchant_name = ? OR UPPER(merchant_name) = ?)""",
        (body.suggested_amount, body.latest_date.strip(), profile_id, merchant, merchant.upper()),
    )
    merchant_key = _recurring_canonical_key(merchant)
    v2_exists = db.execute(
        """SELECT 1
           FROM recurring_obligations
           WHERE profile_id = ? AND merchant_key = ?
           LIMIT 1""",
        (profile_id, merchant_key),
    ).fetchone()
    if result.rowcount == 0 and not v2_exists:
        raise HTTPException(status_code=404, detail="User-declared subscription not found.")
    _record_feedback(
        db,
        merchant=merchant,
        profile_id=profile_id,
        feedback_type="amount_review_dismissed",
        scope="exact_candidate",
        payload={
            "suggested_amount": body.suggested_amount,
            "latest_date": body.latest_date.strip(),
        },
    )
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile_id,
        reason="recurring_amount_review_dismissed",
    )
    return {"status": "dismissed", "merchant": merchant, "suggested_amount": body.suggested_amount}


@app.post("/api/subscriptions/{merchant}/cancel")
def cancel_subscription_endpoint(merchant: str, profile: str | None = Query(None), db=Depends(get_db_session)):
    """
    User confirms an inactive subscription has been cancelled.
    Zombie detection will flag new charges from this merchant.
    """
    from data_manager import cancel_subscription

    if not merchant or not merchant.strip():
        raise HTTPException(status_code=400, detail="Merchant name required.")

    result = cancel_subscription(merchant=merchant.strip(), profile=profile)
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile,
        reason="recurring_subscription_cancelled",
    )
    return result


@app.post("/api/subscriptions/{merchant}/restore")
def restore_subscription_endpoint(merchant: str, profile: str | None = Query(None), db=Depends(get_db_session)):
    """
    Restore a previously dismissed subscription.
    Removes the merchant from the dismissed_recurring table.
    """
    from data_manager import restore_subscription
    import urllib.parse

    decoded_merchant = urllib.parse.unquote(merchant).strip()
    if not decoded_merchant:
        raise HTTPException(status_code=400, detail="Merchant name required.")

    profile_id = profile or "household"

    # Also re-activate seed if it was suppressed
    existing = db.execute(
        """SELECT id FROM subscription_seeds
           WHERE pattern = ? AND source = 'user' AND created_by = ? AND is_active = 0""",
        (decoded_merchant.upper(), profile_id),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE subscription_seeds SET is_active = 1 WHERE id = ?",
            (existing[0],),
        )

    success = restore_subscription(merchant=decoded_merchant, profile=profile)
    if not success:
        raise HTTPException(status_code=404, detail="Merchant not found in dismissed list.")
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile_id,
        reason="recurring_subscription_restored",
    )
    return {"status": "ok", "message": "Subscription restored"}


@app.get("/api/subscriptions/dismissed")
def list_dismissed_subscriptions(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    """Return all dismissed subscription items for a profile."""
    from data_manager import get_dismissed_subscriptions
    items = get_dismissed_subscriptions(profile=profile, conn=db)
    return {"items": items}


@app.get("/api/subscriptions/events")
def list_subscription_events(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    """Return subscription events (alerts) for a profile."""
    from data_manager import get_subscription_events
    return get_subscription_events(profile=profile, conn=db)


class MarkEventsRead(BaseModel):
    event_ids: list[int]


@app.post("/api/subscriptions/events/mark-read")
def mark_events_read_endpoint(body: MarkEventsRead):
    """Mark subscription events as read."""
    from data_manager import mark_events_read
    if not body.event_ids:
        raise HTTPException(status_code=400, detail="event_ids required.")
    count = mark_events_read(body.event_ids)
    return {"status": "ok", "updated": count}


@app.post("/api/subscriptions/redetect")
def redetect_subscriptions(profile: str | None = Depends(validate_profile)):
    """
    Trigger a full re-detection of recurring subscriptions.
    Scans all transactions and updates the merchants table.
    """
    from data_manager import trigger_full_redetection
    try:
        result = trigger_full_redetection(profile=profile)
        status = result.get("status") or "ok"
        if status == "already_running":
            return {
                "status": "already_running",
                "items_detected": 0,
                "events_generated": 0,
            }
        with get_db() as conn:
            _mark_mira_money_outlook_stale_after_write(
                conn=conn,
                profile=profile,
                all_profiles=profile is None,
                reason="recurring_redetected",
            )
        return {
            "status": "ok",
            "items_detected": len(result.get("items", [])),
            "events_generated": len(result.get("events", [])),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync")
def sync(background_tasks: BackgroundTasks, profile: str | None = Query(None)):
    _require_live_mode("Manual sync is disabled in demo mode.")
    # Currently syncs all profiles. Profile param reserved for future selective sync.
    job_id = start_sync("manual-sync", phase="starting", detail="Starting manual sync")
    try:
        data = fetch_fresh_data(sync_job_id=job_id)
        finish_sync(job_id, status="completed")
        _invalidate_copilot_cache()
        background_refresh = _maybe_queue_mira_background_refresh(
            background_tasks=background_tasks,
            profile=None,
            reason="manual_sync_completed",
        )
        with get_db() as conn:
            money_outlook_refresh = _maybe_queue_mira_money_outlook_refresh(
                background_tasks=background_tasks,
                conn=conn,
                profile=None,
                reason="manual_sync_completed",
            )
        return {
            "status": "synced",
            "accounts": len(data["accounts"]),
            "transactions": len(data["transactions"]),
            "last_updated": data["last_updated"],
            "background_analyst_refresh": background_refresh,
            "money_outlook_refresh": money_outlook_refresh,
        }
    except Exception as exc:
        finish_sync(job_id, status="failed", error=str(exc))
        raise


@app.get("/api/sync-status")
def sync_status():
    return get_sync_status()


@app.get("/api/data-health")
def data_health(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_data_health_summary(profile=profile, conn=db)


@app.get("/api/scheduled-transactions")
def scheduled_transactions(
    days: int = Query(45, ge=1, le=180),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    return get_scheduled_transactions_data(days=days, profile=profile, conn=db)


@app.post("/api/analytics/explain-month")
def explain_month(
    body: MonthExplanationRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    try:
        return create_month_explanation(body.month, profile=profile, use_llm=body.use_llm, save=body.save, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/analytics/cash-flow-forecast")
def cash_flow_forecast(
    days: int = Query(90, ge=7, le=180),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    return get_cash_flow_forecast_data(days=days, profile=profile, conn=db)


class CopilotConfirm(BaseModel):
    """Client sends back the confirmation_id, NOT raw SQL."""
    question: str
    confirmation_id: str


@app.post("/api/copilot/ask")
async def copilot_ask(body: CopilotRequest, profile: str | None = Query(None)):
    """Compatibility wrapper for non-streaming clients.

    The product UI uses /api/copilot/ask/stream. Keep this path aligned with
    the same dispatcher/runtime so old callers do not hit retired routers.
    """
    from copilot import ask_copilot
    validated_profile = validate_profile(profile)
    result = ask_copilot(question=body.question, profile=validated_profile, history=body.history)
    return result


@app.post("/api/copilot/ask/stream")
async def copilot_ask_stream(body: CopilotRequest, profile: str | None = Query(None)):
    """Streaming variant of /api/copilot/ask — emits Server-Sent Events so the
    UI can render tool progress and the agent's final answer incrementally."""
    import json as _json
    from copilot_agent import run_agent_stream

    validated_profile = validate_profile(profile)

    def event_stream():
        final_event: dict | None = None
        try:
            for event in run_agent_stream(
                question=body.question,
                profile=validated_profile,
                history=body.history,
            ):
                if event.get("type") == "done":
                    final_event = event
                yield f"data: {_json.dumps(event, default=str)}\n\n"
            if final_event:
                try:
                    tool_trace = final_event.get("tool_trace") or []
                    pending_write = final_event.get("pending_write") or {}
                    route = final_event.get("route") or {}
                    route_intent = route.get("intent") or final_event.get("intent")
                    operation = "write_preview" if pending_write else (route_intent or "read")
                    generated_sql = ""
                    rows_affected = final_event.get("rows_affected")
                    if rows_affected is None:
                        rows_affected = len(final_event.get("data") or []) if isinstance(final_event.get("data"), list) else 0
                    record = prepare_copilot_history_record(
                        profile=validated_profile,
                        question=body.question,
                        generated_sql=generated_sql,
                        result=_json.dumps({"route": route, "tool_trace": tool_trace}, default=str),
                        answer=final_event.get("answer") or "",
                        operation=operation,
                        rows_affected=rows_affected,
                        route=route,
                    )
                    log_copilot_conversation(**record)
                    prune_copilot_conversations(profile=validated_profile)
                    try:
                        from mira import memory_v2

                        memory_v2.schedule_session_summary_after_idle(
                            profile=validated_profile,
                            history=body.history,
                            latest_question=body.question,
                            latest_answer=final_event.get("answer") or "",
                        )
                    except Exception:
                        logger.exception("Failed to schedule Mira session summary")
                except Exception:
                    logger.exception("Failed to persist Copilot conversation history")
        except Exception as e:
            yield f"data: {_json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/copilot/confirm")
async def copilot_confirm(body: CopilotConfirm, profile: str | None = Query(None), db=Depends(get_db_session)):
    """
    Confirm and execute a write operation previewed by the copilot.
    Client sends a confirmation_id that references a server-stored structured operation.
    The client can no longer supply arbitrary SQL, and Mira no longer stores SQL for previews.
    """
    from data_manager import execute_pending_write_operation
    from pending_operations import pending_error_message, retrieve_pending_operation

    validated_profile = validate_profile(profile)
    pending, code = retrieve_pending_operation(body.confirmation_id, validated_profile, conn=db)
    if pending is None:
        status = 410 if code in {"confirmation_expired", "confirmation_consumed"} else 404
        raise HTTPException(
            status_code=status,
            detail={"code": code or "confirmation_not_found", "message": pending_error_message(code)},
        )

    try:
        result = execute_pending_write_operation(
            pending["operation"],
            pending.get("params") or {},
            validated_profile,
            conn=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "write_rejected", "message": str(exc)})
    from copilot import _log_conversation
    _log_conversation(
        validated_profile,
        body.question,
        "",
        json.dumps({"operation": pending["operation"], "params": pending.get("params") or {}}, default=str),
        result.get("answer") or "",
        "write_executed",
        int(result.get("rows_affected") or 0),
    )
    _invalidate_copilot_cache()
    return result


@app.get("/api/copilot/history")
def copilot_history(
    profile: str | None = Depends(validate_profile),
    limit: int = Query(40, ge=1, le=200),
    db=Depends(get_db_session),
):
    return {"items": get_copilot_conversations(limit=limit, profile=profile, conn=db)}


@app.delete("/api/copilot/history")
def clear_copilot_history(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    deleted = clear_copilot_conversations(profile=profile, conn=db)
    return {"cleared": deleted}


@app.delete("/api/copilot/history/{conversation_id}")
def delete_copilot_history_item(
    conversation_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    deleted = delete_copilot_conversation(conversation_id, profile=profile, conn=db)
    if not deleted:
        raise HTTPException(status_code=404, detail="Copilot history item not found.")
    return {"deleted": deleted}


@app.post("/api/copilot/insights")
def save_insight(
    body: SaveInsightRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Save to memory: extract a durable takeaway from the Q&A pair via LLM and append
    to the persistent memory file as a 'saved' entry. Returns the new entry, or
    {saved: false, reason} if nothing memorable was found.
    """
    import memory as _mem

    question = body.question.strip()
    answer = body.answer.strip()
    if not question or not answer:
        raise HTTPException(status_code=400, detail="question and answer are required")

    takeaway = _mem.extract_takeaway(question, answer)
    if not takeaway:
        return {
            "saved": False,
            "reason": "No durable takeaway detected — this turn was a lookup or routine answer.",
        }

    new_id = _mem.insert_entry(
        profile=profile,
        section=takeaway["section"],
        body=takeaway["body"],
        confidence="saved",
        evidence=takeaway.get("evidence", ""),
        conn=db,
    )
    db.commit()
    row = db.execute(
        "SELECT id, profile_id, section, body, confidence, evidence, theme, created_at "
        "FROM memory_entries WHERE id = ?",
        (new_id,),
    ).fetchone()
    _invalidate_copilot_cache()
    return {"saved": True, "entry": dict(row)}


@app.get("/api/copilot/insights")
def list_insights(
    profile: str | None = Depends(validate_profile),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(get_db_session),
):
    rows = db.execute(
        """
        SELECT id, profile_id, question, answer, kind, pinned, source_conversation_id, created_at
        FROM saved_insights
        WHERE (? IS NULL OR profile_id = ?)
        ORDER BY pinned DESC, created_at DESC
        LIMIT ?
        """,
        (profile, profile, limit),
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENT MEMORY (about_user.md)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/memory/entries")
def memory_list_entries(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    return {"items": _mem.list_active_entries(profile, db), "sections": [
        {"key": k, "label": label} for k, label in _mem.SECTIONS
    ]}


@app.post("/api/memory/entries")
def memory_create_entry(
    body: MemoryEntryCreate,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    try:
        new_id = _mem.insert_entry(
            profile=profile,
            section=body.section,
            body=body.body,
            confidence=body.confidence,
            evidence=body.evidence,
            conn=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    row = db.execute(
        "SELECT id, profile_id, section, body, confidence, evidence, theme, created_at "
        "FROM memory_entries WHERE id = ?",
        (new_id,),
    ).fetchone()
    _invalidate_copilot_cache()
    return dict(row)


@app.patch("/api/memory/entries/{entry_id}")
def memory_update_entry(
    entry_id: int,
    body: MemoryEntryUpdate,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    try:
        new_id = _mem.supersede_entry(
            old_id=entry_id,
            profile=profile,
            new_body=body.body,
            new_evidence=body.evidence or "",
            conn=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.commit()
    row = db.execute(
        "SELECT id, profile_id, section, body, confidence, evidence, theme, created_at "
        "FROM memory_entries WHERE id = ?",
        (new_id,),
    ).fetchone()
    _invalidate_copilot_cache()
    return dict(row)


@app.delete("/api/memory/entries/{entry_id}")
def memory_delete_entry(
    entry_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    removed = _mem.delete_entry(entry_id=entry_id, profile=profile, conn=db)
    if not removed:
        raise HTTPException(status_code=404, detail="entry not found")
    db.commit()
    _invalidate_copilot_cache()
    return {"deleted": True, "id": entry_id}


@app.get("/api/memory/markdown")
def memory_markdown(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """Return the rendered about_user.md text plus a token-budget estimate."""
    import memory as _mem
    text = _mem.render_markdown(profile, db)
    char_count = len(text)
    # Crude token estimate: ~4 chars per token. Good enough for a budget gauge.
    token_estimate = max(1, char_count // 4) if text else 0
    return {
        "markdown": text,
        "token_estimate": token_estimate,
        "char_count": char_count,
        "budget": 4000,
    }


@app.get("/api/memory/proposals")
def memory_list_proposals(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    return {"items": _mem.list_pending_proposals(profile, db)}


@app.post("/api/memory/proposals/{proposal_id}/accept")
def memory_accept_proposal(
    proposal_id: int,
    body: MemoryProposalAccept | None = None,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    try:
        new_id = _mem.accept_proposal(
            proposal_id=proposal_id,
            profile=profile,
            conn=db,
            body_override=(body.body if body else None),
            section_override=(body.section if body else None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    row = db.execute(
        "SELECT id, profile_id, section, body, confidence, evidence, theme, created_at "
        "FROM memory_entries WHERE id = ?",
        (new_id,),
    ).fetchone()
    _invalidate_copilot_cache()
    return {"accepted": True, "entry": dict(row)}


@app.post("/api/memory/proposals/{proposal_id}/reject")
def memory_reject_proposal(
    proposal_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    rejected = _mem.reject_proposal(proposal_id=proposal_id, conn=db)
    if not rejected:
        raise HTTPException(status_code=404, detail="proposal not found or already resolved")
    db.commit()
    _invalidate_copilot_cache()
    return {"rejected": True, "id": proposal_id}


@app.post("/api/memory/consolidate")
def memory_consolidate(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """On-demand lint pass — proposes supersedes/merges/removals as proposals the user reviews."""
    import memory as _mem
    proposals = _mem.run_consolidation(profile=profile, conn=db)
    db.commit()
    _invalidate_copilot_cache()
    return {"proposals_created": len(proposals), "items": proposals}


# ══════════════════════════════════════════════════════════════════════════════
# MIRA MEMORY V2
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/mira/memories")
def mira_memory_list(
    profile: str | None = Depends(validate_profile),
    include_inactive: bool = Query(False),
    include_expired: bool = Query(False),
    memory_type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db=Depends(get_db_session),
):
    from mira import memory_v2

    return {
        "items": memory_v2.list_memories(
            db,
            profile,
            include_inactive=include_inactive,
            include_expired=include_expired,
            memory_type=memory_type,
            limit=limit,
        )
    }


@app.post("/api/mira/memories")
def mira_memory_create(
    body: MiraMemoryCreate,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira import memory_v2

    try:
        result = memory_v2.remember_user_context(
            conn=db,
            profile=profile,
            text=body.text,
            memory_type=body.memory_type,
            topic=body.topic,
            source_summary=body.source_summary,
            source_turn_id=body.source_turn_id,
            pinned=body.pinned,
            expires_at=body.expires_at,
            consent="explicit",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result.get("saved"):
        db.commit()
        _invalidate_copilot_cache()
    return result


@app.patch("/api/mira/memories/{memory_id}")
def mira_memory_update(
    memory_id: int,
    body: MiraMemoryUpdate,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira import memory_v2

    try:
        updated = memory_v2.update_memory(
            conn=db,
            profile=profile,
            memory_id=memory_id,
            normalized_text=body.normalized_text,
            memory_type=body.memory_type,
            topic=body.topic,
            sensitivity=body.sensitivity,
            confidence=body.confidence,
            pinned=body.pinned,
            expires_at=body.expires_at,
            status=body.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="memory not found")
    db.commit()
    _invalidate_copilot_cache()
    return updated


@app.delete("/api/mira/memories/{memory_id}")
def mira_memory_delete(
    memory_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira import memory_v2

    result = memory_v2.forget_memory(conn=db, profile=profile, memory_id=memory_id)
    if not result.get("forgot"):
        raise HTTPException(status_code=404, detail=result.get("reason") or "memory not found")
    db.commit()
    _invalidate_copilot_cache()
    return {"deleted": True, "id": memory_id}


@app.get("/api/mira/stated-intents")
def mira_stated_intents_endpoint(
    subject_type: str | None = Query(None),
    subject_key: str | None = Query(None),
    include_inactive: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.stated_intents import list_stated_intents, stated_intent_memory_enabled

    if not stated_intent_memory_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "items": [],
            "summary": {"count": 0},
        }
    items = list_stated_intents(
        conn=db,
        profile=profile,
        subject_type=subject_type,
        subject_key=subject_key,
        include_inactive=include_inactive,
        limit=100,
    )
    return {
        "enabled": True,
        "status": "ok",
        "items": items,
        "summary": {"count": len(items)},
    }


@app.post("/api/mira/stated-intents")
def mira_stated_intent_create_endpoint(
    body: MiraStatedIntentCreateRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.stated_intents import create_stated_intent, stated_intent_memory_enabled

    if not stated_intent_memory_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "stated_intent_memory_disabled",
        }
    try:
        intent = create_stated_intent(
            conn=db,
            profile=profile,
            subject_type=body.subject_type,
            subject_key=body.subject_key,
            subject_label=body.subject_label,
            intent_kind=body.intent_kind,
            baseline_scope=body.baseline_scope or "mtd_vs_prior_3_full_months",
            target_text=body.target_text,
            feedback_state=body.feedback_state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, "status": "stored", "intent": intent}


@app.patch("/api/mira/stated-intents/{intent_id}")
def mira_stated_intent_update_endpoint(
    intent_id: int,
    body: MiraStatedIntentUpdateRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.stated_intents import stated_intent_memory_enabled, update_stated_intent

    if not stated_intent_memory_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "stated_intent_memory_disabled",
        }
    try:
        intent = update_stated_intent(
            conn=db,
            profile=profile,
            intent_id=intent_id,
            target_text=body.target_text,
            status=body.status,
            feedback_state=body.feedback_state,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not intent:
        raise HTTPException(status_code=404, detail="Stated intent not found.")
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, "status": "updated", "intent": intent}


@app.delete("/api/mira/stated-intents/{intent_id}")
def mira_stated_intent_clear_endpoint(
    intent_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.stated_intents import clear_stated_intent, stated_intent_memory_enabled

    if not stated_intent_memory_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "stated_intent_memory_disabled",
        }
    intent = clear_stated_intent(conn=db, profile=profile, intent_id=intent_id)
    if not intent:
        raise HTTPException(status_code=404, detail="Stated intent not found.")
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, "status": "dismissed", "intent": intent}


@app.post("/api/mira/stated-intents/{intent_id}/evaluate")
def mira_stated_intent_evaluate_endpoint(
    intent_id: int,
    as_of: str | None = Query(None),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.stated_intents import evaluate_stated_intent, stated_intent_memory_enabled

    if not stated_intent_memory_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "stated_intent_memory_disabled",
        }
    intent = evaluate_stated_intent(conn=db, profile=profile, intent_id=intent_id, as_of=as_of)
    if not intent:
        raise HTTPException(status_code=404, detail="Stated intent not found.")
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, "status": "evaluated", "intent": intent}


@app.get("/api/mira/habit-streaks")
def mira_habit_streaks_endpoint(
    subject_type: str | None = Query(None),
    subject_key: str | None = Query(None),
    include_inactive: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.habit_streaks import habit_streaks_enabled, list_habit_streaks

    if not habit_streaks_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "items": [],
            "summary": {"count": 0},
        }
    items = list_habit_streaks(
        conn=db,
        profile=profile,
        subject_type=subject_type,
        subject_key=subject_key,
        include_inactive=include_inactive,
        limit=100,
    )
    return {
        "enabled": True,
        "status": "ok",
        "items": items,
        "summary": {"count": len(items)},
    }


@app.post("/api/mira/habit-streaks/generate")
def mira_habit_streaks_generate_endpoint(
    as_of: str | None = Query(None),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.habit_streaks import generate_habit_streaks, habit_streaks_enabled

    if not habit_streaks_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "habit_streaks_disabled",
            "items": [],
        }
    result = generate_habit_streaks(conn=db, profile=profile, as_of=as_of)
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, **result}


@app.delete("/api/mira/habit-streaks/{streak_id}")
def mira_habit_streak_dismiss_endpoint(
    streak_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.habit_streaks import dismiss_habit_streak, habit_streaks_enabled

    if not habit_streaks_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "habit_streaks_disabled",
        }
    streak = dismiss_habit_streak(conn=db, profile=profile, streak_id=streak_id)
    if not streak:
        raise HTTPException(status_code=404, detail="Habit streak not found.")
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, "status": "dismissed", "streak": streak}


@app.get("/api/mira/monthly-retrospectives")
def mira_monthly_retrospectives_endpoint(
    include_inactive: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.monthly_retrospectives import list_monthly_retrospectives, monthly_retrospective_enabled

    if not monthly_retrospective_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "items": [],
            "summary": {"count": 0},
        }
    items = list_monthly_retrospectives(
        conn=db,
        profile=profile,
        include_inactive=include_inactive,
        limit=24,
    )
    return {
        "enabled": True,
        "status": "ok",
        "items": items,
        "summary": {"count": len(items)},
    }


@app.post("/api/mira/monthly-retrospectives/generate")
def mira_monthly_retrospective_generate_endpoint(
    month_key: str | None = Query(None),
    as_of: str | None = Query(None),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.monthly_retrospectives import generate_monthly_retrospective, monthly_retrospective_enabled

    if not monthly_retrospective_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "monthly_retrospective_disabled",
            "item": None,
        }
    try:
        result = generate_monthly_retrospective(conn=db, profile=profile, month_key=month_key, as_of=as_of)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, **result}


@app.delete("/api/mira/monthly-retrospectives/{retrospective_id}")
def mira_monthly_retrospective_dismiss_endpoint(
    retrospective_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.monthly_retrospectives import dismiss_monthly_retrospective, monthly_retrospective_enabled

    if not monthly_retrospective_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "monthly_retrospective_disabled",
        }
    item = dismiss_monthly_retrospective(conn=db, profile=profile, retrospective_id=retrospective_id)
    if not item:
        raise HTTPException(status_code=404, detail="Monthly retrospective not found.")
    db.commit()
    _invalidate_copilot_cache()
    return {"enabled": True, "status": "dismissed", "item": item}


@app.get("/api/mira/session-summaries")
def mira_session_summary_list(
    profile: str | None = Depends(validate_profile),
    include_inactive: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db=Depends(get_db_session),
):
    from mira import memory_v2

    return {
        "items": memory_v2.list_session_summaries(
            db,
            profile,
            include_inactive=include_inactive,
            limit=limit,
        )
    }


@app.patch("/api/mira/session-summaries/{summary_id}")
def mira_session_summary_update(
    summary_id: int,
    body: MiraSessionSummaryUpdate,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira import memory_v2

    try:
        updated = memory_v2.update_session_summary(
            conn=db,
            profile=profile,
            summary_id=summary_id,
            summary_text=body.summary_text,
            status=body.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="session summary not found")
    db.commit()
    _invalidate_copilot_cache()
    return updated


@app.delete("/api/mira/session-summaries/{summary_id}")
def mira_session_summary_delete(
    summary_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira import memory_v2

    if not memory_v2.delete_session_summary(conn=db, profile=profile, summary_id=summary_id):
        raise HTTPException(status_code=404, detail="session summary not found")
    db.commit()
    _invalidate_copilot_cache()
    return {"deleted": True, "id": summary_id}


@app.get("/api/copilot/explain-category")
def copilot_explain_category(
    merchant: str = Query(..., description="Merchant name or description fragment"),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Deterministic tool: explain why a merchant is categorized the way it is.
    Backed by real transaction + rule data — no LLM.
    """
    data = explain_category_assignment(merchant_query=merchant, profile=profile, conn=db)
    count = data["transaction_count"]
    dominant_cat = data["dominant_category"] or "an unknown category"
    dominant_src = data["dominant_source"] or "unknown"
    pattern = data["normalized_pattern"]
    rule = data["rule"]

    source_label = {
        "user": "a manual override",
        "user-rule": "a user-defined rule",
        "llm": "AI categorization",
        "rule": "a built-in rule",
        "fallback": "the fallback default",
        "teller": "the bank's own category",
        "enricher": "merchant enrichment",
        "merchant-memory": "merchant memory",
    }.get(dominant_src, dominant_src)

    if count == 0:
        answer = f'No transactions found matching "{merchant}" (normalized: {pattern}).'
    else:
        rule_detail = ""
        if rule:
            rule_detail = (
                f" A {'user' if rule['source'] == 'user' else 'built-in'} rule exists "
                f"for pattern **{rule['pattern']}** (priority {rule['priority']})."
            )
        answer = (
            f'**{merchant}** is categorized as **{dominant_cat}** '
            f"across {count} transaction{'s' if count != 1 else ''}. "
            f"Assigned by {source_label}.{rule_detail}"
        )

    return {
        "answer": answer,
        "operation": "read",
        "distribution": data["distribution"],
        "samples": data["samples"],
        "rule": rule,
        "transaction_count": count,
    }


@app.get("/api/copilot/merchants-missing-category")
def copilot_merchants_missing_category(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Deterministic tool: find merchant patterns with uncategorized transactions.
    """
    items = find_merchants_missing_category(profile=profile, conn=db)
    total_tx = sum(item["transaction_count"] for item in items)
    if not items:
        answer = "No uncategorized transactions found. Your data looks clean!"
    else:
        patterns = ", ".join(item["pattern"] for item in items[:5])
        more = f" and {len(items) - 5} more" if len(items) > 5 else ""
        answer = (
            f"Found **{len(items)}** merchant pattern{'s' if len(items) != 1 else ''} "
            f"with {total_tx} uncategorized transaction{'s' if total_tx != 1 else ''}: "
            f"{patterns}{more}."
        )
    return {"answer": answer, "operation": "read", "items": items}


class BulkRecategorizePreviewRequest(BaseModel):
    merchant_query: str
    new_category: str


@app.post("/api/copilot/bulk-recategorize-preview")
def copilot_bulk_recategorize_preview(
    body: BulkRecategorizePreviewRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Deterministic tool: preview moving all transactions for a merchant to a new category.
    Returns a confirmation_id so the existing /copilot/confirm route can execute it.
    """
    from pending_operations import store_pending_operation

    data = bulk_recategorize_preview(
        merchant_query=body.merchant_query,
        new_category=body.new_category,
        profile=profile,
        conn=db,
    )
    count = data["count"]

    if count == 0:
        return {
            "answer": (
                f'No transactions found for "{body.merchant_query}" that aren\'t already '
                f'categorized as **{body.new_category}**.'
            ),
            "operation": "read",
            "count": 0,
            "preview_changes": [],
            "needs_confirmation": False,
        }

    pending = data["pending_operation"]
    confirmation_id = store_pending_operation(
        pending["operation"],
        pending["params"],
        profile,
        {"rows_affected": count, "samples": data["samples"]},
        conn=db,
    )

    preview_changes = [
        {"column": "category", "raw_value": body.new_category, "new_value": body.new_category}
    ]

    answer = (
        f"Found **{count}** {body.merchant_query} transaction{'s' if count != 1 else ''} "
        f"to move to **{body.new_category}**. Confirm to apply."
    )

    return {
        "answer": answer,
        "operation": "write_preview",
        "count": count,
        "samples": data["samples"],
        "preview_changes": preview_changes,
        "confirmation_id": confirmation_id,
        "needs_confirmation": True,
        "rows_affected": count,
    }


class PreviewRuleRequest(BaseModel):
    pattern: str
    category: str


@app.post("/api/copilot/preview-rule")
def copilot_preview_rule(
    body: PreviewRuleRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Deterministic tool: preview creating a new user category rule.
    Returns a confirmation_id so the existing /copilot/confirm route can execute the INSERT.
    """
    from pending_operations import store_pending_operation

    data = preview_rule_creation(
        raw_pattern=body.pattern,
        category=body.category,
        profile=profile,
        conn=db,
    )
    count = data["count"]
    existing = data["existing_rule"]
    pattern = data["pattern"]

    pending = data["pending_operation"]
    confirmation_id = store_pending_operation(
        pending["operation"],
        pending["params"],
        profile,
        {"rows_affected": count, "samples": data["samples"]},
        conn=db,
    )

    preview_changes = [
        {"column": "rule", "raw_value": f"{pattern} → {body.category}", "new_value": body.category}
    ]

    if existing:
        existing_note = (
            f" Note: a rule for **{pattern}** already exists "
            f"(currently → {existing['category']}) — this will replace it."
        )
    else:
        existing_note = ""

    answer = (
        f"Creating rule **{pattern}** → **{body.category}** will apply to "
        f"**{count}** existing transaction{'s' if count != 1 else ''} "
        f"and all future matches.{existing_note} Confirm to create."
    )

    return {
        "answer": answer,
        "operation": "write_preview",
        "count": count,
        "samples": data["samples"],
        "preview_changes": preview_changes,
        "confirmation_id": confirmation_id,
        "needs_confirmation": True,
        "rows_affected": count,
        "existing_rule": existing,
    }


class RenameMerchantRequest(BaseModel):
    old_name: str
    new_name: str


@app.post("/api/copilot/rename-merchant-preview")
def copilot_rename_merchant_preview(
    body: RenameMerchantRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Deterministic tool: preview renaming a merchant across all matching transactions.
    Returns a confirmation_id so the existing /copilot/confirm route can execute both UPDATEs.
    """
    from pending_operations import store_pending_operation

    data = rename_merchant_variants(
        old_pattern=body.old_name,
        new_name=body.new_name,
        profile=profile,
        conn=db,
    )
    count = data["count"]

    if count == 0:
        return {
            "answer": f'No transactions found matching "{body.old_name}".',
            "operation": "read",
            "count": 0,
            "preview_changes": [],
            "needs_confirmation": False,
        }

    pending = data["pending_operation"]
    confirmation_id = store_pending_operation(
        pending["operation"],
        pending["params"],
        profile,
        {"rows_affected": count, "samples": data["samples"]},
        conn=db,
    )
    preview_changes = [
        {"column": "merchant_name", "raw_value": body.new_name, "new_value": body.new_name}
    ]

    return {
        "answer": (
            f"Found **{count}** transaction{'s' if count != 1 else ''} for "
            f"**{body.old_name}** to rename to **{body.new_name}**. Confirm to apply."
        ),
        "operation": "write_preview",
        "count": count,
        "samples": data["samples"],
        "preview_changes": preview_changes,
        "confirmation_id": confirmation_id,
        "needs_confirmation": True,
        "rows_affected": count,
    }


@app.get("/api/copilot/data-browser")
def copilot_data_browser(
    table: str = Query(..., description="Safe allowlisted table name"),
    search: str | None = Query(None),
    limit: int = Query(100, ge=1, le=250),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    try:
        rows = get_data_browser_rows(table=table, profile=profile, search=search, limit=limit, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"table": table, "items": rows}


@app.get("/api/budgets")
def budgets(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return {"items": get_category_budgets(profile=profile, conn=db)}


class BudgetUpdate(BaseModel):
    amount: float | None = None
    rollover_mode: str | None = None
    rollover_balance: float | None = None


@app.patch("/api/budgets/{category_name}")
def update_budget_endpoint(
    category_name: str,
    body: BudgetUpdate,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    try:
        result = update_category_budget(
            category=category_name,
            amount=body.amount,
            profile=profile,
            conn=db,
            rollover_mode=body.rollover_mode,
            rollover_balance=body.rollover_balance,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile,
        reason="budget_changed",
    )
    return {"status": "updated", "budget": result}


class GoalPayload(BaseModel):
    id: int | None = None
    name: str
    goal_type: str = "custom"
    target_amount: float = 0
    current_amount: float = 0
    target_date: str | None = None
    linked_category: str | None = None
    linked_account_id: str | None = None


@app.get("/api/goals")
def goals(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return {"items": get_goals(profile=profile, conn=db)}


@app.post("/api/goals")
def create_goal(body: GoalPayload, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    try:
        goal = upsert_goal(body.model_dump(), profile=profile, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile,
        reason="goal_created",
    )
    return {"status": "created", "goal": goal}


@app.patch("/api/goals/{goal_id}")
def update_goal(goal_id: int, body: GoalPayload, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    payload = body.model_dump()
    payload["id"] = goal_id
    try:
        goal = upsert_goal(payload, profile=profile, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile,
        reason="goal_changed",
    )
    return {"status": "updated", "goal": goal}


@app.delete("/api/goals/{goal_id}")
def remove_goal(goal_id: int, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    if not delete_goal(goal_id, profile=profile, conn=db):
        raise HTTPException(status_code=404, detail="Goal not found.")
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        profile=profile,
        reason="goal_deleted",
    )
    return {"status": "deleted"}


class TransactionMetadataUpdate(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None
    reviewed: bool | None = None


@app.patch("/api/transactions/{tx_id}/metadata")
def update_transaction_metadata_endpoint(tx_id: str, body: TransactionMetadataUpdate, db=Depends(get_db_session)):
    result = update_transaction_metadata(
        tx_id,
        notes=body.notes,
        tags=body.tags,
        reviewed=body.reviewed,
        conn=db,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    return {"status": "updated", "transaction": result}


class TransactionSplitItem(BaseModel):
    category: str
    amount: float
    notes: str | None = ""
    tags: list[str] | None = None


class TransactionSplitsUpdate(BaseModel):
    splits: list[TransactionSplitItem]


@app.get("/api/transactions/{tx_id}/splits")
def transaction_splits(tx_id: str, db=Depends(get_db_session)):
    return {"items": get_transaction_splits(tx_id, conn=db)}


@app.patch("/api/transactions/{tx_id}/splits")
def update_transaction_splits_endpoint(tx_id: str, body: TransactionSplitsUpdate, db=Depends(get_db_session)):
    result = replace_transaction_splits(
        tx_id,
        [item.model_dump() for item in body.splits],
        conn=db,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    _mark_mira_money_outlook_stale_after_write(
        conn=db,
        tx_id=tx_id,
        reason="transaction_splits_changed",
    )
    return {"status": "updated", **result}


class ManualAccountPayload(BaseModel):
    name: str
    account_type: str = "depository"
    account_subtype: str = "manual"
    balance: float = 0
    notes: str | None = ""


class InvestmentHoldingPayload(BaseModel):
    account_id: str | None = None
    symbol: str | None = ""
    name: str
    asset_class: str = "stock"
    quantity: float = 0
    cost_basis: float = 0
    current_price: float = 0
    manual_value: float | None = None
    target_percent: float | None = None
    notes: str | None = ""
    price_as_of: str | None = None


@app.post("/api/manual-accounts")
def create_manual_account_endpoint(body: ManualAccountPayload, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    try:
        account = create_manual_account(body.model_dump(), profile=profile, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "created", "account": account}


@app.patch("/api/manual-accounts/{account_id}")
def update_manual_account_endpoint(account_id: str, body: ManualAccountPayload, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    try:
        account = update_manual_account(account_id, body.model_dump(), profile=profile, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not account:
        raise HTTPException(status_code=404, detail="Manual account not found.")
    return {"status": "updated", "account": account}


@app.delete("/api/manual-accounts/{account_id}")
def delete_manual_account_endpoint(account_id: str, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    if not deactivate_manual_account(account_id, profile=profile, conn=db):
        raise HTTPException(status_code=404, detail="Manual account not found.")
    return {"status": "deleted"}


@app.get("/api/investments")
def investments_endpoint(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_investments_summary_data(profile=profile, conn=db)


@app.post("/api/investments/holdings")
def create_investment_holding_endpoint(body: InvestmentHoldingPayload, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    try:
        holding = upsert_investment_holding(body.model_dump(), profile=profile, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "created", "holding": holding}


@app.patch("/api/investments/holdings/{holding_id}")
def update_investment_holding_endpoint(holding_id: int, body: InvestmentHoldingPayload, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    try:
        holding = upsert_investment_holding(body.model_dump(), holding_id=holding_id, profile=profile, conn=db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not holding:
        raise HTTPException(status_code=404, detail="Holding not found.")
    return {"status": "updated", "holding": holding}


@app.delete("/api/investments/holdings/{holding_id}")
def delete_investment_holding_endpoint(holding_id: int, profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    if not delete_investment_holding(holding_id, profile=profile, conn=db):
        raise HTTPException(status_code=404, detail="Holding not found.")
    return {"status": "deleted"}


@app.get("/api/backup/status")
def backup_status_endpoint(profile: str | None = Depends(validate_profile), db=Depends(get_db_session)):
    return get_backup_status_data(profile=profile, conn=db)


@app.get("/api/backup/export")
def backup_export_endpoint(
    include_credentials: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    data = create_backup_export_data(profile=profile, include_credentials=include_credentials, conn=db)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"folio-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/merchant-directory")
def merchant_directory(
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=250),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    items = get_merchant_directory(profile=profile, search=search, limit=limit, conn=db)
    return {"items": items}


@app.get("/api/merchant-directory/{merchant_key}/transactions")
def merchant_directory_transactions(
    merchant_key: str,
    profile_id: str | None = Query(None),
    profile: str | None = Depends(validate_profile),
    limit: int = Query(25, ge=1, le=100),
    db=Depends(get_db_session),
):
    effective_profile = profile_id or (profile if profile and profile != "household" else None)
    items = get_transactions_for_merchant(
        merchant_key=merchant_key,
        profile_id=effective_profile,
        limit=limit,
        conn=db,
    )
    return {"items": items}


class MerchantDirectoryUpdate(BaseModel):
    profile_id: str
    clean_name: str | None = None
    category: str | None = None
    domain: str | None = None
    industry: str | None = None


@app.patch("/api/merchant-directory/{merchant_key}")
def update_merchant_directory_endpoint(
    merchant_key: str,
    body: MerchantDirectoryUpdate,
    db=Depends(get_db_session),
):
    try:
        result = update_merchant_directory_entry(
            merchant_key=merchant_key,
            profile_id=body.profile_id,
            clean_name=body.clean_name,
            category=body.category,
            domain=body.domain,
            industry=body.industry,
            conn=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result:
        raise HTTPException(status_code=404, detail="Merchant not found.")

    return {"status": "updated", "merchant": result}


@app.get("/api/dashboard-bundle")
def dashboard_bundle(
    nw_interval: str = Query("biweekly", description="weekly or biweekly"),
    as_of: str | None = Query(None, description="Local YYYY-MM-DD date used for dashboard planning metrics"),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Single-request dashboard loader.
    Returns summary, accounts, monthly analytics, category analytics,
    and net-worth time series — all using SQL-level aggregation.
    Replaces 5 separate API calls.
    """
    def _load_bundle():
        bundle = get_dashboard_bundle_data(nw_interval=nw_interval, profile=profile, conn=db, as_of=as_of)
        return {**bundle, "config": _app_config_payload(db)}

    try:
        import copilot_cache

        fingerprint = copilot_cache.db_fingerprint(db, profile)
        return copilot_cache.get_or_set(
            "dashboard_bundle",
            copilot_cache.make_key(nw_interval, profile or "household", as_of or date.today().isoformat(), fingerprint),
            _load_bundle,
        )
    except Exception:
        return _load_bundle()


@app.get("/api/proactive-insights")
def proactive_insights_endpoint(
    background_tasks: BackgroundTasks,
    include_dismissed: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from proactive_insights import list_insights

    items = list_insights(profile=profile, include_dismissed=include_dismissed, conn=db, generate=True)
    auto_refresh = _maybe_queue_mira_background_refresh(
        background_tasks=background_tasks,
        profile=profile,
        conn=db,
        reason="proactive_insights_open",
    )
    return {"items": items, "auto_refresh": auto_refresh, "job": _mira_background_job_snapshot(profile)}


def _mira_background_job_key(profile: str | None) -> str:
    return profile if profile and profile != "household" else "household"


def _mira_background_job_snapshot(profile: str | None) -> dict:
    key = _mira_background_job_key(profile)
    with _mira_background_job_lock:
        return dict(_mira_background_jobs.get(key) or {"status": "idle"})


def _set_mira_background_job(profile: str | None, payload: dict) -> None:
    key = _mira_background_job_key(profile)
    with _mira_background_job_lock:
        _mira_background_jobs[key] = dict(payload)


def _mark_mira_background_job_queued(profile: str | None, payload: dict) -> tuple[bool, dict]:
    key = _mira_background_job_key(profile)
    with _mira_background_job_lock:
        existing = dict(_mira_background_jobs.get(key) or {"status": "idle"})
        if existing.get("status") in {"queued", "running"}:
            return False, existing
        _mira_background_jobs[key] = dict(payload)
        return True, dict(payload)


def _run_mira_background_refresh_task(profile: str | None, force: bool, reason: str = "manual_refresh") -> None:
    from mira.background_analyst import run_background_mira_analysis

    started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _set_mira_background_job(profile, {"status": "running", "started_at": started_at, "force": force, "reason": reason})
    try:
        run = run_background_mira_analysis(profile=profile, force=force)
        _set_mira_background_job(
            profile,
            {
                "status": "completed",
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "force": force,
                "reason": reason,
                "run": run,
            },
        )
    except Exception as exc:
        logger.exception("Mira background analyst refresh failed")
        _set_mira_background_job(
            profile,
            {
                "status": "error",
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "force": force,
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


def _maybe_queue_mira_background_refresh(
    *,
    background_tasks: BackgroundTasks | None,
    profile: str | None,
    reason: str,
    conn=None,
) -> dict:
    from mira.background_analyst import background_analyst_auto_decision

    try:
        decision = background_analyst_auto_decision(profile=profile, conn=conn)
    except Exception as exc:
        logger.exception("Mira background analyst auto-decision failed")
        return {"status": "error", "reason": f"decision_error:{type(exc).__name__}"}

    if not decision.get("should_queue"):
        if decision.get("reason") == "fresh_cache":
            run = {"status": "fresh_cache", "stored_count": 0, "fresh_cache": True}
            _set_mira_background_job(
                profile,
                {
                    "status": "completed",
                    "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "force": False,
                    "reason": reason,
                    "auto": True,
                    "run": run,
                },
            )
            return {"status": "fresh_cache", "reason": "fresh_cache", "run": run}
        return {"status": "skipped", "reason": decision.get("reason") or "not_needed", "decision": decision}

    queued_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    queued_payload = {
        "status": "queued",
        "queued_at": queued_at,
        "force": False,
        "reason": reason,
        "auto": True,
        "decision": decision,
    }
    queued, job = _mark_mira_background_job_queued(profile, queued_payload)
    if not queued:
        return {"status": job.get("status") or "queued", "reason": "already_queued", "job": job}

    if background_tasks is not None:
        background_tasks.add_task(_run_mira_background_refresh_task, profile, False, reason)
    else:
        _run_mira_background_refresh_task(profile, False, reason)
        job = _mira_background_job_snapshot(profile)
    return {"status": "queued", "reason": reason, "job": job}


def _mira_advisor_read_job_snapshot(profile: str | None) -> dict:
    key = _mira_background_job_key(profile)
    with _mira_advisor_read_job_lock:
        return dict(_mira_advisor_read_jobs.get(key) or {"status": "idle"})


def _set_mira_advisor_read_job(profile: str | None, payload: dict) -> None:
    key = _mira_background_job_key(profile)
    with _mira_advisor_read_job_lock:
        _mira_advisor_read_jobs[key] = dict(payload)


def _mark_mira_advisor_read_job_queued(profile: str | None, payload: dict) -> tuple[bool, dict]:
    key = _mira_background_job_key(profile)
    with _mira_advisor_read_job_lock:
        existing = dict(_mira_advisor_read_jobs.get(key) or {"status": "idle"})
        if existing.get("status") in {"queued", "running"}:
            return False, existing
        _mira_advisor_read_jobs[key] = dict(payload)
        return True, dict(payload)


def _advisor_read_run_summary(run: dict) -> dict:
    quality = run.get("quality") if isinstance(run.get("quality"), dict) else {}
    return {
        "status": run.get("status"),
        "stored_count": run.get("stored_count"),
        "latency_ms": run.get("latency_ms"),
        "quality_ok": bool(quality.get("ok")),
        "quality_score": quality.get("score"),
        "coverage_count": quality.get("coverage_count"),
        "required_count": quality.get("required_count"),
        "failure_reasons": (quality.get("failure_reasons") or [])[:8],
        "post_advisor_rewarm": run.get("post_advisor_rewarm"),
    }


def _run_mira_advisor_read_generation_task(profile: str | None, force: bool) -> None:
    from mira.advisor_lens_synthesis import run_advisor_lens_background_memo

    started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _set_mira_advisor_read_job(profile, {"status": "running", "started_at": started_at, "force": force})
    try:
        with get_db() as conn:
            run = run_advisor_lens_background_memo(conn=conn, profile=profile, force=force)
        run_summary = _advisor_read_run_summary(run)
        stored = int(run.get("stored_count") or 0)
        job_status = "completed" if (run.get("status") == "ok" and stored > 0) or run.get("status") == "fresh_cache" else "no_valid_memo"
        _set_mira_advisor_read_job(
            profile,
            {
                "status": job_status,
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "force": force,
                "run": run_summary,
            },
        )
    except Exception as exc:
        logger.exception("Mira advisor read generation failed")
        _set_mira_advisor_read_job(
            profile,
            {
                "status": "error",
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "force": force,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


@app.post("/api/proactive-insights/background-refresh")
def proactive_insights_background_refresh_endpoint(
    background_tasks: BackgroundTasks,
    force: bool = Query(False),
    wait: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.background_analyst import has_fresh_background_analyst_insight, run_background_mira_analysis
    from proactive_insights import list_insights

    if wait:
        run = run_background_mira_analysis(profile=profile, conn=db, force=force)
        _set_mira_background_job(
            profile,
            {
                "status": "completed",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "force": force,
                "run": run,
            },
        )
        return {
            "status": "completed",
            "run": run,
            "items": list_insights(profile=profile, include_dismissed=False, conn=db, generate=False),
        }

    job = _mira_background_job_snapshot(profile)
    if job.get("status") in {"queued", "running"} and not force:
        return {
            "status": job["status"],
            "job": job,
            "items": list_insights(profile=profile, include_dismissed=False, conn=db, generate=False),
        }
    if not force and has_fresh_background_analyst_insight(profile=profile, conn=db):
        run = {"status": "fresh_cache", "stored_count": 0, "fresh_cache": True}
        _set_mira_background_job(
            profile,
            {
                "status": "completed",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "force": force,
                "run": run,
            },
        )
        return {
            "status": "fresh_cache",
            "run": run,
            "items": list_insights(profile=profile, include_dismissed=False, conn=db, generate=False),
        }

    _set_mira_background_job(
        profile,
        {
            "status": "queued",
            "queued_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "force": force,
            "reason": "manual_refresh",
        },
    )
    background_tasks.add_task(_run_mira_background_refresh_task, profile, force, "manual_refresh")
    return {
        "status": "queued",
        "job": _mira_background_job_snapshot(profile),
        "items": list_insights(profile=profile, include_dismissed=False, conn=db, generate=False),
    }


@app.get("/api/proactive-insights/background-refresh/status")
def proactive_insights_background_refresh_status_endpoint(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from proactive_insights import list_insights

    return {
        "job": _mira_background_job_snapshot(profile),
        "items": list_insights(profile=profile, include_dismissed=False, conn=db, generate=False),
    }


def _advisor_read_effective_profile(conn, profile: str | None) -> str | None:
    normalized = _canonicalize_profile_id(profile)
    if normalized and normalized != "household":
        return normalized
    try:
        real_profiles = sorted(_load_valid_profiles(conn))
    except Exception:
        logger.debug("Failed to resolve advisor read profile scope", exc_info=True)
        return normalized or None
    if len(real_profiles) == 1:
        return real_profiles[0]
    return normalized or None


def _money_outlook_effective_profile(conn, profile: str | None) -> str | None:
    return _advisor_read_effective_profile(conn, profile)


def _mira_money_outlook_public_snapshot(snapshot: dict | None) -> dict | None:
    if not snapshot:
        return None
    evidence = snapshot.get("evidence") if isinstance(snapshot.get("evidence"), dict) else {}
    summary = evidence.get("summary") if isinstance(evidence.get("summary"), dict) else {}
    public = {key: value for key, value in dict(snapshot).items() if key != "evidence"}
    public["evidence_summary"] = {
        "as_of_date": evidence.get("as_of_date"),
        "mtd_income": summary.get("mtd_income"),
        "mtd_spend": summary.get("mtd_spend"),
        "expected_month_income": summary.get("expected_month_income"),
        "expected_month_outflow": summary.get("expected_month_outflow"),
        "current_flexible_mtd": summary.get("current_flexible_mtd"),
        "flexible_baseline": summary.get("flexible_baseline"),
    }
    public["safe_to_spend"] = {
        "safe_to_spend_today": snapshot.get("safe_to_spend_today"),
        "safe_to_spend_this_week": snapshot.get("safe_to_spend_this_week"),
        "buffer_status": snapshot.get("buffer_status"),
        "next_pressure_date": snapshot.get("low_point_date"),
        "top_caveat": snapshot.get("safe_to_spend_top_caveat"),
    }
    public["low_point"] = {
        "low_point_date": snapshot.get("low_point_date"),
        "low_point_amount": snapshot.get("low_point_amount"),
        "buffer_amount": snapshot.get("buffer_amount"),
        "buffer_status": snapshot.get("buffer_status"),
        "buffer_breach": bool(snapshot.get("buffer_breach")),
        "drivers": snapshot.get("low_point_drivers") or [],
    }
    return public


def _mira_money_outlook_job_key(profile: str | None) -> str:
    return str(profile or "household")


def _mira_money_outlook_job_snapshot(profile: str | None) -> dict:
    key = _mira_money_outlook_job_key(profile)
    with _mira_money_outlook_job_lock:
        return dict(_mira_money_outlook_jobs.get(key) or {"status": "idle"})


def _set_mira_money_outlook_job(profile: str | None, payload: dict) -> None:
    key = _mira_money_outlook_job_key(profile)
    with _mira_money_outlook_job_lock:
        _mira_money_outlook_jobs[key] = dict(payload)


def _mark_mira_money_outlook_job_queued(profile: str | None, payload: dict) -> tuple[bool, dict]:
    key = _mira_money_outlook_job_key(profile)
    with _mira_money_outlook_job_lock:
        existing = dict(_mira_money_outlook_jobs.get(key) or {"status": "idle"})
        if existing.get("status") in {"queued", "running"}:
            return False, existing
        _mira_money_outlook_jobs[key] = dict(payload)
        return True, dict(payload)


def _run_mira_money_outlook_refresh_task(profile: str | None, reason: str = "manual_refresh") -> None:
    from database import get_db
    from mira.money_outlook import store_money_outlook_snapshot

    started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _set_mira_money_outlook_job(profile, {"status": "running", "started_at": started_at, "reason": reason})
    try:
        with get_db() as conn:
            effective_profile = _money_outlook_effective_profile(conn, profile)
            snapshot = store_money_outlook_snapshot(conn, profile=effective_profile)
        finished_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _set_mira_money_outlook_job(
            profile,
            {
                "status": "completed",
                "started_at": started_at,
                "finished_at": finished_at,
                "reason": reason,
                "month_key": snapshot.get("month_key"),
                "snapshot_id": snapshot.get("id"),
                "confidence": snapshot.get("confidence"),
                "fingerprint": snapshot.get("fingerprint"),
            },
        )
    except Exception as exc:
        logger.exception("Mira money outlook refresh failed")
        _set_mira_money_outlook_job(
            profile,
            {
                "status": "failed",
                "started_at": started_at,
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


def _maybe_queue_mira_money_outlook_refresh(
    *,
    background_tasks: BackgroundTasks | None,
    conn,
    profile: str | None,
    reason: str,
) -> dict:
    from mira.money_outlook import money_outlook_enabled, money_outlook_needs_refresh

    if not money_outlook_enabled():
        return {"queued": False, "reason": "money_outlook_disabled", "job": _mira_money_outlook_job_snapshot(profile)}
    effective_profile = _money_outlook_effective_profile(conn, profile)
    if not money_outlook_needs_refresh(conn, profile=effective_profile):
        return {"queued": False, "reason": "fresh_snapshot_exists", "job": _mira_money_outlook_job_snapshot(effective_profile)}
    queued_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    queued, job = _mark_mira_money_outlook_job_queued(
        effective_profile,
        {
            "status": "queued",
            "queued_at": queued_at,
            "reason": reason,
        },
    )
    if queued:
        if background_tasks is not None:
            background_tasks.add_task(_run_mira_money_outlook_refresh_task, effective_profile, reason)
        else:
            _run_mira_money_outlook_refresh_task(effective_profile, reason)
            job = _mira_money_outlook_job_snapshot(effective_profile)
    return {"queued": queued, "reason": reason if queued else "already_queued", "job": job}


def _mark_mira_money_outlook_stale_after_write(
    *,
    conn,
    profile: str | None = None,
    tx_id: str | None = None,
    all_profiles: bool = False,
    as_of: str | None = None,
    reason: str = "data_changed",
) -> dict:
    from mira.money_outlook import ensure_money_outlook_tables, mark_money_outlook_snapshots_stale, money_outlook_enabled

    if not money_outlook_enabled():
        return {"stale": False, "reason": "money_outlook_disabled", "profiles": []}

    ensure_money_outlook_tables(conn)
    profiles: list[str | None] = []
    if all_profiles:
        rows = conn.execute("SELECT DISTINCT profile_id FROM mira_outlook_snapshots").fetchall()
        profiles = [row[0] for row in rows if row and row[0]]
    else:
        scoped_profile = profile
        if tx_id:
            row = conn.execute("SELECT profile_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
            if row:
                scoped_profile = row[0]
        profiles = [_money_outlook_effective_profile(conn, scoped_profile)]

    stale_count = 0
    touched_profiles: list[str] = []
    for item in dict.fromkeys(str(value or "household") for value in profiles):
        changed = mark_money_outlook_snapshots_stale(conn, profile=item, as_of=as_of)
        stale_count += changed
        if changed:
            touched_profiles.append(item)

    if stale_count:
        stale_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        for item in touched_profiles:
            _set_mira_money_outlook_job(
                item,
                {
                    "status": "stale",
                    "stale_at": stale_at,
                    "reason": reason,
                    "stale_count": stale_count,
                },
            )
    return {"stale": stale_count > 0, "reason": reason, "profiles": touched_profiles, "stale_count": stale_count}


@app.get("/api/mira/money-outlook")
def mira_money_outlook_endpoint(
    profile: str | None = Depends(validate_profile),
    as_of: str | None = None,
    db=Depends(get_db_session),
):
    from mira.money_outlook import load_latest_money_outlook_snapshot, money_outlook_enabled

    enabled = money_outlook_enabled()
    effective_profile = _money_outlook_effective_profile(db, profile)
    job = _mira_money_outlook_job_snapshot(effective_profile)
    if not enabled:
        return {
            "enabled": False,
            "snapshot": None,
            "empty_reason": "disabled",
            "job": job,
        }

    snapshot = load_latest_money_outlook_snapshot(db, profile=effective_profile, as_of=as_of)
    if not snapshot:
        return {
            "enabled": True,
            "snapshot": None,
            "empty_reason": "no_fresh_snapshot",
            "job": job,
        }
    return {
        "enabled": True,
        "snapshot": _mira_money_outlook_public_snapshot(snapshot),
        "empty_reason": None,
        "job": job,
    }


@app.post("/api/mira/money-outlook/generate")
def mira_money_outlook_generate_endpoint(
    profile: str | None = Depends(validate_profile),
    as_of: str | None = None,
    db=Depends(get_db_session),
):
    from mira.money_outlook import money_outlook_enabled, store_money_outlook_snapshot

    effective_profile = _money_outlook_effective_profile(db, profile)
    if not money_outlook_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "snapshot": None,
            "empty_reason": "disabled",
            "job": _mira_money_outlook_job_snapshot(effective_profile),
        }

    generated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    snapshot = store_money_outlook_snapshot(db, profile=effective_profile, as_of=as_of)
    job = {
        "status": "completed",
        "finished_at": generated_at,
        "reason": "manual_generate",
        "month_key": snapshot.get("month_key"),
        "snapshot_id": snapshot.get("id"),
        "confidence": snapshot.get("confidence"),
        "fingerprint": snapshot.get("fingerprint"),
    }
    _set_mira_money_outlook_job(effective_profile, job)
    return {
        "enabled": True,
        "status": "ok",
        "snapshot": _mira_money_outlook_public_snapshot(snapshot),
        "empty_reason": None,
        "job": job,
    }


@app.get("/api/mira/advisor-read")
def mira_advisor_read_endpoint(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.advisor_lens_synthesis import advisor_lens_context_enabled, advisor_lens_ui_enabled, list_lens_advisor_memos

    enabled = advisor_lens_ui_enabled()
    context_enabled = advisor_lens_context_enabled()
    generation_enabled = _advisor_read_generation_enabled()
    advisor_profile = _advisor_read_effective_profile(db, profile)
    memo = None
    if enabled:
        memos = list_lens_advisor_memos(profile=advisor_profile, conn=db, limit=1)
        if memos:
            latest_delta = _latest_public_advisor_delta(conn=db, profile=advisor_profile, memo=memos[0])
            memo = _public_advisor_read_payload(memos[0], delta=latest_delta)
            memo = _attach_public_advisor_feedback(memo, conn=db, profile=advisor_profile)
    return {
        "enabled": enabled,
        "context_enabled": context_enabled,
        "generation_enabled": generation_enabled,
        "memo": memo,
        "job": _mira_advisor_read_job_snapshot(advisor_profile),
        "empty_reason": None if memo else ("ui_disabled" if not enabled else "no_stored_memo"),
    }


@app.post("/api/mira/advisor-read/refresh")
def mira_advisor_read_refresh_endpoint(
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.advisor_lens_synthesis import (
        advisor_lens_context_enabled,
        advisor_lens_ui_enabled,
        list_lens_advisor_memos,
        run_advisor_lens_portrait_delta,
    )

    enabled = advisor_lens_ui_enabled()
    context_enabled = advisor_lens_context_enabled()
    generation_enabled = _advisor_read_generation_enabled()
    advisor_profile = _advisor_read_effective_profile(db, profile)
    memo = None
    preflight: dict | None = None
    if enabled:
        memos = list_lens_advisor_memos(profile=advisor_profile, conn=db, limit=1)
        if memos:
            try:
                preflight = run_advisor_lens_portrait_delta(conn=db, profile=advisor_profile, store=True)
            except Exception as exc:
                logger.exception("Mira advisor read delta refresh failed")
                preflight = {
                    "status": "error",
                    "decision": "delta_preflight_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            latest_delta = _latest_public_advisor_delta(conn=db, profile=advisor_profile, memo=memos[0])
            if not latest_delta and preflight and preflight.get("delta") and preflight.get("decision") == "queue_full_advisor_synthesis":
                latest_delta = _public_advisor_delta_payload(
                    {
                        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "source_memo_fingerprint": memos[0].get("fingerprint"),
                        "delta_packet": preflight.get("delta"),
                    },
                    memo=memos[0],
                )
            memo = _public_advisor_read_payload(memos[0], delta=latest_delta)
            memo = _attach_public_advisor_feedback(memo, conn=db, profile=advisor_profile)
        else:
            preflight = {
                "status": "missing_memo",
                "decision": "needs_full_rebuild",
                "reason": "no_stored_advisor_memo",
            }
    return {
        "status": (preflight or {}).get("status") or "loaded",
        "enabled": enabled,
        "context_enabled": context_enabled,
        "generation_enabled": generation_enabled,
        "memo": memo,
        "job": _mira_advisor_read_job_snapshot(advisor_profile),
        "empty_reason": None if memo else ("ui_disabled" if not enabled else "no_stored_memo"),
        "preflight": {
            "status": (preflight or {}).get("status"),
            "decision": (preflight or {}).get("decision"),
            "reason": (preflight or {}).get("reason"),
            "stored_delta_count": (preflight or {}).get("stored_delta_count"),
            "expired_delta_count": (preflight or {}).get("expired_delta_count"),
            "duplicate_delta": bool((preflight or {}).get("duplicate_delta")),
            "needs_full_rebuild": bool(((preflight or {}).get("delta") or {}).get("needs_full_rebuild")),
        } if preflight else None,
    }


@app.post("/api/mira/advisor-read/generate")
def mira_advisor_read_generate_endpoint(
    background_tasks: BackgroundTasks,
    force: bool = Query(True),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.advisor_lens_synthesis import advisor_lens_context_enabled, advisor_lens_ui_enabled, list_lens_advisor_memos

    enabled = advisor_lens_ui_enabled()
    context_enabled = advisor_lens_context_enabled()
    generation_enabled = _advisor_read_generation_enabled()
    advisor_profile = _advisor_read_effective_profile(db, profile)
    memos = list_lens_advisor_memos(profile=advisor_profile, conn=db, limit=1) if enabled else []
    memo = None
    if memos:
        latest_delta = _latest_public_advisor_delta(conn=db, profile=advisor_profile, memo=memos[0])
        memo = _public_advisor_read_payload(memos[0], delta=latest_delta)
        memo = _attach_public_advisor_feedback(memo, conn=db, profile=advisor_profile)
    if not enabled:
        return {
            "status": "disabled",
            "reason": "ui_disabled",
            "enabled": enabled,
            "context_enabled": context_enabled,
            "generation_enabled": generation_enabled,
            "memo": memo,
            "job": _mira_advisor_read_job_snapshot(advisor_profile),
        }
    if not generation_enabled:
        return {
            "status": "disabled",
            "reason": "generation_disabled",
            "enabled": enabled,
            "context_enabled": context_enabled,
            "generation_enabled": generation_enabled,
            "memo": memo,
            "job": _mira_advisor_read_job_snapshot(advisor_profile),
        }

    queued_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    queued_payload = {
        "status": "queued",
        "queued_at": queued_at,
        "force": force,
        "reason": "manual_ui_generate",
    }
    queued, job = _mark_mira_advisor_read_job_queued(advisor_profile, queued_payload)
    if queued:
        background_tasks.add_task(_run_mira_advisor_read_generation_task, advisor_profile, force)
    return {
        "status": "queued" if queued else (job.get("status") or "queued"),
        "enabled": enabled,
        "context_enabled": context_enabled,
        "generation_enabled": generation_enabled,
        "memo": memo,
        "job": job,
    }


@app.post("/api/mira/advisor-read/followup")
def mira_advisor_read_followup_endpoint(
    body: AdvisorReadFollowupRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.advisor_lens_synthesis import advisor_lens_context_enabled, advisor_lens_ui_enabled, list_lens_advisor_memos

    if not advisor_lens_ui_enabled() or not advisor_lens_context_enabled():
        return {
            "status": "disabled",
            "reason": "advisor_read_context_disabled",
            "answer": "Mira's financial read follow-ups are disabled right now.",
        }
    advisor_profile = _advisor_read_effective_profile(db, profile)
    memos = list_lens_advisor_memos(profile=advisor_profile, conn=db, limit=1)
    if not memos:
        raise HTTPException(
            status_code=404,
            detail={"code": "advisor_read_missing", "message": "Mira has not stored a validated financial read yet."},
        )
    question = str(body.question or "").strip() or "What should I understand from Mira's read?"
    latest_delta = _latest_public_advisor_delta(conn=db, profile=advisor_profile, memo=memos[0])
    result = _compose_advisor_read_followup(
        memo=memos[0],
        question=question,
        followup_type=body.followup_type,
        history=body.history,
        delta=latest_delta,
    )
    return {
        "status": "ok" if not result["final_reject_reasons"] else "guarded",
        "operation": "advisor_read_followup",
        "answer": result["answer"],
        "memo_id": result["memo_id"],
        "memo_generated_at": result["memo_generated_at"],
        "delta": result.get("delta"),
        "answer_context": {
            "used": True,
            "reason": "advisor_read_followup",
            "count": 1,
            "items": [
                {
                    "id": result["memo_id"],
                    "generated_at": result["memo_generated_at"],
                }
            ],
        },
        "answer_guard": {
            "path": "advisor_read_followup",
            "used_fallback": result["used_fallback"],
            "reject_reasons": result["reject_reasons"],
            "final_reject_reasons": result["final_reject_reasons"],
            "error": result["llm_error"] or "",
        },
    }


@app.get("/api/mira/financial-feedback")
def mira_financial_feedback_endpoint(
    target_type: str | None = Query(None),
    target_id: str | None = Query(None),
    subject_type: str | None = Query(None),
    subject_key: str | None = Query(None),
    include_cleared: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.financial_feedback import financial_feedback_loop_enabled, list_financial_feedback

    advisor_profile = _advisor_read_effective_profile(db, profile)
    if not financial_feedback_loop_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "feedback": [],
            "summary": {"count": 0},
        }
    feedback = list_financial_feedback(
        conn=db,
        profile=advisor_profile,
        target_type=target_type,
        target_id=target_id,
        subject_type=subject_type,
        subject_key=subject_key,
        include_cleared=include_cleared,
        limit=100,
    )
    return {
        "enabled": True,
        "status": "ok",
        "feedback": feedback,
        "summary": {"count": len(feedback)},
    }


@app.post("/api/mira/financial-feedback")
def mira_financial_feedback_create_endpoint(
    body: MiraFinancialFeedbackRequest,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.financial_feedback import financial_feedback_loop_enabled, record_financial_feedback

    advisor_profile = _advisor_read_effective_profile(db, profile)
    if not financial_feedback_loop_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "financial_feedback_loop_disabled",
        }
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    try:
        feedback = record_financial_feedback(conn=db, profile=advisor_profile, feedback=payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "enabled": True,
        "status": "stored",
        "feedback": feedback,
    }


@app.delete("/api/mira/financial-feedback/{feedback_id}")
def mira_financial_feedback_clear_endpoint(
    feedback_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.financial_feedback import clear_financial_feedback, financial_feedback_loop_enabled

    advisor_profile = _advisor_read_effective_profile(db, profile)
    if not financial_feedback_loop_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "financial_feedback_loop_disabled",
        }
    feedback = clear_financial_feedback(conn=db, profile=advisor_profile, feedback_id=feedback_id)
    if not feedback:
        raise HTTPException(status_code=404, detail="Financial feedback not found.")
    return {
        "enabled": True,
        "status": "cleared",
        "feedback": feedback,
    }


@app.post("/api/mira/financial-feedback/{feedback_id}/promote-memory")
def mira_financial_feedback_promote_memory_endpoint(
    feedback_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    import memory as _mem
    from mira.financial_feedback import feedback_memory_candidate, financial_feedback_loop_enabled, get_financial_feedback

    advisor_profile = _advisor_read_effective_profile(db, profile)
    if not financial_feedback_loop_enabled():
        return {
            "enabled": False,
            "status": "disabled",
            "reason": "financial_feedback_loop_disabled",
        }
    feedback = get_financial_feedback(conn=db, profile=advisor_profile, feedback_id=feedback_id)
    if not feedback:
        raise HTTPException(status_code=404, detail="Financial feedback not found.")
    candidate = feedback_memory_candidate(feedback)
    if not candidate:
        raise HTTPException(status_code=400, detail="Feedback cannot be promoted to memory.")
    existing_id = _mem.find_active_entry_id(
        profile=advisor_profile,
        section=candidate["section"],
        body=candidate["body"],
        conn=db,
    )
    if existing_id:
        row = db.execute(
            "SELECT id, profile_id, section, body, confidence, evidence, theme, created_at "
            "FROM memory_entries WHERE id = ?",
            (existing_id,),
        ).fetchone()
        return {
            "enabled": True,
            "status": "already_stored",
            "entry": dict(row),
            "feedback": feedback,
        }
    try:
        entry_id = _mem.insert_entry(profile=advisor_profile, conn=db, **candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    row = db.execute(
        "SELECT id, profile_id, section, body, confidence, evidence, theme, created_at "
        "FROM memory_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    _invalidate_copilot_cache()
    return {
        "enabled": True,
        "status": "stored",
        "entry": dict(row),
        "feedback": feedback,
    }


@app.get("/api/mira/advisor-cases")
def mira_advisor_cases_endpoint(
    include_dismissed: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.advisor_cases import list_advisor_cases, list_or_refresh_advisor_cases

    if include_dismissed:
        items = list_advisor_cases(profile=profile, include_dismissed=True, conn=db)
    else:
        items = list_or_refresh_advisor_cases(profile=profile, conn=db)
    return {
        "items": items,
        "job": _mira_background_job_snapshot(profile),
    }


@app.post("/api/mira/advisor-cases/refresh")
def mira_advisor_cases_refresh_endpoint(
    force: bool = Query(False),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.advisor_cases import list_advisor_cases, refresh_advisor_cases

    run = refresh_advisor_cases(profile=profile, conn=db, force=force)
    return {
        "status": run.get("status") or "ok",
        "run": run,
        "items": list_advisor_cases(profile=profile, conn=db),
    }


@app.post("/api/mira/advisor-cases/{case_id}/dismiss")
def dismiss_mira_advisor_case_endpoint(
    case_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from mira.advisor_cases import dismiss_advisor_case

    if not dismiss_advisor_case(case_id=case_id, profile=profile, conn=db):
        raise HTTPException(status_code=404, detail="Advisor case not found.")
    return {"status": "dismissed", "id": case_id}


@app.post("/api/proactive-insights/{insight_id}/dismiss")
def dismiss_proactive_insight_endpoint(
    insight_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from proactive_insights import dismiss_insight

    if not dismiss_insight(insight_id, profile=profile, conn=db):
        raise HTTPException(status_code=404, detail="Insight not found.")
    return {"status": "dismissed", "id": insight_id}


@app.post("/api/proactive-insights/{insight_id}/restore")
def restore_proactive_insight_endpoint(
    insight_id: int,
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    from proactive_insights import restore_insight

    if not restore_insight(insight_id, profile=profile, conn=db):
        raise HTTPException(status_code=404, detail="Insight not found.")
    return {"status": "active", "id": insight_id}


@app.get("/api/analytics/net-worth-series")
def net_worth_series(
    interval: str = Query("weekly", description="weekly or biweekly"),
    profile: str | None = Depends(validate_profile),
    db=Depends(get_db_session),
):
    """
    Compute a running net-worth time series from transaction history.
    Returns one data point per week (or bi-week), preserving intra-month
    volatility that the monthly endpoint destroys.
    """
    return get_net_worth_series_data(interval=interval, profile=profile, conn=db)

# ══════════════════════════════════════════════════════════════════════════════
# TELLER CONNECT ENROLLMENT
# ══════════════════════════════════════════════════════════════════════════════


class EnrollRequest(BaseModel):
    accessToken: str
    institutionName: str = ""
    enrollmentId: str | None = None


@app.get("/api/teller-config")
def teller_config():
    """
    Return the Teller application ID and environment so the frontend
    can initialize Teller Connect without hardcoding secrets.
    """
    if DEMO_MODE:
        return {
            **_app_config_payload(),
            "enabled": False,
            "applicationId": "",
            "environment": "sandbox",
        }

    app_id = os.getenv("TELLER_APPLICATION_ID", "")
    env = os.getenv("TELLER_ENVIRONMENT", "sandbox")
    if not app_id:
        raise HTTPException(
            status_code=503,
            detail="TELLER_APPLICATION_ID not configured on the server.",
        )
    return {
        **_app_config_payload(),
        "enabled": True,
        "applicationId": app_id,
        "environment": env,
    }


@app.post("/api/enroll")
def enroll_account(req: EnrollRequest, background_tasks: BackgroundTasks):
    _require_live_mode("Bank enrollment is disabled in demo mode.")
    """
    Handle a new Teller Connect enrollment.

    1. Validate the token by fetching accounts from Teller.
    2. Attempt to resolve the owner's name via the Identity API.
    3. Persist the token in the encrypted token store.
    4. Hot-reload the in-memory token/profile registries.
    5. Trigger a data sync for the new accounts.
    """
    from bank import (
        get_accounts_for_token,
        get_identity,
        reload_tokens_and_profiles,
    )
    from token_store import save_token

    # 1. Validate — can we actually use this token?
    accounts = get_accounts_for_token(req.accessToken)
    if not accounts:
        raise HTTPException(
            status_code=400,
            detail="Could not fetch accounts with the provided token. It may be invalid or expired.",
        )

    # 2. Resolve identity
    first_account_id = accounts[0].get("id", "")
    identity = {"first_name": "", "last_name": "", "full_name": ""}
    if first_account_id:
        identity = get_identity(req.accessToken, first_account_id)

    # Determine profile name: prefer identity first name, fall back to "primary"
    profile_name = (
        identity["first_name"].lower().strip()
        if identity["first_name"]
        else "primary"
    )

    # Sanitize: if the name looks like a company or is too long, fall back
    if len(profile_name) > 20 or " " in profile_name:
        profile_name = "primary"
        
    # 3. Persist
    profile_record = _ensure_profile(
        profile_name,
        display_name=identity["first_name"] or profile_name,
    )
    was_new = save_token(
        profile=profile_record["id"],
        token=req.accessToken,
        institution=req.institutionName,
        owner_name=identity["full_name"],
        enrollment_id=req.enrollmentId,
    )

    # 4. Hot-reload
    reload_tokens_and_profiles()
    _invalidate_profile_cache()

    # 5. Sync the new accounts into the transaction database
    sync_result = {"accounts": 0, "transactions": 0}
    job_id = start_sync("enrollment", phase="starting", detail="Starting account enrollment sync")
    try:
        data = fetch_fresh_data(sync_job_id=job_id)
        sync_result = {
            "accounts": len(data.get("accounts", [])),
            "transactions": len(data.get("transactions", [])),
        }
        finish_sync(job_id, status="completed")
        sync_result["background_analyst_refresh"] = _maybe_queue_mira_background_refresh(
            background_tasks=background_tasks,
            profile=profile_record["id"],
            reason="enrollment_sync_completed",
        )
    except Exception as e:
        finish_sync(job_id, status="failed", error=str(e))
        logger.warning("Post-enrollment sync failed (non-fatal): %s", e)

    institution = req.institutionName or accounts[0].get("institution", {}).get("name", "Unknown")

    return {
        "status": "enrolled" if was_new else "already_exists",
        "profile": profile_record["id"],
        "institution": institution,
        "owner": identity["full_name"],
        "accounts_found": len(accounts),
        "synced": sync_result,
    }


@app.get("/api/enrollments")
def list_enrollments():
    """Return all active Teller Connect enrollments (metadata only, no tokens)."""
    if DEMO_MODE:
        return []
    from token_store import load_all_enrollments
    return load_all_enrollments()


class DeactivateEnrollment(BaseModel):
    id: int


@app.post("/api/enrollments/deactivate")
def deactivate_enrollment(body: DeactivateEnrollment):
    """Soft-delete an enrollment. The token will no longer be used on next reload."""
    _require_live_mode("Bank enrollment changes are disabled in demo mode.")
    from token_store import deactivate_token
    from bank import reload_tokens_and_profiles

    success = deactivate_token(body.id)
    if not success:
        raise HTTPException(status_code=404, detail="Enrollment not found or already inactive.")

    reload_tokens_and_profiles()
    _invalidate_profile_cache()

    return {"status": "deactivated", "id": body.id}


# ── Provider Migration ────────────────────────────────────────────────────────

@app.get("/api/migration/status")
def migration_status(db=Depends(get_db_session)):
    """
    Lightweight check: do both Teller and SimpleFIN have active data?
    Returns {needs_migration, overlap_days, simplefin_window_start}.
    Used by the dashboard to decide whether to show the migration banner.
    """
    if DEMO_MODE:
        return {"needs_migration": False, "overlap_days": 0, "simplefin_window_start": None}

    teller_count = db.execute(
        "SELECT COUNT(*) FROM enrolled_tokens WHERE is_active = 1"
    ).fetchone()[0]

    sf_count = db.execute(
        "SELECT COUNT(*) FROM simplefin_connections WHERE is_active = 1"
    ).fetchone()[0]

    if not teller_count or not sf_count:
        return {"needs_migration": False, "overlap_days": 0, "simplefin_window_start": None}

    sf_start = db.execute(
        "SELECT MIN(date) FROM transactions WHERE id LIKE 'sf_%' AND is_excluded = 0"
    ).fetchone()[0]

    teller_end = db.execute(
        "SELECT MAX(date) FROM transactions WHERE id NOT LIKE 'sf_%' AND is_excluded = 0"
    ).fetchone()[0]

    if not sf_start or not teller_end:
        return {"needs_migration": False, "overlap_days": 0, "simplefin_window_start": sf_start}

    from datetime import date as _date
    try:
        d1 = _date.fromisoformat(sf_start)
        d2 = _date.fromisoformat(teller_end)
        overlap_days = max(0, (d2 - d1).days)
    except ValueError:
        overlap_days = 0

    return {
        "needs_migration": overlap_days > 0,
        "overlap_days": overlap_days,
        "simplefin_window_start": sf_start,
    }


@app.get("/api/migration/preview")
def migration_preview(db=Depends(get_db_session)):
    _require_live_mode("Provider migration is disabled in demo mode.")
    from migration import analyze_migration
    return analyze_migration(db)


class MigrationExecuteRequest(BaseModel):
    mappings: list[dict]  # [{"teller_account_id": "...", "sf_account_id": "..." | None}]
    deactivate_teller: bool = True


@app.post("/api/migration/execute")
def migration_execute(
    req: MigrationExecuteRequest,
    db=Depends(get_db_session),
):
    _require_live_mode("Provider migration is disabled in demo mode.")
    from migration import execute_migration
    try:
        result = execute_migration(req.mappings, req.deactivate_teller, db)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _invalidate_profile_cache()
    return result


# ── SimpleFIN Bridge ─────────────────────────────────────────────────────────

class SimpleFINClaimRequest(BaseModel):
    setupToken: str
    profile: str
    displayName: str = ""


@app.post("/api/simplefin/claim")
def simplefin_claim(req: SimpleFINClaimRequest, background_tasks: BackgroundTasks):
    _require_live_mode("SimpleFIN connection is disabled in demo mode.")
    """
    Exchange a SimpleFIN Setup Token for an Access URL.

    1. base64-decode → claim URL → POST to get permanent Access URL.
    2. Encrypt and store in simplefin_connections table.
    3. Kick off initial sync in the background (LLM categorization can take
       30-120 s — running it synchronously causes the frontend proxy to timeout).
    """
    import simplefin

    requested_profile = req.profile or "primary"
    canonical_profile = _canonicalize_profile_id(requested_profile)
    if not canonical_profile:
        raise HTTPException(status_code=400, detail="Profile is required.")
    if canonical_profile == "household":
        raise HTTPException(status_code=400, detail="'household' is reserved and cannot be used as a profile.")

    # 1. Claim
    try:
        access_url = simplefin.claim_setup_token(req.setupToken)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    profile_record = _ensure_profile(canonical_profile, display_name=requested_profile)

    # 2. Store
    try:
        conn_id = simplefin.save_connection(
            profile=profile_record["id"],
            access_url=access_url,
            display_name=req.displayName,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    _invalidate_profile_cache()

    # 3. Initial sync runs in the background so this endpoint returns immediately
    job_id = start_sync("simplefin", phase="queued", detail="Queued SimpleFIN initial sync")

    def _bg_sync():
        try:
            update_phase(job_id, "starting", "Starting SimpleFIN initial sync")
            fetch_simplefin_data(sync_job_id=job_id)
            finish_sync(job_id, status="completed")
            _maybe_queue_mira_background_refresh(
                background_tasks=None,
                profile=profile_record["id"],
                reason="simplefin_initial_sync_completed",
            )
        except Exception as e:
            finish_sync(job_id, status="failed", error=str(e))
            logger.warning("Post-claim SimpleFIN background sync failed: %s", e)

    background_tasks.add_task(_bg_sync)

    return {
        "status": "connected",
        "connection_id": conn_id,
        "profile": profile_record["id"],
        "displayName": req.displayName,
        "syncing": True,
    }


@app.get("/api/simplefin/connections")
def simplefin_connections():
    """Return all active SimpleFIN connections (metadata only, no access URLs)."""
    if DEMO_MODE:
        return []
    import simplefin
    return simplefin.load_all_connections()


class SimpleFINDeactivate(BaseModel):
    id: int


@app.post("/api/simplefin/connections/deactivate")
def simplefin_deactivate(body: SimpleFINDeactivate):
    """Soft-delete a SimpleFIN connection."""
    _require_live_mode("SimpleFIN connection changes are disabled in demo mode.")
    import simplefin

    success = simplefin.deactivate_connection(body.id)
    if not success:
        raise HTTPException(status_code=404, detail="Connection not found or already inactive.")

    _invalidate_profile_cache()
    return {"status": "deactivated", "id": body.id}


@app.post("/api/simplefin/sync")
def simplefin_sync(background_tasks: BackgroundTasks):
    """Trigger a SimpleFIN-only sync (does not touch Teller)."""
    _require_live_mode("SimpleFIN sync is disabled in demo mode.")
    job_id = start_sync("simplefin", phase="starting", detail="Starting SimpleFIN sync")
    try:
        data = fetch_simplefin_data(sync_job_id=job_id)
        finish_sync(job_id, status="completed")
        _invalidate_copilot_cache()
        background_refresh = _maybe_queue_mira_background_refresh(
            background_tasks=background_tasks,
            profile=None,
            reason="simplefin_sync_completed",
        )
        with get_db() as conn:
            money_outlook_refresh = _maybe_queue_mira_money_outlook_refresh(
                background_tasks=background_tasks,
                conn=conn,
                profile=None,
                reason="simplefin_sync_completed",
            )
        return {
            "status": "synced",
            "accounts": len(data.get("accounts", [])),
            "transactions": len(data.get("transactions", [])),
            "last_updated": data.get("last_updated"),
            "background_analyst_refresh": background_refresh,
            "money_outlook_refresh": money_outlook_refresh,
        }
    except Exception as exc:
        finish_sync(job_id, status="failed", error=str(exc))
        raise
