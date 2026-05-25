"""
database.py
SQLite database initialization, connection management, and schema definition.
"""

import sqlite3
import os
import threading
import re
import time
from pathlib import Path
from contextlib import contextmanager
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - fallback for lightweight scripts
    def load_dotenv():
        return False
from log_config import get_logger

load_dotenv()

logger = get_logger(__name__)

DB_FILE = os.getenv("DB_FILE", "Folio.db")
# If DB_FILE is an absolute path (e.g., /data/Folio.db from Docker),
# use it directly. Otherwise, place it relative to this file's directory.
_db_file_path = Path(DB_FILE)
if _db_file_path.is_absolute():
    DB_PATH = _db_file_path
else:
    DB_PATH = Path(__file__).parent / DB_FILE

_local = threading.local()
_wal_lock = threading.Lock()
_wal_initialized = False
_SQLITE_CONNECT_RETRIES = int(os.getenv("SQLITE_CONNECT_RETRIES", "3"))
_SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "10000"))
DESCRIPTION_NORMALIZED_VERSION = "2"


def _default_journal_mode() -> str:
    """Avoid WAL for the bind-mounted app DB, from both host and container paths."""
    if str(DB_PATH).startswith("/data/"):
        return "DELETE"
    try:
        if DB_PATH.resolve() == (Path(__file__).resolve().parent.parent / "data" / "Folio.db"):
            return "DELETE"
    except OSError:
        pass
    return "WAL"


_DEFAULT_JOURNAL_MODE = _default_journal_mode()
_SQLITE_JOURNAL_MODE = os.getenv("SQLITE_JOURNAL_MODE", _DEFAULT_JOURNAL_MODE).upper()
if _SQLITE_JOURNAL_MODE not in {"DELETE", "WAL", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}:
    logger.warning("Invalid SQLITE_JOURNAL_MODE=%s; falling back to %s", _SQLITE_JOURNAL_MODE, _DEFAULT_JOURNAL_MODE)
    _SQLITE_JOURNAL_MODE = _DEFAULT_JOURNAL_MODE


def _sqlite_regexp(pattern, value) -> int:
    """SQLite REGEXP implementation used by Copilot-generated queries."""
    if pattern is None or value is None:
        return 0
    try:
        return 1 if re.search(str(pattern), str(value), re.IGNORECASE) else 0
    except re.error:
        logger.debug("Invalid REGEXP pattern passed to SQLite helper: %s", pattern)
        return 0


def _is_transient_sqlite_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "disk i/o error" in message or "database is locked" in message or "database is busy" in message


def _open_connection() -> sqlite3.Connection:
    """Open SQLite with a short retry for transient Docker/macOS bind-mount I/O hiccups."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    delay = 0.05
    last_error: Exception | None = None
    for attempt in range(max(1, _SQLITE_CONNECT_RETRIES)):
        try:
            return sqlite3.connect(
                str(DB_PATH),
                check_same_thread=False,
                timeout=max(1.0, _SQLITE_BUSY_TIMEOUT_MS / 1000),
            )
        except sqlite3.OperationalError as exc:
            last_error = exc
            if not _is_transient_sqlite_error(exc) or attempt >= _SQLITE_CONNECT_RETRIES - 1:
                raise
            logger.warning("SQLite open failed transiently (%s); retrying in %.2fs", exc, delay)
            time.sleep(delay)
            delay *= 2
    raise last_error or sqlite3.OperationalError("failed to open SQLite database")


def _ensure_wal_mode() -> None:
    """Set the SQLite journal mode once per process."""
    global _wal_initialized
    if _wal_initialized:
        return
    with _wal_lock:
        if _wal_initialized:
            return
        delay = 0.05
        for attempt in range(max(1, _SQLITE_CONNECT_RETRIES)):
            conn = None
            try:
                conn = _open_connection()
                conn.execute(f"PRAGMA journal_mode={_SQLITE_JOURNAL_MODE}")
                conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
                conn.close()
                _wal_initialized = True
                return
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if not _is_transient_sqlite_error(exc) or attempt >= _SQLITE_CONNECT_RETRIES - 1:
                    raise
                logger.warning("SQLite WAL setup failed transiently (%s); retrying in %.2fs", exc, delay)
                time.sleep(delay)
                delay *= 2


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    conn.create_function("REGEXP", 2, _sqlite_regexp)
    return conn


def get_connection() -> sqlite3.Connection:
    """
    Get a thread-local SQLite connection.
    Returns the same connection for the same thread (reuse within request).

    DEPRECATED: Use get_db_session() with FastAPI Depends() for request-scoped
    connections that are properly closed. This function is retained only for
    background tasks (e.g., sync) and module-level code paths that cannot
    use FastAPI dependency injection.
    """
    _ensure_wal_mode()
    if not hasattr(_local, "connection") or _local.connection is None:
        conn = _open_connection()
        _local.connection = _configure_connection(conn)
    else:
        _configure_connection(_local.connection)
    return _local.connection


@contextmanager
def get_db():
    """Context manager for database operations with auto-commit/rollback."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def get_db_session():
    """
    FastAPI dependency that provides a request-scoped DB connection.
    Commits on success, rolls back on exception, and CLOSES the connection
    in the finally block — preventing connection leaks in long-running servers.

    Usage in route handlers:
        @app.get("/api/example")
        def example(db: sqlite3.Connection = Depends(get_db_session)):
            rows = db.execute("SELECT ...").fetchall()
            return rows
    """
    _ensure_wal_mode()
    conn = _open_connection()
    conn = _configure_connection(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_thread_local_connection():
    """
    Close and discard the thread-local connection if one exists.
    Called during shutdown to clean up any remaining connections.
    """
    if hasattr(_local, "connection") and _local.connection is not None:
        try:
            _local.connection.close()
        except Exception:
            pass
        _local.connection = None


def dict_from_row(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return dict(row)


def dicts_from_rows(rows: list[sqlite3.Row]) -> list[dict]:
    """Convert a list of sqlite3.Row to list of dicts."""
    return [dict(r) for r in rows]


def init_db():
    """
    Create all tables if they don't exist.
    Safe to call multiple times (idempotent).
    """
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_expense_type(conn)
        _migrate_category_parent(conn)
        _migrate_merchants_table(conn)
        _migrate_merchant_aliases(conn)
        _migrate_transaction_expense_type(conn)
        _migrate_description_normalized(conn)
        _migrate_transaction_merchant_identity(conn)
        _migrate_canonical_merchant_metadata(conn)
        _migrate_category_pinned(conn)
        _migrate_accounts_provider(conn)
        _migrate_accounts_last_four(conn)
        _migrate_memory_entries_theme(conn)
        _migrate_mira_memory_v2_tables(conn)
        _migrate_mira_session_summary_tables(conn)
        _migrate_mira_financial_understanding_tables(conn)
        _migrate_mira_financial_feedback_tables(conn)
        _migrate_mira_stated_intent_tables(conn)
        _migrate_mira_habit_streak_tables(conn)
        _migrate_mira_monthly_retrospective_tables(conn)
        _migrate_mira_money_outlook_tables(conn)
        _migrate_public_planning_tables(conn)
        _migrate_user_declared_subscription_category(conn)
        _migrate_user_declared_subscription_expected_day(conn)
        _migrate_user_declared_subscription_amount_review(conn)
        _migrate_investments_lite(conn)
        _migrate_recurring_obligations(conn)
        _migrate_mira_hardening_tables(conn)
        _migrate_transaction_intelligence_tables(conn)
        _seed_default_categories(conn)
        _seed_system_rules(conn)
        _deactivate_brittle_cashflow_system_rules(conn)
        _seed_teller_category_map(conn)          


def _migrate_expense_type(conn: sqlite3.Connection):
    """
    Backfill expense_type and expense_type_source for existing databases
    that were created before these columns existed. Idempotent.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(categories)").fetchall()]
    if "expense_type" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN expense_type TEXT DEFAULT 'variable' CHECK(expense_type IN ('fixed', 'variable', 'non_expense'))")
    if "expense_type_source" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN expense_type_source TEXT DEFAULT 'system' CHECK(expense_type_source IN ('system', 'user'))")

    # Backfill known defaults (only update rows still at 'variable' that should be fixed/non_expense)
    # Only backfill if expense_type_source is 'system' (don't override user choices)
    KNOWN_FIXED = ("Utilities", "Housing", "Subscriptions", "Taxes", "Insurance")
    KNOWN_NON_EXPENSE = ("Savings Transfer", "Credit Card Payment", "Income", "Credits & Refunds", "Personal Transfer")

    for name in KNOWN_FIXED:
        conn.execute(
            "UPDATE categories SET expense_type = 'fixed' WHERE name = ? AND expense_type = 'variable' AND (expense_type_source = 'system' OR expense_type_source IS NULL)",
            (name,),
        )
    for name in KNOWN_NON_EXPENSE:
        conn.execute(
            "UPDATE categories SET expense_type = 'non_expense' WHERE name = ? AND expense_type = 'variable' AND (expense_type_source = 'system' OR expense_type_source IS NULL)",
            (name,),
        )


def _migrate_category_parent(conn: sqlite3.Connection):
    """Ensure categories.parent_category exists for older databases."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(categories)").fetchall()]
    if "parent_category" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN parent_category TEXT DEFAULT NULL")

def _migrate_merchants_table(conn: sqlite3.Connection):
    """
    Ensure merchants table has all required columns for Enhancement 4
    (cancelled_at, cancelled_by_user). Idempotent.
    """
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(merchants)").fetchall()]
    except Exception:
        return  # Table doesn't exist yet; SCHEMA_SQL will create it

    if not cols:
        return

    if "cancelled_at" not in cols:
        conn.execute("ALTER TABLE merchants ADD COLUMN cancelled_at TEXT")
    if "cancelled_by_user" not in cols:
        conn.execute("ALTER TABLE merchants ADD COLUMN cancelled_by_user INTEGER DEFAULT 0")


def _migrate_merchant_aliases(conn: sqlite3.Connection):
    """Create merchant_aliases and backfill legacy user display overrides."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS merchant_aliases (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_key TEXT NOT NULL,
            profile_id  TEXT NOT NULL,
            display_name TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'user',
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(merchant_key, profile_id)
        );

        CREATE INDEX IF NOT EXISTS idx_merchant_aliases_profile
            ON merchant_aliases(profile_id);
        CREATE INDEX IF NOT EXISTS idx_merchant_aliases_key
            ON merchant_aliases(merchant_key);
        """
    )

    conn.execute(
        """
        INSERT INTO merchant_aliases (merchant_key, profile_id, display_name, source)
        SELECT UPPER(TRIM(merchant_key)),
               profile_id,
               TRIM(clean_name),
               'user'
          FROM merchants
         WHERE COALESCE(profile_id, '') != ''
           AND NULLIF(TRIM(clean_name), '') IS NOT NULL
           AND UPPER(TRIM(clean_name)) != UPPER(TRIM(COALESCE(merchant_key, '')))
           AND COALESCE(source, '') = 'user'
        ON CONFLICT(merchant_key, profile_id) DO NOTHING
        """
    )
        
def _migrate_transaction_expense_type(conn: sqlite3.Connection):
    """
    Ensure transactions table has an expense_type column for transfer
    sub-classification (transfer_internal, transfer_household, transfer_external).
    Idempotent — safe to call on every startup.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    if "expense_type" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN expense_type TEXT DEFAULT NULL")
        logger.info("Added expense_type column to transactions table.")


def _migrate_description_normalized_category_rules(conn: sqlite3.Connection) -> None:
    """Retarget active fallback category rules before rebuilding fingerprints."""
    rules = conn.execute(
        """SELECT id, pattern, category, priority, source, profile_id
           FROM category_rules
           WHERE is_active = 1 AND match_type = 'contains'
           ORDER BY id ASC"""
    ).fetchall()
    migrated = 0
    created = 0
    skipped = 0

    for rule in rules:
        rule_id, pattern, category, priority, source, profile_id = rule
        normalized_pattern = (pattern or "").upper().strip()
        if not normalized_pattern:
            continue

        params = [normalized_pattern, normalized_pattern, normalized_pattern]
        where = [
            "(description_normalized = ? OR UPPER(COALESCE(merchant_key, '')) = ? OR UPPER(COALESCE(merchant_name, '')) = ?)"
        ]
        if profile_id:
            where.append("profile_id = ?")
            params.append(profile_id)

        matched_rows = conn.execute(
            f"""SELECT description, merchant_key, merchant_name
                FROM transactions
                WHERE {' AND '.join(where)}""",
            params,
        ).fetchall()
        if not matched_rows:
            continue

        candidates: list[str] = []
        seen_candidates: set[str] = set()
        for description, merchant_key, merchant_name in matched_rows:
            key = (merchant_key or "").upper().strip()
            name = (merchant_name or "").upper().strip()
            current = _extract_merchant_pattern(description)
            if current == normalized_pattern or key == normalized_pattern or name == normalized_pattern:
                continue

            candidate = key or name or current
            if not candidate or candidate in seen_candidates:
                continue
            seen_candidates.add(candidate)
            candidates.append(candidate)

        if not candidates:
            continue
        if len(candidates) > 25:
            skipped += 1
            logger.warning(
                "Skipped category rule fingerprint migration for %s; %d replacement candidates.",
                normalized_pattern,
                len(candidates),
            )
            continue

        replacement = candidates[0]
        conn.execute(
            """UPDATE category_rules
               SET pattern = ?
               WHERE id = ?""",
            (replacement, rule_id),
        )
        migrated += 1

        for extra in candidates[1:]:
            exists = conn.execute(
                """SELECT id
                   FROM category_rules
                   WHERE pattern = ?
                     AND category = ?
                     AND source = ?
                     AND match_type = 'contains'
                     AND is_active = 1
                     AND COALESCE(profile_id, '') = COALESCE(?, '')
                   LIMIT 1""",
                (extra, category, source, profile_id),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """INSERT INTO category_rules
                   (pattern, match_type, category, priority, source, profile_id)
                   VALUES (?, 'contains', ?, ?, ?, ?)""",
                (extra, category, priority, source, profile_id),
            )
            created += 1

    if migrated or created or skipped:
        logger.info(
            "Migrated category rule fingerprints for description_normalized v%s: %d updated, %d created, %d skipped.",
            DESCRIPTION_NORMALIZED_VERSION,
            migrated,
            created,
            skipped,
        )


def _migrate_description_normalized(conn: sqlite3.Connection):
    """
    Add description_normalized column and index to transactions, then backfill
    existing rows. Idempotent — safe to call on every startup.

    description_normalized stores a conservative transaction fingerprint from
    _extract_merchant_pattern(description). It removes mechanical bank noise
    without trying to infer merchant identity; enriched merchant_key/merchant_name
    remain the primary matching identity elsewhere.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    if "description_normalized" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN description_normalized TEXT DEFAULT NULL")
        logger.info("Added description_normalized column to transactions table.")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_description_normalized "
        "ON transactions(description_normalized)"
    )

    version_row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'description_normalized_version'"
    ).fetchone()
    current_version = (version_row[0] if version_row else "") or ""
    needs_rebuild = current_version != DESCRIPTION_NORMALIZED_VERSION
    if needs_rebuild:
        _migrate_description_normalized_category_rules(conn)
        conn.commit()

    # Backfill/rebuild existing rows in batches of 500 to avoid a single huge transaction.
    # _extract_merchant_pattern is Python-only, so we fetch-loop-update.
    _BATCH = 500
    total = 0
    while True:
        if needs_rebuild:
            rows = conn.execute(
                "SELECT id, description, description_normalized FROM transactions ORDER BY id LIMIT ? OFFSET ?",
                (_BATCH, total),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, description, description_normalized FROM transactions WHERE description_normalized IS NULL LIMIT ?",
                (_BATCH,),
            ).fetchall()
        if not rows:
            break
        for row in rows:
            normalized = _extract_merchant_pattern(row[1])  # row[0]=id, row[1]=description
            if row[2] != normalized:
                conn.execute(
                    "UPDATE transactions SET description_normalized = ? WHERE id = ?",
                    (normalized, row[0]),
                )
        total += len(rows)
        conn.commit()

    if needs_rebuild:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES ('description_normalized_version', ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                   value = excluded.value,
                   updated_at = datetime('now')""",
            (DESCRIPTION_NORMALIZED_VERSION,),
        )
        conn.commit()
        logger.info("Rebuilt description_normalized fingerprints to version %s.", DESCRIPTION_NORMALIZED_VERSION)
    if total:
        logger.info("Backfilled description_normalized for %d transactions.", total)


def _migrate_transaction_merchant_identity(conn: sqlite3.Connection):
    """
    Add explicit merchant identity columns.

    description_normalized remains as legacy rule-pattern data. This migration
    intentionally backfills merchant_key only from existing merchant_name, never
    from regex-derived description_normalized for ambiguous unenriched rows.
    """
    from merchant_identity import (
        MERCHANT_PURCHASE,
        canonicalize_merchant_key,
        infer_non_merchant_kind,
        normalize_merchant_kind,
    )

    cols = [row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    additions = {
        "merchant_key": "TEXT DEFAULT ''",
        "merchant_source": "TEXT DEFAULT ''",
        "merchant_confidence": "TEXT DEFAULT ''",
        "merchant_kind": "TEXT DEFAULT ''",
    }
    for col, ddl in additions.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")
            logger.info("Added %s column to transactions table.", col)

    updated = 0
    while True:
        rows = conn.execute(
            """SELECT id, description, raw_description, category, transaction_type,
                      expense_type, merchant_name, merchant_key, merchant_kind,
                      enriched, merchant_source, merchant_confidence
               FROM transactions
               WHERE NULLIF(TRIM(COALESCE(merchant_name, '')), '') IS NOT NULL
                 AND (
                    COALESCE(merchant_key, '') = ''
                    OR COALESCE(merchant_kind, '') = ''
                    OR COALESCE(merchant_source, '') = ''
                 )
               LIMIT 500"""
        ).fetchall()
        if not rows:
            break
        batch_updated = 0
        for row in rows:
            tx = dict(row)
            merchant_key = canonicalize_merchant_key(tx.get("merchant_key") or tx.get("merchant_name"))
            merchant_kind = normalize_merchant_kind(tx.get("merchant_kind"))
            merchant_source = (tx.get("merchant_source") or "").strip()
            merchant_confidence = (tx.get("merchant_confidence") or "").strip()

            if merchant_key and not merchant_kind:
                merchant_kind = MERCHANT_PURCHASE
            if merchant_key and merchant_kind == "unknown":
                merchant_kind = MERCHANT_PURCHASE
            if merchant_key and merchant_source in ("", "none"):
                merchant_source = "legacy"

            if merchant_key or merchant_kind or merchant_source or merchant_confidence:
                conn.execute(
                    """UPDATE transactions
                       SET merchant_key = COALESCE(NULLIF(?, ''), merchant_key, ''),
                           merchant_kind = COALESCE(NULLIF(?, ''), merchant_kind, ''),
                           merchant_source = COALESCE(NULLIF(?, ''), merchant_source, ''),
                           merchant_confidence = COALESCE(NULLIF(?, ''), merchant_confidence, '')
                       WHERE id = ?""",
                    (merchant_key, merchant_kind, merchant_source, merchant_confidence, tx["id"]),
                )
                updated += 1
                batch_updated += 1
        conn.commit()
        if batch_updated == 0:
            break

    # Mark obvious non-merchant rows without creating merchant keys from raw descriptions.
    # Keep ambiguous blank-merchant rows untouched so startup does not rewrite the whole ledger.
    non_merchant_updated = 0
    while True:
        rows = conn.execute(
            """SELECT id, description, raw_description, category, transaction_type,
                      expense_type, merchant_name, merchant_key, merchant_kind,
                      enriched, merchant_source, merchant_confidence
               FROM transactions
               WHERE COALESCE(merchant_key, '') = ''
                 AND NULLIF(TRIM(COALESCE(merchant_name, '')), '') IS NULL
                 AND COALESCE(merchant_kind, '') = ''
                 AND (
                    UPPER(COALESCE(description, '') || ' ' || COALESCE(raw_description, '')) LIKE '%ZELLE%'
                    OR UPPER(COALESCE(description, '') || ' ' || COALESCE(raw_description, '')) LIKE '%CREDIT CRD%'
                    OR UPPER(COALESCE(description, '') || ' ' || COALESCE(raw_description, '')) LIKE '%EPAY%'
                    OR UPPER(COALESCE(description, '') || ' ' || COALESCE(raw_description, '')) LIKE '%PAYROLL%'
                    OR UPPER(COALESCE(description, '') || ' ' || COALESCE(raw_description, '')) LIKE '%IRS%'
                    OR UPPER(COALESCE(category, '')) IN ('INCOME', 'PAYROLL', 'TAXES', 'FEES')
                 )
               LIMIT 500"""
        ).fetchall()
        if not rows:
            break
        batch_updated = 0
        for row in rows:
            tx = dict(row)
            merchant_kind = infer_non_merchant_kind(tx)
            if not merchant_kind:
                continue
            conn.execute(
                """UPDATE transactions
                   SET merchant_kind = ?,
                       merchant_source = COALESCE(NULLIF(merchant_source, ''), 'rule'),
                       merchant_confidence = COALESCE(NULLIF(merchant_confidence, ''), 'high')
                   WHERE id = ?""",
                (merchant_kind, tx["id"]),
            )
            updated += 1
            non_merchant_updated += 1
            batch_updated += 1
        conn.commit()
        if batch_updated == 0:
            break

    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_merchant_key ON transactions(merchant_key)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transactions_profile_merchant_key "
        "ON transactions(profile_id, merchant_key)"
    )
    if updated:
        logger.info("Backfilled merchant identity for %d transactions.", updated)
    if non_merchant_updated:
        logger.info("Classified %d obvious non-merchant transactions.", non_merchant_updated)


def _migrate_canonical_merchant_metadata(conn: sqlite3.Connection):
    """Bridge legacy merchant metadata/aliases onto canonical merchant_key values."""
    from merchant_identity import canonicalize_merchant_key

    rows = conn.execute(
        """SELECT merchant_key, clean_name, logo_url, domain, category, industry,
                  source, is_subscription, subscription_frequency, subscription_amount,
                  subscription_status, cancelled_at, cancelled_by_user,
                  last_charge_date, next_expected_date, total_spent, charge_count,
                  profile_id
           FROM merchants
           WHERE COALESCE(profile_id, '') != ''
             AND NULLIF(TRIM(clean_name), '') IS NOT NULL"""
    ).fetchall()
    copied = 0
    for row in rows:
        item = dict(row)
        canonical_key = canonicalize_merchant_key(item.get("clean_name"))
        profile_id = item.get("profile_id")
        if not canonical_key or not profile_id:
            continue
        if canonical_key == canonicalize_merchant_key(item.get("merchant_key")):
            continue
        conn.execute(
            """INSERT INTO merchants
               (merchant_key, clean_name, logo_url, domain, category, industry,
                source, is_subscription, subscription_frequency, subscription_amount,
                subscription_status, cancelled_at, cancelled_by_user,
                last_charge_date, next_expected_date, total_spent, charge_count,
                profile_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(merchant_key, profile_id) DO UPDATE SET
                   clean_name = COALESCE(NULLIF(merchants.clean_name, ''), excluded.clean_name),
                   logo_url = COALESCE(NULLIF(merchants.logo_url, ''), excluded.logo_url),
                   domain = COALESCE(NULLIF(merchants.domain, ''), excluded.domain),
                   category = COALESCE(NULLIF(merchants.category, ''), excluded.category),
                   industry = COALESCE(NULLIF(merchants.industry, ''), excluded.industry),
                   is_subscription = MAX(merchants.is_subscription, excluded.is_subscription),
                   subscription_frequency = COALESCE(NULLIF(merchants.subscription_frequency, ''), excluded.subscription_frequency),
                   subscription_amount = COALESCE(merchants.subscription_amount, excluded.subscription_amount),
                   subscription_status = COALESCE(NULLIF(merchants.subscription_status, ''), excluded.subscription_status),
                   cancelled_at = COALESCE(NULLIF(merchants.cancelled_at, ''), excluded.cancelled_at),
                   cancelled_by_user = MAX(merchants.cancelled_by_user, excluded.cancelled_by_user),
                   last_charge_date = COALESCE(NULLIF(merchants.last_charge_date, ''), excluded.last_charge_date),
                   next_expected_date = COALESCE(NULLIF(merchants.next_expected_date, ''), excluded.next_expected_date),
                   updated_at = datetime('now')""",
            (
                canonical_key,
                item.get("clean_name"),
                item.get("logo_url"),
                item.get("domain"),
                item.get("category"),
                item.get("industry"),
                item.get("source") or "legacy",
                item.get("is_subscription") or 0,
                item.get("subscription_frequency"),
                item.get("subscription_amount"),
                item.get("subscription_status"),
                item.get("cancelled_at"),
                item.get("cancelled_by_user") or 0,
                item.get("last_charge_date"),
                item.get("next_expected_date"),
                item.get("total_spent") or 0,
                item.get("charge_count") or 0,
                profile_id,
            ),
        )
        conn.execute(
            """INSERT INTO merchant_aliases (merchant_key, profile_id, display_name, source)
               VALUES (?, ?, ?, 'user')
               ON CONFLICT(merchant_key, profile_id) DO NOTHING""",
            (canonical_key, profile_id, item.get("clean_name")),
        )
        copied += 1
    if copied:
        logger.info("Bridged %d merchant metadata rows to canonical merchant keys.", copied)


def _migrate_category_pinned(conn: sqlite3.Connection):
    """
    Add category_pinned column to transactions.
    When 1, the transaction was manually recategorized as a one-off — future rule
    applications and syncs must not overwrite its category.
    Idempotent — safe to call on every startup.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    if "category_pinned" not in cols:
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN category_pinned INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("Added category_pinned column to transactions table.")


def _migrate_accounts_last_four(conn: sqlite3.Connection):
    """
    Add last_four column to accounts for cleaner cross-provider matching.
    Backfills by extracting the last 4-digit sequence from account_name or id.
    Idempotent.
    """
    import re as _re
    cols = [row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "last_four" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN last_four TEXT DEFAULT NULL")
        rows = conn.execute("SELECT id, account_name FROM accounts").fetchall()
        for acct_id, acct_name in rows:
            for source in (acct_name or "", acct_id or ""):
                seqs = _re.findall(r'\d{4,}', source)
                if seqs:
                    conn.execute(
                        "UPDATE accounts SET last_four = ? WHERE id = ?",
                        (seqs[-1][-4:], acct_id),
                    )
                    break
        logger.info("Added last_four column to accounts table.")


def _migrate_accounts_provider(conn: sqlite3.Connection):
    """
    Add provider column to accounts table so we can distinguish
    Teller vs SimpleFIN (or future) account sources. Idempotent.
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "provider" not in cols:
        conn.execute(
            "ALTER TABLE accounts ADD COLUMN provider TEXT NOT NULL DEFAULT 'teller'"
        )
        logger.info("Added provider column to accounts table.")


def _migrate_memory_entries_theme(conn: sqlite3.Connection):
    """Add theme column to memory_entries (used to link inferred entries to their observation theme)."""
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(memory_entries)").fetchall()]
    except Exception:
        return
    if cols and "theme" not in cols:
        conn.execute("ALTER TABLE memory_entries ADD COLUMN theme TEXT DEFAULT NULL")


def _migrate_mira_memory_v2_tables(conn: sqlite3.Connection):
    """Create the additive Mira Memory V2 tables for existing databases."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_memories (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id              TEXT REFERENCES profiles(id),
            scope                   TEXT NOT NULL DEFAULT 'profile' CHECK(scope IN ('profile', 'household')),
            memory_type             TEXT NOT NULL CHECK(memory_type IN (
                'preference', 'goal', 'constraint', 'stressor', 'commitment',
                'rejected_advice', 'coaching_state', 'identity_fact', 'tone_preference'
            )),
            topic                   TEXT NOT NULL DEFAULT '',
            normalized_text         TEXT NOT NULL,
            original_text           TEXT NOT NULL DEFAULT '',
            source_summary          TEXT DEFAULT '',
            sensitivity             TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
            confidence              REAL NOT NULL DEFAULT 1.0,
            source_conversation_id  INTEGER REFERENCES copilot_conversations(id),
            source_turn_id          TEXT DEFAULT NULL,
            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at            TEXT DEFAULT NULL,
            expires_at              TEXT DEFAULT NULL,
            pinned                  INTEGER NOT NULL DEFAULT 0,
            superseded_by           INTEGER REFERENCES mira_memories(id),
            status                  TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'deleted', 'rejected')),
            consent                 TEXT NOT NULL DEFAULT 'explicit',
            consent_at              TEXT DEFAULT (datetime('now')),
            metadata_json           TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_mira_memories_active
            ON mira_memories(profile_id, scope, status, memory_type, topic, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_memories_expiry
            ON mira_memories(expires_at) WHERE expires_at IS NOT NULL AND status = 'active';
        CREATE INDEX IF NOT EXISTS idx_mira_memories_topic
            ON mira_memories(profile_id, topic, status);

        CREATE TABLE IF NOT EXISTS mira_memory_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id       INTEGER REFERENCES mira_memories(id),
            profile_id      TEXT REFERENCES profiles(id),
            event_type      TEXT NOT NULL,
            before_json     TEXT NOT NULL DEFAULT '{}',
            after_json      TEXT NOT NULL DEFAULT '{}',
            reason          TEXT DEFAULT '',
            source_turn_id  TEXT DEFAULT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_mira_memory_events_memory
            ON mira_memory_events(memory_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_memory_events_profile
            ON mira_memory_events(profile_id, created_at DESC);
        """
    )


def _migrate_mira_session_summary_tables(conn: sqlite3.Connection):
    """Create the off-hot-path Mira session summary table."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_session_summaries (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id                  TEXT REFERENCES profiles(id),
            scope                       TEXT NOT NULL DEFAULT 'profile' CHECK(scope IN ('profile', 'household')),
            session_key                 TEXT NOT NULL,
            summary_text                TEXT NOT NULL,
            topics_json                 TEXT NOT NULL DEFAULT '[]',
            user_goals_json             TEXT NOT NULL DEFAULT '[]',
            unresolved_followups_json   TEXT NOT NULL DEFAULT '[]',
            preferences_seen_json       TEXT NOT NULL DEFAULT '[]',
            stress_signals_json         TEXT NOT NULL DEFAULT '[]',
            evidence_refs_json          TEXT NOT NULL DEFAULT '[]',
            source_turn_ids_json        TEXT NOT NULL DEFAULT '[]',
            confidence                  REAL NOT NULL DEFAULT 0.75,
            status                      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'deleted')),
            metadata_json               TEXT NOT NULL DEFAULT '{}',
            created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at                TEXT DEFAULT NULL,
            UNIQUE(profile_id, session_key)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_session_summaries_active
            ON mira_session_summaries(profile_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_session_summaries_session_key
            ON mira_session_summaries(profile_id, session_key);
        """
    )


def _migrate_mira_financial_understanding_tables(conn: sqlite3.Connection):
    """Create the off-hot-path Mira financial understanding read model."""
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


def _migrate_mira_financial_feedback_tables(conn: sqlite3.Connection):
    """Create user feedback records for Mira's financial understanding layer."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_financial_feedback (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id          TEXT DEFAULT NULL,
            target_type         TEXT NOT NULL CHECK(target_type IN ('fact', 'insight', 'advisor_card', 'advisor_read', 'category', 'merchant', 'profile', 'cashflow', 'account')),
            target_id           TEXT NOT NULL DEFAULT '',
            fact_id             INTEGER DEFAULT NULL,
            insight_id          INTEGER DEFAULT NULL,
            subject_type        TEXT NOT NULL CHECK(subject_type IN ('profile', 'category', 'merchant', 'subscription', 'account', 'cashflow', 'advisor_read')),
            subject_key         TEXT NOT NULL DEFAULT '',
            feedback_type       TEXT NOT NULL CHECK(feedback_type IN ('accepted', 'dismissed', 'corrected', 'snoozed', 'too_sensitive', 'more_like_this', 'less_like_this')),
            correction_text     TEXT NOT NULL DEFAULT '',
            normalized_effect   TEXT NOT NULL CHECK(normalized_effect IN ('acknowledge', 'suppress', 'downrank', 'uprank', 'reframe', 'promote_to_memory', 'update_operating_preference')),
            safe_summary        TEXT NOT NULL DEFAULT '',
            sensitivity         TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
            source              TEXT NOT NULL DEFAULT 'chat' CHECK(source IN ('chat', 'dashboard', 'memory_ui', 'proactive_card', 'advisor_read')),
            metadata_json       TEXT NOT NULL DEFAULT '{}',
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at          TEXT DEFAULT NULL,
            status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'cleared'))
        );

        CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_profile_created
            ON mira_financial_feedback(profile_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_target
            ON mira_financial_feedback(profile_id, target_type, target_id, status);
        CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_subject
            ON mira_financial_feedback(profile_id, subject_type, subject_key, status);
        CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_effect
            ON mira_financial_feedback(profile_id, normalized_effect, status);
        """
    )


def _migrate_mira_stated_intent_tables(conn: sqlite3.Connection):
    """Create subject-scoped stated money commitments for Mira."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_stated_intents (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id          TEXT DEFAULT NULL,
            memory_id           INTEGER DEFAULT NULL REFERENCES mira_memories(id),
            subject_type        TEXT NOT NULL CHECK(subject_type IN ('merchant', 'category', 'habit', 'account', 'cashflow')),
            subject_key         TEXT NOT NULL DEFAULT '',
            subject_label       TEXT NOT NULL DEFAULT '',
            intent_kind         TEXT NOT NULL CHECK(intent_kind IN ('cut', 'hold', 'grow', 'monitor', 'avoid', 'increase')),
            baseline_scope      TEXT NOT NULL DEFAULT 'mtd_vs_prior_3_full_months',
            target_text         TEXT NOT NULL DEFAULT '',
            status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'completed', 'dismissed')),
            feedback_state      TEXT NOT NULL DEFAULT 'neutral' CHECK(feedback_state IN ('neutral', 'more_like_this', 'less_like_this', 'too_sensitive')),
            last_evaluated_at   TEXT DEFAULT NULL,
            evaluation_json     TEXT NOT NULL DEFAULT '{}',
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_mira_stated_intents_profile_status
            ON mira_stated_intents(profile_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_stated_intents_subject
            ON mira_stated_intents(profile_id, subject_type, subject_key, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mira_stated_intents_memory
            ON mira_stated_intents(profile_id, memory_id)
            WHERE memory_id IS NOT NULL;
        """
    )


def _migrate_mira_habit_streak_tables(conn: sqlite3.Connection):
    """Create deterministic positive habit streak facts for Mira."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_habit_streaks (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id          TEXT DEFAULT NULL,
            subject_type        TEXT NOT NULL CHECK(subject_type IN ('category', 'merchant', 'cashflow', 'habit')),
            subject_key         TEXT NOT NULL DEFAULT '',
            subject_label       TEXT NOT NULL DEFAULT '',
            streak_kind         TEXT NOT NULL CHECK(streak_kind IN ('under_envelope', 'lower_frequency', 'higher_savings', 'on_time_bill', 'lower_variance')),
            streak_length       INTEGER NOT NULL DEFAULT 0,
            current_value_json  TEXT NOT NULL DEFAULT '{}',
            baseline_json       TEXT NOT NULL DEFAULT '{}',
            summary             TEXT NOT NULL DEFAULT '',
            confidence          TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
            sensitivity         TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
            generated_at        TEXT NOT NULL,
            valid_until         TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'stale', 'dismissed')),
            fingerprint         TEXT NOT NULL,
            UNIQUE(profile_id, fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_profile_status
            ON mira_habit_streaks(profile_id, status, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_subject
            ON mira_habit_streaks(profile_id, subject_type, subject_key, status);
        CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_valid_until
            ON mira_habit_streaks(valid_until);
        """
    )


def _migrate_mira_monthly_retrospective_tables(conn: sqlite3.Connection):
    """Create closed-month retrospective diary entries for Mira."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_monthly_retrospectives (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id                  TEXT DEFAULT NULL,
            month_key                   TEXT NOT NULL,
            summary                     TEXT NOT NULL DEFAULT '',
            wins_json                   TEXT NOT NULL DEFAULT '[]',
            friction_json               TEXT NOT NULL DEFAULT '[]',
            improvement_themes_json     TEXT NOT NULL DEFAULT '[]',
            evidence_json               TEXT NOT NULL DEFAULT '{}',
            confidence                  TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
            generated_at                TEXT NOT NULL,
            status                      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'dismissed', 'stale')),
            fingerprint                 TEXT NOT NULL,
            UNIQUE(profile_id, month_key)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_monthly_retrospectives_profile_month
            ON mira_monthly_retrospectives(profile_id, month_key DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_monthly_retrospectives_status
            ON mira_monthly_retrospectives(profile_id, status, generated_at DESC);
        """
    )


def _migrate_mira_money_outlook_tables(conn: sqlite3.Connection):
    """Create cached deterministic money-outlook projections for Mira."""
    def add_column_if_missing(column: str, ddl: str) -> None:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(mira_outlook_snapshots)").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE mira_outlook_snapshots ADD COLUMN {column} {ddl}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mira_outlook_snapshots (
            id                                      INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id                              TEXT DEFAULT NULL,
            generated_at                            TEXT NOT NULL,
            valid_until                             TEXT NOT NULL,
            month_key                               TEXT NOT NULL,
            projected_end_balance                   REAL DEFAULT NULL,
            projected_income_remaining              REAL NOT NULL DEFAULT 0,
            projected_obligations_remaining         REAL NOT NULL DEFAULT 0,
            projected_flexible_spend_remaining      REAL NOT NULL DEFAULT 0,
            projected_savings_delta                 REAL DEFAULT NULL,
            target_savings_amount                   REAL NOT NULL DEFAULT 0,
            safe_to_spend_today                     REAL DEFAULT NULL,
            safe_to_spend_this_week                 REAL DEFAULT NULL,
            buffer_amount                           REAL DEFAULT NULL,
            buffer_status                           TEXT NOT NULL DEFAULT 'unknown',
            low_point_date                          TEXT DEFAULT NULL,
            low_point_amount                        REAL DEFAULT NULL,
            buffer_breach                           INTEGER NOT NULL DEFAULT 0,
            low_point_drivers_json                  TEXT NOT NULL DEFAULT '[]',
            confidence                              TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
            drivers_json                            TEXT NOT NULL DEFAULT '[]',
            caveats_json                            TEXT NOT NULL DEFAULT '[]',
            evidence_json                           TEXT NOT NULL DEFAULT '{}',
            fingerprint                             TEXT NOT NULL,
            UNIQUE(profile_id, month_key, fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_mira_outlook_snapshots_profile_month
            ON mira_outlook_snapshots(profile_id, month_key, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_mira_outlook_snapshots_valid_until
            ON mira_outlook_snapshots(valid_until);
        """
    )
    add_column_if_missing("safe_to_spend_today", "REAL DEFAULT NULL")
    add_column_if_missing("safe_to_spend_this_week", "REAL DEFAULT NULL")
    add_column_if_missing("buffer_amount", "REAL DEFAULT NULL")
    add_column_if_missing("buffer_status", "TEXT NOT NULL DEFAULT 'unknown'")
    add_column_if_missing("low_point_date", "TEXT DEFAULT NULL")
    add_column_if_missing("low_point_amount", "REAL DEFAULT NULL")
    add_column_if_missing("buffer_breach", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing("low_point_drivers_json", "TEXT NOT NULL DEFAULT '[]'")


def _migrate_public_planning_tables(conn: sqlite3.Connection):
    """Add public-release planning, ledger metadata, split, and manual account support."""
    tx_cols = [row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()]
    tx_additions = {
        "notes": "TEXT DEFAULT ''",
        "tags": "TEXT DEFAULT ''",
        "reviewed": "INTEGER NOT NULL DEFAULT 0",
    }
    for col, ddl in tx_additions.items():
        if col not in tx_cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")

    budget_cols = [row[1] for row in conn.execute("PRAGMA table_info(category_budgets)").fetchall()]
    if budget_cols and "rollover_mode" not in budget_cols:
        conn.execute("ALTER TABLE category_budgets ADD COLUMN rollover_mode TEXT NOT NULL DEFAULT 'none'")
    if budget_cols and "rollover_balance" not in budget_cols:
        conn.execute("ALTER TABLE category_budgets ADD COLUMN rollover_balance REAL NOT NULL DEFAULT 0.0")

    account_cols = [row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if account_cols and "manual_updated_at" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN manual_updated_at TEXT DEFAULT NULL")
    if account_cols and "manual_notes" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN manual_notes TEXT DEFAULT ''")
    if account_cols and "usual_due_day" not in account_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN usual_due_day INTEGER DEFAULT NULL")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id      TEXT NOT NULL,
            name            TEXT NOT NULL,
            goal_type       TEXT NOT NULL DEFAULT 'custom',
            target_amount   REAL NOT NULL DEFAULT 0.0,
            current_amount  REAL NOT NULL DEFAULT 0.0,
            target_date     TEXT DEFAULT NULL,
            linked_category TEXT DEFAULT NULL,
            linked_account_id TEXT DEFAULT NULL,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_goals_profile_active
            ON goals(profile_id, is_active);

        CREATE TABLE IF NOT EXISTS transaction_splits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id  TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
            category        TEXT NOT NULL REFERENCES categories(name),
            amount          REAL NOT NULL,
            notes           TEXT DEFAULT '',
            tags            TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_transaction_splits_tx
            ON transaction_splits(transaction_id);
        CREATE INDEX IF NOT EXISTS idx_transaction_splits_category
            ON transaction_splits(category);

        CREATE TABLE IF NOT EXISTS manual_account_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            profile_id  TEXT NOT NULL,
            balance     REAL NOT NULL,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_manual_snapshots_account_date
            ON manual_account_snapshots(account_id, recorded_at);
        CREATE INDEX IF NOT EXISTS idx_manual_snapshots_profile_date
            ON manual_account_snapshots(profile_id, recorded_at);
        """
    )


def _migrate_user_declared_subscription_category(conn: sqlite3.Connection):
    """Remember the obligation category for user-declared recurring items."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(user_declared_subscriptions)").fetchall()]
    if "category" not in cols:
        conn.execute("ALTER TABLE user_declared_subscriptions ADD COLUMN category TEXT DEFAULT 'Subscriptions'")
        conn.execute("UPDATE user_declared_subscriptions SET category = 'Subscriptions' WHERE category IS NULL OR category = ''")


def _migrate_user_declared_subscription_expected_day(conn: sqlite3.Connection):
    """Allow user-confirmed recurring items to carry an expected day-of-month."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(user_declared_subscriptions)").fetchall()]
    if "expected_day" not in cols:
        conn.execute("ALTER TABLE user_declared_subscriptions ADD COLUMN expected_day INTEGER DEFAULT NULL")


def _migrate_user_declared_subscription_amount_review(conn: sqlite3.Connection):
    """Remember dismissed recurring amount suggestions until newer evidence arrives."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(user_declared_subscriptions)").fetchall()]
    if "amount_review_dismissed_amount" not in cols:
        conn.execute("ALTER TABLE user_declared_subscriptions ADD COLUMN amount_review_dismissed_amount REAL DEFAULT NULL")
    if "amount_review_dismissed_latest_date" not in cols:
        conn.execute("ALTER TABLE user_declared_subscriptions ADD COLUMN amount_review_dismissed_latest_date TEXT DEFAULT NULL")
    if "amount_review_dismissed_at" not in cols:
        conn.execute("ALTER TABLE user_declared_subscriptions ADD COLUMN amount_review_dismissed_at TEXT DEFAULT NULL")


def _migrate_investments_lite(conn: sqlite3.Connection):
    """Add local-first manual holdings support without broker sync or price networking."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS investment_holdings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id      TEXT NOT NULL REFERENCES profiles(id),
            account_id      TEXT REFERENCES accounts(id) ON DELETE SET NULL,
            symbol          TEXT DEFAULT '',
            name            TEXT NOT NULL,
            asset_class     TEXT NOT NULL DEFAULT 'stock',
            quantity        REAL DEFAULT 0.0,
            cost_basis      REAL DEFAULT 0.0,
            current_price   REAL DEFAULT 0.0,
            manual_value    REAL DEFAULT NULL,
            target_percent  REAL DEFAULT NULL,
            notes           TEXT DEFAULT '',
            price_as_of     TEXT DEFAULT NULL,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_holdings_profile
            ON investment_holdings(profile_id, asset_class);
        CREATE INDEX IF NOT EXISTS idx_holdings_account
            ON investment_holdings(account_id);
        CREATE INDEX IF NOT EXISTS idx_holdings_symbol
            ON investment_holdings(symbol);
        """
    )


def _migrate_mira_hardening_tables(conn: sqlite3.Connection):
    """Tables used by Mira write confirmations and proactive insights."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending_operations (
            nonce        TEXT PRIMARY KEY,
            profile_id   TEXT DEFAULT NULL,
            operation    TEXT NOT NULL,
            params_json  TEXT NOT NULL DEFAULT '{}',
            preview_json TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at   TEXT NOT NULL,
            consumed_at  TEXT DEFAULT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_pending_operations_profile
            ON pending_operations(profile_id, expires_at);
        CREATE INDEX IF NOT EXISTS idx_pending_operations_expiry
            ON pending_operations(expires_at, consumed_at);

        CREATE TABLE IF NOT EXISTS proactive_insights (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id    TEXT DEFAULT NULL,
            kind          TEXT NOT NULL,
            insight_type  TEXT DEFAULT NULL,
            title         TEXT NOT NULL,
            body          TEXT NOT NULL,
            severity      TEXT NOT NULL DEFAULT 'info',
            priority      INTEGER NOT NULL DEFAULT 100,
            confidence    TEXT DEFAULT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            assumptions_json TEXT NOT NULL DEFAULT '[]',
            recommended_action TEXT DEFAULT '',
            fingerprint   TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'active',
            generated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            dismissed_at  TEXT DEFAULT NULL,
            dismissed_reason TEXT DEFAULT '',
            dismissed_type TEXT DEFAULT '',
            restored_at   TEXT DEFAULT NULL,
            valid_until   TEXT DEFAULT NULL,
            suppressed_at TEXT DEFAULT NULL,
            UNIQUE(profile_id, fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_proactive_insights_profile_status
            ON proactive_insights(profile_id, status, generated_at);
        CREATE INDEX IF NOT EXISTS idx_proactive_insights_kind
            ON proactive_insights(kind);
        """
    )
    _migrate_proactive_insight_phase5_columns(conn)


def _migrate_proactive_insight_phase5_columns(conn: sqlite3.Connection):
    cols = [row[1] for row in conn.execute("PRAGMA table_info(proactive_insights)").fetchall()]
    additions = {
        "insight_type": "TEXT DEFAULT NULL",
        "priority": "INTEGER NOT NULL DEFAULT 100",
        "confidence": "TEXT DEFAULT NULL",
        "assumptions_json": "TEXT NOT NULL DEFAULT '[]'",
        "recommended_action": "TEXT DEFAULT ''",
        "dismissed_reason": "TEXT DEFAULT ''",
        "dismissed_type": "TEXT DEFAULT ''",
        "restored_at": "TEXT DEFAULT NULL",
        "valid_until": "TEXT DEFAULT NULL",
        "suppressed_at": "TEXT DEFAULT NULL",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE proactive_insights ADD COLUMN {name} {ddl}")


def _migrate_transaction_intelligence_tables(conn: sqlite3.Connection):
    """Add Transaction Intelligence 2.0 enrichment and correction tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transaction_enrichment (
            transaction_id              TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
            profile_id                  TEXT NOT NULL,
            canonical_counterparty      TEXT DEFAULT '',
            display_counterparty        TEXT DEFAULT '',
            top_level_category          TEXT DEFAULT '',
            leaf_category               TEXT DEFAULT '',
            purpose_category            TEXT DEFAULT '',
            essentiality                TEXT DEFAULT 'unknown',
            recurrence                  TEXT DEFAULT 'unknown',
            semantic_type               TEXT DEFAULT 'spending',
            confidence_json             TEXT NOT NULL DEFAULT '{}',
            evidence_summary            TEXT DEFAULT '',
            evidence_json               TEXT NOT NULL DEFAULT '{}',
            source                      TEXT NOT NULL DEFAULT 'rules',
            method                      TEXT NOT NULL DEFAULT 'deterministic',
            model_version               TEXT NOT NULL DEFAULT 'transaction_enrichment_rules_v2',
            taxonomy_version            TEXT NOT NULL DEFAULT 'folio_taxonomy_v1',
            user_reviewed               INTEGER NOT NULL DEFAULT 0,
            created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (transaction_id, profile_id)
        );

        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_profile
            ON transaction_enrichment(profile_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_category
            ON transaction_enrichment(profile_id, top_level_category, leaf_category);
        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_counterparty
            ON transaction_enrichment(profile_id, canonical_counterparty);
        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_review
            ON transaction_enrichment(profile_id, user_reviewed);

        CREATE TABLE IF NOT EXISTS transaction_enrichment_corrections (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id      TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
            profile_id          TEXT NOT NULL,
            corrected_field     TEXT NOT NULL,
            old_value           TEXT DEFAULT '',
            new_value           TEXT NOT NULL,
            source              TEXT NOT NULL DEFAULT 'user/manual',
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_tx_enrichment_corrections_tx
            ON transaction_enrichment_corrections(profile_id, transaction_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tx_enrichment_corrections_field
            ON transaction_enrichment_corrections(profile_id, corrected_field, created_at);
        """
    )


def _migrate_recurring_obligations(conn: sqlite3.Connection):
    """Add the recurring obligation model and backfill it from legacy tables."""
    event_cols = [row[1] for row in conn.execute("PRAGMA table_info(subscription_events)").fetchall()]
    if "event_key" not in event_cols:
        conn.execute("ALTER TABLE subscription_events ADD COLUMN event_key TEXT DEFAULT NULL")

    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_events_event_key
            ON subscription_events(event_key)
            WHERE event_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS recurring_detection_runs (
            id                  TEXT PRIMARY KEY,
            profile_id          TEXT NOT NULL,
            mode                TEXT NOT NULL DEFAULT 'shadow',
            detector_version    INTEGER NOT NULL DEFAULT 1,
            started_at          TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at        TEXT,
            txn_count           INTEGER DEFAULT 0,
            candidate_count     INTEGER DEFAULT 0,
            status              TEXT NOT NULL DEFAULT 'running'
        );

        CREATE INDEX IF NOT EXISTS idx_recurring_runs_profile
            ON recurring_detection_runs(profile_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_recurring_runs_status
            ON recurring_detection_runs(profile_id, status);

        CREATE TABLE IF NOT EXISTS recurring_obligations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id          TEXT NOT NULL,
            obligation_key      TEXT NOT NULL,
            merchant_key        TEXT NOT NULL,
            display_name        TEXT NOT NULL DEFAULT '',
            service_tag         TEXT DEFAULT '',
            seed_name           TEXT DEFAULT '',
            category            TEXT DEFAULT 'Subscriptions',
            amount_cents        INTEGER NOT NULL DEFAULT 0,
            amount_p10_cents    INTEGER,
            amount_p90_cents    INTEGER,
            frequency           TEXT DEFAULT 'monthly',
            anchor_day          INTEGER,
            anchor_month        INTEGER,
            anchor_mode         TEXT DEFAULT 'observed_pattern',
            next_expected_date  TEXT,
            state               TEXT NOT NULL DEFAULT 'candidate',
            source              TEXT NOT NULL DEFAULT 'algorithm',
            confidence_score    INTEGER NOT NULL DEFAULT 0,
            confidence_label    TEXT NOT NULL DEFAULT 'low',
            evidence_json       TEXT NOT NULL DEFAULT '{}',
            first_seen_date     TEXT,
            last_seen_date      TEXT,
            last_run_id         TEXT,
            detector_version    INTEGER NOT NULL DEFAULT 1,
            last_user_action_at TEXT,
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now')),
            UNIQUE(profile_id, obligation_key)
        );

        CREATE INDEX IF NOT EXISTS idx_recurring_obligations_profile_state
            ON recurring_obligations(profile_id, state);
        CREATE INDEX IF NOT EXISTS idx_recurring_obligations_profile_merchant
            ON recurring_obligations(profile_id, merchant_key);
        CREATE INDEX IF NOT EXISTS idx_recurring_obligations_run
            ON recurring_obligations(last_run_id);

        CREATE TABLE IF NOT EXISTS recurring_feedback (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id          TEXT NOT NULL,
            obligation_key      TEXT DEFAULT '',
            merchant_key        TEXT NOT NULL,
            feedback_type       TEXT NOT NULL,
            scope               TEXT NOT NULL DEFAULT 'merchant',
            payload_json        TEXT NOT NULL DEFAULT '{}',
            expires_at          TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            superseded_at       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_recurring_feedback_active
            ON recurring_feedback(profile_id, merchant_key, feedback_type, scope)
            WHERE superseded_at IS NULL;
        CREATE INDEX IF NOT EXISTS idx_recurring_feedback_obligation
            ON recurring_feedback(profile_id, obligation_key)
            WHERE superseded_at IS NULL;

        CREATE TABLE IF NOT EXISTS recurring_events_v2 (
            merchant_key        TEXT NOT NULL,
            profile_id          TEXT NOT NULL,
            event_type          TEXT NOT NULL,
            period_bucket       TEXT NOT NULL,
            payload_json        TEXT NOT NULL DEFAULT '{}',
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (merchant_key, profile_id, event_type, period_bucket)
        );

        CREATE INDEX IF NOT EXISTS idx_recurring_events_v2_profile
            ON recurring_events_v2(profile_id, created_at DESC);

        INSERT OR IGNORE INTO recurring_obligations
            (profile_id, obligation_key, merchant_key, display_name, service_tag,
             category, amount_cents, frequency, anchor_day, anchor_mode,
             next_expected_date, state, source, confidence_score, confidence_label,
             evidence_json, first_seen_date, last_seen_date, detector_version,
             last_user_action_at, created_at, updated_at)
        SELECT
            COALESCE(profile_id, 'household') AS profile_id,
            UPPER(TRIM(merchant_key)) || ':user:' || UPPER(TRIM(COALESCE(NULLIF(clean_name, ''), merchant_key))) AS obligation_key,
            UPPER(TRIM(merchant_key)) AS merchant_key,
            COALESCE(NULLIF(clean_name, ''), merchant_key) AS display_name,
            COALESCE(NULLIF(clean_name, ''), merchant_key) AS service_tag,
            COALESCE(NULLIF(category, ''), 'Subscriptions') AS category,
            CAST(ROUND(COALESCE(subscription_amount, 0) * 100) AS INTEGER) AS amount_cents,
            COALESCE(NULLIF(subscription_frequency, ''), 'monthly') AS frequency,
            CASE
                WHEN next_expected_date IS NOT NULL AND length(next_expected_date) >= 10
                THEN CAST(substr(next_expected_date, 9, 2) AS INTEGER)
                ELSE NULL
            END AS anchor_day,
            'observed_pattern' AS anchor_mode,
            next_expected_date,
            CASE
                WHEN COALESCE(cancelled_by_user, 0) = 1 THEN 'cancelled'
                WHEN COALESCE(subscription_status, '') = 'inactive' THEN 'inactive'
                WHEN source = 'user' THEN 'confirmed'
                WHEN COALESCE(subscription_status, 'active') = 'active' THEN 'active'
                ELSE 'candidate'
            END AS state,
            COALESCE(NULLIF(source, ''), 'algorithm') AS source,
            CASE
                WHEN source = 'user' THEN 100
                WHEN source = 'seed' THEN 75
                ELSE 60
            END AS confidence_score,
            CASE
                WHEN source = 'user' THEN 'user'
                WHEN source = 'seed' THEN 'high'
                ELSE 'medium'
            END AS confidence_label,
            '{"backfilled_from":"merchants"}' AS evidence_json,
            last_charge_date,
            last_charge_date,
            1,
            CASE WHEN COALESCE(cancelled_by_user, 0) = 1 THEN cancelled_at ELSE NULL END,
            COALESCE(created_at, datetime('now')),
            COALESCE(updated_at, datetime('now'))
        FROM merchants
        WHERE is_subscription = 1
          AND COALESCE(merchant_key, '') != '';

        INSERT OR IGNORE INTO recurring_obligations
            (profile_id, obligation_key, merchant_key, display_name, service_tag,
             category, amount_cents, frequency, anchor_day, anchor_mode,
             next_expected_date, state, source, confidence_score, confidence_label,
             evidence_json, detector_version, last_user_action_at, created_at, updated_at)
        SELECT
            profile_id,
            UPPER(TRIM(merchant_name)) || ':user:' || UPPER(TRIM(merchant_name)) AS obligation_key,
            UPPER(TRIM(merchant_name)) AS merchant_key,
            merchant_name,
            merchant_name,
            COALESCE(NULLIF(category, ''), 'Subscriptions'),
            CAST(ROUND(COALESCE(amount, 0) * 100) AS INTEGER),
            COALESCE(NULLIF(frequency, ''), 'monthly'),
            expected_day,
            CASE WHEN expected_day IS NOT NULL THEN 'exact_day' ELSE 'observed_pattern' END,
            NULL,
            'confirmed',
            'user',
            100,
            'user',
            '{"backfilled_from":"user_declared_subscriptions"}',
            1,
            COALESCE(updated_at, created_at, datetime('now')),
            COALESCE(created_at, datetime('now')),
            COALESCE(updated_at, datetime('now'))
        FROM user_declared_subscriptions
        WHERE is_active = 1
          AND COALESCE(merchant_name, '') != '';

        INSERT INTO recurring_feedback
            (profile_id, obligation_key, merchant_key, feedback_type, scope,
             payload_json, created_at)
        SELECT
            profile_id,
            UPPER(TRIM(merchant_name)) || ':merchant',
            UPPER(TRIM(merchant_name)),
            'dismissed',
            'merchant',
            '{"backfilled_from":"dismissed_recurring"}',
            COALESCE(dismissed_at, datetime('now'))
        FROM dismissed_recurring d
        WHERE COALESCE(merchant_name, '') != ''
          AND NOT EXISTS (
              SELECT 1
              FROM recurring_feedback f
              WHERE f.profile_id = d.profile_id
                AND (
                    f.merchant_key = UPPER(TRIM(d.merchant_name))
                    OR REPLACE(UPPER(TRIM(f.merchant_key)), '_', ' ') = UPPER(TRIM(d.merchant_name))
                )
                AND f.feedback_type = 'dismissed'
                AND f.scope = 'merchant'
                AND f.superseded_at IS NULL
          );
        """
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    is_default      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id                  TEXT PRIMARY KEY,
    profile_id          TEXT NOT NULL REFERENCES profiles(id),
    institution_name    TEXT DEFAULT '',
    account_name        TEXT DEFAULT '',
    account_type        TEXT NOT NULL DEFAULT 'depository',
    account_subtype     TEXT DEFAULT '',
    current_balance     REAL DEFAULT 0.0,
    available_balance   REAL DEFAULT 0.0,
    usual_due_day       INTEGER DEFAULT NULL,
    currency            TEXT DEFAULT 'USD',
    last_synced_at      TEXT,
    is_active           INTEGER DEFAULT 1,
    FOREIGN KEY (profile_id) REFERENCES profiles(id)
);

CREATE TABLE IF NOT EXISTS categories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT UNIQUE NOT NULL,
    is_system           INTEGER DEFAULT 1,
    parent_category     TEXT DEFAULT NULL,
    expense_type        TEXT DEFAULT 'variable' CHECK(expense_type IN ('fixed', 'variable', 'non_expense')),
    expense_type_source TEXT DEFAULT 'system' CHECK(expense_type_source IN ('system', 'user')),
    is_active           INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id                      TEXT PRIMARY KEY,
    account_id              TEXT REFERENCES accounts(id),
    profile_id              TEXT NOT NULL REFERENCES profiles(id),
    date                    TEXT NOT NULL,
    description             TEXT NOT NULL DEFAULT '',
    raw_description         TEXT DEFAULT '',
    amount                  REAL NOT NULL DEFAULT 0.0,
    category                TEXT REFERENCES categories(name),
    categorization_source   TEXT NOT NULL DEFAULT 'uncategorized',
    original_category       TEXT DEFAULT NULL,
    transaction_type        TEXT DEFAULT '',
    counterparty_name       TEXT DEFAULT '',
    counterparty_type       TEXT DEFAULT '',
    teller_category         TEXT DEFAULT '',
    account_name            TEXT DEFAULT '',
    account_type            TEXT DEFAULT '',
    merchant_name           TEXT DEFAULT '',
    merchant_domain         TEXT DEFAULT '',
    merchant_industry       TEXT DEFAULT '',
    merchant_city           TEXT DEFAULT '',
    merchant_state          TEXT DEFAULT '',
    merchant_key            TEXT DEFAULT '',
    merchant_source         TEXT DEFAULT '',
    merchant_confidence     TEXT DEFAULT '',
    merchant_kind           TEXT DEFAULT '',
    enriched                INTEGER DEFAULT 0,
    confidence              TEXT DEFAULT '',
    is_excluded             INTEGER DEFAULT 0,
    expense_type            TEXT DEFAULT NULL,
    description_normalized  TEXT DEFAULT NULL,
    notes                   TEXT DEFAULT '',
    tags                    TEXT DEFAULT '',
    reviewed                INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT DEFAULT (datetime('now')),
    updated_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_profile ON transactions(profile_id);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_transactions_profile_date ON transactions(profile_id, date);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
CREATE VIEW IF NOT EXISTS transactions_visible AS
SELECT *
FROM transactions
WHERE COALESCE(is_excluded, 0) = 0;
CREATE TABLE IF NOT EXISTS transaction_enrichment (
    transaction_id              TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    profile_id                  TEXT NOT NULL,
    canonical_counterparty      TEXT DEFAULT '',
    display_counterparty        TEXT DEFAULT '',
    top_level_category          TEXT DEFAULT '',
    leaf_category               TEXT DEFAULT '',
    purpose_category            TEXT DEFAULT '',
    essentiality                TEXT DEFAULT 'unknown',
    recurrence                  TEXT DEFAULT 'unknown',
    semantic_type               TEXT DEFAULT 'spending',
    confidence_json             TEXT NOT NULL DEFAULT '{}',
    evidence_summary            TEXT DEFAULT '',
    evidence_json               TEXT NOT NULL DEFAULT '{}',
    source                      TEXT NOT NULL DEFAULT 'rules',
    method                      TEXT NOT NULL DEFAULT 'deterministic',
    model_version               TEXT NOT NULL DEFAULT 'transaction_enrichment_rules_v2',
    taxonomy_version            TEXT NOT NULL DEFAULT 'folio_taxonomy_v1',
    user_reviewed               INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (transaction_id, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_profile
    ON transaction_enrichment(profile_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_category
    ON transaction_enrichment(profile_id, top_level_category, leaf_category);
CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_counterparty
    ON transaction_enrichment(profile_id, canonical_counterparty);
CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_review
    ON transaction_enrichment(profile_id, user_reviewed);

CREATE TABLE IF NOT EXISTS transaction_enrichment_corrections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id      TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    profile_id          TEXT NOT NULL,
    corrected_field     TEXT NOT NULL,
    old_value           TEXT DEFAULT '',
    new_value           TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'user/manual',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tx_enrichment_corrections_tx
    ON transaction_enrichment_corrections(profile_id, transaction_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tx_enrichment_corrections_field
    ON transaction_enrichment_corrections(profile_id, corrected_field, created_at);
CREATE TABLE IF NOT EXISTS category_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,
    match_type  TEXT DEFAULT 'contains',
    category    TEXT NOT NULL REFERENCES categories(name),
    priority    INTEGER DEFAULT 100,
    source      TEXT NOT NULL DEFAULT 'user',
    profile_id  TEXT DEFAULT NULL,
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (category) REFERENCES categories(name),
    FOREIGN KEY (profile_id) REFERENCES profiles(id)
);

CREATE INDEX IF NOT EXISTS idx_rules_source_priority ON category_rules(source, priority DESC);
CREATE INDEX IF NOT EXISTS idx_rules_active ON category_rules(is_active);

CREATE TABLE IF NOT EXISTS merchant_aliases (
    merchant_key TEXT NOT NULL,
    profile_id   TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'user',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (merchant_key, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_merchant_aliases_profile
    ON merchant_aliases(profile_id);
CREATE INDEX IF NOT EXISTS idx_merchant_aliases_key
    ON merchant_aliases(merchant_key);

CREATE TABLE IF NOT EXISTS transaction_splits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category        TEXT NOT NULL REFERENCES categories(name),
    amount          REAL NOT NULL,
    notes           TEXT DEFAULT '',
    tags            TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transaction_splits_tx
    ON transaction_splits(transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_splits_category
    ON transaction_splits(category);

CREATE TABLE IF NOT EXISTS goals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT NOT NULL,
    name            TEXT NOT NULL,
    goal_type       TEXT NOT NULL DEFAULT 'custom',
    target_amount   REAL NOT NULL DEFAULT 0.0,
    current_amount  REAL NOT NULL DEFAULT 0.0,
    target_date     TEXT DEFAULT NULL,
    linked_category TEXT DEFAULT NULL,
    linked_account_id TEXT DEFAULT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_goals_profile_active
    ON goals(profile_id, is_active);

CREATE TABLE IF NOT EXISTS category_budgets (
    profile_id      TEXT NOT NULL,
    category        TEXT NOT NULL REFERENCES categories(name),
    amount          REAL NOT NULL,
    rollover_mode   TEXT NOT NULL DEFAULT 'none',
    rollover_balance REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (profile_id, category)
);

CREATE INDEX IF NOT EXISTS idx_category_budgets_profile ON category_budgets(profile_id);

CREATE TABLE IF NOT EXISTS net_worth_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    profile_id  TEXT NOT NULL REFERENCES profiles(id),
    total_assets REAL DEFAULT 0.0,
    total_owed  REAL DEFAULT 0.0,
    net_worth   REAL DEFAULT 0.0,
    UNIQUE(date, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_nw_profile_date ON net_worth_history(profile_id, date);

CREATE TABLE IF NOT EXISTS copilot_conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          TEXT REFERENCES profiles(id),
    user_message        TEXT NOT NULL,
    generated_sql       TEXT DEFAULT '',
    query_result        TEXT DEFAULT '',
    assistant_response  TEXT DEFAULT '',
    operation_type      TEXT DEFAULT 'read',
    rows_affected       INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS saved_insights (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id              TEXT REFERENCES profiles(id),
    question                TEXT NOT NULL,
    answer                  TEXT NOT NULL,
    kind                    TEXT NOT NULL DEFAULT 'insight' CHECK(kind IN ('insight', 'decision', 'policy_note')),
    pinned                  INTEGER NOT NULL DEFAULT 0,
    source_conversation_id  INTEGER REFERENCES copilot_conversations(id),
    created_at              TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_saved_insights_profile ON saved_insights(profile_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_insights_pinned ON saved_insights(profile_id, pinned) WHERE pinned = 1;

CREATE TABLE IF NOT EXISTS memory_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT REFERENCES profiles(id),
    section         TEXT NOT NULL CHECK(section IN ('identity', 'preferences', 'goals', 'concerns', 'open_questions')),
    body            TEXT NOT NULL,
    confidence      TEXT NOT NULL DEFAULT 'stated' CHECK(confidence IN ('stated', 'saved', 'inferred')),
    evidence        TEXT DEFAULT '',
    theme           TEXT DEFAULT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    superseded_at   TEXT DEFAULT NULL,
    superseded_by   INTEGER REFERENCES memory_entries(id),
    expires_at      TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_entries_active ON memory_entries(profile_id, section) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memory_entries_expiry ON memory_entries(expires_at) WHERE expires_at IS NOT NULL AND superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_memory_entries_theme ON memory_entries(profile_id, theme) WHERE theme IS NOT NULL AND superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS memory_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT REFERENCES profiles(id),
    theme           TEXT NOT NULL,
    note            TEXT NOT NULL,
    source_conversation_id INTEGER REFERENCES copilot_conversations(id),
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_obs_profile_theme ON memory_observations(profile_id, theme, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT REFERENCES profiles(id),
    section         TEXT NOT NULL CHECK(section IN ('identity', 'preferences', 'goals', 'concerns', 'open_questions')),
    body            TEXT NOT NULL,
    confidence      TEXT NOT NULL DEFAULT 'inferred' CHECK(confidence IN ('stated', 'saved', 'inferred')),
    evidence        TEXT DEFAULT '',
    theme           TEXT DEFAULT NULL,
    supersedes_id   INTEGER REFERENCES memory_entries(id),
    source          TEXT NOT NULL DEFAULT 'agent' CHECK(source IN ('agent', 'observation_threshold', 'consolidation', 'save_to_memory')),
    source_conversation_id INTEGER REFERENCES copilot_conversations(id),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'rejected')),
    created_at      TEXT DEFAULT (datetime('now')),
    resolved_at     TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_proposals_pending ON memory_proposals(profile_id, status, created_at DESC) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS mira_memories (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id              TEXT REFERENCES profiles(id),
    scope                   TEXT NOT NULL DEFAULT 'profile' CHECK(scope IN ('profile', 'household')),
    memory_type             TEXT NOT NULL CHECK(memory_type IN (
        'preference', 'goal', 'constraint', 'stressor', 'commitment',
        'rejected_advice', 'coaching_state', 'identity_fact', 'tone_preference'
    )),
    topic                   TEXT NOT NULL DEFAULT '',
    normalized_text         TEXT NOT NULL,
    original_text           TEXT NOT NULL DEFAULT '',
    source_summary          TEXT DEFAULT '',
    sensitivity             TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
    confidence              REAL NOT NULL DEFAULT 1.0,
    source_conversation_id  INTEGER REFERENCES copilot_conversations(id),
    source_turn_id          TEXT DEFAULT NULL,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at            TEXT DEFAULT NULL,
    expires_at              TEXT DEFAULT NULL,
    pinned                  INTEGER NOT NULL DEFAULT 0,
    superseded_by           INTEGER REFERENCES mira_memories(id),
    status                  TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'deleted', 'rejected')),
    consent                 TEXT NOT NULL DEFAULT 'explicit',
    consent_at              TEXT DEFAULT (datetime('now')),
    metadata_json           TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_mira_memories_active
    ON mira_memories(profile_id, scope, status, memory_type, topic, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mira_memories_expiry
    ON mira_memories(expires_at) WHERE expires_at IS NOT NULL AND status = 'active';
CREATE INDEX IF NOT EXISTS idx_mira_memories_topic
    ON mira_memories(profile_id, topic, status);

CREATE TABLE IF NOT EXISTS mira_memory_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       INTEGER REFERENCES mira_memories(id),
    profile_id      TEXT REFERENCES profiles(id),
    event_type      TEXT NOT NULL,
    before_json     TEXT NOT NULL DEFAULT '{}',
    after_json      TEXT NOT NULL DEFAULT '{}',
    reason          TEXT DEFAULT '',
    source_turn_id  TEXT DEFAULT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mira_memory_events_memory
    ON mira_memory_events(memory_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mira_memory_events_profile
    ON mira_memory_events(profile_id, created_at DESC);

CREATE TABLE IF NOT EXISTS mira_session_summaries (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id                  TEXT REFERENCES profiles(id),
    scope                       TEXT NOT NULL DEFAULT 'profile' CHECK(scope IN ('profile', 'household')),
    session_key                 TEXT NOT NULL,
    summary_text                TEXT NOT NULL,
    topics_json                 TEXT NOT NULL DEFAULT '[]',
    user_goals_json             TEXT NOT NULL DEFAULT '[]',
    unresolved_followups_json   TEXT NOT NULL DEFAULT '[]',
    preferences_seen_json       TEXT NOT NULL DEFAULT '[]',
    stress_signals_json         TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json          TEXT NOT NULL DEFAULT '[]',
    source_turn_ids_json        TEXT NOT NULL DEFAULT '[]',
    confidence                  REAL NOT NULL DEFAULT 0.75,
    status                      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'deleted')),
    metadata_json               TEXT NOT NULL DEFAULT '{}',
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at                TEXT DEFAULT NULL,
    UNIQUE(profile_id, session_key)
);

CREATE INDEX IF NOT EXISTS idx_mira_session_summaries_active
    ON mira_session_summaries(profile_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mira_session_summaries_session_key
    ON mira_session_summaries(profile_id, session_key);

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

CREATE TABLE IF NOT EXISTS mira_financial_feedback (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          TEXT DEFAULT NULL,
    target_type         TEXT NOT NULL CHECK(target_type IN ('fact', 'insight', 'advisor_card', 'advisor_read', 'category', 'merchant', 'profile', 'cashflow', 'account')),
    target_id           TEXT NOT NULL DEFAULT '',
    fact_id             INTEGER DEFAULT NULL,
    insight_id          INTEGER DEFAULT NULL,
    subject_type        TEXT NOT NULL CHECK(subject_type IN ('profile', 'category', 'merchant', 'subscription', 'account', 'cashflow', 'advisor_read')),
    subject_key         TEXT NOT NULL DEFAULT '',
    feedback_type       TEXT NOT NULL CHECK(feedback_type IN ('accepted', 'dismissed', 'corrected', 'snoozed', 'too_sensitive', 'more_like_this', 'less_like_this')),
    correction_text     TEXT NOT NULL DEFAULT '',
    normalized_effect   TEXT NOT NULL CHECK(normalized_effect IN ('acknowledge', 'suppress', 'downrank', 'uprank', 'reframe', 'promote_to_memory', 'update_operating_preference')),
    safe_summary        TEXT NOT NULL DEFAULT '',
    sensitivity         TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
    source              TEXT NOT NULL DEFAULT 'chat' CHECK(source IN ('chat', 'dashboard', 'memory_ui', 'proactive_card', 'advisor_read')),
    metadata_json       TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at          TEXT DEFAULT NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'cleared'))
);

CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_profile_created
    ON mira_financial_feedback(profile_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_target
    ON mira_financial_feedback(profile_id, target_type, target_id, status);
CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_subject
    ON mira_financial_feedback(profile_id, subject_type, subject_key, status);
CREATE INDEX IF NOT EXISTS idx_mira_financial_feedback_effect
    ON mira_financial_feedback(profile_id, normalized_effect, status);

CREATE TABLE IF NOT EXISTS mira_stated_intents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          TEXT DEFAULT NULL,
    memory_id           INTEGER DEFAULT NULL REFERENCES mira_memories(id),
    subject_type        TEXT NOT NULL CHECK(subject_type IN ('merchant', 'category', 'habit', 'account', 'cashflow')),
    subject_key         TEXT NOT NULL DEFAULT '',
    subject_label       TEXT NOT NULL DEFAULT '',
    intent_kind         TEXT NOT NULL CHECK(intent_kind IN ('cut', 'hold', 'grow', 'monitor', 'avoid', 'increase')),
    baseline_scope      TEXT NOT NULL DEFAULT 'mtd_vs_prior_3_full_months',
    target_text         TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'paused', 'completed', 'dismissed')),
    feedback_state      TEXT NOT NULL DEFAULT 'neutral' CHECK(feedback_state IN ('neutral', 'more_like_this', 'less_like_this', 'too_sensitive')),
    last_evaluated_at   TEXT DEFAULT NULL,
    evaluation_json     TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mira_stated_intents_profile_status
    ON mira_stated_intents(profile_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mira_stated_intents_subject
    ON mira_stated_intents(profile_id, subject_type, subject_key, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mira_stated_intents_memory
    ON mira_stated_intents(profile_id, memory_id)
    WHERE memory_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS mira_habit_streaks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id          TEXT DEFAULT NULL,
    subject_type        TEXT NOT NULL CHECK(subject_type IN ('category', 'merchant', 'cashflow', 'habit')),
    subject_key         TEXT NOT NULL DEFAULT '',
    subject_label       TEXT NOT NULL DEFAULT '',
    streak_kind         TEXT NOT NULL CHECK(streak_kind IN ('under_envelope', 'lower_frequency', 'higher_savings', 'on_time_bill', 'lower_variance')),
    streak_length       INTEGER NOT NULL DEFAULT 0,
    current_value_json  TEXT NOT NULL DEFAULT '{}',
    baseline_json       TEXT NOT NULL DEFAULT '{}',
    summary             TEXT NOT NULL DEFAULT '',
    confidence          TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
    sensitivity         TEXT NOT NULL DEFAULT 'low' CHECK(sensitivity IN ('low', 'medium', 'high')),
    generated_at        TEXT NOT NULL,
    valid_until         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'stale', 'dismissed')),
    fingerprint         TEXT NOT NULL,
    UNIQUE(profile_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_profile_status
    ON mira_habit_streaks(profile_id, status, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_subject
    ON mira_habit_streaks(profile_id, subject_type, subject_key, status);
CREATE INDEX IF NOT EXISTS idx_mira_habit_streaks_valid_until
    ON mira_habit_streaks(valid_until);

CREATE TABLE IF NOT EXISTS mira_monthly_retrospectives (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id                  TEXT DEFAULT NULL,
    month_key                   TEXT NOT NULL,
    summary                     TEXT NOT NULL DEFAULT '',
    wins_json                   TEXT NOT NULL DEFAULT '[]',
    friction_json               TEXT NOT NULL DEFAULT '[]',
    improvement_themes_json     TEXT NOT NULL DEFAULT '[]',
    evidence_json               TEXT NOT NULL DEFAULT '{}',
    confidence                  TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
    generated_at                TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'dismissed', 'stale')),
    fingerprint                 TEXT NOT NULL,
    UNIQUE(profile_id, month_key)
);

CREATE INDEX IF NOT EXISTS idx_mira_monthly_retrospectives_profile_month
    ON mira_monthly_retrospectives(profile_id, month_key DESC);
CREATE INDEX IF NOT EXISTS idx_mira_monthly_retrospectives_status
    ON mira_monthly_retrospectives(profile_id, status, generated_at DESC);

CREATE TABLE IF NOT EXISTS mira_outlook_snapshots (
    id                                      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id                              TEXT DEFAULT NULL,
    generated_at                            TEXT NOT NULL,
    valid_until                             TEXT NOT NULL,
    month_key                               TEXT NOT NULL,
    projected_end_balance                   REAL DEFAULT NULL,
    projected_income_remaining              REAL NOT NULL DEFAULT 0,
    projected_obligations_remaining         REAL NOT NULL DEFAULT 0,
    projected_flexible_spend_remaining      REAL NOT NULL DEFAULT 0,
    projected_savings_delta                 REAL DEFAULT NULL,
    target_savings_amount                   REAL NOT NULL DEFAULT 0,
    confidence                              TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('high', 'medium', 'low')),
    drivers_json                            TEXT NOT NULL DEFAULT '[]',
    caveats_json                            TEXT NOT NULL DEFAULT '[]',
    evidence_json                           TEXT NOT NULL DEFAULT '{}',
    fingerprint                             TEXT NOT NULL,
    UNIQUE(profile_id, month_key, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_mira_outlook_snapshots_profile_month
    ON mira_outlook_snapshots(profile_id, month_key, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mira_outlook_snapshots_valid_until
    ON mira_outlook_snapshots(valid_until);

CREATE TABLE IF NOT EXISTS subscription_seeds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    pattern         TEXT NOT NULL,
    frequency_hint  TEXT DEFAULT 'monthly',
    category        TEXT DEFAULT 'Subscriptions',
    source          TEXT NOT NULL DEFAULT 'system' CHECK(source IN ('system', 'user')),
    created_by      TEXT DEFAULT NULL,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(pattern, source, created_by)
);

CREATE INDEX IF NOT EXISTS idx_sub_seeds_pattern ON subscription_seeds(pattern);
CREATE INDEX IF NOT EXISTS idx_sub_seeds_source ON subscription_seeds(source);
CREATE INDEX IF NOT EXISTS idx_sub_seeds_active ON subscription_seeds(is_active);

CREATE TABLE IF NOT EXISTS enrichment_cache (
    pattern_key         TEXT PRIMARY KEY,
    merchant_name       TEXT NOT NULL DEFAULT '',
    merchant_domain     TEXT DEFAULT '',
    merchant_industry   TEXT DEFAULT '',
    merchant_city       TEXT DEFAULT '',
    merchant_state      TEXT DEFAULT '',
    merchant_country    TEXT DEFAULT '',
    source              TEXT NOT NULL DEFAULT 'trove' CHECK(source IN ('trove', 'seed')),
    hit_count           INTEGER DEFAULT 1,
    first_seen          TEXT DEFAULT (datetime('now')),
    last_seen           TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_enrichment_cache_source ON enrichment_cache(source);

CREATE TABLE IF NOT EXISTS teller_category_map (
    teller_category     TEXT PRIMARY KEY,
    folio_category      TEXT DEFAULT NULL,
    confidence          TEXT NOT NULL DEFAULT 'rule-medium' CHECK(confidence IN ('rule-high', 'rule-medium')),
    source              TEXT NOT NULL DEFAULT 'system' CHECK(source IN ('system', 'user')),
    created_at          TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS enrolled_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile         TEXT    NOT NULL,
    institution     TEXT    NOT NULL DEFAULT '',
    token_encrypted TEXT    NOT NULL,
    owner_name      TEXT    NOT NULL DEFAULT '',
    enrollment_id   TEXT    DEFAULT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    UNIQUE(profile, token_encrypted)
);

CREATE INDEX IF NOT EXISTS idx_enrolled_tokens_profile ON enrolled_tokens(profile);
CREATE INDEX IF NOT EXISTS idx_enrolled_tokens_active ON enrolled_tokens(is_active);

CREATE TABLE IF NOT EXISTS merchants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_key        TEXT NOT NULL,
    clean_name          TEXT,
    logo_url            TEXT,
    domain              TEXT,
    category            TEXT,
    industry            TEXT,
    source              TEXT DEFAULT 'trove',
    is_subscription     INTEGER DEFAULT 0,
    subscription_frequency TEXT,
    subscription_amount REAL,
    subscription_status TEXT,
    cancelled_at        TEXT,
    cancelled_by_user   INTEGER DEFAULT 0,
    last_charge_date    TEXT,
    next_expected_date  TEXT,
    total_spent         REAL DEFAULT 0,
    charge_count        INTEGER DEFAULT 0,
    profile_id          TEXT,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(merchant_key, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_merchants_profile ON merchants(profile_id);
CREATE INDEX IF NOT EXISTS idx_merchants_subscription ON merchants(is_subscription, profile_id);
CREATE INDEX IF NOT EXISTS idx_merchants_key ON merchants(merchant_key);

CREATE TABLE IF NOT EXISTS user_declared_subscriptions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_name       TEXT NOT NULL,
    amount              REAL NOT NULL,
    frequency           TEXT NOT NULL DEFAULT 'monthly',
    category            TEXT NOT NULL DEFAULT 'Subscriptions',
    expected_day        INTEGER DEFAULT NULL,
    amount_review_dismissed_amount REAL DEFAULT NULL,
    amount_review_dismissed_latest_date TEXT DEFAULT NULL,
    amount_review_dismissed_at TEXT DEFAULT NULL,
    profile_id          TEXT NOT NULL,
    is_active           INTEGER DEFAULT 1,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now')),
    UNIQUE(merchant_name, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_user_declared_subs_profile ON user_declared_subscriptions(profile_id);
CREATE INDEX IF NOT EXISTS idx_user_declared_subs_active ON user_declared_subscriptions(is_active, profile_id);

CREATE TABLE IF NOT EXISTS subscription_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type          TEXT NOT NULL,
    merchant_name       TEXT NOT NULL,
    profile_id          TEXT NOT NULL,
    detail              TEXT DEFAULT '{}',
    event_key           TEXT DEFAULT NULL,
    created_at          TEXT DEFAULT (datetime('now')),
    is_read             INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sub_events_profile ON subscription_events(profile_id);
CREATE INDEX IF NOT EXISTS idx_sub_events_unread ON subscription_events(is_read, profile_id);
CREATE INDEX IF NOT EXISTS idx_sub_events_type ON subscription_events(event_type);

CREATE TABLE IF NOT EXISTS dismissed_recurring (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_name       TEXT NOT NULL,
    profile_id          TEXT NOT NULL,
    dismissed_at        TEXT DEFAULT (datetime('now')),
    UNIQUE(merchant_name, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_dismissed_recurring_profile ON dismissed_recurring(profile_id);

CREATE TABLE IF NOT EXISTS simplefin_connections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    profile             TEXT NOT NULL,
    display_name        TEXT NOT NULL DEFAULT '',
    access_url_encrypted TEXT NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    last_synced_at      TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sf_connections_profile ON simplefin_connections(profile);
CREATE INDEX IF NOT EXISTS idx_sf_connections_active  ON simplefin_connections(is_active);

CREATE TABLE IF NOT EXISTS receipt_imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT NOT NULL DEFAULT 'household',
    store_name      TEXT NOT NULL DEFAULT '',
    receipt_date    TEXT DEFAULT NULL,
    subtotal        REAL DEFAULT NULL,
    tax             REAL DEFAULT NULL,
    total           REAL DEFAULT NULL,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'approved', 'discarded')),
    parser_model    TEXT NOT NULL DEFAULT '',
    confidence      REAL DEFAULT 0.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_receipt_imports_profile_status ON receipt_imports(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_receipt_imports_date ON receipt_imports(receipt_date);

CREATE TABLE IF NOT EXISTS receipt_items (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_import_id     INTEGER NOT NULL REFERENCES receipt_imports(id) ON DELETE CASCADE,
    raw_item_text         TEXT NOT NULL DEFAULT '',
    normalized_item_name  TEXT NOT NULL DEFAULT '',
    quantity              REAL DEFAULT NULL,
    unit                  TEXT NOT NULL DEFAULT '',
    total_price           REAL DEFAULT NULL,
    unit_price            REAL DEFAULT NULL,
    confidence            REAL DEFAULT 0.0,
    user_corrected        INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_receipt_items_import ON receipt_items(receipt_import_id);
CREATE INDEX IF NOT EXISTS idx_receipt_items_normalized ON receipt_items(normalized_item_name);

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_operations (
    nonce        TEXT PRIMARY KEY,
    profile_id   TEXT DEFAULT NULL,
    operation    TEXT NOT NULL,
    params_json  TEXT NOT NULL DEFAULT '{}',
    preview_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL,
    consumed_at  TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_operations_profile
    ON pending_operations(profile_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_pending_operations_expiry
    ON pending_operations(expires_at, consumed_at);

CREATE TABLE IF NOT EXISTS proactive_insights (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    TEXT DEFAULT NULL,
    kind          TEXT NOT NULL,
    insight_type  TEXT DEFAULT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT 'info',
    priority      INTEGER NOT NULL DEFAULT 100,
    confidence    TEXT DEFAULT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    recommended_action TEXT DEFAULT '',
    fingerprint   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    generated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    dismissed_at  TEXT DEFAULT NULL,
    dismissed_reason TEXT DEFAULT '',
    dismissed_type TEXT DEFAULT '',
    restored_at   TEXT DEFAULT NULL,
    valid_until   TEXT DEFAULT NULL,
    suppressed_at TEXT DEFAULT NULL,
    UNIQUE(profile_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_proactive_insights_profile_status
    ON proactive_insights(profile_id, status, generated_at);
CREATE INDEX IF NOT EXISTS idx_proactive_insights_kind
    ON proactive_insights(kind);
"""

# ══════════════════════════════════════════════════════════════════════════════
# SEEDING
# ══════════════════════════════════════════════════════════════════════════════

# Each entry is (name, expense_type).
# 'fixed'       = recurring / committed costs
# 'variable'    = discretionary spending
# 'non_expense' = transfers, income, credit card payments (excluded from F/V split)
DEFAULT_CATEGORIES = [
    ("Food & Dining",       "variable"),
    ("Groceries",           "variable"),
    ("Transportation",      "variable"),
    ("Gas & Fuel",          "variable"),
    ("Parking & Tolls",     "variable"),
    ("Auto Maintenance",    "variable"),
    ("Vehicle Registration","variable"),
    ("Entertainment",       "variable"),
    ("Shopping",            "variable"),
    ("Household Supplies",  "variable"),
    ("Personal Care",       "variable"),
    ("Healthcare",          "variable"),
    ("Utilities",           "fixed"),
    ("Internet & Phone",    "fixed"),
    ("Housing",             "fixed"),
    ("Rent & Mortgage",     "fixed"),
    ("Home Maintenance",    "variable"),
    ("Education",           "variable"),
    ("Childcare",           "fixed"),
    ("Pets",                "variable"),
    ("Gifts & Donations",   "variable"),
    ("Auto Payment",        "fixed"),
    ("Debt & Loan Payment", "fixed"),
    ("Savings Transfer",    "non_expense"),
    ("Credit Card Payment", "non_expense"),
    ("Income",              "non_expense"),
    ("Credits & Refunds",   "non_expense"),
    ("Personal Transfer",   "non_expense"),
    ("Cash Withdrawal",     "non_expense"),
    ("Cash Deposit",        "non_expense"),
    ("Investment Transfer", "non_expense"),
    ("Subscriptions",       "fixed"),
    ("Fees & Charges",      "variable"),
    ("Travel",              "variable"),
    ("Taxes",               "fixed"),
    ("Insurance",           "fixed"),
    ("Other",               "variable"),
]


def _seed_default_categories(conn: sqlite3.Connection):
    """Insert default categories if they don't exist."""
    for cat_name, exp_type in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, is_system, expense_type, expense_type_source) VALUES (?, 1, ?, 'system')",
            (cat_name, exp_type),
        )
    conn.execute(
        """UPDATE categories
              SET is_system = 1,
                  expense_type = 'non_expense',
                  expense_type_source = 'system',
                  is_active = 1
            WHERE name = 'Credits & Refunds'"""
    )

# System rules derived from categorizer.py's regex patterns
# These replace the hardcoded patterns — now queryable and editable
SYSTEM_RULES = [
    # Savings Transfer patterns (high confidence)
    {"pattern": r"transfer\s+to\s+sav", "match_type": "regex", "category": "Savings Transfer", "priority": 900, "source": "system"},
    {"pattern": r"transfer\s+from\s+chk", "match_type": "regex", "category": "Savings Transfer", "priority": 900, "source": "system"},
    {"pattern": r"transfer\s+from\s+sav", "match_type": "regex", "category": "Savings Transfer", "priority": 900, "source": "system"},
    {"pattern": r"transfer\s+to\s+chk", "match_type": "regex", "category": "Savings Transfer", "priority": 900, "source": "system"},
    {"pattern": r"savings\s+transfer", "match_type": "regex", "category": "Savings Transfer", "priority": 900, "source": "system"},
    {"pattern": r"online\s+(?:scheduled\s+)?transfer", "match_type": "regex", "category": "Savings Transfer", "priority": 850, "source": "system"},
    {"pattern": r"internal\s+transfer", "match_type": "regex", "category": "Savings Transfer", "priority": 850, "source": "system"},
    {"pattern": r"account\s+transfer", "match_type": "regex", "category": "Savings Transfer", "priority": 850, "source": "system"},
    {"pattern": r"xfer\s+(?:to|from)", "match_type": "regex", "category": "Savings Transfer", "priority": 850, "source": "system"},
    {"pattern": r"mobile\s+transfer", "match_type": "regex", "category": "Savings Transfer", "priority": 850, "source": "system"},

    # Tax patterns (medium confidence)
    {"pattern": r"\birs\b", "match_type": "regex", "category": "Taxes", "priority": 700, "source": "system"},
    {"pattern": r"tax\s*(?:payment|pymt|pmt|refund)", "match_type": "regex", "category": "Taxes", "priority": 700, "source": "system"},
    {"pattern": r"\bus\s*treasury\b", "match_type": "regex", "category": "Taxes", "priority": 700, "source": "system"},
    {"pattern": r"state\s*tax", "match_type": "regex", "category": "Taxes", "priority": 700, "source": "system"},
    {"pattern": r"franchise\s*tax", "match_type": "regex", "category": "Taxes", "priority": 700, "source": "system"},
    {"pattern": r"tax\s*board", "match_type": "regex", "category": "Taxes", "priority": 700, "source": "system"},
    {"pattern": r"usataxpymt", "match_type": "regex", "category": "Taxes", "priority": 700, "source": "system"},

    # P2P patterns (medium confidence)
    {"pattern": r"\bzelle\b", "match_type": "regex", "category": "Personal Transfer", "priority": 600, "source": "system"},
    {"pattern": r"\bvenmo\b", "match_type": "regex", "category": "Personal Transfer", "priority": 600, "source": "system"},
    {"pattern": r"\bcashapp\b", "match_type": "regex", "category": "Personal Transfer", "priority": 600, "source": "system"},
    {"pattern": r"cash\s*app", "match_type": "regex", "category": "Personal Transfer", "priority": 600, "source": "system"},
    {"pattern": r"paypal.*(?:send|p2p|instant)", "match_type": "regex", "category": "Personal Transfer", "priority": 600, "source": "system"},
]


def _seed_system_rules(conn: sqlite3.Connection):
    """Insert system categorization rules if the table is empty."""
    count = conn.execute(
        "SELECT COUNT(*) FROM category_rules WHERE source = 'system'"
    ).fetchone()[0]

    if count > 0:
        return  # Already seeded

    for rule in SYSTEM_RULES:
        conn.execute(
            """INSERT INTO category_rules (pattern, match_type, category, priority, source)
               VALUES (?, ?, ?, ?, ?)""",
            (rule["pattern"], rule["match_type"], rule["category"], rule["priority"], rule["source"]),
        )


def _deactivate_brittle_cashflow_system_rules(conn: sqlite3.Connection):
    """Keep historical broad card-payment regexes from acting as direct rules."""
    brittle_patterns = (
        r"credit\s*c(?:a)?rd",
        r"credit\s*crd",
        r"\bepay\b",
        r"\bautopay\b",
        r"applecard",
        r"gsbank.*payment",
        r"card\s*payment",
    )
    conn.executemany(
        """UPDATE category_rules
              SET is_active = 0
            WHERE source = 'system'
              AND category = 'Credit Card Payment'
              AND pattern = ?""",
        [(pattern,) for pattern in brittle_patterns],
    )

# Teller category → Folio category mapping defaults.
# Source: Teller API docs (28 documented values as of 2025).
# NULL folio_category means "no useful signal — skip, let other rules or LLM handle it."
# Users can override any mapping via source='user' rows in the DB.
TELLER_CATEGORY_DEFAULTS = [
    # (teller_category, folio_category, confidence)
    ("accommodation", "Travel",          "rule-medium"),
    ("advertising",   None,              "rule-medium"),
    ("bar",           "Food & Dining",   "rule-medium"),
    ("charity",       None,              "rule-medium"),
    ("clothing",      "Shopping",        "rule-medium"),
    ("dining",        "Food & Dining",   "rule-medium"),
    ("education",     "Education",       "rule-medium"),
    ("electronics",   "Shopping",        "rule-medium"),
    ("entertainment", "Entertainment",   "rule-medium"),
    ("fuel",          "Gas & Fuel",      "rule-medium"),
    ("general",       None,              "rule-medium"),
    ("groceries",     "Groceries",       "rule-medium"),
    ("health",        "Healthcare",      "rule-medium"),
    ("home",          "Housing",         "rule-medium"),
    ("income",        "Income",          "rule-medium"),
    ("insurance",     "Insurance",       "rule-medium"),
    ("investment",    "Investment Transfer", "rule-medium"),
    ("loan",          "Debt & Loan Payment", "rule-medium"),
    ("office",        "Shopping",        "rule-medium"),
    ("phone",         "Internet & Phone","rule-medium"),
    ("service",       None,              "rule-medium"),
    ("shopping",      "Shopping",        "rule-medium"),
    ("software",      "Subscriptions",   "rule-medium"),
    ("sport",         "Entertainment",   "rule-medium"),
    ("tax",           "Taxes",           "rule-medium"),
    ("transport",     "Transportation",  "rule-medium"),
    ("transportation","Transportation",  "rule-medium"),
    ("utilities",     "Utilities",       "rule-medium"),
]

def _seed_teller_category_map(conn: sqlite3.Connection):
    """
    Seed the teller_category_map table with system defaults.
    Re-run safe — refreshes system rows while leaving user overrides alone.
    """
    for teller_cat, folio_cat, confidence in TELLER_CATEGORY_DEFAULTS:
        conn.execute(
            """INSERT INTO teller_category_map
               (teller_category, folio_category, confidence, source)
               VALUES (?, ?, ?, 'system')
               ON CONFLICT(teller_category) DO UPDATE SET
                   folio_category = excluded.folio_category,
                   confidence = excluded.confidence
               WHERE teller_category_map.source = 'system'""",
            (teller_cat, folio_cat, confidence),
        )


def _extract_merchant_pattern(description: str) -> str:
    """
    Extract a conservative, reusable transaction fingerprint without semantic
    merchant or location stripping.

    This intentionally avoids hardcoded city lists, global state-code
    removal, and broad delimiter splitting. Merchant identity should come from
    enrichment/merchant_key; this fallback only removes mechanical transaction
    noise that is structurally obvious across users.
    """
    import re

    if not description:
        return ""

    def _domain_to_token(match: re.Match) -> str:
        host = match.group(1) or ""
        labels = [part for part in re.split(r"[.\-]+", host.upper()) if part]
        if not labels:
            return " "
        return f" {labels[-1]} "

    text = description.upper().strip()
    text = text.replace("·", " ")

    # Remove mechanically obvious identifiers and bank/network boilerplate.
    text = re.sub(r"\bID:\s*\*+|\bID:\s*[A-Z0-9._:-]+", " ", text)
    text = re.sub(r"\bCO\s*ID:\s*[A-Z0-9._:-]+", " ", text)
    text = re.sub(r"\bINDN:\s*[^,;]+", " ", text)
    text = re.sub(r"\bCONF(?:IRMATION)?#?\s*[A-Z0-9._:-]+", " ", text)

    # Store/terminal/reference numbers that have clear syntax.
    text = re.sub(r"#\s*\d+\b", " ", text)
    text = re.sub(r"\b((?:STORE|STR|LOCATION|LOC|TERM|TERMINAL|REF|TRACE|AUTH)\s*)#?\s*\d+\b", r"\1", text)

    # Dates, phone numbers, and long opaque numeric IDs.
    text = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    text = re.sub(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b", " ", text)
    text = re.sub(r"\b\d{3}[-.]\d{7}\b", " ", text)
    text = re.sub(r"\b\d{5,}\b", " ", text)
    text = re.sub(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{6,}\b", " ", text)

    # Common URL/billing suffixes are mechanical descriptors, not merchant names.
    text = re.sub(r"\b(?:HTTPS?|WWW)\b", " ", text)
    text = re.sub(
        r"\b([A-Z0-9.-]+)\.(?:COM|NET|ORG|IO|CO|AI|TV|FM|APP|ME|US|UK|LY)(?:/[A-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?\b",
        _domain_to_token,
        text,
    )
    text = re.sub(r"\bCOM\s+BILL\b", " ", text)

    # Treat punctuation as separators but keep all meaningful tokens; do not
    # assume the first chunk before '*' or '-' is the merchant.
    text = re.sub(r"[*|•]", " ", text)
    text = re.sub(r"[-_/]+", " ", text)
    text = re.sub(r"[^A-Z0-9& ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if text:
        seen_tokens: set[str] = set()
        deduped_tokens: list[str] = []
        for token in text.split():
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            deduped_tokens.append(token)
        text = " ".join(deduped_tokens)

    if len(text) < 3 or text in ("ACH", "DEBIT", "CREDIT", "PAYMENT", "TRANSFER", "CHECK", "UNKNOWN"):
        return ""

    return text


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def sync_subscription_seeds():
    """
    Load subscription_seeds.json into the subscription_seeds table.
    System seeds are upserted on every startup.
    User-contributed seeds (source='user') are never touched.
    """
    import json as json_module
    from pathlib import Path as FilePath

    seed_path = FilePath(__file__).parent / "subscription_seeds.json"
    if not seed_path.exists():
        logger.info("subscription_seeds.json not found — skipping seed sync.")
        return

    with open(seed_path, "r", encoding="utf-8") as f:
        data = json_module.load(f)

    # Accept both top-level list and {"subscriptions": [...]} wrapper
    if isinstance(data, list):
        seeds = data
    elif isinstance(data, dict):
        seeds = data.get("subscriptions", data.get("merchants", []))
    else:
        seeds = []

    if not seeds:
        return

    with get_db() as conn:
        # Clear all system seeds and re-insert (ensures deletions in JSON are reflected)
        conn.execute("DELETE FROM subscription_seeds WHERE source = 'system'")

        for seed in seeds:
            name = seed.get("name", "")
            frequency_hint = seed.get("frequency_hint", "monthly")
            category = seed.get("category", "Subscriptions")

            for pattern in seed.get("patterns", []):
                conn.execute(
                    """INSERT OR IGNORE INTO subscription_seeds
                       (name, pattern, frequency_hint, category, source)
                       VALUES (?, ?, ?, ?, 'system')""",
                    (name, pattern.upper(), frequency_hint, category),
                )

    count = 0
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM subscription_seeds WHERE source = 'system'"
        ).fetchone()[0]
    logger.info("Subscription seeds synced: %d system patterns loaded.", count)


def sync_enrichment_cache_from_seeds():
    """
    Populate enrichment_cache with merchant identities derived from
    subscription_seeds. Runs on startup after seed sync.

    Only inserts seed-sourced entries — never overwrites Trove-sourced entries
    (which have richer metadata like domain, city, industry).

    Uses the same dedup key normalization as enricher.py so that runtime
    lookups match correctly.
    """
    import re as _re

    def _normalize_pattern_key(pattern: str) -> str:
        """Match enricher.py's _dedup_key normalization."""
        normalized = _re.sub(r"\d{6,}", "XXXXX", pattern)
        return normalized.upper().strip()

    with get_db() as conn:
        # Load all active subscription seeds
        rows = conn.execute(
            """SELECT DISTINCT name, pattern, category
               FROM subscription_seeds
               WHERE is_active = 1
               ORDER BY source DESC, length(pattern) DESC"""
        ).fetchall()

        if not rows:
            return

        inserted = 0
        for row in rows:
            name = row[0]
            pattern = row[1]
            category = row[2] or "Subscriptions"

            pattern_key = _normalize_pattern_key(pattern)

            if len(pattern_key) < 3:
                continue

            # INSERT OR IGNORE — never overwrite existing entries
            # (Trove entries are richer and should take precedence)
            result = conn.execute(
                """INSERT OR IGNORE INTO enrichment_cache
                   (pattern_key, merchant_name, merchant_industry, source)
                   VALUES (?, ?, ?, 'seed')""",
                (pattern_key, name, category),
            )
            if result.rowcount > 0:
                inserted += 1

    logger.info(
        "Enrichment cache seeded: %d patterns from subscription seeds.", inserted
    )

def upsert_merchant_from_enrichment(
    conn: sqlite3.Connection,
    merchant_key: str,
    enrichment: dict,
    profile_id: str,
    source: str = "trove",
):
    """
    Upsert the merchants table with enrichment data.
    Never overwrites fields where existing source = 'user'.
    Trove data can update trove-sourced fields and fill empty fields.
    """
    if not merchant_key or not profile_id:
        return

    clean_name = (enrichment.get("name") or enrichment.get("merchant_name") or "").strip()
    domain = (enrichment.get("domain") or enrichment.get("merchant_domain") or "").strip()
    category = (enrichment.get("category") or "").strip()
    industry = (enrichment.get("industry") or enrichment.get("merchant_industry") or "").strip()
    logo_url = (enrichment.get("logo_url") or "").strip()

    if not clean_name and not domain:
        return

    try:
        conn.execute(
            """INSERT INTO merchants
               (merchant_key, clean_name, logo_url, domain, category, industry, source, profile_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(merchant_key, profile_id) DO UPDATE SET
                   clean_name = CASE
                       WHEN merchants.source = 'user' AND merchants.clean_name != '' THEN merchants.clean_name
                       WHEN excluded.clean_name != '' THEN excluded.clean_name
                       ELSE merchants.clean_name END,
                   logo_url = CASE
                       WHEN excluded.logo_url != '' THEN excluded.logo_url
                       ELSE merchants.logo_url END,
                   domain = CASE
                       WHEN merchants.source = 'user' AND merchants.domain != '' THEN merchants.domain
                       WHEN excluded.domain != '' THEN excluded.domain
                       ELSE merchants.domain END,
                   category = CASE
                       WHEN merchants.source = 'user' AND merchants.category != '' THEN merchants.category
                       WHEN excluded.category != '' THEN excluded.category
                       ELSE merchants.category END,
                   industry = CASE
                       WHEN merchants.source = 'user' AND merchants.industry != '' THEN merchants.industry
                       WHEN excluded.industry != '' THEN excluded.industry
                       ELSE merchants.industry END,
                   source = CASE
                       WHEN merchants.source = 'user' THEN 'user'
                       ELSE excluded.source END,
                   updated_at = datetime('now')""",
            (merchant_key, clean_name, logo_url, domain, category, industry, source, profile_id),
        )
    except Exception as e:
        logger.debug("Merchant upsert failed for %s: %s", merchant_key, e)
# ══════════════════════════════════════════════════════════════════════════════
# NOTE: Database initialization is handled by main.py's startup event.
# Do NOT auto-initialize here — it causes double-init and can trigger
# DB operations during Docker build or test imports.
# ══════════════════════════════════════════════════════════════════════════════
