"""
data_manager.py
Handles data fetching, syncing from Teller, and persistence via SQLite.
Replaces the old JSON cache with database operations.
"""

import re
import os
import threading
import calendar
from datetime import date, datetime, timedelta
from uuid import uuid4
from dotenv import load_dotenv
import bank
from bank import get_all_accounts_by_profile, get_transactions, get_balances
from categorizer import categorize_transactions, _rule_based_categorize
from cashflow_classifier import CREDITS_REFUNDS_CATEGORY, build_batch_pair_evidence
from database import DB_PATH, get_db, dicts_from_rows, _extract_merchant_pattern
from merchant_identity import (
    build_merchant_identity,
    canonicalize_merchant_key,
)
from recurring_obligations import (
    add_months as _recurring_add_months,
    backfill_from_legacy as _backfill_recurring_obligations,
    canonical_key as _recurring_canonical_key,
    get_recurring_bundle as _get_obligation_recurring_bundle,
    get_scheduled_bundle as _get_obligation_scheduled_bundle,
    merchant_match_keys as _recurring_match_keys,
    record_feedback as _record_recurring_feedback,
    restore_obligation as _restore_recurring_obligation,
    sync_legacy_subscription_cache as _sync_recurring_legacy_cache,
    upsert_user_obligation as _upsert_user_recurring_obligation,
)
from log_config import get_logger

load_dotenv()

logger = get_logger(__name__)

_lock = threading.Lock()
_recurring_redetection_locks: dict[str, threading.Lock] = {}

_COPILOT_HISTORY_CHAT_PREVIEW_CHARS = int(os.getenv("COPILOT_HISTORY_CHAT_PREVIEW_CHARS", "1000"))
_COPILOT_HISTORY_FINANCE_PREVIEW_CHARS = int(os.getenv("COPILOT_HISTORY_FINANCE_PREVIEW_CHARS", "4000"))
_COPILOT_HISTORY_RESULT_CHARS = int(os.getenv("COPILOT_HISTORY_RESULT_CHARS", "8000"))
_COPILOT_HISTORY_SQL_CHARS = int(os.getenv("COPILOT_HISTORY_SQL_CHARS", "8000"))
_COPILOT_HISTORY_MAX_ROWS = int(os.getenv("COPILOT_HISTORY_MAX_ROWS", "500"))


def _set_sync_phase(sync_job_id: str | None, phase: str, detail: str | None = None):
    if not sync_job_id:
        return
    try:
        from sync_status import update_phase
        update_phase(sync_job_id, phase, detail)
    except Exception:
        logger.debug("Could not update sync phase to %s", phase)


def _escape_like(pattern: str) -> str:
    """Escape SQL LIKE wildcards in a pattern to prevent unintended matches."""
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

EMPTY_DATA = {
    "last_updated": None,
    "accounts": [],
    "transactions": [],
}

# Categories excluded from spending calculations (duplicated from main.py for
# SQL-level filtering — keep in sync with main.py constants).
TRANSFER_CATEGORIES = ("Savings Transfer", "Personal Transfer", "Credit Card Payment")
CASHFLOW_DIRECT_CATEGORIES = ("Cash Withdrawal", "Cash Deposit", "Investment Transfer")
NON_SPENDING_CATEGORIES = TRANSFER_CATEGORIES + CASHFLOW_DIRECT_CATEGORIES + ("Income", CREDITS_REFUNDS_CATEGORY)


# Valid transfer sub-classification values
TRANSFER_INTERNAL = "transfer_internal"
TRANSFER_HOUSEHOLD = "transfer_household"
TRANSFER_EXTERNAL = "transfer_external"
TRANSFER_CC_PAYMENT = "transfer_cc_payment"

TRANSACTIONS_TABLE = "transactions"
VISIBLE_TRANSACTIONS_VIEW = "transactions_visible"


def _transactions_source(include_excluded: bool = False, alias: str | None = None) -> str:
    """Return the canonical transaction relation for reads or raw-table writes."""
    source = TRANSACTIONS_TABLE if include_excluded else VISIBLE_TRANSACTIONS_VIEW
    return f"{source} {alias}" if alias else source


def _canonical_merchant_key_sql(tx_alias: str = "t") -> str:
    """SQL expression for the canonical merchant key used by UI grouping."""
    return (
        f"UPPER(TRIM(COALESCE(NULLIF({tx_alias}.merchant_key,''), NULLIF({tx_alias}.merchant_name,''), "
        f"{tx_alias}.description_normalized, {tx_alias}.description, '')))"
    )


def _canonical_merchant_label_sql(tx_alias: str = "t") -> str:
    """SQL expression for the canonical merchant display fallback."""
    return (
        f"COALESCE(NULLIF({tx_alias}.merchant_name,''), "
        f"{tx_alias}.description_normalized, {tx_alias}.description)"
    )


def _merchant_alias_join_sql(tx_alias: str = "t", merchant_alias: str = "merchant_alias") -> str:
    """LEFT JOIN merchant_aliases on the canonical transaction merchant key."""
    return (
        f"LEFT JOIN merchant_aliases {merchant_alias} "
        f"ON {merchant_alias}.profile_id = {tx_alias}.profile_id "
        f"AND UPPER(TRIM(COALESCE({merchant_alias}.merchant_key, ''))) = {_canonical_merchant_key_sql(tx_alias)}"
    )


def _merchant_metadata_join_sql(tx_alias: str = "t", merchant_meta: str = "merchant_meta") -> str:
    """LEFT JOIN canonical merchant metadata keyed by profile + merchant_key."""
    return (
        f"LEFT JOIN merchants {merchant_meta} "
        f"ON {merchant_meta}.profile_id = {tx_alias}.profile_id "
        f"AND UPPER(TRIM(COALESCE({merchant_meta}.merchant_key, ''))) = {_canonical_merchant_key_sql(tx_alias)}"
    )


def _merchant_display_source_sql(
    tx_alias: str = "t",
    merchant_alias: str = "merchant_alias",
    merchant_meta: str = "merchant_meta",
) -> str:
    """SQL expression for the source of the merchant label shown in the UI."""
    merchant_label_sql = _canonical_merchant_label_sql(tx_alias)
    return (
        "CASE "
        f"WHEN NULLIF({merchant_alias}.display_name, '') IS NOT NULL "
        f"AND UPPER(TRIM({merchant_alias}.display_name)) != UPPER(TRIM(COALESCE(NULLIF({merchant_meta}.clean_name, ''), {merchant_label_sql}, ''))) "
        "THEN 'user' "
        f"WHEN NULLIF({merchant_meta}.clean_name, '') IS NOT NULL "
        f"THEN COALESCE(NULLIF({merchant_meta}.source, ''), 'directory') "
        f"WHEN NULLIF({tx_alias}.merchant_name, '') IS NOT NULL "
        f"THEN COALESCE(NULLIF({tx_alias}.merchant_source, ''), 'enriched') "
        "ELSE 'raw' END"
    )


def _build_account_lookup(conn) -> dict:
    """
    Build a lookup structure mapping account last-four digits to a set of
    profile IDs that own accounts with those digits.

    Returns:
        dict[str, set[str]] — e.g. {"1234": {"primary", "wife"}, "5678": {"primary"}}
    """
    rows = conn.execute(
        """SELECT account_name, id, profile_id FROM accounts WHERE is_active = 1"""
    ).fetchall()

    lookup: dict[str, set[str]] = {}
    for row in rows:
        acct_name = row[0] or ""
        acct_id = row[1] or ""
        profile_id = row[2] or ""

        # Extract last 4 digits from account name (e.g., "Checking ...1234")
        for source in (acct_name, acct_id):
            digits = re.findall(r'\d{4,}', source)
            for d in digits:
                last4 = d[-4:]
                if last4 not in lookup:
                    lookup[last4] = set()
                lookup[last4].add(profile_id)

    return lookup


def _classify_transfer_type(
    tx: dict,
    account_lookup: dict[str, set[str]],
) -> str:
    """
    Sub-classify a transfer transaction as internal, household, external,
    or cc_payment.

    Only called for transactions where category == one of TRANSFER_CATEGORIES
    (Savings Transfer, Personal Transfer, Credit Card Payment).

    Args:
        tx: transaction dict with at least 'description', 'profile' or 'profile_id',
            and 'category'
        account_lookup: {last4_digits: set_of_profile_ids} from _build_account_lookup

    Returns:
        One of: TRANSFER_INTERNAL, TRANSFER_HOUSEHOLD, TRANSFER_EXTERNAL, TRANSFER_CC_PAYMENT
    """
    # Credit Card Payment is always its own type — never internal/household/external
    category = tx.get("category", "")
    if category == "Credit Card Payment":
        return TRANSFER_CC_PAYMENT

    tx_profile = tx.get("profile") or tx.get("profile_id") or "primary"
    description = (tx.get("description") or "") + " " + (tx.get("raw_description") or "")

    # Extract all 4-digit sequences from description that could be account fragments
    fragments = re.findall(r'\b\d{4}\b', description)

    for frag in fragments:
        owning_profiles = account_lookup.get(frag)
        if owning_profiles is None:
            continue

        # If any owning profile is the same as the transaction's profile → internal
        if tx_profile in owning_profiles:
            # Check if OTHER profiles also own this fragment — ambiguous,
            # but same-profile match takes priority
            return TRANSFER_INTERNAL

        # Fragment matches an account in a different profile → household
        return TRANSFER_HOUSEHOLD

    # Savings transfers are internal by default even when the bank description
    # does not expose account fragments.
    if category == "Savings Transfer":
        return TRANSFER_INTERNAL

    # No account fragment matched → external
    return TRANSFER_EXTERNAL


def _apply_linked_account_pair_overrides(transactions: list[dict]) -> None:
    """Force linked depository account pairs into transfer categories before insert."""
    pair_evidence_by_index = build_batch_pair_evidence(transactions)
    for idx, tx in enumerate(transactions):
        pair = pair_evidence_by_index.get(idx)
        if not pair or pair.get("kind") != "linked_account_transfer":
            continue
        if (tx.get("categorization_source") or "") in {"user", "user-rule"}:
            continue

        category = pair.get("category") or "Savings Transfer"
        if category not in TRANSFER_CATEGORIES:
            continue

        tx["category"] = category
        tx["confidence"] = "rule"
        tx["categorization_source"] = "rule-high"
        tx["expense_type"] = pair.get("expense_type")

# ══════════════════════════════════════════════════════════════════════════════
# READ OPERATIONS — TARGETED QUERIES (preferred)
# ══════════════════════════════════════════════════════════════════════════════

def get_accounts_filtered(profile: str | None = None, conn=None) -> list[dict]:
    """Fetch accounts with optional profile filter, pushed into SQL."""
    def _query(c):
        sql = """SELECT id, account_name as name, account_subtype as type,
                        CASE WHEN account_type IN ('credit', 'loan') THEN 1 ELSE 0 END as is_credit,
                        account_type,
                        current_balance as balance, currency, profile_id as profile,
                        COALESCE(provider, 'teller') as provider,
                        last_synced_at, manual_updated_at, manual_notes, usual_due_day
                 FROM accounts WHERE is_active = 1"""
        params = []
        if profile and profile != "household":
            sql += " AND profile_id = ?"
            params.append(profile)
        rows = c.execute(sql, params).fetchall()
        accounts = dicts_from_rows(rows)
        for acct in accounts:
            acct["is_credit"] = bool(acct.get("is_credit", 0))
            acct["is_manual"] = acct.get("provider") == "manual"
            acct["manual_is_stale"] = False
            if acct["is_manual"] and acct.get("manual_updated_at"):
                try:
                    updated = datetime.fromisoformat(str(acct["manual_updated_at"]).replace("Z", "+00:00"))
                    stale_days = (datetime.now(updated.tzinfo) - updated).days
                    acct["manual_stale_days"] = stale_days
                    acct["manual_is_stale"] = stale_days > 30
                except Exception:
                    acct["manual_is_stale"] = False
        return accounts

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _normalize_usual_due_day(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Usual due day must be an integer from 1 to 31.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("Usual due day must be an integer from 1 to 31.")
    try:
        day = int(value)
    except (TypeError, ValueError):
        raise ValueError("Usual due day must be an integer from 1 to 31.")
    if day < 1 or day > 31:
        raise ValueError("Usual due day must be an integer from 1 to 31.")
    return day


def update_account_payment_details(account_id: str, payload: dict, profile: str | None = None, conn=None) -> dict | None:
    """Update lightweight payment metadata for active credit card accounts."""
    due_day = _normalize_usual_due_day(payload.get("usual_due_day"))

    def _update(c):
        sql = "SELECT * FROM accounts WHERE id = ? AND is_active = 1"
        params = [account_id]
        if profile and profile != "household":
            sql += " AND profile_id = ?"
            params.append(profile)
        row = c.execute(sql, params).fetchone()
        if not row:
            return None
        current = dict(row)
        account_type = (current.get("account_type") or "").strip().lower()
        if account_type not in {"credit", "credit_card"}:
            raise ValueError("Payment details are only available for credit card accounts.")
        c.execute("UPDATE accounts SET usual_due_day = ? WHERE id = ?", (due_day, account_id))
        return dict(c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_data_health_summary(profile: str | None = None, conn=None) -> dict:
    """Return account freshness and local data-safety signals for trust surfaces."""

    def _parse_dt(value: str | None):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        except Exception:
            return None

    def _days_since(value: str | None) -> int | None:
        parsed = _parse_dt(value)
        if not parsed:
            return None
        return max(0, (datetime.now(parsed.tzinfo) - parsed).days)

    def _encryption_status() -> dict:
        key_present = bool(os.getenv("TOKEN_ENCRYPTION_KEY"))
        fernet_ready = False
        if key_present:
            try:
                from cryptography.fernet import Fernet
                Fernet(os.getenv("TOKEN_ENCRYPTION_KEY").encode())
                fernet_ready = True
            except Exception:
                fernet_ready = False
        return {
            "key_present": key_present,
            "fernet_ready": fernet_ready,
            "status": "encrypted" if fernet_ready else "plaintext",
            "message": (
                "Credential encryption is active."
                if fernet_ready
                else "TOKEN_ENCRYPTION_KEY is missing or invalid; credential encryption is disabled."
            ),
        }

    def _query(c):
        accounts = get_accounts_filtered(profile=profile, conn=c)
        thresholds = {
            "teller": int(os.getenv("FOLIO_TELLER_STALE_DAYS", "7")),
            "simplefin": int(os.getenv("FOLIO_SIMPLEFIN_STALE_DAYS", "3")),
            "manual": int(os.getenv("FOLIO_MANUAL_STALE_DAYS", "30")),
        }
        account_statuses = []
        provider_counts: dict[str, dict] = {}
        warnings = []
        stale_count = 0

        for acct in accounts:
            provider = (acct.get("provider") or "teller").lower()
            updated_at = acct.get("manual_updated_at") if provider == "manual" else acct.get("last_synced_at")
            if not updated_at:
                row = c.execute("SELECT last_synced_at FROM accounts WHERE id = ?", (acct.get("id"),)).fetchone()
                updated_at = row["last_synced_at"] if row else None
            days = _days_since(updated_at)
            threshold = thresholds.get(provider, thresholds["teller"])
            is_stale = days is None or days > threshold
            if is_stale:
                stale_count += 1

            provider_bucket = provider_counts.setdefault(provider, {"provider": provider, "total": 0, "stale": 0})
            provider_bucket["total"] += 1
            if is_stale:
                provider_bucket["stale"] += 1

            status = {
                "id": acct.get("id"),
                "name": acct.get("name"),
                "profile": acct.get("profile"),
                "provider": provider,
                "account_type": acct.get("account_type"),
                "balance": acct.get("balance"),
                "last_updated_at": updated_at,
                "days_since_update": days,
                "stale_threshold_days": threshold,
                "is_stale": is_stale,
                "is_manual": provider == "manual",
            }
            account_statuses.append(status)

            if is_stale:
                label = "updated" if provider == "manual" else "synced"
                age = "never" if days is None else f"{days} days ago"
                warnings.append({
                    "type": "manual_stale" if provider == "manual" else "sync_stale",
                    "severity": "warning",
                    "account_id": acct.get("id"),
                    "account_name": acct.get("name"),
                    "provider": provider,
                    "message": f"{acct.get('name')} was {label} {age}.",
                    "action": "update_manual" if provider == "manual" else "sync_now",
                })

        active_simplefin_rows = c.execute(
            "SELECT id, profile, display_name, last_synced_at FROM simplefin_connections WHERE is_active = 1"
        ).fetchall()
        simplefin_connections = []
        for row in active_simplefin_rows:
            days = _days_since(row["last_synced_at"])
            simplefin_connections.append({
                "id": row["id"],
                "profile": row["profile"],
                "display_name": row["display_name"],
                "last_synced_at": row["last_synced_at"],
                "days_since_sync": days,
                "is_stale": days is None or days > thresholds["simplefin"],
                "next_eligible_sync_hint": "SimpleFIN syncs are intentionally paced; avoid more than one pull per hour.",
            })

        active_teller_count = c.execute("SELECT COUNT(*) AS count FROM enrolled_tokens WHERE is_active = 1").fetchone()["count"]

        encryption = _encryption_status()
        if encryption["status"] == "plaintext":
            warnings.append({
                "type": "credential_encryption",
                "severity": "warning",
                "message": encryption["message"],
                "action": "configure_encryption",
            })

        latest_updates = [s["last_updated_at"] for s in account_statuses if s.get("last_updated_at")]
        latest_update = max(latest_updates) if latest_updates else None

        return {
            "generated_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
            "status": "attention" if warnings else "healthy",
            "thresholds": thresholds,
            "summary": {
                "total_accounts": len(accounts),
                "stale_accounts": stale_count,
                "warnings": len(warnings),
                "latest_account_update": latest_update,
                "active_teller_enrollments": active_teller_count,
                "active_simplefin_connections": len(simplefin_connections),
            },
            "providers": list(provider_counts.values()),
            "accounts": account_statuses,
            "simplefin_connections": simplefin_connections,
            "encryption": encryption,
            "warnings": warnings,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _account_balance_totals(c, profile: str | None = None) -> tuple[float, float]:
    """Return (assets, owed) with liabilities normalized to positive owed amounts."""
    profile_clause = ""
    params = []
    if profile and profile != "household":
        profile_clause = " AND profile_id = ?"
        params = [profile]
    total_assets = c.execute(
        f"""SELECT COALESCE(SUM(current_balance), 0)
            FROM accounts
            WHERE account_type IN ('depository', 'investment')
              AND is_active = 1{profile_clause}""",
        params,
    ).fetchone()[0]
    total_owed = c.execute(
        f"""SELECT COALESCE(SUM(ABS(current_balance)), 0)
            FROM accounts
            WHERE account_type IN ('credit', 'loan')
              AND is_active = 1{profile_clause}""",
        params,
    ).fetchone()[0]
    return float(total_assets or 0), float(total_owed or 0)


def get_transactions_paginated(
    month: str | None = None,
    category: str | None = None,
    account: str | None = None,
    search: str | None = None,
    reviewed: bool | None = None,
    profile: str | None = None,
    limit: int = 100,
    offset: int = 0,
    conn=None,
    start_date: str | None = None,
    end_date: str | None = None,
    sort: str | None = None,
) -> dict:
    """
    Fetch transactions with all filters pushed into SQL WHERE clauses.
    Returns {"data": [...], "total_count": int, "limit": int, "offset": int}.
    """
    def _query(c):
        tx_source = _transactions_source(alias="t")
        merchant_key_sql = _canonical_merchant_key_sql("t")
        merchant_label_sql = _canonical_merchant_label_sql("t")
        merchant_source_sql = _merchant_display_source_sql("t", "merchant_alias", "merchant_meta")
        alias_join_sql = _merchant_alias_join_sql("t", "merchant_alias")
        metadata_join_sql = _merchant_metadata_join_sql("t", "merchant_meta")
        where_clauses = []
        params = []

        if profile and profile != "household":
            where_clauses.append("t.profile_id = ?")
            params.append(profile)
        if month:
            where_clauses.append("t.date LIKE ?")
            params.append(month + "%")
        else:
            if start_date:
                where_clauses.append("t.date >= ?")
                params.append(start_date)
            if end_date:
                where_clauses.append("t.date <= ?")
                params.append(end_date)
        if category:
            where_clauses.append(
                """(t.category = ? OR EXISTS (
                    SELECT 1 FROM transaction_splits s
                    WHERE s.transaction_id = t.id AND s.category = ?
                ))"""
            )
            params.extend([category, category])
        if account:
            where_clauses.append("t.account_name = ?")
            params.append(account)
        if reviewed is not None:
            where_clauses.append("COALESCE(t.reviewed, 0) = ?")
            params.append(1 if reviewed else 0)
        if search:
            escaped = _escape_like(search.upper())
            where_clauses.append(
                """(
                    UPPER(COALESCE(t.description, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.raw_description, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.merchant_name, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(merchant_alias.display_name, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(merchant_meta.clean_name, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.category, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.notes, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.tags, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.account_name, '')) LIKE ? ESCAPE '\\'
                )"""
            )
            params.extend([f"%{escaped}%"] * 9)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Count query
        count_sql = f"""SELECT COUNT(*)
                        FROM {tx_source}
                        {alias_join_sql}
                        {metadata_join_sql}
                        {where_sql}"""
        total_count = c.execute(count_sql, params).fetchone()[0]

        sort_token = str(sort or "date_desc").strip().lower()
        order_by = {
            "date_asc": "t.date ASC, t.id ASC",
            "amount_desc": "ABS(t.amount) DESC, t.date DESC, t.id DESC",
            "amount_asc": "ABS(t.amount) ASC, t.date DESC, t.id DESC",
        }.get(sort_token, "t.date DESC, t.id DESC")

        # Data query with pagination
        data_sql = f"""SELECT t.id as original_id, t.profile_id as profile, t.date, t.description,
                              t.raw_description, t.amount, t.category, t.original_category,
                              t.categorization_source, t.confidence, t.transaction_type as type,
                              t.counterparty_name, t.counterparty_type, t.teller_category,
                              t.account_name, t.account_type, t.merchant_name, t.merchant_domain,
                              t.merchant_industry, t.merchant_city, t.merchant_state,
                              t.merchant_key, t.merchant_source, t.merchant_confidence, t.merchant_kind,
                              t.enriched, t.is_excluded, t.expense_type, t.updated_at,
                              COALESCE(t.notes, '') as notes,
                              COALESCE(t.tags, '') as tags,
                              COALESCE(t.reviewed, 0) as reviewed,
                              {merchant_key_sql} AS merchant_display_key,
                              COALESCE(NULLIF(merchant_alias.display_name, ''), NULLIF(merchant_meta.clean_name, ''), {merchant_label_sql}) AS merchant_display_name,
                              COALESCE(NULLIF(merchant_meta.industry, ''), NULLIF(t.merchant_industry, ''), '') AS merchant_display_industry,
                              {merchant_source_sql} AS merchant_display_source
                       FROM {tx_source}
                       {alias_join_sql}
                       {metadata_join_sql}
                       {where_sql}
                       ORDER BY {order_by}
                       LIMIT ? OFFSET ?"""
        data_params = params + [limit, offset]
        rows = c.execute(data_sql, data_params).fetchall()
        transactions = dicts_from_rows(rows)
        for tx in transactions:
            tx["enriched"] = bool(tx.get("enriched", 0))
            tx["is_excluded"] = bool(tx.get("is_excluded", 0))
            tx["reviewed"] = bool(tx.get("reviewed", 0))
            tx["tags"] = [tag.strip() for tag in (tx.get("tags") or "").split(",") if tag.strip()]

        return {
            "data": transactions,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def bulk_mark_transactions_reviewed(
    month: str | None = None,
    category: str | None = None,
    account: str | None = None,
    search: str | None = None,
    reviewed: bool | None = None,
    profile: str | None = None,
    target_reviewed: bool = True,
    conn=None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Mark transactions matching the current filter set as reviewed/unreviewed."""
    def _update(c):
        tx_source = _transactions_source(alias="t")
        alias_join_sql = _merchant_alias_join_sql("t", "merchant_alias")
        metadata_join_sql = _merchant_metadata_join_sql("t", "merchant_meta")
        where_clauses = []
        params = []

        if profile and profile != "household":
            where_clauses.append("t.profile_id = ?")
            params.append(profile)
        if month:
            where_clauses.append("t.date LIKE ?")
            params.append(month + "%")
        else:
            if start_date:
                where_clauses.append("t.date >= ?")
                params.append(start_date)
            if end_date:
                where_clauses.append("t.date <= ?")
                params.append(end_date)
        if category:
            where_clauses.append(
                """(t.category = ? OR EXISTS (
                    SELECT 1 FROM transaction_splits s
                    WHERE s.transaction_id = t.id AND s.category = ?
                ))"""
            )
            params.extend([category, category])
        if account:
            where_clauses.append("t.account_name = ?")
            params.append(account)
        if reviewed is not None:
            where_clauses.append("COALESCE(t.reviewed, 0) = ?")
            params.append(1 if reviewed else 0)
        if search:
            escaped = _escape_like(search.upper())
            where_clauses.append(
                """(
                    UPPER(COALESCE(t.description, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.raw_description, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.merchant_name, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(merchant_alias.display_name, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(merchant_meta.clean_name, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.category, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.notes, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.tags, '')) LIKE ? ESCAPE '\\'
                    OR UPPER(COALESCE(t.account_name, '')) LIKE ? ESCAPE '\\'
                )"""
            )
            params.extend([f"%{escaped}%"] * 9)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        id_rows = c.execute(
            f"""SELECT t.id
                FROM {tx_source}
                {alias_join_sql}
                {metadata_join_sql}
                {where_sql}""",
            params,
        ).fetchall()
        ids = [row[0] for row in id_rows]
        if not ids:
            return {"updated_count": 0, "matched_count": 0}

        placeholders = ",".join("?" * len(ids))
        cur = c.execute(
            f"""UPDATE transactions
                SET reviewed = ?, updated_at = datetime('now')
                WHERE id IN ({placeholders})""",
            [1 if target_reviewed else 0, *ids],
        )
        return {"updated_count": cur.rowcount or 0, "matched_count": len(ids)}

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_summary_data(profile: str | None = None, conn=None) -> dict:
    """Compute summary statistics using SQL-level aggregation."""
    def _query(c):
        tx_source = _transactions_source()
        profile_clause = ""
        profile_params = []
        is_household = not profile or profile == "household"
        if not is_household:
            profile_clause = " AND profile_id = ?"
            profile_params = [profile]

        # Build the non-spending categories placeholders
        non_spending_ph = ",".join("?" * len(NON_SPENDING_CATEGORIES))
        transfer_ph = ",".join("?" * len(TRANSFER_CATEGORIES))

        # Transfer exclusion clause for expenses:
        # Household view: exclude transfer_internal AND transfer_household
        # Individual view: exclude only transfer_internal
        if is_household:
            transfer_excl = " AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))"
        else:
            transfer_excl = " AND (expense_type IS NULL OR expense_type != 'transfer_internal')"

        # Transfer exclusion clause for income (receiving side):
        # Same logic — internal transfers showing as income should be excluded
        if is_household:
            income_transfer_excl = " AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))"
        else:
            income_transfer_excl = " AND (expense_type IS NULL OR expense_type != 'transfer_internal')"

        # Income: category='Income' AND amount > 0
        income = c.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM {tx_source} WHERE category = 'Income' AND amount > 0{income_transfer_excl}{profile_clause}",
            profile_params,
        ).fetchone()[0]

        # Expenses: amount < 0 AND category NOT IN non_spending
        expenses = c.execute(
            f"SELECT COALESCE(SUM(ABS(amount)), 0) FROM {tx_source} WHERE amount < 0 AND category NOT IN ({non_spending_ph}){transfer_excl}{profile_clause}",
            list(NON_SPENDING_CATEGORIES) + profile_params,
        ).fetchone()[0]

        # Credits/refunds: explicit category plus temporary compatibility for
        # positive normal-category rows until historical data is migrated.
        refunds = c.execute(
            f"""SELECT COALESCE(SUM(amount), 0)
                FROM {tx_source}
                WHERE amount > 0
                  AND (
                    category = ?
                    OR (category NOT IN ({non_spending_ph}) AND category != 'Income')
                  ){transfer_excl}{profile_clause}""",
            [CREDITS_REFUNDS_CATEGORY] + list(NON_SPENDING_CATEGORIES) + profile_params,
        ).fetchone()[0]
        # Savings: category = 'Savings Transfer'
        savings = c.execute(
            f"SELECT COALESCE(SUM(ABS(amount)), 0) FROM {tx_source} WHERE category = 'Savings Transfer'{profile_clause}",
            profile_params,
        ).fetchone()[0]

        # Transaction counts
        tx_count = c.execute(
            f"SELECT COUNT(*) FROM {tx_source} WHERE 1=1{profile_clause}",
            profile_params,
        ).fetchone()[0]

        enriched_count = c.execute(
            f"SELECT COUNT(*) FROM {tx_source} WHERE enriched = 1{profile_clause}",
            profile_params,
        ).fetchone()[0]

        # Account balances
        acct_profile_clause = ""
        acct_params = []
        if not is_household:
            acct_profile_clause = " AND profile_id = ?"
            acct_params = [profile]

        total_assets, total_owed = _account_balance_totals(c, profile=profile)

        # Last updated
        last_row = c.execute("SELECT MAX(last_synced_at) FROM accounts").fetchone()
        last_updated = last_row[0] if last_row and last_row[0] else None

        # CC Repaid: sum of Credit Card Payment outflows (transfer_cc_payment)
        cc_repaid = c.execute(
            f"SELECT COALESCE(SUM(ABS(amount)), 0) FROM {tx_source} WHERE category = 'Credit Card Payment' AND amount < 0{profile_clause}",
            profile_params,
        ).fetchone()[0]

        # External transfers (Zelle/Venmo to other people) — included in net flow.
        # In household view, household transfers are hidden by design; in an
        # individual view they may still be useful as personal inflow/outflow.
        if is_household:
            personal_transfer_type_clause = "expense_type = 'transfer_external'"
        else:
            personal_transfer_type_clause = "expense_type IN ('transfer_external', 'transfer_household')"

        external_transfers = c.execute(
            f"SELECT COALESCE(SUM(ABS(amount)), 0) FROM {tx_source} WHERE {personal_transfer_type_clause} AND amount < 0{profile_clause}",
            profile_params,
        ).fetchone()[0]

        incoming_transfers = c.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM {tx_source} WHERE {personal_transfer_type_clause} AND amount > 0{profile_clause}",
            profile_params,
        ).fetchone()[0]

        cash_deposits = c.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM {tx_source} WHERE category = 'Cash Deposit' AND amount > 0{profile_clause}",
            profile_params,
        ).fetchone()[0]
        cash_withdrawals = c.execute(
            f"SELECT COALESCE(SUM(ABS(amount)), 0) FROM {tx_source} WHERE category = 'Cash Withdrawal' AND amount < 0{profile_clause}",
            profile_params,
        ).fetchone()[0]
        investment_inflows = c.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM {tx_source} WHERE category = 'Investment Transfer' AND amount > 0{profile_clause}",
            profile_params,
        ).fetchone()[0]
        investment_outflows = c.execute(
            f"SELECT COALESCE(SUM(ABS(amount)), 0) FROM {tx_source} WHERE category = 'Investment Transfer' AND amount < 0{profile_clause}",
            profile_params,
        ).fetchone()[0]

        net_spending = expenses - refunds
        net_flow = (
            income
            + refunds
            + incoming_transfers
            + cash_deposits
            + investment_inflows
            - expenses
            - external_transfers
            - cash_withdrawals
            - investment_outflows
        )
        return {
            "income": round(income, 2),
            "expenses": round(expenses, 2),
            "refunds": round(refunds, 2),
            "credits_refunds": round(refunds, 2),
            "incoming_transfers": round(incoming_transfers, 2),
            "cash_deposits": round(cash_deposits, 2),
            "cash_withdrawals": round(cash_withdrawals, 2),
            "investment_inflows": round(investment_inflows, 2),
            "investment_outflows": round(investment_outflows, 2),
            "net_spending": round(net_spending, 2),
            "savings": round(savings, 2),
            "net_flow": round(net_flow, 2),
            "savings_rate": round(savings / income * 100, 1) if income > 0 else 0,
            "total_assets": round(total_assets, 2),
            "total_owed": round(total_owed, 2),
            "net_worth": round(total_assets - total_owed, 2),
            "last_updated": last_updated,
            "transaction_count": tx_count,
            "enriched_count": enriched_count,
            "cc_repaid": round(cc_repaid, 2),
            "external_transfers": round(external_transfers, 2),
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_monthly_analytics_data(profile: str | None = None, conn=None) -> list[dict]:
    """
    Compute monthly income/expense/refund/savings aggregation using SQL GROUP BY.
    Transfer sub-classification aware: excludes internal (and household in
    household view) transfers from expense/refund totals.
    """
    def _query(c):
        tx_source = _transactions_source()
        profile_clause = ""
        profile_params = []
        is_household = not profile or profile == "household"
        if not is_household:
            profile_clause = " AND profile_id = ?"
            profile_params = [profile]

        non_spending_ph = ",".join("?" * len(NON_SPENDING_CATEGORIES))

        # Transfer exclusion CASE conditions embedded in conditional aggregation.
        # A transaction is "transfer-excluded" if its expense_type should be hidden
        # in this view context.
        if is_household:
            # Household: exclude transfer_internal and transfer_household
            transfer_ok = "AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))"
        else:
            # Individual: exclude only transfer_internal
            transfer_ok = "AND (expense_type IS NULL OR expense_type != 'transfer_internal')"

        # One query using conditional aggregation
        sql = f"""
            SELECT
                SUBSTR(date, 1, 7) as month,
                COALESCE(SUM(CASE WHEN category = 'Income' AND amount > 0 {transfer_ok} THEN amount ELSE 0 END), 0) as income,
                COALESCE(SUM(CASE WHEN amount < 0 AND category NOT IN ({non_spending_ph}) {transfer_ok} THEN ABS(amount) ELSE 0 END), 0) as expenses,
                COALESCE(SUM(CASE WHEN amount > 0 AND (category = ? OR (category NOT IN ({non_spending_ph}) AND category != 'Income')) {transfer_ok} THEN amount ELSE 0 END), 0) as refunds,
                COALESCE(SUM(CASE WHEN category = 'Savings Transfer' THEN ABS(amount) ELSE 0 END), 0) as savings
            FROM {tx_source}
            WHERE LENGTH(date) >= 7{profile_clause}
            GROUP BY SUBSTR(date, 1, 7)
            ORDER BY month ASC
        """
        params = list(NON_SPENDING_CATEGORIES) + [CREDITS_REFUNDS_CATEGORY] + list(NON_SPENDING_CATEGORIES) + profile_params
        rows = c.execute(sql, params).fetchall()

        result = []
        for row in rows:
            inc = row[1]
            exp = row[2]
            ref = row[3]
            sav = row[4]
            result.append({
                "month": row[0],
                "income": round(inc, 2),
                "expenses": round(exp, 2),
                "refunds": round(ref, 2),
                "savings": round(sav, 2),
                "net": round(inc - exp + ref, 2),
            })

        # Compute CC repaid and personal transfer inflow/outflow per month.
        if is_household:
            personal_transfer_type_clause = "expense_type = 'transfer_external'"
        else:
            personal_transfer_type_clause = "expense_type IN ('transfer_external', 'transfer_household')"

        cc_sql = f"""
            SELECT SUBSTR(date, 1, 7) as month,
                   COALESCE(SUM(ABS(amount)), 0) as cc_repaid
            FROM {tx_source}
            WHERE category = 'Credit Card Payment' AND amount < 0
              AND LENGTH(date) >= 7{profile_clause}
            GROUP BY SUBSTR(date, 1, 7)
        """
        cc_rows = c.execute(cc_sql, profile_params).fetchall()
        cc_by_month = {row[0]: row[1] for row in cc_rows}

        ext_sql = f"""
            SELECT SUBSTR(date, 1, 7) as month,
                   COALESCE(SUM(ABS(amount)), 0) as ext_transfers
            FROM {tx_source}
            WHERE {personal_transfer_type_clause} AND amount < 0
              AND LENGTH(date) >= 7{profile_clause}
            GROUP BY SUBSTR(date, 1, 7)
        """
        ext_rows = c.execute(ext_sql, profile_params).fetchall()
        ext_by_month = {row[0]: row[1] for row in ext_rows}

        incoming_sql = f"""
            SELECT SUBSTR(date, 1, 7) as month,
                   COALESCE(SUM(amount), 0) as incoming_transfers
            FROM {tx_source}
            WHERE {personal_transfer_type_clause} AND amount > 0
              AND LENGTH(date) >= 7{profile_clause}
            GROUP BY SUBSTR(date, 1, 7)
        """
        incoming_rows = c.execute(incoming_sql, profile_params).fetchall()
        incoming_by_month = {row[0]: row[1] for row in incoming_rows}

        direct_sql = f"""
            SELECT SUBSTR(date, 1, 7) as month,
                   COALESCE(SUM(CASE WHEN category = 'Cash Deposit' AND amount > 0 THEN amount ELSE 0 END), 0) AS cash_deposits,
                   COALESCE(SUM(CASE WHEN category = 'Cash Withdrawal' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS cash_withdrawals,
                   COALESCE(SUM(CASE WHEN category = 'Investment Transfer' AND amount > 0 THEN amount ELSE 0 END), 0) AS investment_inflows,
                   COALESCE(SUM(CASE WHEN category = 'Investment Transfer' AND amount < 0 THEN ABS(amount) ELSE 0 END), 0) AS investment_outflows
            FROM {tx_source}
            WHERE LENGTH(date) >= 7{profile_clause}
            GROUP BY SUBSTR(date, 1, 7)
        """
        direct_rows = c.execute(direct_sql, profile_params).fetchall()
        direct_by_month = {
            row[0]: {
                "cash_deposits": row[1],
                "cash_withdrawals": row[2],
                "investment_inflows": row[3],
                "investment_outflows": row[4],
            }
            for row in direct_rows
        }

        for entry in result:
            m = entry["month"]
            direct = direct_by_month.get(m, {})
            entry["cc_repaid"] = round(cc_by_month.get(m, 0), 2)
            entry["external_transfers"] = round(ext_by_month.get(m, 0), 2)
            entry["incoming_transfers"] = round(incoming_by_month.get(m, 0), 2)
            entry["cash_deposits"] = round(direct.get("cash_deposits", 0), 2)
            entry["cash_withdrawals"] = round(direct.get("cash_withdrawals", 0), 2)
            entry["investment_inflows"] = round(direct.get("investment_inflows", 0), 2)
            entry["investment_outflows"] = round(direct.get("investment_outflows", 0), 2)
            entry["credits_refunds"] = entry["refunds"]
            entry["net"] = round(
                entry["income"]
                + entry["refunds"]
                + entry["incoming_transfers"]
                + entry["cash_deposits"]
                + entry["investment_inflows"]
                - entry["expenses"]
                - entry["external_transfers"]
                - entry["cash_withdrawals"]
                - entry["investment_outflows"],
                2,
            )

        return result

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _build_reconstructed_net_worth_state(c, profile: str | None = None) -> dict:
    """
    Reconstruct a historical net-worth timeline from transaction deltas, anchored
    to the current live account balances.

    This remains an approximation when balances move without transactions
    (for example investment market changes), but it gives us exact date anchors
    instead of relying on the dashboard chart's sampled points.
    """
    from datetime import date as dt_date, timedelta

    total_assets, total_owed = _account_balance_totals(c, profile=profile)
    current_net_worth = total_assets - total_owed

    tx_profile_clause = ""
    tx_params = []
    if profile and profile != "household":
        tx_profile_clause = " AND profile_id = ?"
        tx_params = [profile]
    tx_source = _transactions_source()

    daily_rows = c.execute(
        f"""SELECT SUBSTR(date, 1, 10) as day, SUM(amount) as net
            FROM {tx_source}
            WHERE LENGTH(date) >= 10{tx_profile_clause}
            GROUP BY SUBSTR(date, 1, 10)
            ORDER BY day ASC""",
        tx_params,
    ).fetchall()

    if not daily_rows:
        return {
            "current_net_worth": round(current_net_worth, 2),
            "first_date": None,
            "last_date": None,
            "starting_net_worth": round(current_net_worth, 2),
            "cumulative": {},
        }

    daily_net = {row[0]: row[1] for row in daily_rows}
    first_date = dt_date.fromisoformat(daily_rows[0][0])
    last_date = dt_date.fromisoformat(daily_rows[-1][0])

    cumulative = {}
    running = 0.0
    d = first_date
    while d <= last_date:
        running += daily_net.get(d.isoformat(), 0.0)
        cumulative[d] = running
        d += timedelta(days=1)

    total_cumulative = cumulative.get(last_date, 0.0)
    starting_net_worth = current_net_worth - total_cumulative

    return {
        "current_net_worth": round(current_net_worth, 2),
        "first_date": first_date,
        "last_date": last_date,
        "starting_net_worth": starting_net_worth,
        "cumulative": cumulative,
    }


def _reconstructed_net_worth_on(state: dict, target_date) -> float | None:
    """
    Return reconstructed net worth on a specific date.

    Dates before the earliest transaction are unknown, so we return None
    instead of inventing a baseline.
    """
    first_date = state.get("first_date")
    last_date = state.get("last_date")
    cumulative = state.get("cumulative") or {}
    if first_date is None or last_date is None or not cumulative:
        return None
    if target_date < first_date:
        return None

    effective_date = min(target_date, last_date)
    cumulative_value = cumulative.get(effective_date)
    if cumulative_value is None:
        return None

    return round(state["starting_net_worth"] + cumulative_value, 2)


def get_net_worth_delta_metrics(profile: str | None = None, conn=None) -> dict:
    """
    Compute exact-anchor net-worth deltas for the dashboard hero card.

    - MoM: current net worth minus previous month-end net worth
    - YTD: current net worth minus previous year-end net worth

    These values intentionally use exact anchor dates rather than the chart's
    biweekly sampling so the pills reflect the label semantics more closely.
    """
    from datetime import date as dt_date, timedelta

    def _query(c):
        history_profile = _profile_id(profile)
        history_rows = c.execute(
            """SELECT date, net_worth
               FROM net_worth_history
               WHERE profile_id = ?
               ORDER BY date ASC""",
            (history_profile,),
        ).fetchall()
        if history_rows:
            current_net_worth = float(history_rows[-1][1] or 0)
            today = dt_date.today()
            previous_month_end = today.replace(day=1) - timedelta(days=1)
            previous_year_end = dt_date(today.year - 1, 12, 31)

            def latest_on_or_before(target):
                value = None
                for row in history_rows:
                    if row[0] <= target.isoformat():
                        value = float(row[1] or 0)
                    else:
                        break
                return value

            previous_month_net_worth = latest_on_or_before(previous_month_end)
            previous_year_end_net_worth = latest_on_or_before(previous_year_end)
            return {
                "mom": round(current_net_worth - previous_month_net_worth, 2) if previous_month_net_worth is not None else None,
                "ytd": round(current_net_worth - previous_year_end_net_worth, 2) if previous_year_end_net_worth is not None else None,
            }

        state = _build_reconstructed_net_worth_state(c, profile=profile)
        current_net_worth = state["current_net_worth"]
        if state["first_date"] is None:
            return {"mom": None, "ytd": None}

        today = dt_date.today()
        previous_month_end = today.replace(day=1) - timedelta(days=1)
        previous_year_end = dt_date(today.year - 1, 12, 31)

        previous_month_net_worth = _reconstructed_net_worth_on(state, previous_month_end)
        previous_year_end_net_worth = _reconstructed_net_worth_on(state, previous_year_end)

        return {
            "mom": round(current_net_worth - previous_month_net_worth, 2) if previous_month_net_worth is not None else None,
            "ytd": round(current_net_worth - previous_year_end_net_worth, 2) if previous_year_end_net_worth is not None else None,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_category_analytics_data(
    month: str | None = None,
    profile: str | None = None,
    conn=None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """
    Compute per-category spending breakdown using SQL GROUP BY.
    Transfer sub-classification aware: excludes internal (and household in
    household view) transfers from the breakdown.

    Time window: pass `month` (YYYY-MM) for a single month, or `start_date` /
    `end_date` (YYYY-MM-DD) for arbitrary ranges. If both are given, `month`
    takes precedence.
    """
    def _query(c):
        tx_source = _transactions_source()
        where_clauses = []
        params = []
        is_household = not profile or profile == "household"

        if not is_household:
            where_clauses.append("profile_id = ?")
            params.append(profile)
        if month:
            where_clauses.append("date LIKE ?")
            params.append(month + "%")
        else:
            if start_date:
                where_clauses.append("date >= ?")
                params.append(start_date)
            if end_date:
                where_clauses.append("date <= ?")
                params.append(end_date)

        split_count = c.execute("SELECT COUNT(*) FROM transaction_splits").fetchone()[0]
        if split_count:
            alloc_where = []
            alloc_params = []
            if not is_household:
                alloc_where.append("t.profile_id = ?")
                alloc_params.append(profile)
            if month:
                alloc_where.append("t.date LIKE ?")
                alloc_params.append(month + "%")
            else:
                if start_date:
                    alloc_where.append("t.date >= ?")
                    alloc_params.append(start_date)
                if end_date:
                    alloc_where.append("t.date <= ?")
                    alloc_params.append(end_date)
            if is_household:
                alloc_where.append("(t.expense_type IS NULL OR t.expense_type NOT IN ('transfer_internal', 'transfer_household'))")
            else:
                alloc_where.append("(t.expense_type IS NULL OR t.expense_type != 'transfer_internal')")
            alloc_where_sql = (" AND " + " AND ".join(alloc_where)) if alloc_where else ""
            non_spending_ph = ",".join("?" * len(NON_SPENDING_CATEGORIES))
            allocation_sql = f"""
                WITH allocated AS (
                    SELECT s.category,
                           CASE WHEN t.amount < 0 THEN -ABS(s.amount) ELSE ABS(s.amount) END AS amount,
                           t.expense_type
                    FROM transaction_splits s
                    JOIN transactions_visible t ON t.id = s.transaction_id
                    WHERE 1=1{alloc_where_sql}
                    UNION ALL
                    SELECT t.category, t.amount, t.expense_type
                    FROM transactions_visible t
                    WHERE NOT EXISTS (
                        SELECT 1 FROM transaction_splits s WHERE s.transaction_id = t.id
                    ){alloc_where_sql}
                )
                SELECT category,
                       COALESCE(SUM(CASE WHEN amount < 0 AND category NOT IN ({non_spending_ph}) THEN ABS(amount) ELSE 0 END), 0) AS gross,
                       COALESCE(SUM(CASE WHEN amount > 0 AND category NOT IN ({non_spending_ph}) AND category != 'Income' THEN amount ELSE 0 END), 0) AS refunds
                FROM allocated
                GROUP BY category
            """
            allocation_params = alloc_params + alloc_params + list(NON_SPENDING_CATEGORIES) + list(NON_SPENDING_CATEGORIES)
            rows = c.execute(allocation_sql, allocation_params).fetchall()
            expense_type_map = {
                row[0]: row[1]
                for row in c.execute("SELECT name, expense_type FROM categories WHERE is_active = 1").fetchall()
            }
            net_cat = {}
            gross_by_cat = {}
            refund_by_cat = {}
            for row in rows:
                cat = row[0]
                gross = float(row[1] or 0)
                refunds = float(row[2] or 0)
                net_val = gross - refunds
                gross_by_cat[cat] = gross
                refund_by_cat[cat] = refunds
                if net_val > 0:
                    net_cat[cat] = net_val
            total = sum(net_cat.values())
            result = []
            for cat, amt in sorted(net_cat.items(), key=lambda x: -x[1]):
                result.append({
                    "category": cat,
                    "total": round(amt, 2),
                    "gross": round(gross_by_cat.get(cat, 0), 2),
                    "refunds": round(refund_by_cat.get(cat, 0), 2),
                    "percent": round(amt / total * 100, 1) if total > 0 else 0,
                    "expense_type": expense_type_map.get(cat, "variable"),
                })

            transfer_where = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
            savings_total = c.execute(
                f"""SELECT COALESCE(SUM(ABS(amount)), 0)
                    FROM {tx_source}
                    WHERE category = 'Savings Transfer' AND amount < 0{transfer_where}""",
                params,
            ).fetchone()[0]
            personal_transfer_total = c.execute(
                f"""SELECT COALESCE(SUM(ABS(amount)), 0)
                    FROM {tx_source}
                    WHERE category = 'Personal Transfer' AND amount < 0{transfer_where}""",
                params,
            ).fetchone()[0]
            return {
                "categories": result,
                "savings_transfer_total": round(savings_total, 2),
                "personal_transfer_total": round(personal_transfer_total, 2),
            }

        # Transfer exclusion
        if is_household:
            where_clauses.append("(expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))")
        else:
            where_clauses.append("(expense_type IS NULL OR expense_type != 'transfer_internal')")

        where_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""

        non_spending_ph = ",".join("?" * len(NON_SPENDING_CATEGORIES))

        # Gross expenses by category
        expense_sql = f"""
            SELECT category, SUM(ABS(amount)) as total
            FROM {tx_source}
            WHERE amount < 0 AND category NOT IN ({non_spending_ph}){where_sql}
            GROUP BY category
        """
        expense_params = list(NON_SPENDING_CATEGORIES) + params
        expense_rows = c.execute(expense_sql, expense_params).fetchall()
        expense_by_cat = {row[0]: row[1] for row in expense_rows}

        # Refunds by category
        refund_sql = f"""
            SELECT category, SUM(amount) as total
            FROM {tx_source}
            WHERE amount > 0 AND category NOT IN ({non_spending_ph}) AND category != 'Income'{where_sql}
            GROUP BY category
        """
        refund_params = list(NON_SPENDING_CATEGORIES) + params
        refund_rows = c.execute(refund_sql, refund_params).fetchall()
        refund_by_cat = {row[0]: row[1] for row in refund_rows}

        # Compute net per category
        all_cats = set(list(expense_by_cat.keys()) + list(refund_by_cat.keys()))
        net_cat = {}
        for cat in all_cats:
            gross = expense_by_cat.get(cat, 0)
            refs = refund_by_cat.get(cat, 0)
            net_val = gross - refs
            if net_val > 0:
                net_cat[cat] = net_val

        # Load expense_type mapping
        et_rows = c.execute("SELECT name, expense_type FROM categories WHERE is_active = 1").fetchall()
        expense_type_map = {row[0]: row[1] for row in et_rows}

        total = sum(net_cat.values())
        result = []
        for cat, amt in sorted(net_cat.items(), key=lambda x: -x[1]):
            result.append({
                "category": cat,
                "total": round(amt, 2),
                "gross": round(expense_by_cat.get(cat, 0), 2),
                "refunds": round(refund_by_cat.get(cat, 0), 2),
                "percent": round(amt / total * 100, 1) if total > 0 else 0,
                "expense_type": expense_type_map.get(cat, "variable"),
            })

        # Compute transfer totals using the same profile + expense_type filtering
        savings_total = c.execute(
            f"""SELECT COALESCE(SUM(ABS(amount)), 0)
                FROM {tx_source}
                WHERE category = 'Savings Transfer' AND amount < 0{" AND " + " AND ".join(where_clauses) if where_clauses else ""}""",
            params,
        ).fetchone()[0]

        personal_transfer_total = c.execute(
            f"""SELECT COALESCE(SUM(ABS(amount)), 0)
                FROM {tx_source}
                WHERE category = 'Personal Transfer' AND amount < 0{" AND " + " AND ".join(where_clauses) if where_clauses else ""}""",
            params,
        ).fetchone()[0]

        return {
            "categories": result,
            "savings_transfer_total": round(savings_total, 2),
            "personal_transfer_total": round(personal_transfer_total, 2),
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_plan_snapshot_data(
    profile: str | None = None,
    conn=None,
    as_of: str | date | datetime | None = None,
) -> dict:
    """Return compact planning metrics for the dashboard."""
    def _query(c):
        today = _resolve_as_of_date(as_of)
        current_month = today.strftime("%Y-%m")
        days_remaining_this_month = max(0, calendar.monthrange(today.year, today.month)[1] - today.day)
        categories = get_category_analytics_data(month=current_month, profile=profile, conn=c)["categories"]
        budgets = get_category_budgets(profile=profile, conn=c)
        goals = get_goals(profile=profile, conn=c)
        monthly = get_monthly_analytics_data(profile=profile, conn=c)
        scheduled = get_scheduled_transactions_data(days=max(days_remaining_this_month, 1), profile=profile, conn=c)
        current_month_row = next((row for row in monthly if row.get("month") == current_month), None) or {}
        prior_income_months = [
            float(row.get("income") or 0)
            for row in sorted(monthly, key=lambda row: row.get("month") or "", reverse=True)
            if (row.get("month") or "") < current_month and float(row.get("income") or 0) > 0
        ][:3]
        average_income = sum(prior_income_months) / len(prior_income_months) if prior_income_months else 0.0
        current_income = float(current_month_row.get("income") or 0)
        planned_income = max(current_income, average_income)

        budget_by_cat = {item["category"]: float(item.get("amount") or 0) for item in budgets}
        spent_by_cat = {item["category"]: float(item.get("total") or 0) for item in categories}
        total_budget = sum(v for v in budget_by_cat.values() if v > 0)
        budgeted_spent = sum(spent_by_cat.get(cat, 0) for cat in budget_by_cat)
        mandatory_spend = sum(float(item.get("total") or 0) for item in categories if item.get("expense_type") == "fixed")
        variable_spend = sum(float(item.get("total") or 0) for item in categories if item.get("expense_type") == "variable")
        mandatory_remaining = sum(
            float(item.get("amount") or 0)
            for item in scheduled.get("items", [])
            if str(item.get("next_date") or "").startswith(current_month)
            and bool(item.get("confirmed", item.get("source") != "recurring_candidate"))
        )
        mandatory_projected = mandatory_spend + mandatory_remaining
        tx_source = _transactions_source()
        profile_clause = ""
        profile_params = []
        if profile and profile != "household":
            profile_clause = " AND profile_id = ?"
            profile_params = [profile]
        save_first_actual = c.execute(
            f"""SELECT COALESCE(SUM(ABS(amount)), 0)
                FROM {tx_source}
                WHERE category = 'Savings Transfer'
                  AND amount < 0
                  AND date LIKE ?{profile_clause}""",
            [current_month + "%", *profile_params],
        ).fetchone()[0]
        save_first_rate = 0.20
        save_first_target = planned_income * save_first_rate
        safe_to_spend_limit = planned_income - save_first_target - mandatory_projected
        safe_to_spend = safe_to_spend_limit - variable_spend
        over_count = sum(1 for cat, amount in budget_by_cat.items() if amount > 0 and spent_by_cat.get(cat, 0) > amount)

        return {
            "month": current_month,
            "total_budget": round(total_budget, 2),
            "budgeted_spent": round(budgeted_spent, 2),
            "remaining": round(total_budget - budgeted_spent, 2),
            "planned_income": round(planned_income, 2),
            "income_actual": round(current_income, 2),
            "income_basis": "actual" if current_income >= average_income else "trailing_average",
            "save_first_rate": save_first_rate,
            "save_first_target": round(save_first_target, 2),
            "save_first_actual": round(float(save_first_actual or 0), 2),
            "save_first_remaining": round(max(save_first_target - float(save_first_actual or 0), 0), 2),
            "mandatory_spend": round(mandatory_spend, 2),
            "mandatory_remaining": round(mandatory_remaining, 2),
            "mandatory_projected": round(mandatory_projected, 2),
            "variable_spend": round(variable_spend, 2),
            "safe_to_spend_limit": round(safe_to_spend_limit, 2),
            "safe_to_spend_spent": round(variable_spend, 2),
            "safe_to_spend": round(safe_to_spend, 2),
            "active_goal_count": len(goals),
            "over_count": over_count,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_review_queue_data(profile: str | None = None, conn=None) -> dict:
    """Return compact unreviewed transaction metrics for dashboard and exports."""
    def _query(c):
        where = ["COALESCE(reviewed, 0) = 0"]
        params = []
        if profile and profile != "household":
            where.append("profile_id = ?")
            params.append(profile)
        where_sql = " AND ".join(where)
        row = c.execute(
            f"""SELECT COUNT(*) AS count,
                       COALESCE(SUM(CASE
                           WHEN amount < 0
                            AND category NOT IN ({','.join('?' * len(NON_SPENDING_CATEGORIES))})
                            AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))
                           THEN ABS(amount)
                           ELSE 0
                       END), 0) AS spending
                FROM transactions_visible
                WHERE {where_sql}""",
            list(NON_SPENDING_CATEGORIES) + params,
        ).fetchone()
        latest = c.execute(
            f"""SELECT date FROM transactions_visible
                WHERE {where_sql}
                ORDER BY date DESC
                LIMIT 1""",
            params,
        ).fetchone()
        return {
            "unreviewed_count": int(row[0] or 0),
            "unreviewed_spending": round(float(row[1] or 0), 2),
            "latest_unreviewed_date": latest[0] if latest else None,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _profile_id(profile: str | None) -> str:
    return profile if profile and profile != "household" else "household"


def get_goals(profile: str | None = None, conn=None) -> list[dict]:
    profile_id = _profile_id(profile)

    def _with_projection(goal: dict) -> dict:
        target = max(float(goal.get("target_amount") or 0), 0)
        current = max(float(goal.get("current_amount") or 0), 0)
        gap = max(target - current, 0)
        target_date = _parse_date_only(goal.get("target_date"))
        created = _parse_date_only(goal.get("created_at"))
        today = date.today()
        months_remaining = 0
        if target_date and target_date > today:
            months_remaining = max(1, round((target_date - today).days / 30.4375))
        required_monthly = gap / months_remaining if gap > 0 and months_remaining > 0 else 0

        months_elapsed = 0
        average_monthly_progress = 0
        projected_completion_date = None
        if created and created < today and current > 0:
            months_elapsed = max(1, round((today - created).days / 30.4375))
            average_monthly_progress = current / months_elapsed
            if gap > 0 and average_monthly_progress > 0:
                months_to_finish = max(1, round(gap / average_monthly_progress))
                projected_completion_date = (today + timedelta(days=round(months_to_finish * 30.4375))).isoformat()

        if gap <= 0:
            status = "funded"
        elif not target_date:
            status = "needs_target_date"
        elif average_monthly_progress <= 0:
            status = "needs_progress"
        elif average_monthly_progress + 0.01 >= required_monthly:
            status = "on_track"
        else:
            status = "behind"

        projection = {
            "gap": round(gap, 2),
            "progress_percent": round((current / target) * 100, 1) if target > 0 else 0,
            "months_remaining": months_remaining,
            "required_monthly": round(required_monthly, 2),
            "average_monthly_progress": round(average_monthly_progress, 2),
            "projected_completion_date": projected_completion_date,
            "status": status,
        }
        return {**goal, "projection": projection}

    def _query(c):
        rows = c.execute(
            """SELECT id, profile_id, name, goal_type, target_amount, current_amount,
                      target_date, linked_category, linked_account_id, is_active,
                      created_at, updated_at
               FROM goals
               WHERE profile_id = ? AND is_active = 1
               ORDER BY COALESCE(target_date, '9999-99-99'), updated_at DESC""",
            (profile_id,),
        ).fetchall()
        return [_with_projection(row) for row in dicts_from_rows(rows)]

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def upsert_goal(payload: dict, profile: str | None = None, conn=None) -> dict:
    profile_id = _profile_id(profile)

    def _update(c):
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("Goal name is required.")
        goal_id = payload.get("id")
        goal_type = (payload.get("goal_type") or "custom").strip() or "custom"
        target_amount = max(float(payload.get("target_amount") or 0), 0)
        current_amount = max(float(payload.get("current_amount") or 0), 0)
        target_date = (payload.get("target_date") or "").strip() or None
        linked_category = (payload.get("linked_category") or "").strip() or None
        linked_account_id = (payload.get("linked_account_id") or "").strip() or None

        if goal_id:
            c.execute(
                """UPDATE goals
                   SET name = ?, goal_type = ?, target_amount = ?, current_amount = ?,
                       target_date = ?, linked_category = ?, linked_account_id = ?,
                       updated_at = datetime('now')
                   WHERE id = ? AND profile_id = ?""",
                (name, goal_type, target_amount, current_amount, target_date, linked_category, linked_account_id, goal_id, profile_id),
            )
        else:
            cur = c.execute(
                """INSERT INTO goals
                   (profile_id, name, goal_type, target_amount, current_amount, target_date,
                    linked_category, linked_account_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile_id, name, goal_type, target_amount, current_amount, target_date, linked_category, linked_account_id),
            )
            goal_id = cur.lastrowid

        row = c.execute("SELECT * FROM goals WHERE id = ? AND profile_id = ?", (goal_id, profile_id)).fetchone()
        return dict(row)

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def delete_goal(goal_id: int, profile: str | None = None, conn=None) -> bool:
    profile_id = _profile_id(profile)

    def _update(c):
        cur = c.execute(
            "UPDATE goals SET is_active = 0, updated_at = datetime('now') WHERE id = ? AND profile_id = ?",
            (goal_id, profile_id),
        )
        return cur.rowcount > 0

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)
    
def get_monthly_category_breakdown(profile: str | None = None, months: int = 12, conn=None) -> list[dict]:
    """
    Return the top spending categories for each of the last N months.
    Used by the Income vs Spending chart tooltip (Layer 6).

    Returns:
        List of { month: "YYYY-MM", categories: [{ category, total }, ...] }
        Each month includes up to 5 categories (top 4 + "Other" bucket).
    """
    def _query(c):
        tx_source = _transactions_source()
        where_clauses = []
        params = []
        is_household = not profile or profile == "household"

        if not is_household:
            where_clauses.append("profile_id = ?")
            params.append(profile)

        # Transfer exclusion
        if is_household:
            where_clauses.append("(expense_type IS NULL OR expense_type NOT IN ('transfer_internal', 'transfer_household'))")
        else:
            where_clauses.append("(expense_type IS NULL OR expense_type != 'transfer_internal')")

        where_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""

        non_spending_ph = ",".join("?" * len(NON_SPENDING_CATEGORIES))

        split_count = c.execute("SELECT COUNT(*) FROM transaction_splits").fetchone()[0]
        if split_count:
            allocation_sql = f"""
                WITH allocated AS (
                    SELECT SUBSTR(t.date, 1, 7) AS month,
                           s.category,
                           CASE WHEN t.amount < 0 THEN -ABS(s.amount) ELSE ABS(s.amount) END AS amount
                    FROM transaction_splits s
                    JOIN transactions_visible t ON t.id = s.transaction_id
                    WHERE 1=1{where_sql}
                    UNION ALL
                    SELECT SUBSTR(t.date, 1, 7) AS month,
                           t.category,
                           t.amount
                    FROM transactions_visible t
                    WHERE NOT EXISTS (
                        SELECT 1 FROM transaction_splits s WHERE s.transaction_id = t.id
                    ){where_sql}
                )
                SELECT month,
                       category,
                       COALESCE(SUM(CASE WHEN amount < 0 AND category NOT IN ({non_spending_ph}) THEN ABS(amount) ELSE 0 END), 0) AS gross,
                       COALESCE(SUM(CASE WHEN amount > 0 AND category NOT IN ({non_spending_ph}) AND category != 'Income' THEN amount ELSE 0 END), 0) AS refunds
                FROM allocated
                GROUP BY month, category
                ORDER BY month DESC, gross DESC
            """
            rows = c.execute(
                allocation_sql,
                params + params + list(NON_SPENDING_CATEGORIES) + list(NON_SPENDING_CATEGORIES),
            ).fetchall()
            month_cats = {}
            for row in rows:
                m = row[0]
                cat = row[1]
                net = float(row[2] or 0) - float(row[3] or 0)
                if net <= 0:
                    continue
                month_cats.setdefault(m, {})[cat] = net

            sorted_months = sorted(month_cats.keys(), reverse=True)[:months]
            result = []
            for m in sorted(sorted_months):
                sorted_cats = sorted(month_cats[m].items(), key=lambda x: -x[1])
                top_4 = sorted_cats[:4]
                other_total = sum(v for _, v in sorted_cats[4:])
                categories = [{"category": cat, "total": round(total, 2)} for cat, total in top_4]
                if other_total > 0:
                    categories.append({"category": "Other", "total": round(other_total, 2)})
                result.append({"month": m, "categories": categories})
            return result

        # Get gross expenses grouped by month + category
        expense_sql = f"""
            SELECT SUBSTR(date, 1, 7) as month, category, SUM(ABS(amount)) as total
            FROM {tx_source}
            WHERE amount < 0 AND category NOT IN ({non_spending_ph}){where_sql}
            GROUP BY SUBSTR(date, 1, 7), category
            ORDER BY month DESC, total DESC
        """
        expense_params = list(NON_SPENDING_CATEGORIES) + params
        expense_rows = c.execute(expense_sql, expense_params).fetchall()

        # Get refunds grouped by month + category
        refund_sql = f"""
            SELECT SUBSTR(date, 1, 7) as month, category, SUM(amount) as total
            FROM {tx_source}
            WHERE amount > 0 AND category NOT IN ({non_spending_ph}) AND category != 'Income'{where_sql}
            GROUP BY SUBSTR(date, 1, 7), category
        """
        refund_params = list(NON_SPENDING_CATEGORIES) + params
        refund_rows = c.execute(refund_sql, refund_params).fetchall()

        # Build refund lookup: { "YYYY-MM": { "category": refund_total } }
        refund_lookup = {}
        for row in refund_rows:
            m = row[0]
            cat = row[1]
            if m not in refund_lookup:
                refund_lookup[m] = {}
            refund_lookup[m][cat] = row[2]

        # Build per-month category data
        month_cats = {}  # { "YYYY-MM": { "category": net_total } }
        for row in expense_rows:
            m = row[0]
            cat = row[1]
            gross = row[2]
            refund = refund_lookup.get(m, {}).get(cat, 0)
            net = gross - refund
            if net <= 0:
                continue
            if m not in month_cats:
                month_cats[m] = {}
            month_cats[m][cat] = net

        # Sort months descending, take last N
        sorted_months = sorted(month_cats.keys(), reverse=True)[:months]

        result = []
        for m in sorted(sorted_months):
            cats = month_cats[m]
            sorted_cats = sorted(cats.items(), key=lambda x: -x[1])

            top_4 = sorted_cats[:4]
            others = sorted_cats[4:]
            other_total = sum(v for _, v in others)

            categories = [{"category": cat, "total": round(total, 2)} for cat, total in top_4]
            if other_total > 0:
                categories.append({"category": "Other", "total": round(other_total, 2)})

            result.append({
                "month": m,
                "categories": categories,
            })

        return result

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_merchant_insights_data(
    month: str | None = None,
    profile: str | None = None,
    conn=None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_unenriched: bool = False,
) -> list[dict]:
    """
    Merchant-level spending breakdown using SQL aggregation.

    Time window: `month` (YYYY-MM) or `start_date`/`end_date`.
    `include_unenriched=True` falls back to description when merchant_name is
    empty — used by Copilot so partially-enriched merchants (e.g. BILT) are
    still visible. UI keeps the enriched-only default for cleaner display.
    """
    def _query(c):
        tx_source = _transactions_source()
        where_clauses = []
        params = []

        if include_unenriched:
            where_clauses.append("COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''), description) != ''")
        else:
            where_clauses.append("enriched = 1")
            where_clauses.append("COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, '')) != ''")

        non_spending_ph = ",".join("?" * len(NON_SPENDING_CATEGORIES))
        where_clauses.append(f"amount < 0 AND category NOT IN ({non_spending_ph})")
        params.extend(NON_SPENDING_CATEGORIES)

        if profile and profile != "household":
            where_clauses.append("profile_id = ?")
            params.append(profile)
        if month:
            where_clauses.append("date LIKE ?")
            params.append(month + "%")
        else:
            if start_date:
                where_clauses.append("date >= ?")
                params.append(start_date)
            if end_date:
                where_clauses.append("date <= ?")
                params.append(end_date)

        # Use description fallback when include_unenriched, otherwise merchant_name directly
        name_expr = "COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''), description)" if include_unenriched else "COALESCE(NULLIF(merchant_key, ''), merchant_name)"
        label_expr = "COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, ''), description)"

        where_sql = " WHERE " + " AND ".join(where_clauses)

        sql = f"""
            SELECT {label_expr} AS name, merchant_domain, merchant_industry,
                   merchant_city, merchant_state,
                   SUM(ABS(amount)) as total_spent,
                   COUNT(*) as transaction_count
            FROM {tx_source}
            {where_sql}
            GROUP BY {name_expr}
            ORDER BY total_spent DESC
        """
        rows = c.execute(sql, params).fetchall()
        result = []
        for row in rows:
            result.append({
                "name": row[0],
                "domain": row[1] or "",
                "industry": row[2] or "",
                "city": row[3] or "",
                "state": row[4] or "",
                "total_spent": round(row[5], 2),
                "transaction_count": row[6],
            })
        return result

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_net_worth_series_data(interval: str = "weekly", profile: str | None = None, conn=None) -> list[dict]:
    """
    Compute a running net-worth time series for the dashboard chart.

    Stored net-worth snapshots are authoritative for dates where they exist,
    but new snapshot history should not truncate older transaction-derived
    history after a migration. When snapshots start after the first
    transaction, stitch a reconstructed prefix in front of the stored series.
    """
    from datetime import date as dt_date, timedelta

    def _query(c):
        history_profile = _profile_id(profile)
        history_rows = c.execute(
            """SELECT date, net_worth
               FROM net_worth_history
               WHERE profile_id = ?
               ORDER BY date ASC""",
            (history_profile,),
        ).fetchall()

        state = _build_reconstructed_net_worth_state(c, profile=profile)
        step_days = 1 if interval == "daily" else 7 if interval == "weekly" else 14

        def _reconstructed_series(start_date, end_date):
            series = []
            d = start_date
            while d <= end_date:
                nw = _reconstructed_net_worth_on(state, d)
                if nw is not None:
                    series.append({"date": d.isoformat(), "value": nw})
                d += timedelta(days=step_days)

            if series and series[-1]["date"] != end_date.isoformat():
                nw = _reconstructed_net_worth_on(state, end_date)
                if nw is not None:
                    series.append({"date": end_date.isoformat(), "value": nw})
            return series

        if history_rows:
            sampled_history = []
            last_sampled_date = None
            for row in history_rows:
                try:
                    row_date = dt_date.fromisoformat(str(row[0])[:10])
                except ValueError:
                    continue
                if (
                    last_sampled_date is None
                    or (row_date - last_sampled_date).days >= step_days
                    or row == history_rows[-1]
                ):
                    sampled_history.append({"date": row_date.isoformat(), "value": round(float(row[1] or 0), 2)})
                    last_sampled_date = row_date

            if not sampled_history:
                return []

            first_history_date = dt_date.fromisoformat(sampled_history[0]["date"])
            if state["first_date"] is not None and state["first_date"] < first_history_date:
                prefix_end = first_history_date - timedelta(days=1)
                prefix = _reconstructed_series(state["first_date"], min(prefix_end, state["last_date"]))
                return prefix + sampled_history

            if len(sampled_history) >= 2 or state["first_date"] is None or state["last_date"] is None:
                return sampled_history

        if state["first_date"] is None or state["last_date"] is None:
            return []

        return _reconstructed_series(state["first_date"], state["last_date"])

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_dashboard_bundle_data(
    nw_interval: str = "biweekly",
    profile: str | None = None,
    conn=None,
    as_of: str | date | datetime | None = None,
) -> dict:
    """
    Single-request dashboard data using SQL-level aggregation for summary,
    monthly, and category analytics. Net-worth series uses the dedicated helper.
    """
    def _query(c):
        summary_out = get_summary_data(profile=profile, conn=c)
        accounts_out = get_accounts_filtered(profile=profile, conn=c)
        monthly_sorted = get_monthly_analytics_data(profile=profile, conn=c)
        cat_result = get_category_analytics_data(profile=profile, conn=c)
        nw_series = get_net_worth_series_data(interval=nw_interval, profile=profile, conn=c)
        nw_deltas = get_net_worth_delta_metrics(profile=profile, conn=c)
        monthly_cat_breakdown = get_monthly_category_breakdown(profile=profile, months=12, conn=c)
        plan_snapshot = get_plan_snapshot_data(profile=profile, conn=c, as_of=as_of)
        review_queue = get_review_queue_data(profile=profile, conn=c)
        data_health = get_data_health_summary(profile=profile, conn=c)
        scheduled = get_scheduled_transactions_data(days=45, profile=profile, conn=c)
        cash_flow_forecast = get_cash_flow_forecast_data(days=90, profile=profile, conn=c)
        investments = get_investments_summary_data(profile=profile, conn=c)

        return {
            "summary": summary_out,
            "accounts": accounts_out,
            "monthly": monthly_sorted,
            "categories": cat_result["categories"],
            "savingsTransferTotal": cat_result["savings_transfer_total"],
            "personalTransferTotal": cat_result["personal_transfer_total"],
            "netWorthSeries": nw_series,
            "netWorthMomDelta": nw_deltas.get("mom"),
            "netWorthYtdDelta": nw_deltas.get("ytd"),
            "ccRepaid": summary_out.get("cc_repaid", 0),
            "externalTransfers": summary_out.get("external_transfers", 0),
            "incomingTransfers": summary_out.get("incoming_transfers", 0),
            "cashDeposits": summary_out.get("cash_deposits", 0),
            "cashWithdrawals": summary_out.get("cash_withdrawals", 0),
            "investmentInflows": summary_out.get("investment_inflows", 0),
            "investmentOutflows": summary_out.get("investment_outflows", 0),
            "creditsRefunds": summary_out.get("credits_refunds", summary_out.get("refunds", 0)),
            "monthlyCategoryBreakdown": monthly_cat_breakdown,
            "planSnapshot": plan_snapshot,
            "reviewQueue": review_queue,
            "dataHealth": data_health,
            "scheduled": scheduled,
            "cashFlowForecast": cash_flow_forecast,
            "investments": investments,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


# ══════════════════════════════════════════════════════════════════════════════
# INVESTMENTS LITE
# ══════════════════════════════════════════════════════════════════════════════

_ASSET_CLASS_LABELS = {
    "stock": "Stocks",
    "bond": "Bonds",
    "cash": "Cash",
    "fund": "Funds",
    "retirement": "Retirement",
    "crypto": "Crypto",
    "real_estate": "Real Estate",
    "other": "Other",
}


def _holding_market_value(row: dict) -> float:
    manual_value = row.get("manual_value")
    if manual_value is not None:
        return float(manual_value or 0)
    return float(row.get("quantity") or 0) * float(row.get("current_price") or 0)


def _serialize_holding(row: dict) -> dict:
    out = dict(row)
    out["market_value"] = round(_holding_market_value(out), 2)
    out["cost_basis_total"] = round(float(out.get("quantity") or 0) * float(out.get("cost_basis") or 0), 2)
    out["gain_loss"] = round(out["market_value"] - out["cost_basis_total"], 2)
    out["asset_class_label"] = _ASSET_CLASS_LABELS.get(out.get("asset_class"), "Other")
    return out


def get_investments_summary_data(profile: str | None = None, conn=None) -> dict:
    """Return manual holdings, allocation, and drift. No external price calls."""
    def _query(c):
        params = []
        clause = ""
        if profile and profile != "household":
            clause = "WHERE h.profile_id = ?"
            params.append(profile)
        rows = c.execute(
            f"""SELECT h.*, a.account_name, a.account_subtype
                FROM investment_holdings h
                LEFT JOIN accounts a ON a.id = h.account_id
                {clause}
                ORDER BY h.asset_class, UPPER(COALESCE(NULLIF(h.symbol, ''), h.name))""",
            params,
        ).fetchall()
        holdings = [_serialize_holding(dict(row)) for row in rows]
        total_value = round(sum(item["market_value"] for item in holdings), 2)
        total_cost = round(sum(item["cost_basis_total"] for item in holdings), 2)

        allocation_map: dict[str, dict] = {}
        for item in holdings:
            key = item.get("asset_class") or "other"
            bucket = allocation_map.setdefault(
                key,
                {
                    "asset_class": key,
                    "label": _ASSET_CLASS_LABELS.get(key, "Other"),
                    "value": 0.0,
                    "target_percent": None,
                },
            )
            bucket["value"] += item["market_value"]
            if item.get("target_percent") is not None:
                bucket["target_percent"] = (bucket["target_percent"] or 0) + float(item.get("target_percent") or 0)

        allocation = []
        for bucket in allocation_map.values():
            actual = (bucket["value"] / total_value * 100) if total_value else 0
            target = bucket["target_percent"]
            allocation.append({
                **bucket,
                "value": round(bucket["value"], 2),
                "actual_percent": round(actual, 1),
                "target_percent": round(target, 1) if target is not None else None,
                "drift_percent": round(actual - target, 1) if target is not None else None,
            })
        allocation.sort(key=lambda item: item["value"], reverse=True)

        return {
            "holdings": holdings,
            "allocation": allocation,
            "summary": {
                "holding_count": len(holdings),
                "total_value": total_value,
                "total_cost": total_cost,
                "gain_loss": round(total_value - total_cost, 2),
                "gain_loss_percent": round(((total_value - total_cost) / total_cost * 100), 1) if total_cost else None,
                "priced_manually": True,
                "external_price_refresh": "off",
            },
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def upsert_investment_holding(payload: dict, holding_id: int | None = None, profile: str | None = None, conn=None) -> dict:
    profile_id = _profile_id(profile)

    def _update(c):
        name = (payload.get("name") or payload.get("symbol") or "").strip()
        if not name:
            raise ValueError("Holding name or symbol is required.")
        asset_class = (payload.get("asset_class") or "other").strip() or "other"
        if asset_class not in _ASSET_CLASS_LABELS:
            asset_class = "other"
        account_id = (payload.get("account_id") or "").strip() or None
        if account_id:
            acct = c.execute(
                "SELECT id FROM accounts WHERE id = ? AND profile_id = ? AND is_active = 1",
                (account_id, profile_id),
            ).fetchone()
            if not acct:
                raise ValueError("Account not found for this profile.")
        values = {
            "profile_id": profile_id,
            "account_id": account_id,
            "symbol": (payload.get("symbol") or "").strip().upper(),
            "name": name,
            "asset_class": asset_class,
            "quantity": float(payload.get("quantity") or 0),
            "cost_basis": float(payload.get("cost_basis") or 0),
            "current_price": float(payload.get("current_price") or 0),
            "manual_value": float(payload["manual_value"]) if payload.get("manual_value") not in (None, "") else None,
            "target_percent": float(payload["target_percent"]) if payload.get("target_percent") not in (None, "") else None,
            "notes": (payload.get("notes") or "").strip(),
            "price_as_of": (payload.get("price_as_of") or "").strip() or datetime.now().date().isoformat(),
        }
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        if holding_id is None:
            cur = c.execute(
                """INSERT INTO investment_holdings
                   (profile_id, account_id, symbol, name, asset_class, quantity, cost_basis,
                    current_price, manual_value, target_percent, notes, price_as_of, updated_at)
                   VALUES (:profile_id, :account_id, :symbol, :name, :asset_class, :quantity, :cost_basis,
                           :current_price, :manual_value, :target_percent, :notes, :price_as_of, :updated_at)""",
                {**values, "updated_at": now},
            )
            holding_id_out = cur.lastrowid
        else:
            existing = c.execute(
                "SELECT id FROM investment_holdings WHERE id = ? AND profile_id = ?",
                (holding_id, profile_id),
            ).fetchone()
            if not existing:
                return None
            c.execute(
                """UPDATE investment_holdings
                   SET account_id = :account_id, symbol = :symbol, name = :name, asset_class = :asset_class,
                       quantity = :quantity, cost_basis = :cost_basis, current_price = :current_price,
                       manual_value = :manual_value, target_percent = :target_percent, notes = :notes,
                       price_as_of = :price_as_of, updated_at = :updated_at
                   WHERE id = :id AND profile_id = :profile_id""",
                {**values, "updated_at": now, "id": holding_id},
            )
            holding_id_out = holding_id
        row = c.execute("SELECT * FROM investment_holdings WHERE id = ?", (holding_id_out,)).fetchone()
        return _serialize_holding(dict(row))

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def delete_investment_holding(holding_id: int, profile: str | None = None, conn=None) -> bool:
    profile_id = _profile_id(profile)

    def _update(c):
        cur = c.execute("DELETE FROM investment_holdings WHERE id = ? AND profile_id = ?", (holding_id, profile_id))
        return cur.rowcount > 0

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_backup_status_data(profile: str | None = None, conn=None) -> dict:
    """Return backup/export readiness and the DB location without leaking secrets."""
    def _query(c):
        tables = [
            row["name"]
            for row in c.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table'
                     AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            ).fetchall()
        ]
        excluded = {"enrolled_tokens", "simplefin_connections"}
        included = [name for name in tables if name not in excluded]
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        return {
            "db_path": str(DB_PATH),
            "db_size_bytes": db_size,
            "export_format": "json",
            "included_tables": included,
            "excluded_tables": sorted(excluded.intersection(tables)),
            "profile_scope": profile or "household",
            "encryption": get_data_health_summary(profile=profile, conn=c).get("encryption", {}),
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def create_backup_export_data(profile: str | None = None, include_credentials: bool = False, conn=None) -> dict:
    """Create a JSON-safe export. Credential tables are excluded unless explicitly requested."""
    def _query(c):
        status = get_backup_status_data(profile=profile, conn=c)
        excluded = set() if include_credentials else set(status["excluded_tables"])
        tables = []
        for table in status["included_tables"] + ([] if not include_credentials else status["excluded_tables"]):
            if table in excluded:
                continue
            table_info = c.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [row["name"] for row in table_info]
            rows = [dict(row) for row in c.execute(f"SELECT * FROM {table}").fetchall()]
            if profile and profile != "household" and "profile_id" in columns:
                rows = [row for row in rows if row.get("profile_id") == profile]
            elif profile and profile != "household" and "profile" in columns:
                rows = [row for row in rows if row.get("profile") == profile]
            tables.append({"name": table, "columns": columns, "rows": rows})
        return {
            "app": "Folio",
            "created_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
            "profile_scope": profile or "household",
            "credential_tables_included": include_credentials,
            "excluded_tables": [] if include_credentials else status["excluded_tables"],
            "tables": tables,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)

# ══════════════════════════════════════════════════════════════════════════════
# RECURRING / SUBSCRIPTION QUERY OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_recurring_obligation_projection(conn, profile: str | None = None) -> None:
    """Keep the v2 obligation model populated before production recurring reads."""
    _backfill_recurring_obligations(conn, profile=profile)
    _sync_recurring_legacy_cache(conn, profile=profile)


def get_recurring_from_db(profile: str | None = None, conn=None) -> dict:
    """
    Read recurring subscriptions from the v2 obligation model.
    Legacy merchant subscription fields are now compatibility/cache writes only.
    """
    import json as _json

    def _query(c):
        try:
            _ensure_recurring_obligation_projection(c, profile)
            return _get_obligation_recurring_bundle(c, profile)
        except Exception as exc:
            logger.debug("Recurring obligation read fell back to legacy merchants view: %s", exc)

        profile_clause = ""
        profile_params = []
        if profile and profile != "household":
            profile_clause = " AND profile_id = ?"
            profile_params = [profile]

        # Active + inactive subscriptions
        rows = c.execute(
            f"""SELECT merchant_key, clean_name, logo_url, domain, category,
                       industry, subscription_frequency, subscription_amount,
                       subscription_status, last_charge_date, next_expected_date,
                       charge_count, total_spent, cancelled_by_user, cancelled_at,
                       source, profile_id
                FROM merchants
                WHERE is_subscription = 1{profile_clause}
                ORDER BY
                    CASE subscription_status
                        WHEN 'active' THEN 0
                        WHEN 'inactive' THEN 1
                        ELSE 2
                    END,
                    subscription_amount DESC""",
            profile_params,
        ).fetchall()

        # Dismissed items
        dismissed_rows = c.execute(
            f"""SELECT merchant_name, dismissed_at, profile_id
                FROM dismissed_recurring
                WHERE 1=1{profile_clause.replace('profile_id', 'profile_id')}""",
            profile_params,
        ).fetchall()

        # Events (recent, last 50)
        event_rows = c.execute(
            f"""SELECT id, event_type, merchant_name, detail, created_at, is_read, profile_id
                FROM subscription_events
                WHERE 1=1{profile_clause.replace('profile_id', 'profile_id')}
                ORDER BY created_at DESC
                LIMIT 50""",
            profile_params,
        ).fetchall()

        # User-declared subscriptions (to merge in)
        user_decl_rows = c.execute(
            f"""SELECT merchant_name, amount, frequency,
                       COALESCE(NULLIF(category, ''), 'Subscriptions'),
                       profile_id
                FROM user_declared_subscriptions
                WHERE is_active = 1{profile_clause.replace('profile_id', 'profile_id')}""",
            profile_params,
        ).fetchall()

        dismissed_set = set()
        scoped_dismissals = not (profile and profile != "household")
        for row in dismissed_rows:
            row_profile = row[2] or "household"
            for key in _recurring_match_keys(row[0]):
                dismissed_set.add(f"{row_profile}::{key}")
                if not scoped_dismissals:
                    dismissed_set.add(key)

        def _is_dismissed(merchant: str | None, row_profile: str | None) -> bool:
            keys = _recurring_match_keys(merchant)
            if any(key in dismissed_set for key in keys):
                return True
            return any(f"{row_profile or 'household'}::{key}" in dismissed_set for key in keys)

        def _seen_key(merchant: str | None, row_profile: str | None) -> str:
            key = _recurring_canonical_key(merchant or "")
            return f"{row_profile or 'household'}::{key}" if key else ""

        items = []
        active_count = 0
        inactive_count = 0
        cancelled_count = 0
        total_monthly = 0.0
        total_annual = 0.0

        seen_merchants = set()

        # Add user-declared first (Layer 0 — highest priority)
        for row in user_decl_rows:
            merchant_name = row[0]
            row_profile = row[4] or "household"
            if _is_dismissed(merchant_name, row_profile):
                continue
            merchant_key_upper = _seen_key(merchant_name, row_profile)
            seen_merchants.add(merchant_key_upper)

            amt = row[1]
            freq = row[2]
            category = row[3] or "Subscriptions"
            annual = _annualize_amount(amt, freq)

            items.append({
                "merchant": merchant_name,
                "clean_name": merchant_name,
                "logo_url": None,
                "category": category,
                "frequency": freq,
                "amount": round(amt, 2),
                "annual_cost": round(annual, 2),
                "status": "active",
                "confidence": "user",
                "last_charge": None,
                "next_expected": None,
                "charge_count": 0,
                "total_spent": 0,
                "price_change": None,
                "matched_by": "user",
                "cancelled": False,
                "profile": row_profile,
            })
            active_count += 1
            total_annual += annual
            total_monthly += annual / 12

        # Add detection-based subscriptions
        for row in rows:
            merchant_key = row[0]
            row_profile = row[16] or "household"
            if _is_dismissed(merchant_key, row_profile) or _is_dismissed(row[1], row_profile):
                continue
            merchant_seen_key = _seen_key(merchant_key, row_profile)
            if merchant_seen_key in seen_merchants:
                # User declaration wins — update with transaction data
                for item in items:
                    if _seen_key(item["merchant"], item.get("profile")) == merchant_seen_key:
                        item["last_charge"] = row[9]
                        item["next_expected"] = row[10]
                        item["charge_count"] = row[11] or 0
                        item["total_spent"] = round(row[12] or 0, 2)
                        item["clean_name"] = row[1] or item["clean_name"]
                        item["logo_url"] = row[2]
                        break
                continue

            seen_merchants.add(merchant_seen_key)
            status = row[8] or "inactive"
            cancelled = bool(row[13])
            amt = row[7] or 0
            freq = row[6] or "monthly"
            annual = _annualize_amount(amt, freq)

            if cancelled:
                display_status = "cancelled"
            else:
                display_status = status

            items.append({
                "merchant": row[1] or merchant_key,
                "clean_name": row[1] or merchant_key,
                "logo_url": row[2],
                "category": row[4] or "Subscriptions",
                "frequency": freq,
                "amount": round(amt, 2),
                "annual_cost": round(annual, 2),
                "status": display_status,
                "confidence": "high" if row[15] == "seed" else "medium",
                "last_charge": row[9],
                "next_expected": row[10],
                "charge_count": row[11] or 0,
                "total_spent": round(row[12] or 0, 2),
                "price_change": None,
                "matched_by": row[15] or "algorithm",
                "cancelled": cancelled,
                "profile": row_profile,
            })

            if status == "active" and not cancelled:
                active_count += 1
                total_annual += annual
                total_monthly += annual / 12
            elif cancelled:
                cancelled_count += 1
            else:
                inactive_count += 1

        # Build events list
        events = []
        unread_count = 0
        for erow in event_rows:
            try:
                detail = _json.loads(erow[3]) if erow[3] else {}
            except Exception:
                detail = {}
            events.append({
                "id": erow[0],
                "event_type": erow[1],
                "merchant_name": erow[2],
                "detail": detail,
                "created_at": erow[4],
                "is_read": bool(erow[5]),
            })
            if not erow[5]:
                unread_count += 1

        # Build dismissed list
        dismissed = [
            {"merchant": row[0], "dismissed_at": row[1], "profile": row[2] or "household"}
            for row in dismissed_rows
        ]

        return {
            "items": items,
            "active_count": active_count,
            "inactive_count": inactive_count,
            "cancelled_count": cancelled_count,
            "dismissed_count": len(dismissed),
            "total_monthly": round(total_monthly, 2),
            "total_annual": round(total_annual, 2),
            "events": events,
            "unread_event_count": unread_count,
            "dismissed": dismissed,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _annualize_amount(amount: float, frequency: str) -> float:
    """Convert per-period amount to annual cost."""
    multipliers = {"monthly": 12, "quarterly": 4, "semi_annual": 2, "annual": 1}
    return amount * multipliers.get(frequency, 12)


def _advance_recurring_date(base: date, frequency: str) -> date:
    """Advance a date by one recurrence period without fixed-day calendar drift."""
    freq = (frequency or "monthly").lower()
    if freq in {"weekly", "week"}:
        return base + timedelta(days=7)
    if freq in {"biweekly", "bi_weekly", "fortnightly"}:
        return base + timedelta(days=14)
    if freq in {"quarterly", "quarter"}:
        return _recurring_add_months(base, 3)
    if freq in {"annual", "annually", "yearly", "year"}:
        return _recurring_add_months(base, 12)
    if freq in {"semi_annual", "semiannual", "semi-annually"}:
        return _recurring_add_months(base, 6)
    return _recurring_add_months(base, 1)


def _parse_date_only(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T")).date()
    except Exception:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _resolve_as_of_date(value: str | date | datetime | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _parse_date_only(value) or date.today()


def get_scheduled_transactions_data(days: int = 45, profile: str | None = None, conn=None) -> dict:
    """
    Derive upcoming scheduled bills from the v2 recurring obligation model.
    Legacy projections below remain as a safety fallback for pre-migration DBs.
    """
    window_days = max(1, min(int(days or 45), 180))
    today = date.today()
    window_end = today + timedelta(days=window_days)

    def _upcoming_group(merchant: str, category: str | None) -> dict:
        text = f"{merchant or ''} {category or ''}".lower()
        category_name = category or "Subscriptions"
        checks = [
            ("housing", "Housing", ("rent", "mortgage", "apartment", "property", "hoa", "landlord", "housing")),
            ("utilities", "Utilities", ("utility", "utilities", "electric", "water", "gas", "pg&e", "pge", "internet", "xfinity", "comcast", "verizon", "at&t", "phone", "mobile")),
            ("debt", "Debt", ("loan", "student loan", "auto loan", "mortgage", "credit card", "visa", "mastercard", "amex", "debt")),
            ("insurance", "Insurance", ("insurance", "geico", "progressive", "state farm", "allstate", "anthem", "kaiser")),
            ("subscriptions", "Subscriptions", ("subscription", "netflix", "spotify", "apple", "google", "claude", "anthropic", "openai", "chatgpt", "x premium", "twitter", "simplefin")),
        ]
        for key, label, needles in checks:
            if category_name.lower() == label.lower() or any(needle in text for needle in needles):
                return {"key": key, "label": label}
        return {"key": "other", "label": "Other"}

    def _median(values: list[float]) -> float:
        if not values:
            return 0
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    def _detect_frequency_from_dates(dates: list[date]) -> str | None:
        if len(dates) < 2:
            return None
        intervals = sorted((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1))
        med = _median(intervals)
        if 25 <= med <= 38:
            return "monthly"
        if 80 <= med <= 105:
            return "quarterly"
        if 340 <= med <= 400:
            return "annual"
        return None

    def _fixed_obligation_category(category: str | None, merchant: str | None) -> bool:
        text = f"{category or ''} {merchant or ''}".lower()
        return any(token in text for token in (
            "housing", "rent", "mortgage", "hoa",
            "utilities", "utility", "electric", "water", "gas", "internet", "wireless", "phone", "mobile",
            "insurance",
            "loan", "debt",
            "subscription", "software", "saas",
        ))

    def _date_consistent(dates: list[date]) -> bool:
        if len(dates) < 2:
            return False
        days = sorted(d.day for d in dates)
        med_day = _median(days)
        consistent = 0
        for day in days:
            diff = abs(day - med_day)
            if diff <= 5 or (med_day >= 27 and day <= 5) or (day >= 27 and med_day <= 5):
                consistent += 1
        return consistent / len(days) >= 0.65

    def _candidate_is_recent_enough(latest_date: date, frequency: str) -> bool:
        """Avoid projecting cancelled/stale history into the current Upcoming list."""
        max_age_days = {
            "monthly": 70,
            "quarterly": 130,
            "annual": 430,
        }.get(frequency, 70)
        return (today - latest_date).days <= max_age_days

    def _due_from_expected_day(expected_day: int | None) -> date | None:
        if not expected_day:
            return None
        day = max(1, min(int(expected_day), 31))
        month_start = today.replace(day=1)
        for month_offset in (0, 1):
            year = month_start.year + (month_start.month + month_offset - 1) // 12
            month = (month_start.month + month_offset - 1) % 12 + 1
            last_day = calendar.monthrange(year, month)[1]
            candidate = date(year, month, min(day, last_day))
            if candidate >= today:
                return candidate
        return None

    def _is_stale(last_date: date | None, frequency: str) -> bool:
        if not last_date:
            return False
        max_age_days = {
            "monthly": 45,
            "quarterly": 110,
            "annual": 395,
            "semi_annual": 220,
        }.get(frequency or "monthly", 45)
        return (today - last_date).days > max_age_days

    def _query(c):
        try:
            _ensure_recurring_obligation_projection(c, profile)
            return _get_obligation_scheduled_bundle(c, days=window_days, profile=profile, today=today)
        except Exception as exc:
            logger.debug("Scheduled obligation read fell back to legacy projection: %s", exc)

        profile_clause = ""
        params = []
        if profile and profile != "household":
            profile_clause = " AND profile_id = ?"
            params.append(profile)

        rows = c.execute(
            f"""SELECT merchant_key, clean_name, category, subscription_frequency,
                       subscription_amount, subscription_status, last_charge_date,
                       next_expected_date, charge_count, profile_id, source,
                       cancelled_by_user
                FROM merchants
                WHERE is_subscription = 1{profile_clause}
                  AND COALESCE(cancelled_by_user, 0) = 0
                  AND COALESCE(subscription_status, 'active') = 'active'""",
            params,
        ).fetchall()

        declared_rows = c.execute(
            f"""SELECT merchant_name, amount, frequency, profile_id,
                       COALESCE(NULLIF(category, ''), 'Subscriptions') AS category,
                       expected_day,
                       amount_review_dismissed_amount,
                       amount_review_dismissed_latest_date
                FROM user_declared_subscriptions
                WHERE is_active = 1{profile_clause}""",
            params,
        ).fetchall()

        dismissed_rows = c.execute(
            f"""SELECT merchant_name, profile_id
                FROM dismissed_recurring
                WHERE 1=1{profile_clause}""",
            params,
        ).fetchall()
        dismissed = set()
        scoped_dismissals = not (profile and profile != "household")
        for row in dismissed_rows:
            row_profile = row[1] or "household"
            for key_part in _recurring_match_keys(row[0]):
                dismissed.add(f"{row_profile}::{key_part}")
                if not scoped_dismissals:
                    dismissed.add(key_part)

        items = []
        seen = set()
        items_by_key = {}

        def add_item(*, merchant, amount, frequency, category="Subscriptions", status="expected",
                     last_charge=None, next_expected=None, profile_id=None, source="detected",
                     confidence="medium", charge_count=0, expected_day=None,
                     amount_review_dismissed_amount=None, amount_review_dismissed_latest_date=None):
            if not merchant:
                return
            merchant_key = str(merchant).upper().strip()
            canonical_key = (canonicalize_merchant_key(merchant_key) or merchant_key).upper().strip()
            key = f"{canonical_key}::{profile_id or 'household'}"
            if merchant_key in dismissed or canonical_key in dismissed or key in dismissed:
                return
            amt = float(amount or 0)
            if amt <= 0:
                return

            due = _parse_date_only(next_expected)
            last = _parse_date_only(last_charge)
            if due is None:
                due = _due_from_expected_day(expected_day)
            if due is None and last is not None:
                due = _advance_recurring_date(last, frequency)
            while due is not None and due < today:
                due = _advance_recurring_date(due, frequency)

            if due is None:
                schedule_status = "needs_date"
                days_until = None
            else:
                days_until = (due - today).days
                if due < today:
                    schedule_status = "overdue"
                elif due <= today + timedelta(days=7):
                    schedule_status = "due_soon"
                else:
                    schedule_status = status

            if due is not None and due > window_end:
                return

            monthly = _annualize_amount(amt, frequency) / 12
            group = _upcoming_group(merchant, category)
            item = {
                "merchant": merchant,
                "category": category or "Subscriptions",
                "group": group["key"],
                "group_label": group["label"],
                "amount": round(amt, 2),
                "monthly_equivalent": round(monthly, 2),
                "frequency": frequency or "monthly",
                "next_date": due.isoformat() if due else None,
                "days_until": days_until,
                "status": schedule_status,
                "last_charge": last.isoformat() if last else None,
	                "profile": profile_id,
	                "source": source,
	                "confidence": confidence,
	                "confirmed": source == "user",
	                "charge_count": charge_count or 0,
                "expected_day": expected_day,
                "_amount_review_dismissed_amount": amount_review_dismissed_amount,
                "_amount_review_dismissed_latest_date": amount_review_dismissed_latest_date,
            }

            if source == "recurring_candidate":
                if confidence == "low":
                    return
                for existing_item in items:
                    same_date = existing_item.get("next_date") == item.get("next_date")
                    same_group = existing_item.get("group") == item.get("group")
                    same_amount = abs(float(existing_item.get("amount") or 0) - item["amount"]) <= 0.75
                    if same_date and same_group and same_amount:
                        return

            existing = items_by_key.get(key)
            if existing:
                # User declarations win for amount/frequency, but detected rows
                # can still supply the date anchor needed for forecasting.
                if source == "user":
                    existing["amount"] = item["amount"]
                    existing["monthly_equivalent"] = item["monthly_equivalent"]
                    existing["frequency"] = item["frequency"]
                    existing["source"] = "user"
                    existing["confidence"] = "user"
                    existing["category"] = item["category"] or existing["category"]
                if existing.get("status") == "needs_date" and item.get("next_date"):
                    existing["next_date"] = item["next_date"]
                    existing["days_until"] = item["days_until"]
                    existing["status"] = item["status"]
                    existing["last_charge"] = item["last_charge"]
                    existing["charge_count"] = item["charge_count"]
                return

            seen.add(key)
            items_by_key[key] = item
            items.append(item)

        for row in declared_rows:
            # User-declared subscriptions win, but may not have a known due date yet.
            add_item(
                merchant=row[0],
                amount=row[1],
                frequency=row[2],
                profile_id=row[3],
                category=row[4],
                expected_day=row[5],
                amount_review_dismissed_amount=row[6],
                amount_review_dismissed_latest_date=row[7],
                source="user",
                confidence="user",
            )

        for row in rows:
            add_item(
                merchant=row[1] or row[0],
                category=row[2],
                frequency=row[3],
                amount=row[4],
                status=row[5] or "expected",
                last_charge=row[6],
                next_expected=row[7],
                charge_count=row[8],
                profile_id=row[9],
                source=row[10] or "detected",
                confidence="high" if row[10] in {"seed", "user"} else "medium",
            )

        history_params = []
        history_where = [
            "amount < 0",
            "COALESCE(is_excluded, 0) = 0",
            "date >= ?",
        ]
        history_params.append((today - timedelta(days=450)).isoformat())
        if profile and profile != "household":
            history_where.append("profile_id = ?")
            history_params.append(profile)
        history_rows = c.execute(
            f"""SELECT id, date, amount, category,
                       COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''), description) AS merchant,
                       profile_id, expense_type
                FROM transactions_visible
                WHERE {' AND '.join(history_where)}
                ORDER BY date ASC""",
            history_params,
        ).fetchall()

        recent_by_key: dict[str, list[dict]] = {}
        for row in history_rows:
            tx_date = _parse_date_only(row[1])
            merchant = row[4] or ""
            if not tx_date or not merchant:
                continue
            key_base = canonicalize_merchant_key(merchant) or merchant.upper().strip()
            row_key = f"{key_base}::{row[5] or 'household'}"
            recent_by_key.setdefault(row_key, []).append({
                "id": row[0],
                "date": tx_date.isoformat(),
                "amount": round(abs(float(row[2] or 0)), 2),
                "category": row[3] or "",
                "merchant": merchant,
                "profile": row[5],
            })

        groups: dict[str, list[dict]] = {}
        for row in history_rows:
            category = row[3] or ""
            merchant = row[4] or ""
            if not merchant or category in NON_SPENDING_CATEGORIES:
                continue
            if row[6] in {TRANSFER_INTERNAL, TRANSFER_HOUSEHOLD, TRANSFER_EXTERNAL, TRANSFER_CC_PAYMENT}:
                continue
            if not _fixed_obligation_category(category, merchant):
                continue
            key_base = canonicalize_merchant_key(merchant) or merchant.upper().strip()
            key = f"{key_base}::{row[5] or 'household'}"
            groups.setdefault(key, []).append({
                "date": _parse_date_only(row[1]),
                "amount": abs(float(row[2] or 0)),
                "category": category,
                "merchant": merchant,
                "profile_id": row[5],
            })

        for group in groups.values():
            clean = [item for item in group if item["date"]]
            if len(clean) < 2:
                continue
            dates = sorted(item["date"] for item in clean)
            frequency = _detect_frequency_from_dates(dates)
            if not frequency or not _date_consistent(dates):
                continue
            amounts = [item["amount"] for item in clean]
            med_amount = _median(amounts)
            if med_amount <= 0:
                continue
            close_amounts = [amount for amount in amounts if abs(amount - med_amount) / med_amount <= 0.35]
            if len(close_amounts) / len(amounts) < 0.65:
                continue
            latest = max(clean, key=lambda item: item["date"])
            if not _candidate_is_recent_enough(latest["date"], frequency):
                continue
            due = _advance_recurring_date(latest["date"], frequency)
            while due < today - timedelta(days=window_days):
                due = _advance_recurring_date(due, frequency)
            add_item(
                merchant=latest["merchant"],
                amount=med_amount,
                frequency=frequency,
                category=latest["category"],
                last_charge=latest["date"].isoformat(),
                next_expected=due.isoformat(),
                charge_count=len(clean),
                profile_id=latest["profile_id"],
                source="recurring_candidate",
                confidence="medium" if len(clean) >= 3 else "low",
            )

        def _amount_review_suggestion(item: dict, recent: list[dict]) -> dict | None:
            if item.get("source") != "user" or len(recent) < 2:
                return None
            current = float(item.get("amount") or 0)
            if current <= 0:
                return None
            latest_two = recent[:2]
            latest_amounts = [float(tx.get("amount") or 0) for tx in latest_two]
            suggested = round(sum(latest_amounts) / len(latest_amounts), 2)
            if suggested <= 0:
                return None
            spread = max(latest_amounts) - min(latest_amounts)
            if spread > max(5.0, suggested * 0.10):
                return None
            delta = round(suggested - current, 2)
            if abs(delta) < max(5.0, current * 0.05):
                return None
            latest_date = latest_two[0].get("date")
            dismissed_date = item.get("_amount_review_dismissed_latest_date")
            dismissed_amount = item.get("_amount_review_dismissed_amount")
            if dismissed_date == latest_date and dismissed_amount is not None:
                try:
                    if abs(float(dismissed_amount) - suggested) < 0.01:
                        return None
                except (TypeError, ValueError):
                    pass
            return {
                "type": "amount_change",
                "basis": "latest_two_charges",
                "current_amount": round(current, 2),
                "suggested_amount": suggested,
                "latest_amount": round(latest_amounts[0], 2),
                "previous_amount": round(latest_amounts[1], 2),
                "latest_date": latest_date,
                "delta": delta,
                "delta_pct": round((delta / current) * 100, 1),
                "direction": "down" if delta < 0 else "up",
            }

        for item in items:
            merchant_key = str(item.get("merchant") or "").upper().strip()
            canonical_key = (canonicalize_merchant_key(merchant_key) or merchant_key).upper().strip()
            key = f"{canonical_key}::{item.get('profile') or 'household'}"
            recent = sorted(recent_by_key.get(key, []), key=lambda tx: tx["date"], reverse=True)[:5]
            amounts = [tx["amount"] for tx in recent]
            last = _parse_date_only(item.get("last_charge"))
            source = item.get("source") or "detected"
            if source == "user":
                source_label = "User confirmed"
            elif source == "recurring_candidate":
                source_label = "Inferred"
            elif source == "seed":
                source_label = "Seeded"
            else:
                source_label = "Detected"
            item["recent_transactions"] = recent
            item["evidence"] = {
                "source_label": source_label,
                "seen_count": item.get("charge_count") or len(recent),
                "last_paid": item.get("last_charge") or (recent[0]["date"] if recent else None),
                "amount_min": round(min(amounts), 2) if amounts else item.get("amount"),
                "amount_max": round(max(amounts), 2) if amounts else item.get("amount"),
                "stale": _is_stale(last, item.get("frequency") or "monthly"),
            }
            amount_review = _amount_review_suggestion(item, recent)
            if amount_review:
                item["amount_review"] = amount_review
            item.pop("_amount_review_dismissed_amount", None)
            item.pop("_amount_review_dismissed_latest_date", None)

        items.sort(key=lambda item: (item["next_date"] is None, item["next_date"] or "9999-99-99", item["merchant"]))
        scheduled = [item for item in items if item["next_date"]]
        due_soon = [item for item in scheduled if item["status"] in {"overdue", "due_soon"}]
        confirmed_upcoming_total = sum(
            item["amount"]
            for item in scheduled
            if item["days_until"] is not None
            and item["days_until"] >= 0
            and item.get("source") != "recurring_candidate"
        )
        inferred_upcoming_total = sum(
            item["amount"]
            for item in scheduled
            if item["days_until"] is not None
            and item["days_until"] >= 0
            and item.get("source") == "recurring_candidate"
        )
        groups = {}
        for item in items:
            key = item.get("group") or "other"
            if key not in groups:
                groups[key] = {
                    "key": key,
                    "label": item.get("group_label") or "Other",
                    "count": 0,
                    "scheduled_count": 0,
                    "amount": 0,
                    "monthly_equivalent": 0,
                }
            groups[key]["count"] += 1
            groups[key]["monthly_equivalent"] += item.get("monthly_equivalent") or 0
            if item.get("next_date"):
                groups[key]["scheduled_count"] += 1
                groups[key]["amount"] += item.get("amount") or 0
        group_summary = [
            {**group, "amount": round(group["amount"], 2), "monthly_equivalent": round(group["monthly_equivalent"], 2)}
            for group in groups.values()
        ]
        group_summary.sort(key=lambda group: (-group["amount"], group["label"]))

        return {
            "window_days": window_days,
            "start_date": today.isoformat(),
            "end_date": window_end.isoformat(),
            "items": items,
            "scheduled_count": len(scheduled),
            "needs_date_count": len([item for item in items if item["status"] == "needs_date"]),
            "due_soon_count": len(due_soon),
            "upcoming_total": round(confirmed_upcoming_total, 2),
            "confirmed_upcoming_total": round(confirmed_upcoming_total, 2),
            "inferred_upcoming_total": round(inferred_upcoming_total, 2),
            "needs_review_total": round(inferred_upcoming_total, 2),
            "monthly_equivalent_total": round(sum(item["monthly_equivalent"] for item in items), 2),
            "groups": group_summary,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def create_month_explanation(month: str, profile: str | None = None, use_llm: bool = True, save: bool = True, conn=None) -> dict:
    """Build a source-linked month explanation and persist it as a saved insight."""
    import json as _json

    if not re.match(r"^\d{4}-\d{2}$", month or ""):
        raise ValueError("month must be YYYY-MM")

    profile_id = _profile_id(profile)

    def _fallback_answer(facts: dict) -> str:
        summary = facts["summary"] or {}
        top_categories = facts["top_categories"][:3]
        top_merchants = facts["top_merchants"][:3]
        pulse = facts["spending_pulse"][:2]
        upcoming = facts.get("upcoming") or {}
        upcoming_groups = (upcoming.get("groups") or [])[:2]
        upcoming_total = float(upcoming.get("total") or 0)
        lines = [
            f"Takeaway: {facts['month_label']} ended at {summary.get('net_flow_formatted')} net flow after {summary.get('expenses_formatted')} in spending and {summary.get('income_formatted')} in income.",
        ]
        if pulse:
            lines.append("Drivers: The biggest month-over-month moves were " + ", ".join(f"{p['category']} {p['direction']} {p['delta_formatted']}" for p in pulse) + ".")
        elif top_categories:
            lines.append("Drivers: The largest categories were " + ", ".join(f"{c['category']} ({c['total_formatted']})" for c in top_categories[:2]) + ".")
        else:
            lines.append("Drivers: No category movement stood out in the available data.")
        if upcoming_total > 0:
            group_text = ", ".join(f"{g.get('label')} (${float(g.get('amount') or 0):,.0f})" for g in upcoming_groups)
            suffix = f", led by {group_text}" if group_text else ""
            lines.append(f"Watch: ${upcoming_total:,.0f} is scheduled over the next {upcoming.get('window_days')} days{suffix}.")
        elif facts.get("recurring"):
            lines.append(f"Watch: Recurring commitments are running at {facts['recurring']['monthly_formatted']} per month.")
        else:
            lines.append("Watch: No scheduled or recurring pressure was present in the available data.")

        recurring = facts.get("recurring") or {}
        facts_line = f"Facts: recurring runs {recurring.get('monthly_formatted', '$0')} monthly across {recurring.get('active_count', 0)} active items"
        if top_categories:
            facts_line += "; top categories are " + ", ".join(f"{c['category']} ({c['total_formatted']})" for c in top_categories)
        if top_merchants:
            facts_line += "; top merchants are " + ", ".join(f"{m['merchant']} ({m['total_formatted']})" for m in top_merchants)
        lines.append(facts_line + ".")
        return "\n".join(lines)

    def _query(c):
        monthly = get_monthly_analytics_data(profile=profile, conn=c)
        current = next((row for row in monthly if row.get("month") == month), None)
        if not current:
            raise ValueError("No analytics data found for that month.")
        sorted_months = sorted(monthly, key=lambda row: row.get("month") or "")
        current_idx = next((idx for idx, row in enumerate(sorted_months) if row.get("month") == month), -1)
        prev = sorted_months[current_idx - 1] if current_idx > 0 else None

        categories = get_category_analytics_data(month=month, profile=profile, conn=c).get("categories", [])
        prev_categories = get_category_analytics_data(month=prev["month"], profile=profile, conn=c).get("categories", []) if prev else []
        prev_by_cat = {row["category"]: float(row.get("total") or 0) for row in prev_categories}

        top_categories = sorted(categories, key=lambda row: float(row.get("total") or 0), reverse=True)[:8]
        merchant_rows = get_merchant_insights_data(month=month, profile=profile, conn=c)[:8]
        recurring = get_recurring_from_db(profile=profile, conn=c)
        scheduled = get_scheduled_transactions_data(days=45, profile=profile, conn=c)

        pulse = []
        for cat in top_categories:
            total = float(cat.get("total") or 0)
            previous = prev_by_cat.get(cat.get("category"), 0)
            delta = total - previous
            if abs(delta) >= 10:
                pulse.append({
                    "category": cat.get("category"),
                    "direction": "up" if delta > 0 else "down",
                    "delta": round(delta, 2),
                    "delta_formatted": f"${abs(delta):,.0f}",
                    "current": round(total, 2),
                    "previous": round(previous, 2),
                })
        pulse.sort(key=lambda row: abs(row["delta"]), reverse=True)

        income = float(current.get("income") or 0)
        expenses = float(current.get("expenses") or 0)
        external = float(current.get("external_transfers") or 0)
        net_flow = income - expenses - external
        month_label = datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y")
        facts = {
            "month": month,
            "month_label": month_label,
            "summary": {
                "income": round(income, 2),
                "expenses": round(expenses, 2),
                "external_transfers": round(external, 2),
                "net_flow": round(net_flow, 2),
                "income_formatted": f"${income:,.0f}",
                "expenses_formatted": f"${expenses:,.0f}",
                "net_flow_formatted": f"{'-' if net_flow < 0 else ''}${abs(net_flow):,.0f}",
            },
            "top_categories": [
                {**cat, "total_formatted": f"${float(cat.get('total') or 0):,.0f}"}
                for cat in top_categories
            ],
            "top_merchants": [
                {
                    "merchant": row.get("merchant") or row.get("merchant_name") or row.get("clean_name") or row.get("name") or "Merchant",
                    "total": round(float(row.get("total_spent") or row.get("total") or 0), 2),
                    "total_formatted": f"${float(row.get('total_spent') or row.get('total') or 0):,.0f}",
                    "transaction_count": row.get("transaction_count") or row.get("count") or 0,
                }
                for row in merchant_rows
            ],
            "spending_pulse": pulse[:8],
            "recurring": {
                "monthly": round(float(recurring.get("total_monthly") or 0), 2),
                "monthly_formatted": f"${float(recurring.get('total_monthly') or 0):,.0f}",
                "active_count": recurring.get("active_count") or 0,
            },
            "upcoming": {
                "window_days": scheduled.get("window_days"),
                "total": scheduled.get("upcoming_total"),
                "groups": scheduled.get("groups", []),
            },
        }

        answer = None
        source = "deterministic"
        if use_llm:
            try:
                import llm_client
                prompt = (
                    "You are Mira, Folio's local-first personal finance analyst. "
                    "Turn the JSON facts into a compact explanation for an analytics summary card. "
                    "Use only the facts provided. Do not infer causes, intent, employment, income patterns, or advice. "
                    "Do not mention JSON or the prompt. "
                    "Return plain text, not markdown. "
                    "Format exactly four short labeled lines:\n"
                    "Takeaway: one sentence with net flow, spending, and income.\n"
                    "Drivers: one sentence naming the biggest category, merchant, or month-over-month change that explains the month.\n"
                    "Watch: one sentence naming upcoming scheduled expenses or recurring pressure; if neither is present, say that plainly.\n"
                    "Facts: one compact sentence with two or three numbers the user can verify.\n"
                    "Keep the full answer under 120 words. Be specific, calm, and useful.\n\n"
                    f"FACTS:\n{_json.dumps(facts, indent=2)}"
                )
                answer = llm_client.complete(prompt, max_tokens=360, purpose="copilot")
                source = llm_client.get_provider()
            except Exception as exc:
                logger.info("Month explanation LLM unavailable; using deterministic fallback: %s", exc)
        if not answer:
            answer = _fallback_answer(facts)

        question = f"Explain {month_label}"
        insight_id = None
        if save:
            c.execute(
                """INSERT INTO saved_insights (profile_id, question, answer, kind, pinned)
                   VALUES (?, ?, ?, 'insight', 0)""",
                (profile_id, question, answer),
            )
            insight_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "id": insight_id,
            "question": question,
            "answer": answer,
            "facts": facts,
            "source": source,
            "saved": bool(save),
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_cash_flow_forecast_data(days: int = 90, profile: str | None = None, conn=None) -> dict:
    """Return a deterministic cash runway forecast and safe-to-spend number."""
    horizon_days = max(7, min(int(days or 90), 180))
    today = date.today()
    end_date = today + timedelta(days=horizon_days)

    def _months_between(start: date, end: date) -> int:
        return max(1, (end.year - start.year) * 12 + (end.month - start.month) + (1 if end.day >= start.day else 0))

    def _query(c):
        profile_clause = ""
        params = []
        if profile and profile != "household":
            profile_clause = " AND profile_id = ?"
            params = [profile]

        liquid_balance = c.execute(
            f"""SELECT COALESCE(SUM(current_balance), 0)
                FROM accounts
                WHERE account_type = 'depository'
                  AND is_active = 1{profile_clause}""",
            params,
        ).fetchone()[0] or 0

        monthly = get_monthly_analytics_data(profile=profile, conn=c)
        recent_months = [m for m in monthly if m.get("income", 0) > 0][:3]
        avg_monthly_income = (
            sum(float(m.get("income") or 0) for m in recent_months) / len(recent_months)
            if recent_months else 0
        )

        budgets = get_category_budgets(profile=profile, conn=c)
        total_budget = sum(max(float(item.get("amount") or 0), 0) for item in budgets)
        scheduled = get_scheduled_transactions_data(days=horizon_days, profile=profile, conn=c)
        scheduled_items = [
            item for item in scheduled.get("items", [])
            if item.get("next_date") and bool(item.get("confirmed", item.get("source") != "recurring_candidate"))
        ]
        scheduled_monthly = sum(float(item.get("monthly_equivalent") or 0) for item in scheduled_items)
        variable_monthly = max(total_budget - scheduled_monthly, 0)

        goals = get_goals(profile=profile, conn=c)
        goal_monthly = 0.0
        for goal in goals:
            gap = max(float(goal.get("target_amount") or 0) - float(goal.get("current_amount") or 0), 0)
            target = _parse_date_only(goal.get("target_date"))
            months = _months_between(today, target) if target else 12
            goal_monthly += gap / months if gap > 0 else 0

        daily_income = avg_monthly_income / 30
        daily_variable = variable_monthly / 30
        daily_goals = goal_monthly / 30

        scheduled_events = []
        for item in scheduled_items:
            due = _parse_date_only(item.get("next_date"))
            if not due:
                continue
            while due <= end_date:
                if due >= today:
                    scheduled_events.append({
                        "date": due.isoformat(),
                        "merchant": item.get("merchant"),
                        "amount": float(item.get("amount") or 0),
                        "frequency": item.get("frequency") or "monthly",
                    })
                due = _advance_recurring_date(due, item.get("frequency") or "monthly")
        scheduled_events.sort(key=lambda event: event["date"])

        points = []
        balance = float(liquid_balance or 0)
        event_idx = 0
        min_balance = balance
        for offset in range(horizon_days + 1):
            day = today + timedelta(days=offset)
            if offset > 0:
                balance += daily_income
                balance -= daily_variable
                balance -= daily_goals
            while event_idx < len(scheduled_events) and scheduled_events[event_idx]["date"] == day.isoformat():
                balance -= scheduled_events[event_idx]["amount"]
                event_idx += 1
            min_balance = min(min_balance, balance)
            if offset == 0 or offset % 7 == 0 or offset == horizon_days:
                points.append({"date": day.isoformat(), "balance": round(balance, 2)})

        committed_30 = sum(event["amount"] for event in scheduled_events if _parse_date_only(event["date"]) <= today + timedelta(days=30))
        expected_income_30 = daily_income * 30
        variable_30 = daily_variable * 30
        goals_30 = daily_goals * 30
        safe_to_spend = float(liquid_balance or 0) + expected_income_30 - committed_30 - variable_30 - goals_30

        return {
            "start_date": today.isoformat(),
            "end_date": end_date.isoformat(),
            "days": horizon_days,
            "liquid_balance": round(float(liquid_balance or 0), 2),
            "safe_to_spend": round(safe_to_spend, 2),
            "projected_end_balance": points[-1]["balance"] if points else round(float(liquid_balance or 0), 2),
            "projected_low_balance": round(min_balance, 2),
            "inputs": {
                "avg_monthly_income": round(avg_monthly_income, 2),
                "scheduled_30_day_total": round(committed_30, 2),
                "scheduled_monthly_equivalent": round(scheduled_monthly, 2),
                "budgeted_variable_monthly": round(variable_monthly, 2),
                "goal_monthly_need": round(goal_monthly, 2),
            },
            "events": scheduled_events[:40],
            "points": points,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_dismissed_subscriptions(profile: str | None = None, conn=None) -> list[dict]:
    """Return dismissed subscription items for a profile."""
    def _query(c):
        if profile and profile != "household":
            rows = c.execute(
                "SELECT merchant_name, dismissed_at FROM dismissed_recurring WHERE profile_id = ?",
                (profile,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT merchant_name, dismissed_at FROM dismissed_recurring"
            ).fetchall()
        return [{"merchant": row[0], "dismissed_at": row[1]} for row in rows]

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_subscription_events(profile: str | None = None, conn=None) -> dict:
    """Return subscription events and unread count."""
    import json as _json

    def _query(c):
        params = []
        clause = ""
        if profile and profile != "household":
            clause = " WHERE profile_id = ?"
            params = [profile]

        rows = c.execute(
            f"""SELECT id, event_type, merchant_name, detail, created_at, is_read
                FROM subscription_events{clause}
                ORDER BY created_at DESC
                LIMIT 100""",
            params,
        ).fetchall()

        events = []
        unread_count = 0
        for row in rows:
            try:
                detail = _json.loads(row[3]) if row[3] else {}
            except Exception:
                detail = {}
            events.append({
                "id": row[0],
                "event_type": row[1],
                "merchant_name": row[2],
                "detail": detail,
                "created_at": row[4],
                "is_read": bool(row[5]),
            })
            if not row[5]:
                unread_count += 1

        return {"events": events, "unread_count": unread_count}

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _recurring_identity_matches(value: str | None, target_keys: set[str], target_upper: str) -> bool:
    """Return whether a legacy display/key value matches a canonical recurring target."""
    if not value:
        return False
    if str(value).upper().strip() == target_upper:
        return True
    return bool(_recurring_match_keys(value) & target_keys)


def _legacy_merchant_ids_for_recurring(conn, merchant: str, profile_id: str) -> list[int]:
    """Find legacy merchant rows by canonical key or exact display-name fallback."""
    target_keys = _recurring_match_keys(merchant)
    merchant_key = _recurring_canonical_key(merchant)
    if merchant_key:
        target_keys.add(merchant_key)
    target_upper = merchant.upper().strip()
    rows = conn.execute(
        """SELECT id, merchant_key, clean_name
           FROM merchants
           WHERE profile_id = ?
             AND is_subscription = 1""",
        (profile_id,),
    ).fetchall()
    return [
        row[0]
        for row in rows
        if _recurring_identity_matches(row[1], target_keys, target_upper)
        or _recurring_identity_matches(row[2], target_keys, target_upper)
    ]


def _user_declared_ids_for_recurring(conn, merchant: str, profile_id: str) -> list[int]:
    target_keys = _recurring_match_keys(merchant)
    merchant_key = _recurring_canonical_key(merchant)
    if merchant_key:
        target_keys.add(merchant_key)
    target_upper = merchant.upper().strip()
    rows = conn.execute(
        """SELECT id, merchant_name
           FROM user_declared_subscriptions
           WHERE profile_id = ?""",
        (profile_id,),
    ).fetchall()
    return [
        row[0]
        for row in rows
        if _recurring_identity_matches(row[1], target_keys, target_upper)
    ]


def _dismissed_ids_for_recurring(conn, merchant: str, profile_id: str) -> list[int]:
    target_keys = _recurring_match_keys(merchant)
    merchant_key = _recurring_canonical_key(merchant)
    if merchant_key:
        target_keys.add(merchant_key)
    target_upper = merchant.upper().strip()
    rows = conn.execute(
        """SELECT id, merchant_name
           FROM dismissed_recurring
           WHERE profile_id = ?""",
        (profile_id,),
    ).fetchall()
    return [
        row[0]
        for row in rows
        if _recurring_identity_matches(row[1], target_keys, target_upper)
    ]


def _apply_to_ids(conn, table: str, ids: list[int], sql_suffix: str, params: tuple = ()) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    result = conn.execute(
        f"UPDATE {table} {sql_suffix} WHERE id IN ({placeholders})",
        (*params, *ids),
    )
    return result.rowcount


def declare_subscription(
    merchant: str,
    amount: float,
    frequency: str,
    profile: str | None = None,
    category: str = "Subscriptions",
    expected_day: int | None = None,
) -> dict:
    """
    Store a user-declared subscription.
    Writes to user_declared_subscriptions and merchants tables.
    """
    profile_id = profile or "household"
    category_name = (category or "Subscriptions").strip() or "Subscriptions"
    normalized_expected_day = None
    if expected_day is not None:
        normalized_expected_day = max(1, min(int(expected_day), 31))

    with get_db() as conn:
        conn.execute(
            """INSERT INTO user_declared_subscriptions
               (merchant_name, amount, frequency, category, expected_day, profile_id)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(merchant_name, profile_id) DO UPDATE SET
                   amount = excluded.amount,
                   frequency = excluded.frequency,
	                   category = excluded.category,
	                   expected_day = excluded.expected_day,
	                   amount_review_dismissed_amount = NULL,
	                   amount_review_dismissed_latest_date = NULL,
	                   amount_review_dismissed_at = NULL,
	                   is_active = 1,
	                   updated_at = datetime('now')""",
            (merchant, amount, frequency, category_name, normalized_expected_day, profile_id),
        )

        # Also upsert merchants table
        merchant_key = _recurring_canonical_key(merchant)
        from recurring import _annualize
        annual = _annualize(amount, frequency)
        conn.execute(
            """INSERT INTO merchants
               (merchant_key, clean_name, category, source, is_subscription,
                subscription_frequency, subscription_amount, subscription_status,
                profile_id)
               VALUES (?, ?, ?, 'user', 1, ?, ?, 'active', ?)
               ON CONFLICT(merchant_key, profile_id) DO UPDATE SET
                   clean_name = excluded.clean_name,
                   category = excluded.category,
                   is_subscription = 1,
                   subscription_frequency = excluded.subscription_frequency,
                   subscription_amount = excluded.subscription_amount,
                   subscription_status = 'active',
                   source = 'user',
                   cancelled_by_user = 0,
                   cancelled_at = NULL,
                   updated_at = datetime('now')""",
            (
                merchant_key,
                merchant,
                category_name,
                frequency,
                amount,
                profile_id,
	            ),
	        )

        user_obligation_key = _upsert_user_recurring_obligation(
            conn,
            merchant=merchant,
            amount=amount,
            frequency=frequency,
            profile_id=profile_id,
            category=category_name,
            expected_day=normalized_expected_day,
        )
        _record_recurring_feedback(
            conn,
            merchant=merchant,
            profile_id=profile_id,
            feedback_type="confirmed",
            scope="exact_candidate",
            obligation_key=user_obligation_key,
            payload={
                "amount": amount,
                "frequency": frequency,
                "category": category_name,
                "expected_day": normalized_expected_day,
            },
        )

        if normalized_expected_day:
            today = date.today()
            month_start = today.replace(day=1)
            next_date = None
            for month_offset in (0, 1):
                year = month_start.year + (month_start.month + month_offset - 1) // 12
                month = (month_start.month + month_offset - 1) % 12 + 1
                last_day = calendar.monthrange(year, month)[1]
                candidate = date(year, month, min(normalized_expected_day, last_day))
                if candidate >= today:
                    next_date = candidate.isoformat()
                    break
            if next_date:
                conn.execute(
                    """UPDATE merchants
                       SET next_expected_date = ?, updated_at = datetime('now')
                       WHERE merchant_key = ? AND profile_id = ?""",
                    (next_date, merchant_key, profile_id),
                )

        dismissed_ids = _dismissed_ids_for_recurring(conn, merchant, profile_id)
        if dismissed_ids:
            placeholders = ",".join("?" * len(dismissed_ids))
            conn.execute(
                f"DELETE FROM dismissed_recurring WHERE id IN ({placeholders})",
                dismissed_ids,
            )

        # Fetch the record to return
        row = conn.execute(
            """SELECT merchant_name, amount, frequency, profile_id, created_at,
                      COALESCE(NULLIF(category, ''), 'Subscriptions'), expected_day
               FROM user_declared_subscriptions
               WHERE merchant_name = ? AND profile_id = ?""",
            (merchant, profile_id),
        ).fetchone()

    return {
        "merchant": row[0],
        "amount": row[1],
        "frequency": row[2],
        "profile_id": row[3],
        "created_at": row[4],
        "category": row[5],
        "expected_day": row[6],
        "annual_cost": round(_annualize(row[1], row[2]), 2),
    }


def cancel_subscription(merchant: str, profile: str | None = None) -> dict:
    """Mark a subscription as cancelled by the user."""
    from datetime import datetime as _dt
    profile_id = profile or "household"
    now = _dt.now().isoformat()

    with get_db() as conn:
        merchant_ids = _legacy_merchant_ids_for_recurring(conn, merchant, profile_id)
        if merchant_ids:
            placeholders = ",".join("?" * len(merchant_ids))
            conn.execute(
                f"""UPDATE merchants
                    SET cancelled_at = ?,
                        cancelled_by_user = 1,
                        subscription_status = 'cancelled',
                        next_expected_date = NULL,
                        updated_at = datetime('now')
                    WHERE id IN ({placeholders})""",
                (now, *merchant_ids),
            )
        declared_ids = _user_declared_ids_for_recurring(conn, merchant, profile_id)
        if declared_ids:
            placeholders = ",".join("?" * len(declared_ids))
            conn.execute(
                f"""UPDATE user_declared_subscriptions
                    SET is_active = 0,
                        updated_at = datetime('now')
                    WHERE id IN ({placeholders})""",
                declared_ids,
            )
        _record_recurring_feedback(
            conn,
            merchant=merchant,
            profile_id=profile_id,
            feedback_type="cancelled",
            scope="merchant",
            payload={"cancelled_at": now},
        )

    return {"status": "ok", "cancelled_at": now}


def restore_subscription(merchant: str, profile: str | None = None) -> bool:
    """Restore a dismissed/cancelled recurring item."""
    profile_id = profile or "household"

    with get_db() as conn:
        dismissed_ids = _dismissed_ids_for_recurring(conn, merchant, profile_id)
        dismissed_count = 0
        if dismissed_ids:
            placeholders = ",".join("?" * len(dismissed_ids))
            dismissed_count = conn.execute(
                f"DELETE FROM dismissed_recurring WHERE id IN ({placeholders})",
                dismissed_ids,
            ).rowcount
        merchant_ids = _legacy_merchant_ids_for_recurring(conn, merchant, profile_id)
        merchant_count = 0
        if merchant_ids:
            placeholders = ",".join("?" * len(merchant_ids))
            merchant_count = conn.execute(
                f"""UPDATE merchants
                    SET cancelled_by_user = 0,
                        cancelled_at = NULL,
                        subscription_status = CASE
                            WHEN subscription_status = 'cancelled' THEN 'inactive'
                            ELSE subscription_status
                        END,
                        updated_at = datetime('now')
                    WHERE id IN ({placeholders})""",
                merchant_ids,
            ).rowcount
        declared_ids = _user_declared_ids_for_recurring(conn, merchant, profile_id)
        declared_count = 0
        if declared_ids:
            placeholders = ",".join("?" * len(declared_ids))
            declared_count = conn.execute(
                f"""UPDATE user_declared_subscriptions
                    SET is_active = 1,
                        updated_at = datetime('now')
                    WHERE id IN ({placeholders})""",
                declared_ids,
            ).rowcount
        restored_obligation = _restore_recurring_obligation(conn, merchant=merchant, profile_id=profile_id)
        return dismissed_count > 0 or merchant_count > 0 or declared_count > 0 or restored_obligation


def mark_events_read(event_ids: list[int]) -> int:
    """Mark subscription events as read. Returns count of updated rows."""
    if not event_ids:
        return 0

    with get_db() as conn:
        placeholders = ",".join("?" * len(event_ids))
        result = conn.execute(
            f"UPDATE subscription_events SET is_read = 1 WHERE id IN ({placeholders})",
            event_ids,
        )
        return result.rowcount


def _mark_missing_legacy_recurring_inactive(profile: str | None, items: list[dict], conn=None) -> int:
    """Mark legacy detector-owned rows missing from a redetection run inactive."""
    profile_id = profile or "household"
    seen_by_profile: dict[str, set[str]] = {}
    for item in items:
        item_profile = item.get("profile") or profile_id
        key = _recurring_canonical_key(item.get("merchant") or "")
        if key:
            seen_by_profile.setdefault(item_profile, set()).add(key)

    def _apply(c) -> int:
        if profile and profile != "household":
            profiles = [profile]
        else:
            rows = c.execute(
                """SELECT DISTINCT COALESCE(profile_id, 'household')
                   FROM merchants
                   WHERE is_subscription = 1
                     AND source IN ('seed', 'algorithm', 'category')"""
            ).fetchall()
            profiles = [row[0] for row in rows] or ["household"]

        updated = 0
        for scoped_profile in profiles:
            seen = seen_by_profile.get(scoped_profile, set())
            params: list = [scoped_profile]
            not_seen_clause = ""
            if seen:
                placeholders = ",".join("?" * len(seen))
                not_seen_clause = f"AND merchant_key NOT IN ({placeholders})"
                params.extend(sorted(seen))
            result = c.execute(
                f"""UPDATE merchants
                    SET subscription_status = 'inactive',
                        next_expected_date = NULL,
                        updated_at = datetime('now')
                    WHERE profile_id = ?
                      AND is_subscription = 1
                      AND COALESCE(cancelled_by_user, 0) = 0
                      AND source IN ('seed', 'algorithm', 'category')
                      {not_seen_clause}""",
                params,
            )
            updated += result.rowcount
        return updated

    if conn is not None:
        return _apply(conn)
    with get_db() as c:
        return _apply(c)


def trigger_full_redetection(profile: str | None = None) -> dict:
    """
    Run full recurring detection across all transactions and persist results.
    Used for manual refresh via POST /api/subscriptions/redetect.
    """
    from recurring import RecurringDetector, write_detection_results_to_db

    profile_id = profile or "household"
    lock = _recurring_redetection_locks.setdefault(profile_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"status": "already_running", "items": [], "events": []}

    try:
        data = get_data()
        txns = data["transactions"]

        if profile and profile != "household":
            txns = [t for t in txns if t.get("profile") == profile or t.get("profile_id") == profile]

        detector = RecurringDetector(get_db_conn=get_db)
        result = detector.detect(
            transactions=txns,
            profile=profile,
            generate_events=True,
        )

        write_detection_results_to_db(
            get_db_conn=get_db,
            items=result["items"],
            events=result.get("events", []),
            profile=profile,
        )
        result["legacy_rows_marked_inactive"] = _mark_missing_legacy_recurring_inactive(profile, result["items"])

        return result
    finally:
        lock.release()


# ══════════════════════════════════════════════════════════════════════════════
# READ OPERATIONS — FULL DATASET (legacy, use sparingly)
# ══════════════════════════════════════════════════════════════════════════════

def get_data(force_refresh: bool = False) -> dict:
    """
    Returns ALL data from SQLite — loads every transaction and account into memory.

    ⚠️  DEPRECATED FOR GENERAL USE — prefer targeted query functions above.

    This function exists ONLY for endpoints that genuinely need the full
    transaction list in memory, specifically:
      - /api/analytics/recurring — the recurring-detection algorithm needs
        to group, sort, and iterate over all expense transactions with
        complex cross-transaction analysis (interval detection, amount
        consistency, merchant grouping, seed matching) that cannot be
        efficiently expressed as SQL queries.

    All other endpoints should use the targeted query functions
    (get_transactions_paginated, get_summary_data, get_monthly_analytics_data,
    get_category_analytics_data, get_merchant_insights_data, etc.) which push
    filters and aggregation into SQL.
    """
    if force_refresh:
        return fetch_fresh_data(incremental=True)

    with get_db() as conn:
        # Check if we have any data
        count = conn.execute(
            f"SELECT COUNT(*) FROM {_transactions_source()}"
        ).fetchone()[0]
        if count == 0:
            return EMPTY_DATA

        # Accounts
        acct_rows = conn.execute(
            """SELECT id, account_name as name, account_subtype as type,
                      CASE WHEN account_type IN ('credit', 'loan') THEN 1 ELSE 0 END as is_credit,
                      account_type,
                      current_balance as balance, currency, profile_id as profile,
                      COALESCE(provider, 'teller') as provider
               FROM accounts WHERE is_active = 1"""
        ).fetchall()
        accounts = dicts_from_rows(acct_rows)

        # Transactions
        tx_rows = conn.execute(
            """SELECT id as original_id, profile_id as profile, date, description,
                      raw_description, amount, category, categorization_source,
                      confidence, transaction_type as type,
                      counterparty_name, counterparty_type, teller_category,
                      account_name, account_type, merchant_name, merchant_domain,
                      merchant_industry, merchant_city, merchant_state,
                      merchant_key, merchant_source, merchant_confidence, merchant_kind,
                      enriched, is_excluded, expense_type
               FROM transactions_visible
               ORDER BY date DESC"""
        ).fetchall()
        transactions = dicts_from_rows(tx_rows)

        # Convert sqlite integer booleans to Python bools for enriched
        for tx in transactions:
            tx["enriched"] = bool(tx.get("enriched", 0))
            tx["is_excluded"] = bool(tx.get("is_excluded", 0))

        # Convert account is_credit to bool
        for acct in accounts:
            acct["is_credit"] = bool(acct.get("is_credit", 0))

        # Last updated: most recent sync timestamp from accounts
        last_row = conn.execute(
            "SELECT MAX(last_synced_at) FROM accounts"
        ).fetchone()
        last_updated = last_row[0] if last_row and last_row[0] else None

        return {
            "last_updated": last_updated,
            "accounts": accounts,
            "transactions": transactions,
        }


def get_cached_tx_ids() -> set:
    """Get all existing transaction IDs from the database."""
    with get_db() as conn:
        rows = conn.execute("SELECT id FROM transactions").fetchall()
        return {row[0] for row in rows}


# ══════════════════════════════════════════════════════════════════════════════
# SYNC (Teller → SQLite)
# ══════════════════════════════════════════════════════════════════════════════

def _sync_teller_accounts(conn, cached_ids: set, now: str) -> list[dict]:
    """
    Fetch accounts and transactions from Teller, upsert accounts,
    and return a list of new (uncategorised) transaction dicts.
    This is the original Teller sync logic, extracted for clarity.
    """
    logger.info("Fetching accounts from Teller...")
    accounts = get_all_accounts_by_profile()
    new_transactions = []

    for account in accounts:
        token = account["access_token"]
        name = account["name"]
        subtype = account.get("subtype", "")
        profile = account.get("profile", "primary")
        institution = account.get("institution", {}).get("name", "")
        logger.info("[%s] Fetching transactions for %s...", profile, name)

        # Ensure this account's profile exists regardless
        conn.execute(
            "INSERT OR IGNORE INTO profiles (id, display_name) VALUES (?, ?)",
            (profile, profile.title()),
        )

        all_txns = get_transactions(account["id"], token)
        balances = get_balances(account["id"], token)

        new_txns = [t for t in all_txns if t.get("id") not in cached_ids]

        for t in new_txns:
            t["account_name"] = name
            t["account_type"] = subtype
            t["profile"] = profile

        # Upsert account
        # Teller account types: depository, credit, loan, investment
        # Map to our internal types for net worth classification
        teller_type = account.get("type", "depository")
        is_credit = subtype in ("credit_card", "credit") or teller_type == "credit"
        is_loan = teller_type == "loan"
        is_investment = teller_type == "investment"

        # Balance selection: credit/loan use ledger, others use available
        if is_credit or is_loan:
            balance = balances.get("ledger") or balances.get("available")
        else:
            balance = balances.get("available") or balances.get("ledger")

        # Determine internal account_type
        if is_credit:
            internal_type = "credit"
        elif is_loan:
            internal_type = "loan"
        elif is_investment:
            internal_type = "investment"
        else:
            internal_type = "depository"

        conn.execute(
            """INSERT OR REPLACE INTO accounts
               (id, profile_id, institution_name, account_name,
                account_type, account_subtype, current_balance,
                currency, last_synced_at, is_active, provider)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'teller')""",
            (
                account["id"],
                profile,
                institution,
                name,
                internal_type,
                subtype,
                float(balance) if balance else 0.0,
                account.get("currency", "USD"),
                now,
            ),
        )

        if new_txns:
            logger.info(
                "    Found %d new (skipping %d cached)",
                len(new_txns), len(all_txns) - len(new_txns),
            )
            categorized = categorize_transactions(new_txns)
            new_transactions.extend(categorized)
        else:
            logger.info("    No new transactions for %s", name)

    return new_transactions


def _sync_simplefin_accounts(conn, cached_ids: set, now: str) -> list[dict]:
    """
    Fetch accounts and transactions from all active SimpleFIN connections,
    upsert accounts, and return a list of new (uncategorised) transaction dicts.
    """
    import simplefin

    connections = simplefin._load_active_access_urls()
    if not connections:
        return []

    logger.info("SimpleFIN: found %d active connection(s).", len(connections))
    new_transactions = []

    for sf_conn in connections:
        conn_id = sf_conn["id"]
        profile = sf_conn["profile"]
        display_name = sf_conn.get("display_name", "")

        if not simplefin._should_sync(sf_conn.get("last_synced_at")):
            logger.info(
                "SimpleFIN connection %d (%s) synced recently — skipping (rate limit).",
                conn_id, display_name,
            )
            continue

        # Ensure this connection's profile exists
        conn.execute(
            "INSERT OR IGNORE INTO profiles (id, display_name) VALUES (?, ?)",
            (profile, profile.title()),
        )

        # SimpleFIN requires start-date or it returns balances only (no transactions).
        # Full sync: fetch up to 1 year of history.
        # Incremental sync: fetch 90-day window to catch any late-posted transactions.
        # Check specifically for existing SimpleFIN transactions (id prefix 'sf_') for
        # this connection's profile — avoids falsely treating a fresh SimpleFIN connection
        # as incremental just because Teller transactions already exist in the DB.
        from datetime import timedelta
        sf_exists = conn.execute(
            "SELECT 1 FROM transactions WHERE id LIKE 'sf_%' AND profile_id = ? LIMIT 1",
            (profile,),
        ).fetchone()
        if sf_exists:
            start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        else:
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        try:
            raw_data = simplefin.fetch_data(sf_conn["access_url"], start_date=start_date)
        except ValueError as exc:
            logger.warning("SimpleFIN connection %d fetch failed: %s", conn_id, exc)
            continue

        sf_accounts, sf_txns = simplefin.normalize_all(raw_data, profile)

        # Upsert accounts
        for acct in sf_accounts:
            conn.execute(
                """INSERT OR REPLACE INTO accounts
                   (id, profile_id, institution_name, account_name,
                    account_type, account_subtype, current_balance,
                    currency, last_synced_at, is_active, provider)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'simplefin')""",
                (
                    acct["id"],
                    acct["profile"],
                    acct["institution_name"],
                    acct["account_name"],
                    acct["account_type"],
                    acct["account_subtype"],
                    acct["current_balance"],
                    acct["currency"],
                    now,
                ),
            )

        # Filter to new transactions only
        new_txns = [t for t in sf_txns if t.get("id") not in cached_ids]

        if new_txns:
            logger.info(
                "SimpleFIN connection %d (%s): %d new transactions (skipping %d cached).",
                conn_id, display_name, len(new_txns), len(sf_txns) - len(new_txns),
            )
            categorized = categorize_transactions(new_txns)
            new_transactions.extend(categorized)
        else:
            logger.info("SimpleFIN connection %d (%s): no new transactions.", conn_id, display_name)

        # Update last_synced_at
        simplefin.update_last_synced(conn_id, now)

    return new_transactions


def _post_sync(all_new_transactions: list[dict], conn, now: str):
    """
    Shared post-sync steps: transfer classification, insertion,
    net-worth snapshot, recurring detection, transfer reclassification.
    """
    _apply_linked_account_pair_overrides(all_new_transactions)

    # Build account lookup once for transfer classification
    account_lookup = _build_account_lookup(conn)

    # Classify transfer sub-types before insertion
    for tx in all_new_transactions:
        category = tx.get("category", "Other")
        if category in TRANSFER_CATEGORIES and not tx.get("expense_type"):
            tx["expense_type"] = _classify_transfer_type(tx, account_lookup)

    # Insert new transactions
    for tx in all_new_transactions:
        _insert_transaction(conn, tx)

    # Snapshot net worth
    _snapshot_net_worth(conn, now)


def _post_sync_recurring(all_new_transactions: list[dict]):
    """Run recurring detection and transfer reclassification after sync."""
    if all_new_transactions:
        try:
            from recurring import RecurringDetector, write_detection_results_to_db

            new_merchant_keys = set()
            for tx in all_new_transactions:
                merchant = canonicalize_merchant_key(tx.get("merchant_key") or tx.get("merchant_name") or "")
                if merchant:
                    new_merchant_keys.add(merchant)

            if new_merchant_keys:
                logger.info(
                    "Running incremental recurring detection for %d merchant keys...",
                    len(new_merchant_keys),
                )

                all_data = get_data()
                all_txns = all_data["transactions"]

                profiles_seen = {tx.get("profile", "primary") for tx in all_new_transactions}
                for prof in profiles_seen:
                    prof_txns = [t for t in all_txns if t.get("profile") == prof]
                    detector = RecurringDetector(get_db_conn=get_db)
                    result = detector.detect(
                        transactions=prof_txns,
                        profile=prof,
                        merchant_keys=new_merchant_keys,
                        generate_events=True,
                    )
                    write_detection_results_to_db(
                        get_db_conn=get_db,
                        items=result["items"],
                        events=result.get("events", []),
                        profile=prof,
                        mode="incremental",
                    )

                logger.info("Incremental recurring detection complete.")

        except Exception as e:
            logger.warning("Incremental recurring detection failed (non-fatal): %s", e)

    try:
        reclassify_transfers()
    except Exception as e:
        logger.warning("Transfer reclassification failed (non-fatal): %s", e)


def fetch_fresh_data(incremental: bool = True, sync_job_id: str | None = None) -> dict:
    """
    Fetch from Teller API (and SimpleFIN if configured) and write to SQLite.
    Called by manual and automatic provider sync paths.
    """
    with _lock:
        _set_sync_phase(sync_job_id, "preparing")
        cached_ids = get_cached_tx_ids() if incremental else set()

        if cached_ids:
            logger.info("Database has %d existing transactions.", len(cached_ids))
        else:
            logger.info("No cached transactions — full fetch.")

        now = datetime.now().isoformat()

        with get_db() as conn:
            # Ensure profiles exist
            for profile_name in bank.PROFILES.keys():
                conn.execute(
                    "INSERT OR IGNORE INTO profiles (id, display_name) VALUES (?, ?)",
                    (profile_name, profile_name.title()),
                )

            # Ensure the virtual 'household' profile exists (aggregate of all profiles)
            conn.execute(
                "INSERT OR IGNORE INTO profiles (id, display_name) VALUES ('household', 'Household')",
            )

            all_new_transactions = []

            # ── Teller sync ──
            try:
                _set_sync_phase(sync_job_id, "fetching_accounts", "Syncing Teller accounts")
                teller_txns = _sync_teller_accounts(conn, cached_ids, now)
                all_new_transactions.extend(teller_txns)
            except Exception as e:
                logger.warning("Teller sync failed (non-fatal): %s", e)

            # ── SimpleFIN sync ──
            try:
                _set_sync_phase(sync_job_id, "fetching_accounts", "Syncing SimpleFIN accounts")
                sf_txns = _sync_simplefin_accounts(conn, cached_ids, now)
                all_new_transactions.extend(sf_txns)
            except Exception as e:
                logger.warning("SimpleFIN sync failed (non-fatal): %s", e)

            # ── Shared post-sync pipeline ──
            _set_sync_phase(sync_job_id, "writing_transactions", "Writing transactions and balances")
            _post_sync(all_new_transactions, conn, now)

        _set_sync_phase(sync_job_id, "finalizing", "Finalizing derived data")
        _post_sync_recurring(all_new_transactions)

        total_count_row = None
        with get_db() as conn:
            total_count_row = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()

        total_count = total_count_row[0] if total_count_row else 0
        logger.info("Sync complete: %d new, %d total.", len(all_new_transactions), total_count)

        _set_sync_phase(sync_job_id, "completed", "Sync complete")
        return get_data()


def fetch_simplefin_data(sync_job_id: str | None = None) -> dict:
    """
    Fetch from SimpleFIN only (no Teller). Used by /api/simplefin/sync
    to allow independent testing of the SimpleFIN pipeline.
    """
    with _lock:
        _set_sync_phase(sync_job_id, "preparing")
        cached_ids = get_cached_tx_ids()
        now = datetime.now().isoformat()

        with get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO profiles (id, display_name) VALUES ('household', 'Household')",
            )

            _set_sync_phase(sync_job_id, "fetching_accounts", "Syncing SimpleFIN accounts")
            sf_txns = _sync_simplefin_accounts(conn, cached_ids, now)
            _set_sync_phase(sync_job_id, "writing_transactions", "Writing transactions and balances")
            _post_sync(sf_txns, conn, now)

        _set_sync_phase(sync_job_id, "finalizing", "Finalizing derived data")
        _post_sync_recurring(sf_txns)

        total_count_row = None
        with get_db() as conn:
            total_count_row = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()

        total_count = total_count_row[0] if total_count_row else 0
        logger.info("SimpleFIN sync complete: %d new, %d total.", len(sf_txns), total_count)

        _set_sync_phase(sync_job_id, "completed", "Sync complete")
        return get_data()


def _insert_transaction(conn, tx: dict):
    """Insert a single categorized transaction into the database."""
    profile = tx.get("profile", "primary")
    tx_id = tx.get("original_id", "")

    # Determine categorization source from confidence
    confidence = tx.get("confidence", "")
    cat_source = tx.get("categorization_source", "")

    if not cat_source:
        if confidence == "manual":
            cat_source = "user"
        elif confidence == "rule":
            cat_source = "rule-high"
        elif confidence in ("high", "medium", "low"):
            cat_source = "llm"
        elif confidence == "fallback":
            cat_source = "fallback"
        else:
            cat_source = "unknown"

    # Use the category already determined by the categorizer pipeline
    # (user rules were already checked in categorizer.py Phase 1.6)
    category = tx.get("category", "Other")

    # Ensure category exists
    conn.execute(
        "INSERT OR IGNORE INTO categories (name, is_system) VALUES (?, 0)",
        (category,),
    )

    # Resolve account_id
    account_id = None
    acct_name = tx.get("account_name", "")
    if acct_name:
        row = conn.execute(
            "SELECT id FROM accounts WHERE account_name = ? AND profile_id = ?",
            (acct_name, profile),
        ).fetchone()
        if row:
            account_id = row[0]

    desc_normalized = _extract_merchant_pattern(tx.get("description", ""))
    merchant_identity = build_merchant_identity(tx)

    conn.execute(
        """INSERT OR IGNORE INTO transactions
           (id, account_id, profile_id, date, description, raw_description,
            amount, category, categorization_source, transaction_type,
            counterparty_name, counterparty_type, teller_category,
            account_name, account_type, merchant_name, merchant_domain,
            merchant_industry, merchant_city, merchant_state, merchant_key,
            merchant_source, merchant_confidence, merchant_kind,
            enriched, confidence, expense_type, description_normalized)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tx_id,
            account_id,
            profile,
            tx.get("date", ""),
            tx.get("description", ""),
            tx.get("raw_description", ""),
            float(tx.get("amount", 0)),
            category,
            cat_source,
            tx.get("type", ""),
            tx.get("counterparty_name", ""),
            tx.get("counterparty_type", ""),
            tx.get("teller_category", ""),
            acct_name,
            tx.get("account_type", ""),
            tx.get("merchant_name", ""),
            tx.get("merchant_domain", ""),
            tx.get("merchant_industry", ""),
            tx.get("merchant_city", ""),
            tx.get("merchant_state", ""),
            merchant_identity["merchant_key"],
            merchant_identity["source"],
            merchant_identity["confidence"],
            merchant_identity["kind"],
            1 if tx.get("enriched") else 0,
            tx.get("confidence", ""),
            tx.get("expense_type"),
            desc_normalized,
        ),
    )


def _check_user_rules(conn, description: str) -> str | None:
    """
    Check if any user-defined category rules match this transaction.
    User rules are checked first (highest priority).
    Returns the category name if a rule matches, None otherwise.
    """
    if not description:
        return None

    rules = conn.execute(
        """SELECT pattern, match_type, category FROM category_rules
           WHERE source = 'user' AND is_active = 1
           ORDER BY priority DESC"""
    ).fetchall()

    # Canonical form for 'contains' matching — same function used to create the rule pattern.
    # Comparing canonical-to-canonical (equality) instead of pattern-in-raw-description
    # prevents mid-string noise tokens (e.g. store numbers) from breaking the match.
    desc_normalized = _extract_merchant_pattern(description)
    desc_upper = description.upper()  # still needed for 'exact' match_type

    for rule in rules:
        pattern = rule[0]
        match_type = rule[1]
        category = rule[2]

        if match_type == "contains":
            if desc_normalized and pattern == desc_normalized:
                return category
        elif match_type == "regex":
            if re.search(pattern, description, re.IGNORECASE):
                return category
        elif match_type == "exact":
            if pattern.upper() == desc_upper:
                return category

    return None


def _snapshot_net_worth(conn, timestamp: str):
    """Take a net worth snapshot after sync."""
    today = timestamp[:10]  # YYYY-MM-DD

    # Get all profiles
    profiles = conn.execute("SELECT id FROM profiles").fetchall()

    for profile_row in profiles:
        profile_id = profile_row[0]

        assets, owed = _account_balance_totals(conn, profile=profile_id)

        conn.execute(
            """INSERT OR REPLACE INTO net_worth_history
               (date, profile_id, total_assets, total_owed, net_worth)
               VALUES (?, ?, ?, ?, ?)""",
            (today, profile_id, assets, owed, assets - owed),
        )

    # Household snapshot (all accounts)
    total_assets, total_owed = _account_balance_totals(conn, profile="household")

    conn.execute(
        """INSERT OR REPLACE INTO net_worth_history
           (date, profile_id, total_assets, total_owed, net_worth)
           VALUES (?, 'household', ?, ?, ?)""",
        (today, total_assets, total_owed, total_assets - total_owed),
    )


# ══════════════════════════════════════════════════════════════════════════════
# WRITE OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

def update_transaction_category(tx_id: str, new_category: str, one_off: bool = False) -> dict | bool:
    """
    Update a single transaction's category (user override).

    one_off=False (default / "Always"): creates a user rule, applies it retroactively
    to all matching transactions, and syncs merchant metadata — identical to the
    previous behaviour.

    one_off=True ("Just this transaction"): only updates this transaction and sets
    category_pinned=1 so future rule applications cannot overwrite it.  No rule is
    created and no other transactions are touched.

    Returns:
        - False if transaction not found
        - dict with {"updated": True} and optionally subscription_prompt data
          if new_category is "Subscriptions"
    """
    with get_db() as conn:
        # Get the transaction
        row = conn.execute(
            """SELECT description, category, amount, merchant_name, profile_id,
                      merchant_key, raw_description, account_type, transaction_type,
                      counterparty_type, teller_category
               FROM transactions
               WHERE id = ?""",
            (tx_id,),
        ).fetchone()

        if not row:
            return False

        description = row[0]
        old_category = row[1]
        tx_amount = row[2]
        tx_merchant_name = row[3]
        tx_profile_id = row[4]
        tx_merchant_key = row[5] if len(row) > 5 else ""
        tx_for_expense_type = {
            "description": description,
            "raw_description": row[6] if len(row) > 6 else "",
            "amount": tx_amount,
            "category": new_category,
            "profile_id": tx_profile_id,
            "account_type": row[7] if len(row) > 7 else "",
            "type": row[8] if len(row) > 8 else "",
            "counterparty_type": row[9] if len(row) > 9 else "",
            "teller_category": row[10] if len(row) > 10 else "",
        }
        account_lookup = _build_account_lookup(conn)
        new_expense_type = (
            _classify_transfer_type(tx_for_expense_type, account_lookup)
            if new_category in TRANSFER_CATEGORIES
            else None
        )

        # Ensure category exists before the transaction update for FK safety.
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, is_system) VALUES (?, 0)",
            (new_category,),
        )

        # Update the transaction.
        # category_pinned=1 when one-off: guards against future rule overwrites.
        # category_pinned=0 when always: clears any prior pin so rules can apply normally.
        conn.execute(
            """UPDATE transactions
               SET category = ?, categorization_source = 'user',
                   original_category = COALESCE(original_category, ?),
                   confidence = 'manual',
                   category_pinned = ?,
                   expense_type = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (new_category, old_category, 1 if one_off else 0, new_expense_type, tx_id),
        )

        if not one_off:
            # "Always" path: create rule, backfill matching transactions, sync merchant.
            pattern = _extract_merchant_pattern(description)
            merchant_pattern = canonicalize_merchant_key(tx_merchant_key or tx_merchant_name or "")
            rule_pattern = merchant_pattern or pattern
            if rule_pattern and len(rule_pattern) >= 3:
                _upsert_user_category_rule(
                    conn,
                    pattern=rule_pattern,
                    category=new_category,
                    profile_id=tx_profile_id,
                )
                retroactive_count = _apply_category_rule_to_transactions(
                    conn,
                    pattern=rule_pattern,
                    category=new_category,
                    profile_id=tx_profile_id,
                    exclude_tx_id=tx_id,
                )
                _upsert_merchant_category_metadata(
                    conn,
                    merchant_key=rule_pattern,
                    profile_id=tx_profile_id,
                    category=new_category,
                )
            else:
                retroactive_count = 0
        else:
            # "One-off" path: no rule, no retroactive application, no merchant sync.
            retroactive_count = 0

        result = {"updated": True, "retroactive_count": retroactive_count}

        # Offer recurrence tracking for fixed-obligation categories. This keeps
        # the workflow anchored on the transaction the user just corrected.
        recurring_categories = {
            "subscriptions",
            "insurance",
            "utilities",
            "internet & phone",
            "housing",
            "rent & mortgage",
            "auto payment",
            "debt & loan payment",
            "childcare",
            "debt",
        }
        if new_category.strip().lower() in recurring_categories:
            merchant_pattern = _extract_merchant_pattern(description)
            result["subscription_prompt"] = True
            result["recurring_prompt"] = True
            result["category"] = new_category
            result["merchant"] = tx_merchant_name if tx_merchant_name else (merchant_pattern or description[:50])
            result["amount"] = round(abs(float(tx_amount)), 2) if tx_amount else 0.0
            result["transaction_id"] = tx_id
        
        return result


def _upsert_user_category_rule(
    conn,
    pattern: str,
    category: str,
    profile_id: str | None = None,
) -> int | None:
    """Create or update a user rule scoped to an optional profile."""
    normalized_pattern = (pattern or "").upper().strip()
    normalized_category = (category or "").strip()
    normalized_profile = (profile_id or "").strip() or None
    if not normalized_pattern or len(normalized_pattern) < 3 or not normalized_category:
        return None

    existing = conn.execute(
        """SELECT id
           FROM category_rules
           WHERE pattern = ?
             AND source = 'user'
             AND is_active = 1
             AND COALESCE(profile_id, '') = COALESCE(?, '')
           ORDER BY priority DESC, id DESC
           LIMIT 1""",
        (normalized_pattern, normalized_profile),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE category_rules
               SET category = ?,
                   priority = 1000
               WHERE id = ?""",
            (normalized_category, existing[0]),
        )
        return existing[0]

    cur = conn.execute(
        """INSERT INTO category_rules
           (pattern, match_type, category, priority, source, profile_id)
           VALUES (?, 'contains', ?, 1000, 'user', ?)""",
        (normalized_pattern, normalized_category, normalized_profile),
    )
    return cur.lastrowid


def _apply_category_rule_to_transactions(
    conn,
    pattern: str,
    category: str,
    profile_id: str | None = None,
    exclude_tx_id: str | None = None,
    key_pattern: str | None = None,
) -> int:
    """Apply a merchant-pattern category to matching transactions immediately."""
    normalized_pattern = (pattern or "").upper().strip()
    normalized_category = (category or "").strip()
    normalized_profile = (profile_id or "").strip() or None
    normalized_key_pattern = (key_pattern or normalized_pattern).upper().strip()
    if not normalized_pattern or not normalized_category:
        return 0

    where = [
        "(description_normalized = ? OR UPPER(COALESCE(merchant_key, '')) = ? OR UPPER(COALESCE(merchant_name, '')) = ?)",
        "categorization_source != 'user'",
        "category_pinned = 0",
    ]
    params: list = [normalized_pattern, normalized_key_pattern, normalized_pattern]

    if normalized_profile:
        where.append("profile_id = ?")
        params.append(normalized_profile)
    if exclude_tx_id:
        where.append("id != ?")
        params.append(exclude_tx_id)

    rows = conn.execute(
        f"""SELECT id, profile_id, description, raw_description, amount, account_type,
                   transaction_type, counterparty_type, teller_category
              FROM transactions
             WHERE {' AND '.join(where)}""",
        params,
    ).fetchall()
    if not rows:
        return 0

    account_lookup = _build_account_lookup(conn)
    updated = 0
    for row in rows:
        tx_for_expense_type = {
            "profile_id": row[1],
            "description": row[2] or "",
            "raw_description": row[3] or "",
            "amount": row[4] or 0,
            "category": normalized_category,
            "account_type": row[5] or "",
            "type": row[6] or "",
            "counterparty_type": row[7] or "",
            "teller_category": row[8] or "",
        }
        new_expense_type = (
            _classify_transfer_type(tx_for_expense_type, account_lookup)
            if normalized_category in TRANSFER_CATEGORIES
            else None
        )
        cur = conn.execute(
            """UPDATE transactions
               SET category = ?,
                   categorization_source = 'user-rule',
                   confidence = 'rule',
                   expense_type = ?,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (normalized_category, new_expense_type, row[0]),
        )
        updated += cur.rowcount or 0
    return updated


def _upsert_merchant_category_metadata(
    conn,
    merchant_key: str,
    profile_id: str | None,
    category: str,
) -> None:
    """Keep merchant directory metadata aligned with the latest chosen category."""
    normalized_key = (merchant_key or "").upper().strip()
    normalized_profile = (profile_id or "").strip() or None
    normalized_category = (category or "").strip()
    if not normalized_key or not normalized_profile or not normalized_category:
        return

    existing = conn.execute(
        """SELECT merchant_key
           FROM merchants
           WHERE profile_id = ?
             AND (
                 UPPER(TRIM(COALESCE(merchant_key, ''))) = ?
                 OR UPPER(TRIM(COALESCE(clean_name, ''))) = ?
             )
           ORDER BY CASE WHEN UPPER(TRIM(COALESCE(merchant_key, ''))) = ? THEN 0 ELSE 1 END,
                    updated_at DESC
           LIMIT 1""",
        (normalized_profile, normalized_key, normalized_key, normalized_key),
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE merchants
               SET category = ?,
                   source = 'user',
                   updated_at = datetime('now')
               WHERE merchant_key = ? AND profile_id = ?""",
            (normalized_category, existing[0], normalized_profile),
        )
        return

    conn.execute(
        """INSERT INTO merchants
               (merchant_key, profile_id, category, source, updated_at)
           VALUES (?, ?, ?, 'user', datetime('now'))""",
        (normalized_key, normalized_profile, normalized_category),
    )


def update_transaction_excluded(tx_id: str, is_excluded: bool, conn=None) -> dict | None:
    """Update the exclusion flag for a single transaction."""
    def _update(c):
        existing = c.execute(
            """SELECT id, profile_id, description, amount, category, merchant_name,
                      is_excluded, categorization_source, updated_at
               FROM transactions
               WHERE id = ?""",
            (tx_id,),
        ).fetchone()
        if not existing:
            return None

        c.execute(
            """UPDATE transactions
               SET is_excluded = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (1 if is_excluded else 0, tx_id),
        )
        row = c.execute(
            """SELECT id, profile_id, description, amount, category, merchant_name,
                      is_excluded, categorization_source, updated_at
               FROM transactions
               WHERE id = ?""",
            (tx_id,),
        ).fetchone()
        return dict(row) if row else None

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def update_transaction_metadata(
    tx_id: str,
    notes: str | None = None,
    tags: list[str] | None = None,
    reviewed: bool | None = None,
    conn=None,
) -> dict | None:
    """Update notes, tags, and reviewed state for a transaction."""
    def _update(c):
        row = c.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not row:
            return None
        updates = []
        params = []
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes.strip())
        if tags is not None:
            normalized_tags = ",".join(sorted({str(tag).strip() for tag in tags if str(tag).strip()}))
            updates.append("tags = ?")
            params.append(normalized_tags)
        if reviewed is not None:
            updates.append("reviewed = ?")
            params.append(1 if reviewed else 0)
        if not updates:
            return {"id": tx_id}
        updates.append("updated_at = datetime('now')")
        params.append(tx_id)
        c.execute(f"UPDATE transactions SET {', '.join(updates)} WHERE id = ?", params)
        updated = c.execute(
            "SELECT id, notes, tags, reviewed, updated_at FROM transactions WHERE id = ?",
            (tx_id,),
        ).fetchone()
        result = dict(updated)
        result["tags"] = [tag for tag in (result.get("tags") or "").split(",") if tag]
        result["reviewed"] = bool(result.get("reviewed", 0))
        return result

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_transaction_splits(tx_id: str, conn=None) -> list[dict]:
    def _query(c):
        rows = c.execute(
            """SELECT id, transaction_id, category, amount, notes, tags, created_at, updated_at
               FROM transaction_splits
               WHERE transaction_id = ?
               ORDER BY id""",
            (tx_id,),
        ).fetchall()
        items = dicts_from_rows(rows)
        for item in items:
            item["tags"] = [tag for tag in (item.get("tags") or "").split(",") if tag]
        return items

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def replace_transaction_splits(tx_id: str, splits: list[dict], conn=None) -> dict | None:
    """Replace all split allocations for a transaction."""
    def _update(c):
        tx = c.execute("SELECT id, amount FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not tx:
            return None

        c.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (tx_id,))
        total = 0.0
        for split in splits:
            category = (split.get("category") or "").strip()
            amount = abs(float(split.get("amount") or 0))
            if not category or amount <= 0:
                continue
            c.execute("INSERT OR IGNORE INTO categories (name, is_system) VALUES (?, 0)", (category,))
            tags = ",".join(sorted({str(tag).strip() for tag in (split.get("tags") or []) if str(tag).strip()}))
            c.execute(
                """INSERT INTO transaction_splits
                   (transaction_id, category, amount, notes, tags)
                   VALUES (?, ?, ?, ?, ?)""",
                (tx_id, category, amount, (split.get("notes") or "").strip(), tags),
            )
            total += amount

        return {"transaction_id": tx_id, "split_total": round(total, 2), "items": get_transaction_splits(tx_id, conn=c)}

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def create_manual_account(payload: dict, profile: str | None = None, conn=None) -> dict:
    """Create a manual asset or liability account."""
    profile_id = _profile_id(profile)

    def _update(c):
        name = (payload.get("account_name") or payload.get("name") or "").strip()
        if not name:
            raise ValueError("Account name is required.")
        account_type = (payload.get("account_type") or "depository").strip()
        if account_type not in {"depository", "investment", "credit", "loan"}:
            raise ValueError("Unsupported account type.")
        subtype = (payload.get("account_subtype") or payload.get("type") or "manual").strip()
        balance = float(payload.get("balance") or 0)
        account_id = f"manual_{uuid4().hex[:16]}"
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        c.execute(
            """INSERT INTO accounts
               (id, profile_id, institution_name, account_name, account_type, account_subtype,
                current_balance, available_balance, currency, last_synced_at, is_active,
                provider, manual_updated_at, manual_notes)
               VALUES (?, ?, 'Manual', ?, ?, ?, ?, ?, 'USD', ?, 1, 'manual', ?, ?)""",
            (
                account_id,
                profile_id,
                name,
                account_type,
                subtype,
                balance,
                balance,
                now,
                now,
                (payload.get("notes") or "").strip(),
            ),
        )
        c.execute(
            """INSERT INTO manual_account_snapshots (account_id, profile_id, balance, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (account_id, profile_id, balance, now),
        )
        _snapshot_net_worth(c, now)
        return dict(c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def update_manual_account(account_id: str, payload: dict, profile: str | None = None, conn=None) -> dict | None:
    """Update a manual account balance/details and write a balance snapshot."""
    profile_id = _profile_id(profile)

    def _update(c):
        row = c.execute(
            "SELECT * FROM accounts WHERE id = ? AND provider = 'manual' AND profile_id = ? AND is_active = 1",
            (account_id, profile_id),
        ).fetchone()
        if not row:
            return None
        current = dict(row)
        name = (payload.get("account_name") or payload.get("name") or current.get("account_name") or "").strip()
        account_type = (payload.get("account_type") or current.get("account_type") or "depository").strip()
        subtype = (payload.get("account_subtype") or payload.get("type") or current.get("account_subtype") or "manual").strip()
        balance = float(payload.get("balance") if payload.get("balance") is not None else current.get("current_balance") or 0)
        notes = (payload.get("notes") if payload.get("notes") is not None else current.get("manual_notes") or "").strip()
        now = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        c.execute(
            """UPDATE accounts
               SET account_name = ?, account_type = ?, account_subtype = ?,
                   current_balance = ?, available_balance = ?, last_synced_at = ?,
                   manual_updated_at = ?, manual_notes = ?
               WHERE id = ?""",
            (name, account_type, subtype, balance, balance, now, now, notes, account_id),
        )
        c.execute(
            """INSERT INTO manual_account_snapshots (account_id, profile_id, balance, recorded_at)
               VALUES (?, ?, ?, ?)""",
            (account_id, profile_id, balance, now),
        )
        _snapshot_net_worth(c, now)
        return dict(c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def deactivate_manual_account(account_id: str, profile: str | None = None, conn=None) -> bool:
    profile_id = _profile_id(profile)

    def _update(c):
        cur = c.execute(
            "UPDATE accounts SET is_active = 0 WHERE id = ? AND provider = 'manual' AND profile_id = ?",
            (account_id, profile_id),
        )
        if cur.rowcount > 0:
            _snapshot_net_worth(c, datetime.now().replace(microsecond=0).isoformat(sep=" "))
        return cur.rowcount > 0

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_transactions_for_merchant(
    merchant_key: str,
    profile_id: str | None = None,
    limit: int = 25,
    conn=None,
) -> list[dict]:
    """Return recent transactions related to a merchant directory row.

    merchant_key is the effective upper-cased merchant identifier, which is
    UPPER(merchant_name) for enriched merchants or UPPER(description_normalized)
    for unenriched ones. Both comparisons use UPPER() so case mismatches don't
    prevent matches.
    """
    normalized_key = (merchant_key or "").upper().strip()
    if not normalized_key:
        return []

    def _query(c):
        alias_join_sql = _merchant_alias_join_sql("t", "merchant_alias")
        metadata_join_sql = _merchant_metadata_join_sql("t", "merchant_meta")
        merchant_label_sql = _canonical_merchant_label_sql("t")
        merchant_key_sql = _canonical_merchant_key_sql("t")
        merchant_source_sql = _merchant_display_source_sql("t", "merchant_alias", "merchant_meta")
        tx_source = _transactions_source(alias="t")
        sql = f"""SELECT t.id as original_id, t.profile_id as profile, t.date, t.description,
                        t.amount, t.category, t.original_category, t.categorization_source,
                        t.account_name, t.merchant_name, t.merchant_key, t.merchant_source,
                        t.merchant_confidence, t.merchant_kind, t.is_excluded, t.updated_at,
                        COALESCE(NULLIF(merchant_alias.display_name, ''), NULLIF(merchant_meta.clean_name, ''), {merchant_label_sql}) AS merchant_display_name,
                        COALESCE(NULLIF(merchant_meta.industry, ''), NULLIF(t.merchant_industry, ''), '') AS merchant_display_industry,
                        {merchant_source_sql} AS merchant_display_source
                 FROM {tx_source}
                 {alias_join_sql}
                 {metadata_join_sql}
                 WHERE {merchant_key_sql} = ?"""
        params: list = [normalized_key]
        if profile_id:
            sql += " AND t.profile_id = ?"
            params.append(profile_id)
        sql += " ORDER BY t.date DESC, t.id DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        items = dicts_from_rows(rows)
        for item in items:
            item["is_excluded"] = bool(item.get("is_excluded", 0))
        return items

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_category_rule_impact(
    rule_id: int,
    profile: str | None = None,
    limit: int = 20,
    conn=None,
) -> dict | None:
    """Return a rule plus matching transaction count and sample rows."""
    def _query(c):
        rule = c.execute(
            """SELECT id, pattern, match_type, category, priority, source, profile_id,
                      is_active, created_at
               FROM category_rules
               WHERE id = ?""",
            (rule_id,),
        ).fetchone()
        if not rule:
            return None

        rule_item = dict(rule)
        pattern = rule_item["pattern"] or ""
        match_type = rule_item["match_type"] or "contains"
        scoped_profile = profile if profile and profile != "household" else None

        if match_type == "contains":
            sql = """SELECT id as original_id, profile_id as profile, date, description,
                            amount, category, merchant_name, categorization_source
                     FROM transactions_visible
                     WHERE (
                        description_normalized = ?
                        OR UPPER(COALESCE(merchant_key, '')) = ?
                        OR UPPER(COALESCE(merchant_name, '')) = ?
                     )"""
            params: list = [pattern, pattern, pattern]
            count_sql = """SELECT COUNT(*)
                           FROM transactions_visible
                           WHERE (
                              description_normalized = ?
                              OR UPPER(COALESCE(merchant_key, '')) = ?
                              OR UPPER(COALESCE(merchant_name, '')) = ?
                           )"""
            count_params: list = [pattern, pattern, pattern]
            if scoped_profile:
                sql += " AND profile_id = ?"
                count_sql += " AND profile_id = ?"
                params.append(scoped_profile)
                count_params.append(scoped_profile)
            sql += " ORDER BY date DESC, id DESC LIMIT ?"
            params.append(limit)
            count = c.execute(count_sql, count_params).fetchone()[0]
            sample = dicts_from_rows(c.execute(sql, params).fetchall())
            return {**rule_item, "match_count": count, "sample": sample}

        if match_type == "exact":
            sql = """SELECT id as original_id, profile_id as profile, date, description,
                            amount, category, merchant_name, categorization_source
                     FROM transactions_visible
                     WHERE UPPER(COALESCE(description, '')) = ?"""
            params: list = [pattern.upper()]
            count_sql = "SELECT COUNT(*) FROM transactions_visible WHERE UPPER(COALESCE(description, '')) = ?"
            count_params: list = [pattern.upper()]
            if scoped_profile:
                sql += " AND profile_id = ?"
                count_sql += " AND profile_id = ?"
                params.append(scoped_profile)
                count_params.append(scoped_profile)
            sql += " ORDER BY date DESC, id DESC LIMIT ?"
            params.append(limit)
            count = c.execute(count_sql, count_params).fetchone()[0]
            sample = dicts_from_rows(c.execute(sql, params).fetchall())
            return {**rule_item, "match_count": count, "sample": sample}

        sql = """SELECT id as original_id, profile_id as profile, date, description,
                        amount, category, merchant_name, categorization_source
                 FROM transactions_visible WHERE 1=1"""
        params = []
        if scoped_profile:
            sql += " AND profile_id = ?"
            params.append(scoped_profile)
        sql += " ORDER BY date DESC, id DESC"
        rows = dicts_from_rows(c.execute(sql, params).fetchall())

        regex = re.compile(pattern, re.IGNORECASE)
        sample = []
        match_count = 0
        for row in rows:
            if regex.search(row.get("description") or ""):
                match_count += 1
                if len(sample) < limit:
                    sample.append(row)
        return {**rule_item, "match_count": match_count, "sample": sample}

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def add_category(name: str) -> bool:
    """Add a new user-defined category."""
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO categories (name, is_system) VALUES (?, 0)",
                (name,),
            )
            return True
        except Exception:
            return False


def deactivate_category(name: str, replacement_category: str | None = None, conn=None) -> dict | None:
    """Deactivate a custom category, optionally moving existing references first."""
    category_name = (name or "").strip()
    replacement = (replacement_category or "").strip() or None
    if not category_name:
        raise ValueError("Category name cannot be empty.")
    if replacement and replacement == category_name:
        raise ValueError("Replacement category must be different.")

    def _update(c):
        row = c.execute(
            "SELECT name, is_system, is_active FROM categories WHERE name = ?",
            (category_name,),
        ).fetchone()
        if not row:
            return None
        if int(row["is_system"] or 0) == 1:
            raise ValueError("System categories cannot be removed.")
        if int(row["is_active"] or 0) == 0:
            return {
                "category": category_name,
                "deactivated": True,
                "already_inactive": True,
                "replacement_category": replacement,
                "transactions_moved": 0,
                "splits_moved": 0,
                "rules_updated": 0,
            }

        tx_count = c.execute(
            "SELECT COUNT(*) FROM transactions WHERE category = ?",
            (category_name,),
        ).fetchone()[0]
        split_count = c.execute(
            "SELECT COUNT(*) FROM transaction_splits WHERE category = ?",
            (category_name,),
        ).fetchone()[0]
        budget_count = c.execute(
            "SELECT COUNT(*) FROM category_budgets WHERE category = ?",
            (category_name,),
        ).fetchone()[0]
        goal_count = c.execute(
            "SELECT COUNT(*) FROM goals WHERE linked_category = ?",
            (category_name,),
        ).fetchone()[0]

        transactions_moved = 0
        splits_moved = 0
        rules_updated = 0

        if replacement:
            replacement_row = c.execute(
                "SELECT name FROM categories WHERE name = ? AND is_active = 1",
                (replacement,),
            ).fetchone()
            if not replacement_row:
                raise ValueError("Replacement category not found.")

            transactions_moved = c.execute(
                "UPDATE transactions SET category = ?, updated_at = datetime('now') WHERE category = ?",
                (replacement, category_name),
            ).rowcount or 0
            splits_moved = c.execute(
                "UPDATE transaction_splits SET category = ?, updated_at = datetime('now') WHERE category = ?",
                (replacement, category_name),
            ).rowcount or 0
            rules_updated = c.execute(
                "UPDATE category_rules SET category = ? WHERE category = ?",
                (replacement, category_name),
            ).rowcount or 0
            c.execute(
                "UPDATE merchants SET category = ?, updated_at = datetime('now') WHERE category = ?",
                (replacement, category_name),
            )
            c.execute(
                "UPDATE goals SET linked_category = ?, updated_at = datetime('now') WHERE linked_category = ?",
                (replacement, category_name),
            )

            for budget in c.execute(
                """SELECT profile_id, amount, rollover_mode, rollover_balance
                     FROM category_budgets
                    WHERE category = ?""",
                (category_name,),
            ).fetchall():
                c.execute(
                    """INSERT OR IGNORE INTO category_budgets
                       (profile_id, category, amount, rollover_mode, rollover_balance)
                       VALUES (?, ?, ?, ?, ?)""",
                    (budget[0], replacement, budget[1], budget[2], budget[3]),
                )
            c.execute("DELETE FROM category_budgets WHERE category = ?", (category_name,))
        elif tx_count or split_count or budget_count or goal_count:
            raise ValueError("Category is still in use. Choose a replacement category first.")
        else:
            rules_updated = c.execute(
                "UPDATE category_rules SET is_active = 0 WHERE category = ? AND is_active = 1",
                (category_name,),
            ).rowcount or 0
            c.execute(
                "UPDATE merchants SET category = NULL, updated_at = datetime('now') WHERE category = ?",
                (category_name,),
            )

        c.execute("UPDATE categories SET parent_category = NULL WHERE parent_category = ?", (category_name,))
        c.execute(
            "UPDATE categories SET is_active = 0, parent_category = NULL WHERE name = ?",
            (category_name,),
        )

        return {
            "category": category_name,
            "deactivated": True,
            "replacement_category": replacement,
            "transactions_moved": transactions_moved,
            "splits_moved": splits_moved,
            "rules_updated": rules_updated,
        }

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_categories() -> list[str]:
    """Get all active category names."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name FROM categories WHERE is_active = 1 ORDER BY name"
        ).fetchall()
        return [row[0] for row in rows]


def get_categories_meta(conn=None) -> list[dict]:
    """Return category metadata for settings/control surfaces."""
    def _query(c):
        rows = c.execute(
            """SELECT name, is_system, parent_category, expense_type, expense_type_source,
                      is_active, created_at,
                      (SELECT COUNT(*)
                         FROM transactions t
                        WHERE t.category = categories.name
                          AND COALESCE(t.is_excluded, 0) = 0) AS transaction_count,
                      (SELECT COUNT(*)
                         FROM category_rules r
                        WHERE r.category = categories.name
                          AND r.is_active = 1) AS active_rule_count
               FROM categories
               ORDER BY is_active DESC, name"""
        ).fetchall()
        return dicts_from_rows(rows)

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def update_category_parent(category_name: str, parent_category: str | None, conn=None) -> dict | None:
    """Assign or clear a category parent group."""
    normalized_parent = parent_category.strip() if isinstance(parent_category, str) else None
    if normalized_parent == "":
        normalized_parent = None
    if normalized_parent and normalized_parent == category_name:
        raise ValueError("A category cannot be its own parent.")

    def _update(c):
        current = c.execute(
            "SELECT name FROM categories WHERE name = ? AND is_active = 1",
            (category_name,),
        ).fetchone()
        if not current:
            return None

        if normalized_parent:
            parent = c.execute(
                "SELECT name FROM categories WHERE name = ? AND is_active = 1",
                (normalized_parent,),
            ).fetchone()
            if not parent:
                raise ValueError("Parent category not found.")

        c.execute(
            "UPDATE categories SET parent_category = ? WHERE name = ?",
            (normalized_parent, category_name),
        )

        row = c.execute(
            """SELECT name, is_system, parent_category, expense_type, expense_type_source,
                      is_active, created_at
               FROM categories WHERE name = ?""",
            (category_name,),
        ).fetchone()
        return dict(row) if row else None

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_category_rules(source: str | None = None) -> list[dict]:
    """Get category rules, optionally filtered by source."""
    with get_db() as conn:
        if source:
            rows = conn.execute(
                """SELECT id, pattern, match_type, category, priority, source, is_active, created_at
                   FROM category_rules WHERE source = ? ORDER BY priority DESC""",
                (source,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, pattern, match_type, category, priority, source, is_active, created_at
                   FROM category_rules ORDER BY priority DESC"""
            ).fetchall()
        return dicts_from_rows(rows)


def update_category_rule(
    rule_id: int,
    category: str | None = None,
    priority: int | None = None,
    is_active: bool | None = None,
    conn=None,
) -> dict | None:
    """Update editable fields on a category rule."""
    updates = []
    params: list = []

    if category is not None:
        updates.append("category = ?")
        params.append(category)
    if priority is not None:
        updates.append("priority = ?")
        params.append(priority)
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if is_active else 0)

    if not updates:
        raise ValueError("No rule changes provided.")

    def _update(c):
        existing = c.execute(
            "SELECT id FROM category_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if not existing:
            return None

        if category is not None:
            category_row = c.execute(
                "SELECT name FROM categories WHERE name = ? AND is_active = 1",
                (category,),
            ).fetchone()
            if not category_row:
                raise ValueError("Category not found.")

        params_with_id = [*params, rule_id]
        c.execute(
            f"UPDATE category_rules SET {', '.join(updates)} WHERE id = ?",
            params_with_id,
        )
        row = c.execute(
            """SELECT id, pattern, match_type, category, priority, source, profile_id,
                      is_active, created_at
               FROM category_rules WHERE id = ?""",
            (rule_id,),
        ).fetchone()
        return dict(row) if row else None

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def _clip_text(value, limit: int) -> str:
    text = "" if value is None else str(value)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def prepare_copilot_history_record(
    *,
    profile: str | None,
    question: str,
    generated_sql: str = "",
    result: str = "",
    answer: str = "",
    operation: str = "read",
    rows_affected: int = 0,
    route: dict | None = None,
) -> dict:
    """Build a bounded history row.

    Copilot may produce long creative/code/general answers, but the activity
    drawer only needs a prompt, short answer preview, and compact audit data.
    The full answer has already been streamed to the user and should not make
    SQLite the transcript archive.
    """
    route = route or {}
    intent = str(route.get("intent") or "").strip().lower()
    operation_key = operation or intent or "read"
    is_chat = intent == "chat" or operation_key == "chat"
    answer_limit = _COPILOT_HISTORY_CHAT_PREVIEW_CHARS if is_chat else _COPILOT_HISTORY_FINANCE_PREVIEW_CHARS
    return {
        "profile": profile,
        "question": _clip_text(question, 4000),
        "sql": _clip_text(generated_sql, _COPILOT_HISTORY_SQL_CHARS),
        "result": _clip_text(result, _COPILOT_HISTORY_RESULT_CHARS),
        "answer": _clip_text(answer, answer_limit),
        "operation": operation_key,
        "rows_affected": rows_affected or 0,
    }


def get_copilot_conversations(limit: int = 50, profile: str | None = None, conn=None) -> list[dict]:
    """Return recent Copilot conversations for the given profile."""
    def _query(c):
        params: list = [limit]
        if profile and profile != "household":
            rows = c.execute(
                """SELECT id, profile_id, user_message, generated_sql, query_result, assistant_response,
                          operation_type, rows_affected, created_at
                   FROM copilot_conversations
                   WHERE profile_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT ?""",
                [profile, limit],
            ).fetchall()
        else:
            rows = c.execute(
                """SELECT id, profile_id, user_message, generated_sql, query_result, assistant_response,
                          operation_type, rows_affected, created_at
                   FROM copilot_conversations
                   ORDER BY created_at DESC, id DESC
                   LIMIT ?""",
                params,
            ).fetchall()
        return dicts_from_rows(rows)

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def log_copilot_conversation(
    profile: str | None,
    question: str,
    sql: str = "",
    result: str = "",
    answer: str = "",
    operation: str = "read",
    rows_affected: int = 0,
    conn=None,
) -> None:
    """Persist a Copilot turn for the Recent Copilot Activity drawer."""
    def _insert(c):
        c.execute(
            """INSERT INTO copilot_conversations
               (profile_id, user_message, generated_sql, query_result,
                assistant_response, operation_type, rows_affected)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (profile, question, sql or "", result or "", answer or "", operation or "read", rows_affected or 0),
        )

    if conn is not None:
        _insert(conn)
        return
    with get_db() as c:
        _insert(c)


def prune_copilot_conversations(profile: str | None = None, keep: int | None = None, conn=None) -> int:
    """Keep recent Copilot activity bounded so SQLite does not grow forever."""
    keep = keep if keep is not None else _COPILOT_HISTORY_MAX_ROWS
    if keep <= 0:
        return clear_copilot_conversations(profile=profile, conn=conn)

    def _prune(c):
        if profile and profile != "household":
            cur = c.execute(
                """DELETE FROM copilot_conversations
                   WHERE profile_id = ?
                     AND id NOT IN (
                       SELECT id FROM copilot_conversations
                       WHERE profile_id = ?
                       ORDER BY created_at DESC, id DESC
                       LIMIT ?
                     )""",
                (profile, profile, keep),
            )
        else:
            cur = c.execute(
                """DELETE FROM copilot_conversations
                   WHERE id NOT IN (
                     SELECT id FROM copilot_conversations
                     ORDER BY created_at DESC, id DESC
                     LIMIT ?
                   )""",
                (keep,),
            )
        return cur.rowcount or 0

    if conn is not None:
        return _prune(conn)
    with get_db() as c:
        return _prune(c)


def clear_copilot_conversations(profile: str | None = None, conn=None) -> int:
    """Delete Copilot activity rows. A concrete profile clears only that profile; household clears all."""
    def _delete(c):
        if profile and profile != "household":
            cur = c.execute("DELETE FROM copilot_conversations WHERE profile_id = ?", (profile,))
        else:
            cur = c.execute("DELETE FROM copilot_conversations")
        return cur.rowcount or 0

    if conn is not None:
        return _delete(conn)
    with get_db() as c:
        return _delete(c)


def delete_copilot_conversation(conversation_id: int, profile: str | None = None, conn=None) -> int:
    """Delete one Copilot activity row, scoped to profile unless viewing household/all."""
    def _delete(c):
        if profile and profile != "household":
            cur = c.execute(
                "DELETE FROM copilot_conversations WHERE id = ? AND profile_id = ?",
                (conversation_id, profile),
            )
        else:
            cur = c.execute("DELETE FROM copilot_conversations WHERE id = ?", (conversation_id,))
        return cur.rowcount or 0

    if conn is not None:
        return _delete(conn)
    with get_db() as c:
        return _delete(c)


def get_data_browser_rows(
    table: str,
    profile: str | None = None,
    search: str | None = None,
    limit: int = 100,
    conn=None,
) -> list[dict]:
    """Return rows for a small safe data-browser allowlist."""
    table_key = (table or "").strip().lower()
    if table_key not in {
        "transactions",
        "accounts",
        "categories",
        "category_rules",
        "merchants",
        "user_declared_subscriptions",
        "subscription_events",
        "dismissed_recurring",
    }:
        raise ValueError("Unsupported table.")

    def _query(c):
        params: list = []

        if table_key == "transactions":
            sql = """SELECT id, date, description, amount, category, profile_id, account_name,
                            merchant_name, categorization_source, is_excluded
                     FROM transactions_visible
                     WHERE 1=1"""
            if profile and profile != "household":
                sql += " AND profile_id = ?"
                params.append(profile)
            if search:
                like = f"%{_escape_like(search)}%"
                sql += " AND (description LIKE ? ESCAPE '\\' OR merchant_name LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\')"
                params.extend([like, like, like])
            sql += " ORDER BY date DESC, created_at DESC LIMIT ?"
            params.append(limit)
            return dicts_from_rows(c.execute(sql, params).fetchall())

        if table_key == "accounts":
            sql = """SELECT id, profile_id, institution_name, account_name, account_type,
                            account_subtype, current_balance, available_balance, last_synced_at
                     FROM accounts WHERE is_active = 1"""
            if profile and profile != "household":
                sql += " AND profile_id = ?"
                params.append(profile)
            if search:
                like = f"%{_escape_like(search)}%"
                sql += " AND (account_name LIKE ? ESCAPE '\\' OR institution_name LIKE ? ESCAPE '\\')"
                params.extend([like, like])
            sql += " ORDER BY profile_id, institution_name, account_name LIMIT ?"
            params.append(limit)
            return dicts_from_rows(c.execute(sql, params).fetchall())

        if table_key == "categories":
            sql = """SELECT name, parent_category, expense_type, expense_type_source, is_system, is_active, created_at
                     FROM categories WHERE 1=1"""
            if search:
                like = f"%{_escape_like(search)}%"
                sql += " AND (name LIKE ? ESCAPE '\\' OR COALESCE(parent_category, '') LIKE ? ESCAPE '\\')"
                params.extend([like, like])
            sql += " ORDER BY name LIMIT ?"
            params.append(limit)
            return dicts_from_rows(c.execute(sql, params).fetchall())

        if table_key == "category_rules":
            sql = """SELECT id, pattern, match_type, category, priority, source, profile_id, is_active, created_at
                     FROM category_rules WHERE 1=1"""
            if search:
                like = f"%{_escape_like(search)}%"
                sql += " AND (pattern LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\')"
                params.extend([like, like])
            sql += " ORDER BY priority DESC, id DESC LIMIT ?"
            params.append(limit)
            return dicts_from_rows(c.execute(sql, params).fetchall())

        if table_key == "merchants":
            return get_merchant_directory(profile=profile, search=search, limit=limit, conn=c)

        if table_key == "user_declared_subscriptions":
            sql = """SELECT merchant_name, amount, frequency, profile_id, is_active, created_at, updated_at
                     FROM user_declared_subscriptions WHERE 1=1"""
            if profile and profile != "household":
                sql += " AND profile_id = ?"
                params.append(profile)
            if search:
                like = f"%{_escape_like(search)}%"
                sql += " AND merchant_name LIKE ? ESCAPE '\\'"
                params.append(like)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            return dicts_from_rows(c.execute(sql, params).fetchall())

        if table_key == "subscription_events":
            sql = """SELECT id, event_type, merchant_name, profile_id, detail, created_at, is_read
                     FROM subscription_events WHERE 1=1"""
            if profile and profile != "household":
                sql += " AND profile_id = ?"
                params.append(profile)
            if search:
                like = f"%{_escape_like(search)}%"
                sql += " AND (merchant_name LIKE ? ESCAPE '\\' OR event_type LIKE ? ESCAPE '\\')"
                params.extend([like, like])
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            return dicts_from_rows(c.execute(sql, params).fetchall())

        sql = """SELECT merchant_name, profile_id, dismissed_at
                 FROM dismissed_recurring WHERE 1=1"""
        if profile and profile != "household":
            sql += " AND profile_id = ?"
            params.append(profile)
        if search:
            like = f"%{_escape_like(search)}%"
            sql += " AND merchant_name LIKE ? ESCAPE '\\'"
            params.append(like)
        sql += " ORDER BY dismissed_at DESC LIMIT ?"
        params.append(limit)
        return dicts_from_rows(c.execute(sql, params).fetchall())

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def get_category_budgets(profile: str | None = None, conn=None) -> list[dict]:
    """Return durable budget settings for a profile."""
    profile_id = profile or "household"

    def _query(c):
        rows = c.execute(
            """SELECT category, amount, rollover_mode, rollover_balance, updated_at
               FROM category_budgets
               WHERE profile_id = ?
               ORDER BY category""",
            (profile_id,),
        ).fetchall()
        return dicts_from_rows(rows)

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def update_category_budget(
    category: str,
    amount: float | None,
    profile: str | None = None,
    conn=None,
    rollover_mode: str | None = None,
    rollover_balance: float | None = None,
) -> dict:
    """Create/update/delete a durable budget for a category."""
    profile_id = profile or "household"

    def _update(c):
        category_row = c.execute(
            "SELECT name FROM categories WHERE name = ? AND is_active = 1",
            (category,),
        ).fetchone()
        if not category_row:
            raise ValueError("Category not found.")

        allowed_rollovers = {"none", "surplus", "deficit", "both"}
        mode = rollover_mode if rollover_mode in allowed_rollovers else "none"
        balance = float(rollover_balance or 0)

        if amount is None or amount <= 0:
            c.execute(
                "DELETE FROM category_budgets WHERE profile_id = ? AND category = ?",
                (profile_id, category),
            )
            return {"category": category, "amount": None, "profile_id": profile_id}

        c.execute(
            """INSERT INTO category_budgets (profile_id, category, amount, rollover_mode, rollover_balance)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(profile_id, category) DO UPDATE SET
                   amount = excluded.amount,
                   rollover_mode = excluded.rollover_mode,
                   rollover_balance = excluded.rollover_balance,
                   updated_at = datetime('now')""",
            (profile_id, category, amount, mode, balance),
        )
        row = c.execute(
            """SELECT category, amount, rollover_mode, rollover_balance, updated_at
               FROM category_budgets
               WHERE profile_id = ? AND category = ?""",
            (profile_id, category),
        ).fetchone()
        result = dict(row) if row else {"category": category, "amount": amount}
        result["profile_id"] = profile_id
        return result

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def get_merchant_directory(
    profile: str | None = None,
    search: str | None = None,
    limit: int = 100,
    conn=None,
) -> list[dict]:
    """Return spend-based merchant totals without multiplying transaction rows.

    The directory is driven from transactions first so every merchant you have
    actually spent money with appears, even without enrichment. Merchant
    display aliases come from merchant_aliases, while merchant metadata is
    overlaid from a deduped merchant view using the existing merchant records
    as a best-effort bridge for older enrichment keys. Non-spending outflows such as
    transfers, income, and credit-card payments are excluded so "Total Spent"
    matches the rest of the product.
    """
    def _query(c):
        params: list = []
        spend_placeholders = ",".join("?" * len(NON_SPENDING_CATEGORIES))
        tx_where = [
            "t.amount < 0",
            "COALESCE(t.category, 'Other') NOT IN (" + spend_placeholders + ")",
            "TRIM(COALESCE(NULLIF(t.merchant_key,''), NULLIF(t.merchant_name,''), t.description_normalized, t.description, '')) != ''",
        ]
        params.extend(NON_SPENDING_CATEGORIES)
        if profile and profile != "household":
            tx_where.append("t.profile_id = ?")
            params.append(profile)

        base_sql = f"""
            WITH merchant_spend AS (
                SELECT
                    UPPER(TRIM(COALESCE(NULLIF(t.merchant_key,''), NULLIF(t.merchant_name,''), t.description_normalized, t.description, ''))) AS merchant_key,
                    MAX(COALESCE(NULLIF(t.merchant_name,''), t.description_normalized, t.description)) AS clean_name,
                    t.profile_id,
                    SUM(ABS(t.amount)) AS total_spent,
                    COUNT(*) AS charge_count,
                    MAX(t.date) AS last_spent_date
                FROM transactions_visible t
                WHERE {" AND ".join(tx_where)}
                GROUP BY
                    UPPER(TRIM(COALESCE(NULLIF(t.merchant_key,''), NULLIF(t.merchant_name,''), t.description_normalized, t.description, ''))),
                    t.profile_id
            ),
            merchant_alias AS (
                SELECT
                    profile_id,
                    UPPER(TRIM(merchant_key)) AS merchant_key,
                    display_name
                FROM merchant_aliases
                WHERE TRIM(COALESCE(merchant_key, '')) != ''
                  AND TRIM(COALESCE(display_name, '')) != ''
            ),
            merchant_overlay_candidates AS (
                SELECT
                    profile_id,
                    UPPER(TRIM(merchant_key)) AS overlay_key,
                    clean_name,
                    industry,
                    category,
                    source,
                    COALESCE(is_subscription, 0) AS is_subscription,
                    subscription_status,
                    last_charge_date
                FROM merchants
                WHERE TRIM(COALESCE(merchant_key, '')) != ''

                UNION ALL

                SELECT
                    profile_id,
                    UPPER(TRIM(clean_name)) AS overlay_key,
                    clean_name,
                    industry,
                    category,
                    source,
                    COALESCE(is_subscription, 0) AS is_subscription,
                    subscription_status,
                    last_charge_date
                FROM merchants
                WHERE TRIM(COALESCE(clean_name, '')) != ''
                  AND UPPER(TRIM(clean_name)) != UPPER(TRIM(COALESCE(merchant_key, '')))
            ),
            merchant_overlay AS (
                SELECT
                    profile_id,
                    overlay_key,
                    COALESCE(
                        MAX(CASE WHEN source = 'user' AND NULLIF(TRIM(clean_name), '') IS NOT NULL THEN clean_name END),
                        MAX(CASE WHEN NULLIF(TRIM(clean_name), '') IS NOT NULL THEN clean_name END)
                    ) AS clean_name,
                    COALESCE(
                        MAX(CASE WHEN source = 'user' AND NULLIF(TRIM(industry), '') IS NOT NULL THEN industry END),
                        MAX(CASE WHEN NULLIF(TRIM(industry), '') IS NOT NULL THEN industry END)
                    ) AS industry,
                    COALESCE(
                        MAX(CASE WHEN source = 'user' AND NULLIF(TRIM(category), '') IS NOT NULL THEN category END),
                        MAX(CASE WHEN NULLIF(TRIM(category), '') IS NOT NULL THEN category END)
                    ) AS category,
                    MAX(is_subscription) AS is_subscription,
                    COALESCE(
                        MAX(CASE WHEN source = 'user' AND NULLIF(TRIM(subscription_status), '') IS NOT NULL THEN subscription_status END),
                        MAX(CASE WHEN NULLIF(TRIM(subscription_status), '') IS NOT NULL THEN subscription_status END)
                    ) AS subscription_status,
                    MAX(last_charge_date) AS last_charge_date
                FROM merchant_overlay_candidates
                GROUP BY profile_id, overlay_key
            ),
            recurring_overlay_candidates AS (
                SELECT
                    profile_id,
                    UPPER(TRIM(merchant_key)) AS overlay_key,
                    display_name AS clean_name,
                    category,
                    CASE WHEN state = 'confirmed' THEN 'active' ELSE state END AS subscription_status,
                    last_seen_date
                FROM recurring_obligations
                WHERE TRIM(COALESCE(merchant_key, '')) != ''
                  AND state IN ('active', 'confirmed', 'candidate', 'inactive', 'stale', 'cancelled')

                UNION ALL

                SELECT
                    profile_id,
                    UPPER(TRIM(display_name)) AS overlay_key,
                    display_name AS clean_name,
                    category,
                    CASE WHEN state = 'confirmed' THEN 'active' ELSE state END AS subscription_status,
                    last_seen_date
                FROM recurring_obligations
                WHERE TRIM(COALESCE(display_name, '')) != ''
                  AND UPPER(TRIM(display_name)) != UPPER(TRIM(COALESCE(merchant_key, '')))
                  AND state IN ('active', 'confirmed', 'candidate', 'inactive', 'stale', 'cancelled')
            ),
            recurring_overlay AS (
                SELECT
                    profile_id,
                    overlay_key,
                    COALESCE(
                        MAX(NULLIF(TRIM(clean_name), '')),
                        overlay_key
                    ) AS clean_name,
                    COALESCE(
                        MAX(NULLIF(TRIM(category), '')),
                        'Subscriptions'
                    ) AS category,
                    1 AS is_subscription,
                    COALESCE(
                        MAX(CASE WHEN subscription_status IN ('active', 'confirmed') THEN 'active' END),
                        MAX(subscription_status)
                    ) AS subscription_status,
                    MAX(last_seen_date) AS last_charge_date
                FROM recurring_overlay_candidates
                GROUP BY profile_id, overlay_key
            )
            SELECT
                spend.merchant_key,
                COALESCE(alias.display_name, recurring_overlay.clean_name, overlay.clean_name, spend.clean_name) AS clean_name,
                overlay.industry,
                COALESCE(recurring_overlay.category, overlay.category) AS category,
                COALESCE(recurring_overlay.is_subscription, overlay.is_subscription, 0) AS is_subscription,
                COALESCE(recurring_overlay.subscription_status, overlay.subscription_status) AS subscription_status,
                COALESCE(recurring_overlay.last_charge_date, overlay.last_charge_date) AS last_charge_date,
                spend.profile_id,
                spend.total_spent,
                spend.charge_count,
                spend.last_spent_date
            FROM merchant_spend spend
            LEFT JOIN merchant_alias alias
                ON alias.profile_id = spend.profile_id
               AND alias.merchant_key = spend.merchant_key
            LEFT JOIN merchant_overlay overlay
                ON overlay.profile_id = spend.profile_id
               AND overlay.overlay_key = spend.merchant_key
            LEFT JOIN recurring_overlay
                ON recurring_overlay.profile_id = spend.profile_id
               AND recurring_overlay.overlay_key = spend.merchant_key
        """

        outer_where = "1=1"
        if search:
            like = f"%{_escape_like(search)}%"
            outer_where = """(
                sub.merchant_key LIKE ? ESCAPE '\\'
                OR COALESCE(sub.clean_name, '') LIKE ? ESCAPE '\\'
                OR COALESCE(sub.industry, '')   LIKE ? ESCAPE '\\'
                OR COALESCE(sub.category, '')   LIKE ? ESCAPE '\\'
            )"""
            params.extend([like, like, like, like])

        sql = f"""
            SELECT * FROM ({base_sql}) sub
            WHERE {outer_where}
            ORDER BY sub.total_spent DESC, sub.charge_count DESC, sub.merchant_key ASC
            LIMIT ?
        """
        params.append(limit)
        items = dicts_from_rows(c.execute(sql, params).fetchall())
        user_rule_categories = _load_user_rule_categories_for_merchants(c, items)
        inferred_categories = _infer_merchant_categories_from_transactions(c, items)
        for item in items:
            item["is_subscription"] = bool(item.get("is_subscription", 0))
            category_key = ((item.get("profile_id") or "").strip(), (item.get("merchant_key") or "").upper().strip())
            item["category"] = (
                inferred_categories.get(category_key)
                or user_rule_categories.get(category_key)
                or item.get("category")
            )
        return items

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _infer_merchant_categories_from_transactions(conn, merchant_items: list[dict]) -> dict[tuple[str, str], str]:
    """Infer the effective merchant category from current transaction history."""
    pending = [
        ((item.get("profile_id") or "").strip(), (item.get("merchant_key") or "").upper().strip())
        for item in merchant_items
        if (item.get("profile_id") or "").strip()
        and (item.get("merchant_key") or "").upper().strip()
    ]
    if not pending:
        return {}

    merchant_keys = sorted({merchant_key for _, merchant_key in pending})
    profile_ids = sorted({profile_id for profile_id, _ in pending})
    if not merchant_keys or not profile_ids:
        return {}

    key_placeholders = ",".join("?" * len(merchant_keys))
    profile_placeholders = ",".join("?" * len(profile_ids))
    spend_placeholders = ",".join("?" * len(NON_SPENDING_CATEGORIES))
    rows = conn.execute(
        f"""
        SELECT profile_id,
               merchant_key,
               category,
               COUNT(*) AS tx_count,
               SUM(CASE WHEN categorization_source IN ('user', 'user-rule')
                         OR confidence = 'manual'
                        THEN 1 ELSE 0 END) AS trusted_count
        FROM (
            SELECT
                t.profile_id,
                UPPER(TRIM(COALESCE(NULLIF(t.merchant_key,''), NULLIF(t.merchant_name,''), t.description_normalized, t.description, ''))) AS merchant_key,
                t.category,
                t.categorization_source,
                t.confidence
            FROM transactions_visible t
            WHERE t.amount < 0
              AND TRIM(COALESCE(t.category, '')) != ''
              AND COALESCE(t.category, 'Other') NOT IN ({spend_placeholders})
              AND UPPER(TRIM(COALESCE(NULLIF(t.merchant_key,''), NULLIF(t.merchant_name,''), t.description_normalized, t.description, ''))) IN ({key_placeholders})
              AND t.profile_id IN ({profile_placeholders})
        ) ranked
        GROUP BY profile_id, merchant_key, category
        """,
        [*NON_SPENDING_CATEGORIES, *merchant_keys, *profile_ids],
    ).fetchall()

    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (row[0] or "", row[1] or "")
        grouped.setdefault(key, []).append(
            {
                "category": row[2] or "",
                "count": int(row[3] or 0),
                "trusted_count": int(row[4] or 0),
            }
        )

    inferred: dict[tuple[str, str], str] = {}
    for key, options in grouped.items():
        if not options:
            continue
        total = sum(option["count"] for option in options)
        best = max(options, key=lambda option: (option["trusted_count"], option["count"], option["category"]))
        if total <= 0 or not best["category"]:
            continue

        dominance = best["count"] / total
        if best["trusted_count"] > 0 or len(options) == 1 or dominance >= 0.7:
            inferred[key] = best["category"]

    return inferred


def _load_user_rule_categories_for_merchants(conn, merchant_items: list[dict]) -> dict[tuple[str, str], str]:
    """Load active user-rule categories keyed by (profile_id, merchant_key)."""
    merchant_keys = sorted({
        (item.get("merchant_key") or "").upper().strip()
        for item in merchant_items
        if (item.get("merchant_key") or "").upper().strip()
    })
    profile_ids = sorted({
        (item.get("profile_id") or "").strip()
        for item in merchant_items
        if (item.get("profile_id") or "").strip()
    })
    if not merchant_keys or not profile_ids:
        return {}

    key_placeholders = ",".join("?" * len(merchant_keys))
    profile_placeholders = ",".join("?" * len(profile_ids))
    rows = conn.execute(
        f"""
        SELECT COALESCE(profile_id, '') AS profile_id, pattern, category
        FROM category_rules
        WHERE source = 'user'
          AND is_active = 1
          AND pattern IN ({key_placeholders})
          AND COALESCE(profile_id, '') IN ('', {profile_placeholders})
        ORDER BY CASE WHEN COALESCE(profile_id, '') = '' THEN 1 ELSE 0 END,
                 priority DESC,
                 id DESC
        """,
        [*merchant_keys, *profile_ids],
    ).fetchall()

    categories: dict[tuple[str, str], str] = {}
    global_categories: dict[str, str] = {}
    for row in rows:
        profile_id = (row[0] or "").strip()
        pattern = (row[1] or "").strip()
        category = row[2] or ""
        if profile_id:
            categories.setdefault((profile_id, pattern), category)
        else:
            global_categories.setdefault(pattern, category)

    for item in merchant_items:
        profile_id = (item.get("profile_id") or "").strip()
        merchant_key = (item.get("merchant_key") or "").upper().strip()
        key = (profile_id, merchant_key)
        if key not in categories and merchant_key in global_categories:
            categories[key] = global_categories[merchant_key]
    return categories


def _get_merchant_alias(conn, merchant_key: str, profile_id: str | None) -> str | None:
    """Return the user-facing alias for a canonical merchant key, if one exists."""
    normalized_key = (merchant_key or "").upper().strip()
    normalized_profile = (profile_id or "").strip()
    if not normalized_key or not normalized_profile:
        return None

    row = conn.execute(
        """SELECT display_name
           FROM merchant_aliases
           WHERE merchant_key = ? AND profile_id = ?""",
        (normalized_key, normalized_profile),
    ).fetchone()
    if not row:
        return None
    alias = (row[0] or "").strip()
    return alias or None


def _upsert_merchant_alias(
    conn,
    merchant_key: str,
    profile_id: str | None,
    display_name: str | None,
) -> str | None:
    """Create, update, or clear a merchant alias for a canonical merchant key."""
    normalized_key = (merchant_key or "").upper().strip()
    normalized_profile = (profile_id or "").strip()
    normalized_name = (display_name or "").strip()
    if not normalized_key or not normalized_profile:
        return None

    if not normalized_name or normalized_name.upper() == normalized_key:
        conn.execute(
            "DELETE FROM merchant_aliases WHERE merchant_key = ? AND profile_id = ?",
            (normalized_key, normalized_profile),
        )
        return None

    conn.execute(
        """INSERT INTO merchant_aliases
               (merchant_key, profile_id, display_name, source, updated_at)
           VALUES (?, ?, ?, 'user', datetime('now'))
           ON CONFLICT(merchant_key, profile_id) DO UPDATE SET
               display_name = excluded.display_name,
               source = 'user',
               updated_at = datetime('now')""",
        (normalized_key, normalized_profile, normalized_name),
    )
    return normalized_name


def update_merchant_directory_entry(
    merchant_key: str,
    profile_id: str,
    clean_name: str | None = None,
    category: str | None = None,
    domain: str | None = None,
    industry: str | None = None,
    conn=None,
) -> dict | None:
    """Update merchant metadata for a single merchant/profile row."""
    metadata_updates = []
    metadata_params: list = []
    normalized_category = (category or "").strip()
    normalized_profile_id = (profile_id or "").strip() or None
    normalized_key = (merchant_key or "").upper().strip()
    retroactive_count = 0

    if category is not None:
        metadata_updates.append("category = ?")
        metadata_params.append(normalized_category or None)
    if domain is not None:
        metadata_updates.append("domain = ?")
        metadata_params.append(domain.strip() or None)
    if industry is not None:
        metadata_updates.append("industry = ?")
        metadata_params.append(industry.strip() or None)

    if clean_name is None and not metadata_updates:
        raise ValueError("No merchant changes provided.")

    def _update(c):
        if not normalized_key or not normalized_profile_id:
            raise ValueError("Merchant key and profile are required.")

        if category is not None and normalized_category:
            category_row = c.execute(
                "SELECT name FROM categories WHERE name = ? AND is_active = 1",
                (normalized_category,),
            ).fetchone()
            if not category_row:
                raise ValueError("Category not found.")

        alias_name = None
        if clean_name is not None:
            alias_name = _upsert_merchant_alias(
                c,
                merchant_key=normalized_key,
                profile_id=normalized_profile_id,
                display_name=clean_name,
            )

        if metadata_updates:
            existing = c.execute(
                "SELECT merchant_key FROM merchants WHERE merchant_key = ? AND profile_id = ?",
                (normalized_key, normalized_profile_id),
            ).fetchone()

            if existing:
                params_with_key = [*metadata_params, normalized_key, normalized_profile_id]
                c.execute(
                    f"""UPDATE merchants
                        SET {', '.join(metadata_updates)},
                            source = 'user',
                            updated_at = datetime('now')
                        WHERE merchant_key = ? AND profile_id = ?""",
                    params_with_key,
                )
            else:
                ins_industry = industry.strip() if industry is not None else None
                ins_category = category.strip() or None if category is not None else None
                ins_domain = domain.strip() if domain is not None else None
                c.execute(
                    """INSERT INTO merchants
                           (merchant_key, profile_id, domain, industry, category, source, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'user', datetime('now'))""",
                    (normalized_key, normalized_profile_id, ins_domain, ins_industry, ins_category),
                )

        if normalized_category:
            pattern = _extract_merchant_pattern(normalized_key) or normalized_key
            _upsert_user_category_rule(
                c,
                pattern=pattern,
                category=normalized_category,
                profile_id=normalized_profile_id,
            )
            retroactive_count = _apply_category_rule_to_transactions(
                c,
                pattern=pattern,
                category=normalized_category,
                profile_id=normalized_profile_id,
            )
        else:
            retroactive_count = 0

        row = c.execute(
            """SELECT merchant_key, clean_name, category, domain, industry, source,
                      is_subscription, subscription_status, subscription_amount,
                      last_charge_date, next_expected_date, profile_id
               FROM merchants
               WHERE merchant_key = ? AND profile_id = ?""",
            (normalized_key, normalized_profile_id),
        ).fetchone()

        result = dict(row) if row else {
            "merchant_key": normalized_key,
            "clean_name": None,
            "category": normalized_category or None,
            "domain": domain.strip() or None if domain is not None else None,
            "industry": industry.strip() or None if industry is not None else None,
            "source": "user",
            "is_subscription": 0,
            "subscription_status": None,
            "subscription_amount": None,
            "last_charge_date": None,
            "next_expected_date": None,
            "profile_id": normalized_profile_id,
        }
        result["clean_name"] = alias_name or _get_merchant_alias(c, normalized_key, normalized_profile_id) or result.get("clean_name") or normalized_key
        result["retroactive_count"] = retroactive_count
        return result

    if conn is not None:
        return _update(conn)
    with get_db() as c:
        return _update(c)


def repair_non_spending_transaction_categories(conn=None) -> int:
    """
    Repair historical ACH/transfer rows that should have been classified as
    non-spending transfers or credit-card payments. User-authored categories
    are never overridden.
    """
    def _repair(c):
        account_lookup = _build_account_lookup(c)
        rows = c.execute(
            """SELECT id, profile_id, description, raw_description, amount, category,
                      categorization_source, transaction_type, account_type,
                      counterparty_type, teller_category, expense_type
               FROM transactions
               WHERE amount < 0
                 AND transaction_type IN ('ach', 'transfer')
                 AND COALESCE(categorization_source, '') NOT IN ('user', 'user-rule')
                 AND COALESCE(category_pinned, 0) = 0"""
        ).fetchall()

        updated = 0
        for row in rows:
            tx = {
                "profile_id": row[1],
                "description": row[2] or "",
                "raw_description": row[3] or "",
                "amount": row[4] or 0,
                "category": row[5] or "",
                "categorization_source": row[6] or "",
                "type": row[7] or "",
                "account_type": row[8] or "",
                "counterparty_type": row[9] or "",
                "teller_category": row[10] or "",
            }
            current_category = tx["category"]
            current_expense_type = row[11]
            new_category, confidence = _rule_based_categorize(tx)
            if confidence != "rule-high" or new_category not in TRANSFER_CATEGORIES:
                continue

            tx["category"] = new_category
            new_expense_type = _classify_transfer_type(tx, account_lookup)
            if new_category == current_category and new_expense_type == current_expense_type:
                continue

            c.execute(
                """UPDATE transactions
                   SET category = ?,
                       categorization_source = 'rule-high',
                       confidence = 'rule',
                       expense_type = ?,
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (new_category, new_expense_type, row[0]),
            )
            updated += 1

        if updated > 0:
            logger.info("Repaired %d historical non-spending transaction categories.", updated)
        return updated

    if conn is not None:
        return _repair(conn)
    with get_db() as c:
        return _repair(c)


def repair_polluted_merchant_categories(conn=None) -> int:
    """Clear enrichment-polluted merchant categories that merely mirror industry."""
    def _repair(c):
        cur = c.execute(
            """
            UPDATE merchants
               SET category = NULL,
                   updated_at = datetime('now')
             WHERE COALESCE(source, '') != 'user'
               AND NULLIF(TRIM(category), '') IS NOT NULL
               AND NULLIF(TRIM(industry), '') IS NOT NULL
               AND UPPER(TRIM(category)) = UPPER(TRIM(industry))
               AND UPPER(TRIM(category)) NOT IN (
                    SELECT UPPER(name) FROM categories WHERE is_active = 1
               )
            """
        )
        repaired = cur.rowcount
        if repaired > 0:
            logger.info("Cleared %d merchant categories polluted by enrichment industry.", repaired)
        return repaired

    if conn is not None:
        return _repair(conn)
    with get_db() as c:
        return _repair(c)


def repair_cc_income_misclassifications(conn=None) -> int:
    """
    Legacy hook retained for startup compatibility.

    The old implementation assumed every positive credit-account Income row was
    a Credit Card Payment. The category-first model no longer performs that live
    mutation automatically; use scripts/audit_cashflow_category_rewrite.py to
    review proposed historical changes before applying any migration.
    """
    def _repair(c):
        return 0

    if conn is not None:
        return _repair(conn)
    with get_db() as c:
        return _repair(c)


def reclassify_transfers(conn=None) -> int:
    """
    Re-classify all transfer transactions' expense_type using the current
    account lookup. Handles:
      - Backfill: transfers with expense_type = NULL
      - Re-evaluation: transfers whose classification may have changed
        (e.g., a new account was linked, turning an 'external' into 'household')

    Returns the number of transactions updated.

    Called:
      - On application startup (backfill NULL expense_types)
      - After new account enrollment (re-evaluate all transfers)
    """
    def _reclassify(c):
        account_lookup = _build_account_lookup(c)

        # Fetch all transfer transactions
        transfer_ph = ",".join("?" * len(TRANSFER_CATEGORIES))
        rows = c.execute(
            f"""SELECT id, profile_id, description, raw_description, category, expense_type
                FROM transactions
                WHERE category IN ({transfer_ph})""",
            list(TRANSFER_CATEGORIES),
        ).fetchall()

        updated = 0
        for row in rows:
            tx_id = row[0]
            tx_dict = {
                "profile_id": row[1],
                "description": row[2] or "",
                "raw_description": row[3] or "",
                "category": row[4] or "",
            }
            current_type = row[5]
            new_type = _classify_transfer_type(tx_dict, account_lookup)

            if new_type != current_type:
                c.execute(
                    "UPDATE transactions SET expense_type = ?, updated_at = datetime('now') WHERE id = ?",
                    (new_type, tx_id),
                )
                updated += 1

        if updated > 0:
            logger.info("Reclassified %d transfer transactions.", updated)
        return updated

    if conn is not None:
        return _reclassify(conn)
    with get_db() as c:
        return _reclassify(c)


# ══════════════════════════════════════════════════════════════════════════════
# COPILOT DETERMINISTIC TOOLS
# These functions back the curated chip prompts on the Copilot page.
# They return structured data; the route handler formats the answer string.
# ══════════════════════════════════════════════════════════════════════════════

def explain_category_assignment(
    merchant_query: str,
    profile: str | None = None,
    conn=None,
) -> dict:
    """
    Return why transactions matching merchant_query received their current category.
    Inspects actual transaction rows and category_rules — no LLM.
    """
    def _query(c):
        pattern = _extract_merchant_pattern(merchant_query)
        if not pattern:
            pattern = merchant_query.upper().strip()
        key_pattern = canonicalize_merchant_key(merchant_query) or pattern

        scoped = profile if profile and profile != "household" else None

        # Distribution of category + source for matching transactions
        base_where = "(description_normalized = ? OR UPPER(COALESCE(merchant_key,'')) = ? OR UPPER(COALESCE(merchant_name,'')) = ?)"
        dist_params: list = [pattern, key_pattern, pattern]
        dist_sql = f"""
            SELECT category, categorization_source, COUNT(*) as cnt
            FROM transactions_visible
            WHERE {base_where}
        """
        if scoped:
            dist_sql += " AND profile_id = ?"
            dist_params.append(scoped)
        dist_sql += " GROUP BY category, categorization_source ORDER BY cnt DESC LIMIT 10"

        dist_rows = dicts_from_rows(c.execute(dist_sql, dist_params).fetchall())

        total = sum(r["cnt"] for r in dist_rows)
        dominant = dist_rows[0] if dist_rows else None

        # Matching active rule (highest priority first)
        rule_row = c.execute(
            """SELECT id, pattern, match_type, category, priority, source
               FROM category_rules
               WHERE pattern = ? AND is_active = 1
               ORDER BY priority DESC LIMIT 1""",
            (pattern,),
        ).fetchone()

        # Recent sample transactions
        sample_params: list = [pattern, key_pattern, pattern]
        sample_sql = f"""
            SELECT date, description, amount, category, categorization_source, merchant_name
            FROM transactions_visible
            WHERE {base_where}
        """
        if scoped:
            sample_sql += " AND profile_id = ?"
            sample_params.append(scoped)
        sample_sql += " ORDER BY date DESC LIMIT 5"
        samples = dicts_from_rows(c.execute(sample_sql, sample_params).fetchall())

        return {
            "merchant_query": merchant_query,
            "normalized_pattern": pattern,
            "dominant_category": dominant["category"] if dominant else None,
            "dominant_source": dominant["categorization_source"] if dominant else None,
            "distribution": dist_rows,
            "rule": dict(rule_row) if rule_row else None,
            "transaction_count": total,
            "samples": samples,
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def find_merchants_missing_category(
    profile: str | None = None,
    conn=None,
) -> list[dict]:
    """
    Return description_normalized patterns that have transactions categorized
    as 'Other', 'Uncategorized', or empty, ordered by transaction count.
    """
    def _query(c):
        scoped = profile if profile and profile != "household" else None
        params: list = []
        sql = """
            SELECT
                COALESCE(description_normalized, UPPER(description)) as pattern,
                COUNT(*) as transaction_count,
                MAX(description) as example_description,
                MAX(date) as most_recent_date
            FROM transactions_visible
            WHERE (
                category IS NULL
                OR UPPER(TRIM(category)) IN ('OTHER', 'UNCATEGORIZED', '')
            )
        """
        if scoped:
            sql += " AND profile_id = ?"
            params.append(scoped)
        sql += """
            GROUP BY COALESCE(description_normalized, UPPER(description))
            HAVING pattern IS NOT NULL AND TRIM(pattern) != ''
            ORDER BY transaction_count DESC
            LIMIT 50
        """
        return dicts_from_rows(c.execute(sql, params).fetchall())

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _write_scoped_profile(profile: str | None) -> str | None:
    return profile if profile and profile != "household" else None


def _merchant_match_values(raw_text: str) -> tuple[str, str]:
    pattern = _extract_merchant_pattern(raw_text)
    if not pattern:
        pattern = (raw_text or "").upper().strip()
    key_pattern = canonicalize_merchant_key(raw_text) or pattern
    return pattern, key_pattern


def _merchant_match_clause(scoped: str | None, *, category_filter: str | None = None) -> tuple[str, list]:
    clause = """
        (description_normalized = ? OR UPPER(COALESCE(merchant_key,'')) = ? OR UPPER(COALESCE(merchant_name,'')) = ?)
    """
    params: list = []
    if category_filter is not None:
        clause += " AND COALESCE(category, '') != ?"
        params.append(category_filter)
    if scoped:
        clause += " AND profile_id = ?"
        params.append(scoped)
    return clause, params


def bulk_recategorize_preview(
    merchant_query: str,
    new_category: str,
    profile: str | None = None,
    conn=None,
) -> dict:
    """
    Preview how many transactions would be moved to new_category for the
    given merchant pattern. Returns count, sample rows, and a structured
    pending operation payload for the confirm step.
    """
    def _query(c):
        pattern, key_pattern = _merchant_match_values(merchant_query)
        scoped = _write_scoped_profile(profile)

        count_params: list = [pattern, key_pattern, pattern]
        match_clause, extra_params = _merchant_match_clause(scoped, category_filter=new_category)
        count_sql = f"""
            SELECT COUNT(*) FROM transactions_visible
            WHERE {match_clause}
        """
        count_params.extend(extra_params)

        count = c.execute(count_sql, count_params).fetchone()[0]

        sample_params: list = [pattern, key_pattern, pattern, new_category]
        sample_clause, sample_extra = _merchant_match_clause(scoped, category_filter=new_category)
        sample_sql = f"""
            SELECT date, description, amount, category as current_category,
                   categorization_source, merchant_name, merchant_key
            FROM transactions_visible
            WHERE {sample_clause}
        """
        sample_params = [pattern, key_pattern, pattern] + sample_extra
        sample_sql += " ORDER BY date DESC LIMIT 20"
        samples = dicts_from_rows(c.execute(sample_sql, sample_params).fetchall())

        return {
            "pattern": pattern,
            "key_pattern": key_pattern,
            "new_category": new_category,
            "count": count,
            "samples": samples,
            "pending_operation": {
                "operation": "bulk_recategorize",
                "params": {
                    "merchant_query": merchant_query,
                    "new_category": new_category,
                },
            },
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def preview_rule_creation(
    raw_pattern: str,
    category: str,
    profile: str | None = None,
    conn=None,
) -> dict:
    """
    Preview the impact of creating a new 'contains' user rule for raw_pattern -> category.
    Reuses the same match logic as get_category_rule_impact (description_normalized equality).
    Returns count, sample rows, whether a rule already exists, and a structured pending operation.
    """
    def _query(c):
        pattern, key_pattern = _merchant_match_values(raw_pattern)
        scoped = _write_scoped_profile(profile)

        # Check for existing rule
        existing_rule = c.execute(
            """SELECT id, pattern, category, priority, source, is_active
               FROM category_rules
               WHERE pattern = ?
                 AND is_active = 1
                 AND COALESCE(profile_id, '') = COALESCE(?, '')
               ORDER BY priority DESC LIMIT 1""",
            (pattern, scoped),
        ).fetchone()

        # Count + sample matching transactions (same logic as get_category_rule_impact contains branch)
        count_params: list = [pattern, key_pattern, pattern]
        count_sql = """
            SELECT COUNT(*) FROM transactions_visible
            WHERE (description_normalized = ? OR UPPER(COALESCE(merchant_key,'')) = ? OR UPPER(COALESCE(merchant_name,'')) = ?)
              AND categorization_source != 'user'
              AND COALESCE(category_pinned, 0) = 0
        """
        if scoped:
            count_sql += " AND profile_id = ?"
            count_params.append(scoped)

        count = c.execute(count_sql, count_params).fetchone()[0]

        sample_params: list = [pattern, key_pattern, pattern]
        sample_sql = """
            SELECT date, description, amount, category as current_category,
                   categorization_source, merchant_name, merchant_key
            FROM transactions_visible
            WHERE (description_normalized = ? OR UPPER(COALESCE(merchant_key,'')) = ? OR UPPER(COALESCE(merchant_name,'')) = ?)
              AND categorization_source != 'user'
              AND COALESCE(category_pinned, 0) = 0
        """
        if scoped:
            sample_sql += " AND profile_id = ?"
            sample_params.append(scoped)
        sample_sql += " ORDER BY date DESC LIMIT 20"
        samples = dicts_from_rows(c.execute(sample_sql, sample_params).fetchall())

        return {
            "pattern": pattern,
            "key_pattern": key_pattern,
            "category": category,
            "count": count,
            "samples": samples,
            "existing_rule": dict(existing_rule) if existing_rule else None,
            "pending_operation": {
                "operation": "create_rule",
                "params": {
                    "pattern": raw_pattern,
                    "category": category,
                },
            },
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def rename_merchant_variants(
    old_pattern: str,
    new_name: str,
    profile: str | None = None,
    conn=None,
) -> dict:
    """
    Preview assigning a merchant display alias across all matching transactions.
    The alias is stored per profile + canonical merchant key, leaving raw bank
    descriptions and enriched merchant_name values untouched.
    """
    def _query(c):
        pattern, key_pattern = _merchant_match_values(old_pattern)
        scoped = _write_scoped_profile(profile)

        count_params: list = [pattern, key_pattern, pattern]
        count_sql = """
            SELECT COUNT(*) FROM transactions_visible
            WHERE (description_normalized = ? OR UPPER(COALESCE(merchant_key,'')) = ? OR UPPER(COALESCE(merchant_name,'')) = ?)
        """
        if scoped:
            count_sql += " AND profile_id = ?"
            count_params.append(scoped)

        count = c.execute(count_sql, count_params).fetchone()[0]

        sample_params: list = [pattern, key_pattern, pattern]
        sample_sql = """
            SELECT date, description, merchant_name, merchant_key, amount, category
            FROM transactions_visible
            WHERE (description_normalized = ? OR UPPER(COALESCE(merchant_key,'')) = ? OR UPPER(COALESCE(merchant_name,'')) = ?)
        """
        if scoped:
            sample_sql += " AND profile_id = ?"
            sample_params.append(scoped)
        sample_sql += " ORDER BY date DESC LIMIT 20"
        samples = dicts_from_rows(c.execute(sample_sql, sample_params).fetchall())

        if scoped:
            target_profiles = [scoped]
        else:
            target_profiles = [
                row[0]
                for row in c.execute(
                    """
                    SELECT DISTINCT profile_id
                    FROM transactions_visible
                    WHERE (description_normalized = ? OR UPPER(COALESCE(merchant_key,'')) = ? OR UPPER(COALESCE(merchant_name,'')) = ?)
                    ORDER BY profile_id ASC
                    """,
                    (pattern, key_pattern, pattern),
                ).fetchall()
                if (row[0] or "").strip()
            ]

        return {
            "pattern": pattern,
            "key_pattern": key_pattern,
            "new_name": new_name,
            "count": count,
            "samples": samples,
            "profiles": target_profiles,
            "pending_operation": {
                "operation": "rename_merchant",
                "params": {
                    "old_name": old_pattern,
                    "new_name": new_name,
                },
            },
        }

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def _write_profile_filter(profile: str | None, column: str = "profile_id") -> tuple[str, list]:
    if profile and profile != "household":
        return f" AND {column} = ?", [profile]
    return "", []


def _transaction_for_write(conn, tx_id: str, profile: str | None = None) -> dict | None:
    tx_id = (tx_id or "").strip()
    if not tx_id:
        return None
    profile_sql, profile_params = _write_profile_filter(profile)
    row = conn.execute(
        f"""SELECT id, profile_id, date, description, amount, category, notes, tags, reviewed
              FROM transactions
             WHERE id = ?{profile_sql}""",
        [tx_id, *profile_params],
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["reviewed"] = bool(result.get("reviewed", 0))
    result["tags"] = [tag for tag in (result.get("tags") or "").split(",") if tag]
    return result


def _goal_for_write(conn, params: dict, profile: str | None = None) -> dict | None:
    profile_id = _profile_id(profile)
    goal_id = params.get("goal_id") or params.get("id")
    if goal_id:
        row = conn.execute(
            "SELECT * FROM goals WHERE id = ? AND profile_id = ? AND is_active = 1",
            (goal_id, profile_id),
        ).fetchone()
        return dict(row) if row else None
    name = (params.get("name") or params.get("goal_name") or "").strip()
    if not name:
        return None
    row = conn.execute(
        """SELECT *
             FROM goals
            WHERE profile_id = ?
              AND is_active = 1
              AND lower(name) = lower(?)
            ORDER BY updated_at DESC
            LIMIT 1""",
        (profile_id, name),
    ).fetchone()
    return dict(row) if row else None


def _manual_account_for_write(conn, params: dict, profile: str | None = None) -> dict | None:
    profile_id = _profile_id(profile)
    account_id = (params.get("account_id") or "").strip()
    if account_id:
        row = conn.execute(
            """SELECT *
                 FROM accounts
                WHERE id = ?
                  AND profile_id = ?
                  AND provider = 'manual'
                  AND is_active = 1""",
            (account_id, profile_id),
        ).fetchone()
        return dict(row) if row else None
    account_name = (params.get("account_name") or params.get("name") or "").strip()
    if not account_name:
        return None
    row = conn.execute(
        """SELECT *
             FROM accounts
            WHERE profile_id = ?
              AND provider = 'manual'
              AND is_active = 1
              AND lower(account_name) = lower(?)
            ORDER BY manual_updated_at DESC, last_synced_at DESC
            LIMIT 1""",
        (profile_id, account_name),
    ).fetchone()
    return dict(row) if row else None


def _normalize_split_items(splits: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in splits or []:
        category = (item.get("category") or "").strip()
        if not category:
            continue
        try:
            amount = round(abs(float(item.get("amount") or 0)), 2)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            continue
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        normalized.append(
            {
                "category": category,
                "amount": amount,
                "notes": (item.get("notes") or "").strip(),
                "tags": [str(tag).strip() for tag in tags if str(tag).strip()],
            }
        )
    return normalized


def preview_general_write_operation(
    operation: str,
    params: dict,
    profile: str | None = None,
    conn=None,
) -> dict:
    """Preview non-categorization Mira write operations without mutating data."""
    def _query(c):
        op = (operation or "").strip()
        payload = dict(params or {})
        profile_id = _profile_id(profile)

        if op == "set_budget":
            category = (payload.get("category") or "").strip()
            if not category:
                raise ValueError("category is required")
            row = c.execute("SELECT name FROM categories WHERE name = ? AND is_active = 1", (category,)).fetchone()
            if not row:
                raise ValueError("Category not found.")
            amount = None if payload.get("amount") in (None, "") else float(payload.get("amount"))
            existing = c.execute(
                "SELECT category, amount, rollover_mode, rollover_balance FROM category_budgets WHERE profile_id = ? AND category = ?",
                (profile_id, category),
            ).fetchone()
            return {
                "count": 1,
                "samples": [dict(existing)] if existing else [],
                "pending_operation": {"operation": op, "params": payload},
                "summary": f"Set {category} budget to {'off' if amount is None or amount <= 0 else f'${amount:,.2f}'}",
                "preview_changes": [{"column": "budget", "raw_value": (dict(existing).get("amount") if existing else None), "new_value": amount}],
            }

        if op == "create_goal":
            name = (payload.get("name") or payload.get("goal_name") or "").strip()
            if not name:
                raise ValueError("goal name is required")
            payload["name"] = name
            payload.setdefault("goal_type", "custom")
            payload["target_amount"] = float(payload.get("target_amount") or 0)
            payload["current_amount"] = float(payload.get("current_amount") or 0)
            return {
                "count": 1,
                "samples": [],
                "pending_operation": {"operation": op, "params": payload},
                "summary": f"Create goal {name} for ${payload['target_amount']:,.2f}",
                "preview_changes": [{"column": "goal", "raw_value": None, "new_value": name}],
            }

        if op in {"update_goal_target", "mark_goal_funded"}:
            goal = _goal_for_write(c, payload, profile)
            if not goal:
                raise ValueError("Goal not found.")
            next_target = goal.get("target_amount")
            next_current = goal.get("current_amount")
            if op == "update_goal_target":
                if payload.get("target_amount") in (None, ""):
                    raise ValueError("target_amount is required")
                next_target = float(payload.get("target_amount") or 0)
                if payload.get("current_amount") not in (None, ""):
                    next_current = float(payload.get("current_amount") or 0)
            else:
                next_current = float(goal.get("target_amount") or 0)
            return {
                "count": 1,
                "samples": [goal],
                "pending_operation": {"operation": op, "params": payload},
                "summary": f"Update goal {goal['name']}",
                "preview_changes": [
                    {"column": "target_amount", "raw_value": goal.get("target_amount"), "new_value": next_target},
                    {"column": "current_amount", "raw_value": goal.get("current_amount"), "new_value": next_current},
                ],
            }

        if op in {"set_transaction_note", "set_transaction_tags", "mark_reviewed", "split_transaction"}:
            tx = _transaction_for_write(c, payload.get("tx_id") or payload.get("transaction_id"), profile)
            if not tx:
                raise ValueError("Transaction not found.")
            preview_changes = []
            if op == "set_transaction_note":
                preview_changes.append({"column": "notes", "raw_value": tx.get("notes") or "", "new_value": (payload.get("note") or payload.get("notes") or "").strip()})
            elif op == "set_transaction_tags":
                tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
                preview_changes.append({"column": "tags", "raw_value": tx.get("tags") or [], "new_value": [str(tag).strip() for tag in tags if str(tag).strip()]})
            elif op == "mark_reviewed":
                preview_changes.append({"column": "reviewed", "raw_value": tx.get("reviewed"), "new_value": bool(payload.get("reviewed", True))})
            else:
                splits = _normalize_split_items(payload.get("splits") or [])
                total = round(sum(item["amount"] for item in splits), 2)
                tx_total = round(abs(float(tx.get("amount") or 0)), 2)
                if not splits:
                    raise ValueError("At least one split item is required.")
                if abs(total - tx_total) > 0.01:
                    raise ValueError(f"Split total ${total:,.2f} must match transaction amount ${tx_total:,.2f}.")
                payload["splits"] = splits
                preview_changes.append({"column": "splits", "raw_value": get_transaction_splits(tx["id"], conn=c), "new_value": splits})
            return {
                "count": 1,
                "samples": [tx],
                "pending_operation": {"operation": op, "params": payload},
                "summary": f"Update transaction {tx['id']}",
                "preview_changes": preview_changes,
            }

        if op == "bulk_mark_reviewed":
            target = bool(payload.get("reviewed", True))
            filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
            page = get_transactions_paginated(
                month=filters.get("month"),
                category=filters.get("category"),
                account=filters.get("account"),
                search=filters.get("search"),
                reviewed=filters.get("reviewed"),
                profile=profile,
                limit=5,
                offset=0,
                conn=c,
                start_date=filters.get("start_date"),
                end_date=filters.get("end_date"),
            )
            count = int(page.get("total_count") or 0)
            return {
                "count": count,
                "samples": page.get("data") or [],
                "pending_operation": {"operation": op, "params": payload},
                "summary": f"Mark {count} transaction(s) as {'reviewed' if target else 'unreviewed'}",
                "preview_changes": [{"column": "reviewed", "raw_value": filters.get("reviewed"), "new_value": target}],
            }

        if op == "update_manual_account_balance":
            account = _manual_account_for_write(c, payload, profile)
            if not account:
                raise ValueError("Manual account not found.")
            if payload.get("balance") in (None, ""):
                raise ValueError("balance is required")
            balance = float(payload.get("balance") or 0)
            payload["account_id"] = account["id"]
            return {
                "count": 1,
                "samples": [account],
                "pending_operation": {"operation": op, "params": payload},
                "summary": f"Update {account.get('account_name') or account['id']} balance to ${balance:,.2f}",
                "preview_changes": [{"column": "current_balance", "raw_value": account.get("current_balance"), "new_value": balance}],
            }

        if op in {"confirm_recurring_obligation", "dismiss_recurring_obligation", "cancel_recurring", "restore_recurring"}:
            merchant = (payload.get("merchant") or payload.get("merchant_name") or "").strip()
            if not merchant:
                raise ValueError("merchant is required")
            merchant_key = _recurring_canonical_key(merchant)
            row = None
            if merchant_key:
                row = c.execute(
                    """SELECT id, display_name, amount_cents, frequency, state, next_expected_date
                         FROM recurring_obligations
                        WHERE profile_id = ? AND merchant_key = ?
                        LIMIT 1""",
                    (profile_id, merchant_key),
                ).fetchone()
            samples = [dict(row)] if row else [{"merchant": merchant, "profile_id": profile_id}]
            action_label = {
                "confirm_recurring_obligation": "Confirm recurring charge",
                "dismiss_recurring_obligation": "Dismiss recurring charge",
                "cancel_recurring": "Cancel recurring charge",
                "restore_recurring": "Restore recurring charge",
            }[op]
            return {
                "count": 1,
                "samples": samples,
                "pending_operation": {"operation": op, "params": payload},
                "summary": f"{action_label}: {merchant}",
                "preview_changes": [{"column": "recurring_state", "raw_value": (dict(row).get("state") if row else None), "new_value": op}],
            }

        raise ValueError(f"Unsupported write operation: {operation}")

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def execute_pending_write_operation(
    operation: str,
    params: dict,
    profile: str | None = None,
    conn=None,
    max_rows: int = 5000,
) -> dict:
    """Execute a previously previewed closed-set Mira write operation."""
    def _query(c):
        op = (operation or "").strip()
        if op == "bulk_recategorize":
            merchant_query = (params.get("merchant_query") or params.get("merchant") or "").strip()
            new_category = (params.get("new_category") or params.get("category") or "").strip()
            if not merchant_query or not new_category:
                raise ValueError("merchant_query and new_category are required")
            preview = bulk_recategorize_preview(merchant_query, new_category, profile, c)
            count = int(preview.get("count") or 0)
            if count > max_rows:
                raise ValueError(f"This operation would affect {count} rows, above the limit of {max_rows}.")
            pattern = preview["pattern"]
            key_pattern = preview["key_pattern"]
            scoped = _write_scoped_profile(profile)
            c.execute("INSERT OR IGNORE INTO categories (name, is_system) VALUES (?, 0)", (new_category,))
            match_clause, extra_params = _merchant_match_clause(scoped, category_filter=new_category)
            id_params = [pattern, key_pattern, pattern] + extra_params
            cursor = c.execute(
                f"""
                UPDATE transactions
                   SET category = ?,
                       categorization_source = 'user-rule',
                       updated_at = datetime('now')
                 WHERE id IN (
                    SELECT id FROM transactions_visible
                     WHERE {match_clause}
                       AND COALESCE(is_excluded, 0) = 0
                 )
                """,
                [new_category] + id_params,
            )
            return {
                "answer": f"Done! Updated {cursor.rowcount} transaction(s).",
                "operation": "write_executed",
                "rows_affected": cursor.rowcount,
                "data": None,
                "needs_confirmation": False,
            }

        if op == "create_rule":
            raw_pattern = (params.get("pattern") or "").strip()
            category = (params.get("category") or "").strip()
            if not raw_pattern or not category:
                raise ValueError("pattern and category are required")
            preview = preview_rule_creation(raw_pattern, category, profile, c)
            count = int(preview.get("count") or 0)
            if count > max_rows:
                raise ValueError(f"This operation would affect {count} rows, above the limit of {max_rows}.")
            c.execute("INSERT OR IGNORE INTO categories (name, is_system) VALUES (?, 0)", (category,))
            scoped = _write_scoped_profile(profile)
            rule_id = _upsert_user_category_rule(
                c,
                pattern=preview["pattern"],
                category=category,
                profile_id=scoped,
            )
            if rule_id is not None:
                c.execute(
                    """UPDATE category_rules
                       SET match_type = 'contains',
                           source = 'user',
                           is_active = 1
                       WHERE id = ?""",
                    (rule_id,),
                )
            updated_count = _apply_category_rule_to_transactions(
                c,
                pattern=preview["pattern"],
                key_pattern=preview.get("key_pattern"),
                category=category,
                profile_id=scoped,
            )
            _upsert_merchant_category_metadata(
                c,
                merchant_key=preview.get("key_pattern") or preview["pattern"],
                profile_id=scoped,
                category=category,
            )
            return {
                "answer": (
                    f"Done! Created rule {preview['pattern']} -> {category} "
                    f"and updated {updated_count} existing transaction(s)."
                ),
                "operation": "write_executed",
                "rows_affected": updated_count,
                "data": {"rule_id": rule_id, "pattern": preview["pattern"], "category": category},
                "needs_confirmation": False,
            }

        if op == "rename_merchant":
            old_name = (params.get("old_name") or "").strip()
            new_name = (params.get("new_name") or "").strip()
            if not old_name or not new_name:
                raise ValueError("old_name and new_name are required")
            preview = rename_merchant_variants(old_name, new_name, profile, c)
            count = int(preview.get("count") or 0)
            if count > max_rows:
                raise ValueError(f"This operation would affect {count} rows, above the limit of {max_rows}.")
            affected = 0
            for target_profile in preview.get("profiles") or []:
                cursor = c.execute(
                    """
                    INSERT INTO merchant_aliases
                        (merchant_key, profile_id, display_name, source, updated_at)
                    VALUES (?, ?, ?, 'user', datetime('now'))
                    ON CONFLICT(merchant_key, profile_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        source = 'user',
                        updated_at = excluded.updated_at
                    """,
                    (preview["key_pattern"], target_profile, new_name),
                )
                affected += max(0, cursor.rowcount)
            return {
                "answer": f"Done! Renamed {count} matching transaction(s) to {new_name}.",
                "operation": "write_executed",
                "rows_affected": affected or count,
                "data": None,
                "needs_confirmation": False,
            }

        if op == "set_budget":
            category = (params.get("category") or "").strip()
            if not category:
                raise ValueError("category is required")
            amount = None if params.get("amount") in (None, "") else float(params.get("amount"))
            result = update_category_budget(
                category=category,
                amount=amount,
                profile=profile,
                conn=c,
                rollover_mode=params.get("rollover_mode"),
                rollover_balance=params.get("rollover_balance"),
            )
            return {
                "answer": f"Done! Updated the {category} budget.",
                "operation": "write_executed",
                "rows_affected": 1,
                "data": result,
                "needs_confirmation": False,
            }

        if op == "create_goal":
            payload = dict(params)
            if payload.get("goal_name") and not payload.get("name"):
                payload["name"] = payload["goal_name"]
            goal = upsert_goal(payload, profile=profile, conn=c)
            return {
                "answer": f"Done! Created goal {goal.get('name')}.",
                "operation": "write_executed",
                "rows_affected": 1,
                "data": goal,
                "needs_confirmation": False,
            }

        if op in {"update_goal_target", "mark_goal_funded"}:
            goal = _goal_for_write(c, params, profile)
            if not goal:
                raise ValueError("Goal not found.")
            payload = dict(goal)
            payload["id"] = goal["id"]
            if op == "update_goal_target":
                if params.get("target_amount") in (None, ""):
                    raise ValueError("target_amount is required")
                payload["target_amount"] = float(params.get("target_amount") or 0)
                if params.get("current_amount") not in (None, ""):
                    payload["current_amount"] = float(params.get("current_amount") or 0)
                if params.get("target_date") not in (None, ""):
                    payload["target_date"] = params.get("target_date")
            else:
                payload["current_amount"] = float(goal.get("target_amount") or 0)
            updated = upsert_goal(payload, profile=profile, conn=c)
            return {
                "answer": f"Done! Updated goal {updated.get('name')}.",
                "operation": "write_executed",
                "rows_affected": 1,
                "data": updated,
                "needs_confirmation": False,
            }

        if op in {"set_transaction_note", "set_transaction_tags", "mark_reviewed"}:
            tx_id = (params.get("tx_id") or params.get("transaction_id") or "").strip()
            if not _transaction_for_write(c, tx_id, profile):
                raise ValueError("Transaction not found.")
            notes = None
            tags = None
            reviewed = None
            if op == "set_transaction_note":
                notes = (params.get("note") or params.get("notes") or "").strip()
            elif op == "set_transaction_tags":
                tags = params.get("tags") if isinstance(params.get("tags"), list) else []
            else:
                reviewed = bool(params.get("reviewed", True))
            updated = update_transaction_metadata(tx_id, notes=notes, tags=tags, reviewed=reviewed, conn=c)
            return {
                "answer": f"Done! Updated transaction {tx_id}.",
                "operation": "write_executed",
                "rows_affected": 1,
                "data": updated,
                "needs_confirmation": False,
            }

        if op == "bulk_mark_reviewed":
            filters = params.get("filters") if isinstance(params.get("filters"), dict) else {}
            target_reviewed = bool(params.get("reviewed", True))
            result = bulk_mark_transactions_reviewed(
                month=filters.get("month"),
                category=filters.get("category"),
                account=filters.get("account"),
                search=filters.get("search"),
                reviewed=filters.get("reviewed"),
                profile=profile,
                target_reviewed=target_reviewed,
                conn=c,
                start_date=filters.get("start_date"),
                end_date=filters.get("end_date"),
            )
            return {
                "answer": f"Done! Marked {result.get('updated_count', 0)} transaction(s) as {'reviewed' if target_reviewed else 'unreviewed'}.",
                "operation": "write_executed",
                "rows_affected": int(result.get("updated_count") or 0),
                "data": result,
                "needs_confirmation": False,
            }

        if op == "update_manual_account_balance":
            account = _manual_account_for_write(c, params, profile)
            if not account:
                raise ValueError("Manual account not found.")
            if params.get("balance") in (None, ""):
                raise ValueError("balance is required")
            payload = {
                "name": params.get("name") or params.get("account_name") or account.get("account_name"),
                "account_type": params.get("account_type") or account.get("account_type") or "depository",
                "account_subtype": params.get("account_subtype") or account.get("account_subtype") or "manual",
                "balance": float(params.get("balance") or 0),
                "notes": params.get("notes") if params.get("notes") is not None else account.get("manual_notes") or "",
            }
            updated = update_manual_account(account["id"], payload, profile=profile, conn=c)
            return {
                "answer": f"Done! Updated {updated.get('account_name') if updated else account['id']} balance.",
                "operation": "write_executed",
                "rows_affected": 1 if updated else 0,
                "data": updated,
                "needs_confirmation": False,
            }

        if op == "split_transaction":
            tx_id = (params.get("tx_id") or params.get("transaction_id") or "").strip()
            tx = _transaction_for_write(c, tx_id, profile)
            if not tx:
                raise ValueError("Transaction not found.")
            splits = _normalize_split_items(params.get("splits") or [])
            total = round(sum(item["amount"] for item in splits), 2)
            tx_total = round(abs(float(tx.get("amount") or 0)), 2)
            if not splits:
                raise ValueError("At least one split item is required.")
            if abs(total - tx_total) > 0.01:
                raise ValueError(f"Split total ${total:,.2f} must match transaction amount ${tx_total:,.2f}.")
            result = replace_transaction_splits(tx_id, splits, conn=c)
            return {
                "answer": f"Done! Split transaction {tx_id} into {len(result.get('items') or [])} item(s).",
                "operation": "write_executed",
                "rows_affected": len(result.get("items") or []),
                "data": result,
                "needs_confirmation": False,
            }

        if op in {"confirm_recurring_obligation", "dismiss_recurring_obligation", "cancel_recurring", "restore_recurring"}:
            merchant = (params.get("merchant") or params.get("merchant_name") or "").strip()
            if not merchant:
                raise ValueError("merchant is required")
            profile_id = _profile_id(profile)
            merchant_key = _recurring_canonical_key(merchant)
            affected = 0

            if op == "confirm_recurring_obligation":
                pattern = (params.get("pattern") or merchant).upper().strip()
                frequency = (params.get("frequency") or params.get("frequency_hint") or "monthly").strip()
                category = (params.get("category") or "Subscriptions").strip() or "Subscriptions"
                existing = c.execute(
                    """SELECT id FROM subscription_seeds
                       WHERE pattern = ? AND source = 'user' AND created_by = ?""",
                    (pattern, profile_id),
                ).fetchone()
                if existing:
                    affected += c.execute(
                        """UPDATE subscription_seeds
                              SET name = ?, frequency_hint = ?, category = ?, is_active = 1
                            WHERE id = ?""",
                        (merchant, frequency, category, existing[0]),
                    ).rowcount or 0
                else:
                    affected += c.execute(
                        """INSERT INTO subscription_seeds
                              (name, pattern, frequency_hint, category, source, created_by)
                            VALUES (?, ?, ?, ?, 'user', ?)""",
                        (merchant, pattern, frequency, category, profile_id),
                    ).rowcount or 0
                if merchant_key:
                    affected += c.execute(
                        """UPDATE recurring_obligations
                              SET state = 'confirmed',
                                  source = CASE WHEN source = 'user' THEN source ELSE 'user_confirmed' END,
                                  confidence_score = MAX(confidence_score, 100),
                                  confidence_label = 'user',
                                  last_user_action_at = datetime('now'),
                                  updated_at = datetime('now')
                            WHERE profile_id = ? AND merchant_key = ?""",
                        (profile_id, merchant_key),
                    ).rowcount or 0
                _record_recurring_feedback(
                    c,
                    merchant=merchant,
                    profile_id=profile_id,
                    feedback_type="confirmed",
                    scope="merchant",
                    payload={"pattern": pattern, "frequency_hint": frequency, "category": category},
                )
                return {
                    "answer": f"Done! Confirmed {merchant} as recurring.",
                    "operation": "write_executed",
                    "rows_affected": affected,
                    "data": {"merchant": merchant, "status": "confirmed"},
                    "needs_confirmation": False,
                }

            if op == "dismiss_recurring_obligation":
                pattern = (params.get("pattern") or merchant).upper().strip()
                existing = c.execute(
                    """SELECT id FROM subscription_seeds
                       WHERE pattern = ? AND source = 'user' AND created_by = ?""",
                    (pattern, profile_id),
                ).fetchone()
                if existing:
                    affected += c.execute("UPDATE subscription_seeds SET is_active = 0 WHERE id = ?", (existing[0],)).rowcount or 0
                else:
                    affected += c.execute(
                        """INSERT INTO subscription_seeds
                              (name, pattern, frequency_hint, category, source, created_by, is_active)
                            VALUES (?, ?, 'monthly', 'Dismissed', 'user', ?, 0)""",
                        (merchant, pattern, profile_id),
                    ).rowcount or 0
                affected += c.execute(
                    """INSERT OR IGNORE INTO dismissed_recurring (merchant_name, profile_id)
                       VALUES (?, ?)""",
                    (merchant, profile_id),
                ).rowcount or 0
                if merchant_key:
                    affected += c.execute(
                        """UPDATE recurring_obligations
                              SET state = 'dismissed',
                                  last_user_action_at = datetime('now'),
                                  updated_at = datetime('now')
                            WHERE profile_id = ? AND merchant_key = ?""",
                        (profile_id, merchant_key),
                    ).rowcount or 0
                _record_recurring_feedback(
                    c,
                    merchant=merchant,
                    profile_id=profile_id,
                    feedback_type="dismissed",
                    scope="merchant",
                    payload={"pattern": pattern},
                )
                return {
                    "answer": f"Done! Dismissed {merchant} as recurring.",
                    "operation": "write_executed",
                    "rows_affected": affected,
                    "data": {"merchant": merchant, "status": "dismissed"},
                    "needs_confirmation": False,
                }

            if op == "cancel_recurring":
                now = datetime.now().isoformat()
                merchant_ids = _legacy_merchant_ids_for_recurring(c, merchant, profile_id)
                if merchant_ids:
                    placeholders = ",".join("?" * len(merchant_ids))
                    affected += c.execute(
                        f"""UPDATE merchants
                              SET cancelled_at = ?,
                                  cancelled_by_user = 1,
                                  subscription_status = 'cancelled',
                                  next_expected_date = NULL,
                                  updated_at = datetime('now')
                            WHERE id IN ({placeholders})""",
                        (now, *merchant_ids),
                    ).rowcount or 0
                declared_ids = _user_declared_ids_for_recurring(c, merchant, profile_id)
                if declared_ids:
                    placeholders = ",".join("?" * len(declared_ids))
                    affected += c.execute(
                        f"""UPDATE user_declared_subscriptions
                              SET is_active = 0,
                                  updated_at = datetime('now')
                            WHERE id IN ({placeholders})""",
                        declared_ids,
                    ).rowcount or 0
                if merchant_key:
                    affected += c.execute(
                        """UPDATE recurring_obligations
                              SET state = 'cancelled',
                                  last_user_action_at = datetime('now'),
                                  updated_at = datetime('now')
                            WHERE profile_id = ? AND merchant_key = ?""",
                        (profile_id, merchant_key),
                    ).rowcount or 0
                _record_recurring_feedback(
                    c,
                    merchant=merchant,
                    profile_id=profile_id,
                    feedback_type="cancelled",
                    scope="merchant",
                    payload={"cancelled_at": now},
                )
                return {
                    "answer": f"Done! Marked {merchant} as cancelled.",
                    "operation": "write_executed",
                    "rows_affected": affected,
                    "data": {"merchant": merchant, "status": "cancelled", "cancelled_at": now},
                    "needs_confirmation": False,
                }

            dismissed_ids = _dismissed_ids_for_recurring(c, merchant, profile_id)
            if dismissed_ids:
                placeholders = ",".join("?" * len(dismissed_ids))
                affected += c.execute(f"DELETE FROM dismissed_recurring WHERE id IN ({placeholders})", dismissed_ids).rowcount or 0
            seed = c.execute(
                """SELECT id FROM subscription_seeds
                   WHERE pattern = ? AND source = 'user' AND created_by = ? AND is_active = 0""",
                (merchant.upper(), profile_id),
            ).fetchone()
            if seed:
                affected += c.execute("UPDATE subscription_seeds SET is_active = 1 WHERE id = ?", (seed[0],)).rowcount or 0
            merchant_ids = _legacy_merchant_ids_for_recurring(c, merchant, profile_id)
            if merchant_ids:
                placeholders = ",".join("?" * len(merchant_ids))
                affected += c.execute(
                    f"""UPDATE merchants
                          SET cancelled_by_user = 0,
                              cancelled_at = NULL,
                              subscription_status = CASE
                                  WHEN subscription_status = 'cancelled' THEN 'inactive'
                                  ELSE subscription_status
                              END,
                              updated_at = datetime('now')
                        WHERE id IN ({placeholders})""",
                    merchant_ids,
                ).rowcount or 0
            declared_ids = _user_declared_ids_for_recurring(c, merchant, profile_id)
            if declared_ids:
                placeholders = ",".join("?" * len(declared_ids))
                affected += c.execute(
                    f"""UPDATE user_declared_subscriptions
                          SET is_active = 1,
                              updated_at = datetime('now')
                        WHERE id IN ({placeholders})""",
                    declared_ids,
                ).rowcount or 0
            if _restore_recurring_obligation(c, merchant=merchant, profile_id=profile_id):
                affected += 1
            if affected <= 0:
                raise ValueError("Recurring item not found.")
            return {
                "answer": f"Done! Restored {merchant}.",
                "operation": "write_executed",
                "rows_affected": affected,
                "data": {"merchant": merchant, "status": "restored"},
                "needs_confirmation": False,
            }

        raise ValueError(f"Unsupported write operation: {operation}")

    if conn is not None:
        return _query(conn)
    with get_db() as c:
        return _query(c)


def backfill_transfer_types() -> int:
    """
    Startup-safe backfill: only runs if there are transfer-category
    transactions with NULL expense_type. Returns count of updated rows.
    """
    with get_db() as c:
        transfer_ph = ",".join("?" * len(TRANSFER_CATEGORIES))
        null_count = c.execute(
            f"""SELECT COUNT(*) FROM transactions
                WHERE category IN ({transfer_ph}) AND expense_type IS NULL""",
            list(TRANSFER_CATEGORIES),
        ).fetchone()[0]

        if null_count == 0:
            return 0

        logger.info("Backfilling expense_type for %d transfer transactions...", null_count)
        return reclassify_transfers(conn=c)
