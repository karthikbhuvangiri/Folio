from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any

from merchant_identity import canonicalize_merchant_key


TAXONOMY_VERSION = "folio_taxonomy_v1"
RULE_VERSION = "transaction_enrichment_rules_v2"
LOW_CONFIDENCE_DEFAULT = 0.7
FALSE_ENV_VALUES = {"0", "false", "no", "off"}

TOP_LEVEL_CATEGORIES = (
    "Income",
    "Transfers",
    "Housing",
    "Utilities",
    "Groceries",
    "Dining",
    "Transportation",
    "Healthcare",
    "Insurance",
    "Debt & Payments",
    "Subscriptions",
    "Entertainment",
    "Shopping",
    "Travel",
    "Taxes",
    "Fees & Financial",
    "Personal Care",
    "Other",
)

FOLIO_CATEGORY_MAP: dict[str, tuple[str, str, str, str]] = {
    "food & dining": ("Dining", "Restaurants", "Meals", "discretionary"),
    "dining": ("Dining", "Restaurants", "Meals", "discretionary"),
    "restaurants": ("Dining", "Restaurants", "Meals", "discretionary"),
    "groceries": ("Groceries", "Groceries", "Household food", "essential"),
    "transportation": ("Transportation", "Transportation", "Mobility", "essential"),
    "gas & fuel": ("Transportation", "Gas & Fuel", "Vehicle fuel", "essential"),
    "parking & tolls": ("Transportation", "Parking & Tolls", "Vehicle access", "essential"),
    "auto maintenance": ("Transportation", "Auto Maintenance", "Vehicle upkeep", "essential"),
    "vehicle registration": ("Transportation", "Vehicle Registration", "Vehicle licensing", "essential"),
    "entertainment": ("Entertainment", "Entertainment", "Leisure", "discretionary"),
    "shopping": ("Shopping", "General Shopping", "Retail purchases", "discretionary"),
    "household supplies": ("Shopping", "Household Supplies", "Household essentials", "essential"),
    "personal care": ("Personal Care", "Personal Care", "Personal services", "discretionary"),
    "healthcare": ("Healthcare", "Healthcare", "Health", "essential"),
    "utilities": ("Utilities", "Utilities", "Home services", "essential"),
    "internet & phone": ("Utilities", "Internet & Phone", "Telecom", "essential"),
    "housing": ("Housing", "Housing", "Shelter", "essential"),
    "rent & mortgage": ("Housing", "Rent & Mortgage", "Shelter payment", "essential"),
    "home maintenance": ("Housing", "Home Maintenance", "Home upkeep", "essential"),
    "education": ("Other", "Education", "Education", "discretionary"),
    "childcare": ("Other", "Childcare", "Caregiving", "essential"),
    "pets": ("Other", "Pets", "Pet care", "discretionary"),
    "gifts & donations": ("Other", "Gifts & Donations", "Giving", "discretionary"),
    "auto payment": ("Debt & Payments", "Auto Payment", "Vehicle debt payment", "essential"),
    "debt & loan payment": ("Debt & Payments", "Debt & Loan Payment", "Debt payment", "essential"),
    "savings transfer": ("Transfers", "Savings Transfer", "Internal transfer", "non_expense"),
    "personal transfer": ("Transfers", "Personal Transfer", "Personal transfer", "non_expense"),
    "cash withdrawal": ("Transfers", "Cash Withdrawal", "Cash movement", "non_expense"),
    "cash deposit": ("Transfers", "Cash Deposit", "Cash movement", "non_expense"),
    "investment transfer": ("Transfers", "Investment Transfer", "Investment movement", "non_expense"),
    "credit card payment": ("Debt & Payments", "Credit Card Payment", "Debt payment", "non_expense"),
    "income": ("Income", "Income", "Income", "non_expense"),
    "subscriptions": ("Subscriptions", "Subscriptions", "Recurring services", "essential"),
    "fees & charges": ("Fees & Financial", "Bank Fees", "Financial fees", "essential"),
    "fees": ("Fees & Financial", "Bank Fees", "Financial fees", "essential"),
    "alcohol": ("Dining", "Alcohol", "Alcohol", "discretionary"),
    "vaping": ("Personal Care", "Vaping", "Tobacco/vaping", "discretionary"),
    "tobacco": ("Personal Care", "Tobacco", "Tobacco/vaping", "discretionary"),
    "credits & refunds": ("Other", "Credits & Refunds", "Refunds and credits", "non_expense"),
    "refunds": ("Other", "Credits & Refunds", "Refunds and credits", "non_expense"),
    "travel": ("Travel", "Travel", "Travel", "discretionary"),
    "taxes": ("Taxes", "Taxes", "Taxes", "essential"),
    "insurance": ("Insurance", "Insurance", "Risk protection", "essential"),
    "other": ("Other", "Other", "Unclassified spending", "unknown"),
}

PROVIDER_CATEGORY_MAP: dict[str, str] = {
    "bar": "Food & Dining",
    "dining": "Food & Dining",
    "groceries": "Groceries",
    "education": "Education",
    "fuel": "Gas & Fuel",
    "transport": "Transportation",
    "transportation": "Transportation",
    "health": "Healthcare",
    "home": "Housing",
    "income": "Income",
    "insurance": "Insurance",
    "investment": "Investment Transfer",
    "loan": "Debt & Loan Payment",
    "phone": "Internet & Phone",
    "software": "Subscriptions",
    "tax": "Taxes",
    "utilities": "Utilities",
}

INDUSTRY_CATEGORY_MAP: dict[str, str] = {
    "grocery": "Groceries",
    "restaurant": "Food & Dining",
    "coffee shop": "Food & Dining",
    "fast food": "Food & Dining",
    "bar / nightlife": "Food & Dining",
    "gas station": "Gas & Fuel",
    "pharmacy": "Healthcare",
    "healthcare provider": "Healthcare",
    "insurance": "Insurance",
    "utilities": "Utilities",
    "internet / telecom": "Internet & Phone",
    "streaming / media": "Subscriptions",
    "software / saas": "Subscriptions",
    "e-commerce marketplace": "Shopping",
    "electronics retail": "Shopping",
    "general retail": "Shopping",
    "home improvement": "Home Maintenance",
    "transportation / rideshare": "Transportation",
    "travel / airline": "Travel",
    "travel / hotel": "Travel",
    "subscription service": "Subscriptions",
    "bank / financial service": "Fees & Charges",
    "government / tax": "Taxes",
    "education": "Education",
    "fitness": "Personal Care",
    "personal care": "Personal Care",
}

SEMANTIC_NON_EXPENSE_CATEGORIES = {
    "income": "income",
    "savings transfer": "transfer",
    "personal transfer": "transfer",
    "cash withdrawal": "transfer",
    "cash deposit": "transfer",
    "investment transfer": "transfer",
    "credit card payment": "payment",
}

NON_MERCHANT_KINDS = {"personal_transfer", "credit_card_payment", "income", "tax", "bank_fee"}

CORRECTABLE_FIELDS = {
    "canonical_counterparty",
    "display_counterparty",
    "top_level_category",
    "leaf_category",
    "purpose_category",
    "essentiality",
    "recurrence",
    "semantic_type",
}

CONFIDENCE_FAMILIES: dict[str, tuple[str, ...]] = {
    "counterparty": ("canonical_counterparty", "display_counterparty"),
    "taxonomy": ("top_level_category", "leaf_category"),
    "purpose": ("purpose_category",),
    "essentiality": ("essentiality",),
    "recurrence": ("recurrence",),
    "semantic_type": ("semantic_type",),
}


def enrichment_repair_enabled() -> bool:
    return os.getenv("MIRA_ENRICHMENT_REPAIR_ENABLED", "0").strip().lower() not in FALSE_ENV_VALUES


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent local schema helper for scripts/tests that do not call init_db()."""
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
        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_profile
            ON transaction_enrichment(profile_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_category
            ON transaction_enrichment(profile_id, top_level_category, leaf_category);
        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_counterparty
            ON transaction_enrichment(profile_id, canonical_counterparty);
        CREATE INDEX IF NOT EXISTS idx_transaction_enrichment_review
            ON transaction_enrichment(profile_id, user_reviewed);
        CREATE INDEX IF NOT EXISTS idx_tx_enrichment_corrections_tx
            ON transaction_enrichment_corrections(profile_id, transaction_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_tx_enrichment_corrections_field
            ON transaction_enrichment_corrections(profile_id, corrected_field, created_at);
        """
    )


def taxonomy_snapshot() -> dict[str, Any]:
    return {
        "version": TAXONOMY_VERSION,
        "top_level_categories": list(TOP_LEVEL_CATEGORIES),
        "folio_category_map": {
            name: {
                "top_level_category": values[0],
                "leaf_category": values[1],
                "purpose_category": values[2],
                "essentiality": values[3],
            }
            for name, values in sorted(FOLIO_CATEGORY_MAP.items())
        },
    }


def enrich_transaction_dict(
    tx: dict[str, Any],
    conn: sqlite3.Connection | None = None,
    *,
    apply_user_corrections: bool = True,
) -> dict[str, Any]:
    profile_id = str(tx.get("profile_id") or tx.get("profile") or "household")
    transaction_id = str(tx.get("id") or tx.get("original_id") or tx.get("transaction_id") or "")
    confidence: dict[str, float] = {}
    evidence: dict[str, Any] = {"layers": []}

    counterparty = _counterparty(tx, conn, profile_id, confidence, evidence)
    category = _category(tx, confidence, evidence)
    semantic_type, semantic_conf = _semantic_type(tx, category)
    confidence["semantic_type"] = semantic_conf
    recurrence, recurrence_conf, recurrence_evidence = _recurrence(tx, conn, profile_id, counterparty["canonical_counterparty"])
    confidence["recurrence"] = recurrence_conf
    if recurrence_evidence:
        evidence["recurring"] = recurrence_evidence
        evidence["layers"].append("recurring_obligation")

    if bool(tx.get("is_excluded")):
        semantic_type = "excluded"
        confidence["semantic_type"] = 1.0
        evidence["layers"].append("explicit_excluded_flag")

    result = {
        "transaction_id": transaction_id,
        "profile_id": profile_id,
        "canonical_counterparty": counterparty["canonical_counterparty"],
        "display_counterparty": counterparty["display_counterparty"],
        "top_level_category": category["top_level_category"],
        "leaf_category": category["leaf_category"],
        "purpose_category": category["purpose_category"],
        "essentiality": category["essentiality"],
        "recurrence": recurrence,
        "semantic_type": semantic_type,
        "confidence": _round_confidence(confidence),
        "evidence_summary": _evidence_summary(tx, counterparty, category, semantic_type, recurrence, evidence),
        "evidence": evidence,
        "source": "rules",
        "method": "deterministic",
        "model_version": RULE_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "user_reviewed": 0,
    }

    if conn is not None and apply_user_corrections and transaction_id:
        _apply_latest_corrections(conn, result)
    return result


def enrich_transaction_by_id(
    conn: sqlite3.Connection,
    transaction_id: str,
    profile_id: str | None = None,
    *,
    persist: bool = False,
) -> dict[str, Any] | None:
    tx = _load_transaction(conn, transaction_id, profile_id)
    if tx is None:
        return None
    enrichment = enrich_transaction_dict(tx, conn)
    if persist:
        upsert_enrichment(conn, enrichment)
        stored = get_stored_enrichment(conn, enrichment["transaction_id"], enrichment["profile_id"])
        return stored or enrichment
    return enrichment


def get_stored_enrichment(conn: sqlite3.Connection, transaction_id: str, profile_id: str | None = None) -> dict[str, Any] | None:
    ensure_schema(conn)
    params: list[Any] = [transaction_id]
    where = "transaction_id = ?"
    if profile_id and profile_id != "household":
        where += " AND profile_id = ?"
        params.append(profile_id)
    row = conn.execute(f"SELECT * FROM transaction_enrichment WHERE {where} LIMIT 1", params).fetchone()
    if row is None:
        return None
    return _row_to_enrichment(row, persisted=True)


def upsert_enrichment(conn: sqlite3.Connection, enrichment: dict[str, Any]) -> None:
    ensure_schema(conn)
    transaction_id = str(enrichment.get("transaction_id") or "")
    profile_id = str(enrichment.get("profile_id") or "household")
    corrected_fields = _corrected_fields(conn, transaction_id, profile_id)
    stored = get_stored_enrichment(conn, transaction_id, profile_id) if corrected_fields else None
    storage = dict(enrichment)
    if stored:
        for field in corrected_fields:
            if field in CORRECTABLE_FIELDS:
                storage[field] = stored.get(field, storage.get(field, ""))
    storage["user_reviewed"] = int(bool(corrected_fields or storage.get("user_reviewed")))

    confidence = storage.get("confidence") if isinstance(storage.get("confidence"), dict) else {}
    evidence = storage.get("evidence") if isinstance(storage.get("evidence"), dict) else {}
    conn.execute(
        """
        INSERT INTO transaction_enrichment (
            transaction_id, profile_id, canonical_counterparty, display_counterparty,
            top_level_category, leaf_category, purpose_category, essentiality,
            recurrence, semantic_type, confidence_json, evidence_summary,
            evidence_json, source, method, model_version, taxonomy_version, user_reviewed
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(transaction_id, profile_id) DO UPDATE SET
            canonical_counterparty = excluded.canonical_counterparty,
            display_counterparty = excluded.display_counterparty,
            top_level_category = excluded.top_level_category,
            leaf_category = excluded.leaf_category,
            purpose_category = excluded.purpose_category,
            essentiality = excluded.essentiality,
            recurrence = excluded.recurrence,
            semantic_type = excluded.semantic_type,
            confidence_json = excluded.confidence_json,
            evidence_summary = excluded.evidence_summary,
            evidence_json = excluded.evidence_json,
            source = excluded.source,
            method = excluded.method,
            model_version = excluded.model_version,
            taxonomy_version = excluded.taxonomy_version,
            user_reviewed = excluded.user_reviewed,
            updated_at = datetime('now')
        """,
        (
            transaction_id,
            profile_id,
            storage.get("canonical_counterparty", ""),
            storage.get("display_counterparty", ""),
            storage.get("top_level_category", ""),
            storage.get("leaf_category", ""),
            storage.get("purpose_category", ""),
            storage.get("essentiality", "unknown"),
            storage.get("recurrence", "unknown"),
            storage.get("semantic_type", "spending"),
            json.dumps(confidence, sort_keys=True),
            storage.get("evidence_summary", ""),
            json.dumps(evidence, sort_keys=True),
            storage.get("source", "rules"),
            storage.get("method", "deterministic"),
            storage.get("model_version", RULE_VERSION),
            storage.get("taxonomy_version", TAXONOMY_VERSION),
            int(bool(storage.get("user_reviewed"))),
        ),
    )


def record_correction(
    conn: sqlite3.Connection,
    *,
    transaction_id: str,
    profile_id: str,
    corrected_field: str,
    new_value: str,
    source: str = "user/manual",
) -> dict[str, Any]:
    ensure_schema(conn)
    if corrected_field not in CORRECTABLE_FIELDS:
        raise ValueError(f"Unsupported correction field: {corrected_field}")
    current = get_stored_enrichment(conn, transaction_id, profile_id)
    if current is None:
        current = enrich_transaction_by_id(conn, transaction_id, profile_id, persist=True)
    if current is None:
        raise ValueError("transaction not found")
    old_value = str(current.get(corrected_field) or "")
    conn.execute(
        """
        INSERT INTO transaction_enrichment_corrections
            (transaction_id, profile_id, corrected_field, old_value, new_value, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (transaction_id, profile_id, corrected_field, old_value, str(new_value), source),
    )
    conn.execute(
        f"""
        UPDATE transaction_enrichment
           SET {corrected_field} = ?,
               user_reviewed = 1,
               updated_at = datetime('now')
         WHERE transaction_id = ? AND profile_id = ?
        """,
        (str(new_value), transaction_id, profile_id),
    )
    updated = get_stored_enrichment(conn, transaction_id, profile_id) or {}
    return {
        "transaction_id": transaction_id,
        "profile_id": profile_id,
        "corrected_field": corrected_field,
        "old_value": old_value,
        "new_value": str(new_value),
        "source": source,
        "enrichment": updated,
    }


def explain_transaction(conn: sqlite3.Connection, transaction_id: str, profile_id: str | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    tx = _load_transaction(conn, transaction_id, profile_id)
    if tx is None:
        return {"error": "transaction not found", "transaction_id": transaction_id}
    stored = get_stored_enrichment(conn, transaction_id, tx.get("profile_id"))
    enrichment = stored or enrich_transaction_dict(tx, conn)
    corrections = _correction_rows(conn, transaction_id, tx.get("profile_id"))
    return {
        "transaction_id": transaction_id,
        "profile_id": tx.get("profile_id"),
        "persisted": bool(stored),
        "transaction": _public_transaction(tx),
        "enrichment": enrichment,
        "confidence": enrichment.get("confidence", {}),
        "evidence_summary": enrichment.get("evidence_summary", ""),
        "corrections": corrections,
        "provenance": {
            "tool": "explain_transaction_enrichment",
            "transaction_id": transaction_id,
            "profile_id": tx.get("profile_id"),
            "source": enrichment.get("source"),
            "method": enrichment.get("method"),
            "model_version": enrichment.get("model_version"),
            "taxonomy_version": enrichment.get("taxonomy_version"),
        },
    }


def find_low_confidence(
    conn: sqlite3.Connection,
    profile_id: str | None = None,
    *,
    threshold: float = LOW_CONFIDENCE_DEFAULT,
    limit: int = 25,
) -> dict[str, Any]:
    ensure_schema(conn)
    threshold = max(0.0, min(float(threshold), 1.0))
    limit = max(1, min(int(limit or 25), 100))
    rows = _candidate_transactions(conn, profile_id, max(limit * 4, 50))
    matches: list[dict[str, Any]] = []
    for tx in rows:
        stored = get_stored_enrichment(conn, tx["id"], tx["profile_id"])
        enrichment = stored or enrich_transaction_dict(tx, conn)
        min_conf = min((enrichment.get("confidence") or {"overall": 0}).values() or [0])
        if min_conf < threshold or not stored:
            weak_families = sorted(_low_confidence_families(enrichment.get("confidence") or {}, threshold=threshold))
            matches.append(
                {
                    "transaction_id": tx["id"],
                    "profile_id": tx["profile_id"],
                    "date": tx.get("date"),
                    "description": tx.get("description"),
                    "amount": tx.get("amount"),
                    "category": tx.get("category"),
                    "persisted": bool(stored),
                    "minimum_confidence": round(float(min_conf), 3),
                    "weak_confidence_families": weak_families,
                    "actionable_quality_flags": _actionable_quality_flags(enrichment, weak_families),
                    "enrichment": _compact_enrichment(enrichment),
                    "evidence_summary": enrichment.get("evidence_summary", ""),
                }
            )
        if len(matches) >= limit:
            break
    return {
        "threshold": threshold,
        "count": len(matches),
        "transactions": matches,
        "provenance": {
            "tool": "find_low_confidence_transactions",
            "profile_id": profile_id or "household",
            "threshold": threshold,
            "limit": limit,
        },
    }


def quality_summary(conn: sqlite3.Connection, profile_id: str | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    pwhere, pparams = _profile_where(profile_id, "t")
    total = conn.execute(f"SELECT COUNT(*) FROM transactions t WHERE 1=1{pwhere}", pparams).fetchone()[0]
    ewhere, eparams = _profile_where(profile_id, "e")
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS persisted,
               SUM(CASE WHEN user_reviewed = 1 THEN 1 ELSE 0 END) AS reviewed,
               SUM(CASE WHEN TRIM(COALESCE(canonical_counterparty, '')) != '' THEN 1 ELSE 0 END) AS canonical_counterparty,
               SUM(CASE WHEN TRIM(COALESCE(display_counterparty, '')) != '' THEN 1 ELSE 0 END) AS display_counterparty,
               SUM(CASE WHEN TRIM(COALESCE(top_level_category, '')) != '' THEN 1 ELSE 0 END) AS top_level_category,
               SUM(CASE WHEN TRIM(COALESCE(leaf_category, '')) != '' THEN 1 ELSE 0 END) AS leaf_category,
               SUM(CASE WHEN TRIM(COALESCE(purpose_category, '')) != '' THEN 1 ELSE 0 END) AS purpose_category,
               SUM(CASE WHEN TRIM(COALESCE(essentiality, 'unknown')) != 'unknown' THEN 1 ELSE 0 END) AS essentiality_known,
               SUM(CASE WHEN TRIM(COALESCE(recurrence, 'unknown')) != 'unknown' THEN 1 ELSE 0 END) AS recurrence_known,
               SUM(CASE WHEN TRIM(COALESCE(confidence_json, '{{}}')) NOT IN ('', '{{}}') THEN 1 ELSE 0 END) AS confidence_json,
               SUM(CASE WHEN TRIM(COALESCE(evidence_json, '{{}}')) NOT IN ('', '{{}}') THEN 1 ELSE 0 END) AS evidence_json
          FROM transaction_enrichment e
         WHERE 1=1{ewhere}
        """,
        eparams,
    ).fetchone()
    persisted = int(row["persisted"] or 0)
    reviewed = int(row["reviewed"] or 0)
    quality_rows = conn.execute(
        f"""
        SELECT canonical_counterparty, display_counterparty, top_level_category,
               leaf_category, purpose_category, essentiality, recurrence,
               semantic_type, confidence_json, user_reviewed
          FROM transaction_enrichment e
         WHERE 1=1{ewhere}
        """,
        eparams,
    ).fetchall()
    low_count = 0
    for item in quality_rows:
        conf = _json_dict(item["confidence_json"])
        if conf and min(conf.values()) < LOW_CONFIDENCE_DEFAULT:
            low_count += 1
    top_rows = conn.execute(
        f"""
        SELECT top_level_category, COUNT(*) AS count
          FROM transaction_enrichment e
         WHERE 1=1{ewhere}
         GROUP BY top_level_category
         ORDER BY count DESC, top_level_category
        """,
        eparams,
    ).fetchall()
    sem_rows = conn.execute(
        f"""
        SELECT semantic_type, COUNT(*) AS count
          FROM transaction_enrichment e
         WHERE 1=1{ewhere}
         GROUP BY semantic_type
         ORDER BY count DESC, semantic_type
        """,
        eparams,
    ).fetchall()
    essentiality_rows = conn.execute(
        f"""
        SELECT essentiality, COUNT(*) AS count
          FROM transaction_enrichment e
         WHERE 1=1{ewhere}
         GROUP BY essentiality
         ORDER BY count DESC, essentiality
        """,
        eparams,
    ).fetchall()
    recurrence_rows = conn.execute(
        f"""
        SELECT recurrence, COUNT(*) AS count
          FROM transaction_enrichment e
         WHERE 1=1{ewhere}
         GROUP BY recurrence
         ORDER BY count DESC, recurrence
        """,
        eparams,
    ).fetchall()

    def _field_metric(key: str) -> dict[str, Any]:
        count = int(row[key] or 0)
        return {"count": count, "ratio": round(count / persisted, 4) if persisted else 0.0}

    confidence_families = _confidence_family_summary(quality_rows, threshold=LOW_CONFIDENCE_DEFAULT)
    quality_modes = _quality_error_modes(
        transaction_count=int(total or 0),
        persisted_count=persisted,
        reviewed_count=reviewed,
        quality_rows=quality_rows,
        confidence_families=confidence_families,
    )

    return {
        "profile_id": profile_id or "household",
        "transaction_count": int(total or 0),
        "persisted_enrichment_count": persisted,
        "coverage_ratio": round(persisted / total, 4) if total else 0.0,
        "user_reviewed_count": reviewed,
        "populated_fields": {
            "canonical_counterparty": _field_metric("canonical_counterparty"),
            "display_counterparty": _field_metric("display_counterparty"),
            "top_level_category": _field_metric("top_level_category"),
            "leaf_category": _field_metric("leaf_category"),
            "purpose_category": _field_metric("purpose_category"),
            "essentiality_known": _field_metric("essentiality_known"),
            "recurrence_known": _field_metric("recurrence_known"),
            "confidence_json": _field_metric("confidence_json"),
            "evidence_json": _field_metric("evidence_json"),
        },
        "low_confidence_count": low_count,
        "actionable_low_confidence_count": int(quality_modes.get("actionable_low_confidence", {}).get("count") or 0),
        "low_confidence_threshold": LOW_CONFIDENCE_DEFAULT,
        "confidence_families": confidence_families,
        "quality_error_modes": quality_modes,
        "taxonomy_version": TAXONOMY_VERSION,
        "model_version": RULE_VERSION,
        "top_level_distribution": [dict(row) for row in top_rows],
        "semantic_type_distribution": [dict(row) for row in sem_rows],
        "essentiality_distribution": [dict(row) for row in essentiality_rows],
        "recurrence_distribution": [dict(row) for row in recurrence_rows],
        "provenance": {
            "tool": "get_enrichment_quality_summary",
            "profile_id": profile_id or "household",
            "source": "transaction_enrichment",
        },
    }


def preview_enrichment_repairs(
    conn: sqlite3.Connection,
    profile_id: str | None = None,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    ensure_schema(conn)
    limit = max(1, min(int(limit or 50), 500))
    rows = _repair_candidate_transactions(conn, profile_id, limit=max(limit * 4, 100))
    candidates: list[dict[str, Any]] = []
    skipped_user_reviewed = 0
    for tx in rows:
        stored = get_stored_enrichment(conn, tx["id"], tx["profile_id"])
        if stored and stored.get("user_reviewed"):
            skipped_user_reviewed += 1
            continue
        fresh = enrich_transaction_dict(tx, conn, apply_user_corrections=False)
        changes = _repair_changes(stored, fresh)
        if not changes:
            continue
        candidates.append(
            {
                "transaction_id": tx.get("id"),
                "profile_id": tx.get("profile_id"),
                "date": tx.get("date"),
                "amount": tx.get("amount"),
                "category": tx.get("category"),
                "display_counterparty": fresh.get("display_counterparty"),
                "changes": changes,
                "reason": _repair_reason(changes, stored),
            }
        )
        if len(candidates) >= limit:
            break
    return {
        "enabled": enrichment_repair_enabled(),
        "status": "ok",
        "candidate_count": len(candidates),
        "candidates": candidates,
        "skipped_user_reviewed": skipped_user_reviewed,
        "truncated": len(candidates) >= limit,
        "model_version": RULE_VERSION,
        "provenance": {
            "profile_id": profile_id or "household",
            "limit": limit,
            "repair_version": RULE_VERSION,
        },
    }


def apply_enrichment_repairs(
    conn: sqlite3.Connection,
    profile_id: str | None = None,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    if not enrichment_repair_enabled():
        return {
            "status": "disabled",
            "enabled": False,
            "applied_count": 0,
            "candidates": [],
            "caveats": ["Set MIRA_ENRICHMENT_REPAIR_ENABLED=1 to apply repair rows."],
        }
    preview = preview_enrichment_repairs(conn, profile_id, limit=limit)
    applied: list[dict[str, Any]] = []
    for candidate in preview.get("candidates") or []:
        tx = _load_transaction(conn, str(candidate.get("transaction_id") or ""), str(candidate.get("profile_id") or ""))
        if not tx:
            continue
        stored = get_stored_enrichment(conn, tx["id"], tx["profile_id"])
        if stored and stored.get("user_reviewed"):
            continue
        upsert_enrichment(conn, enrich_transaction_dict(tx, conn, apply_user_corrections=False))
        applied.append(candidate)
    return {
        **preview,
        "status": "applied",
        "enabled": True,
        "applied_count": len(applied),
        "applied": applied,
    }


def _confidence_family_summary(rows: list[sqlite3.Row], *, threshold: float) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    total = len(rows)
    for family, fields in CONFIDENCE_FAMILIES.items():
        family_values: list[float] = []
        row_values: list[float] = []
        missing_rows = 0
        for row in rows:
            conf = _json_dict(row["confidence_json"])
            values = [_confidence_value(conf.get(field)) for field in fields if _confidence_value(conf.get(field)) is not None]
            if not values:
                missing_rows += 1
                continue
            row_min = min(values)
            row_values.append(row_min)
            family_values.extend(values)
        low_rows = sum(1 for value in row_values if value < threshold)
        summary[family] = {
            "fields": list(fields),
            "rows_with_confidence": len(row_values),
            "missing_rows": missing_rows,
            "low_confidence_rows": low_rows,
            "low_confidence_ratio": round(low_rows / total, 4) if total else 0.0,
            "min_confidence": round(min(family_values), 4) if family_values else None,
            "avg_confidence": round(sum(family_values) / len(family_values), 4) if family_values else None,
        }
    return summary


def _quality_error_modes(
    *,
    transaction_count: int,
    persisted_count: int,
    reviewed_count: int,
    quality_rows: list[sqlite3.Row],
    confidence_families: dict[str, Any],
) -> dict[str, Any]:
    missing_enrichment = max(0, transaction_count - persisted_count)
    unknown_essentiality = 0
    weak_counterparty = 0
    weak_taxonomy = 0
    weak_semantic_type = 0
    recurrence_only_low = 0
    actionable_low = 0
    for row in quality_rows:
        conf = _json_dict(row["confidence_json"])
        family_lows = _low_confidence_families(conf, threshold=LOW_CONFIDENCE_DEFAULT)
        essentiality = str(row["essentiality"] or "").strip().lower()
        top = str(row["top_level_category"] or "").strip().lower()
        leaf = str(row["leaf_category"] or "").strip().lower()
        if essentiality in {"", "unknown"}:
            unknown_essentiality += 1
        if "counterparty" in family_lows:
            weak_counterparty += 1
        if "taxonomy" in family_lows or top in {"", "other"} or leaf in {"", "other", "uncategorized"}:
            weak_taxonomy += 1
        if "semantic_type" in family_lows:
            weak_semantic_type += 1
        if family_lows == {"recurrence"}:
            recurrence_only_low += 1
        if family_lows - {"recurrence"} or essentiality in {"", "unknown"} or top in {"", "other"}:
            actionable_low += 1

    return {
        "missing_enrichment": _quality_mode(
            missing_enrichment,
            "Transactions without persisted enrichment rows.",
            repairable=missing_enrichment > 0,
        ),
        "unknown_essentiality": _quality_mode(
            unknown_essentiality,
            "Rows where essential vs discretionary status is still unknown.",
            repairable=unknown_essentiality > 0,
        ),
        "weak_counterparty_confidence": _quality_mode(
            weak_counterparty,
            "Rows where merchant/counterparty identity is lower confidence.",
            repairable=False,
        ),
        "weak_taxonomy_confidence": _quality_mode(
            weak_taxonomy,
            "Rows with weak or overly generic category taxonomy.",
            repairable=weak_taxonomy > 0,
        ),
        "weak_semantic_type_confidence": _quality_mode(
            weak_semantic_type,
            "Rows where spending/refund/transfer/payment semantics are lower confidence.",
            repairable=False,
        ),
        "recurrence_only_low_confidence": _quality_mode(
            recurrence_only_low,
            "Rows whose only weak signal is recurrence; this should not be treated as wrong category data.",
            repairable=False,
        ),
        "actionable_low_confidence": _quality_mode(
            actionable_low,
            "Rows with non-recurrence quality issues that may affect advisor interpretation.",
            repairable=actionable_low > 0,
        ),
        "user_reviewed_rows": _quality_mode(
            reviewed_count,
            "Rows protected by explicit user correction/review.",
            repairable=False,
        ),
        "family_summary": confidence_families,
    }


def _quality_mode(count: int, description: str, *, repairable: bool) -> dict[str, Any]:
    return {"count": int(count or 0), "description": description, "repairable": bool(repairable)}


def _low_confidence_families(confidence: dict[str, Any], *, threshold: float) -> set[str]:
    lows: set[str] = set()
    for family, fields in CONFIDENCE_FAMILIES.items():
        values = [_confidence_value(confidence.get(field)) for field in fields if _confidence_value(confidence.get(field)) is not None]
        if values and min(values) < threshold:
            lows.add(family)
    return lows


def _actionable_quality_flags(enrichment: dict[str, Any], weak_families: list[str]) -> list[str]:
    flags: list[str] = []
    essentiality = str(enrichment.get("essentiality") or "").strip().lower()
    top = str(enrichment.get("top_level_category") or "").strip().lower()
    leaf = str(enrichment.get("leaf_category") or "").strip().lower()
    if essentiality in {"", "unknown"}:
        flags.append("unknown_essentiality")
    if top in {"", "other"} or leaf in {"", "other", "uncategorized"}:
        flags.append("generic_taxonomy")
    for family in weak_families:
        if family == "recurrence":
            continue
        flags.append(f"weak_{family}_confidence")
    return sorted(set(flags))


def _repair_candidate_transactions(conn: sqlite3.Connection, profile_id: str | None, *, limit: int) -> list[dict[str, Any]]:
    pwhere, params = _profile_where(profile_id, "t")
    source = "transactions_visible" if _relation_exists(conn, "transactions_visible") else "transactions"
    rows = conn.execute(
        f"""
        SELECT t.*
          FROM {source} t
          LEFT JOIN transaction_enrichment e
            ON e.transaction_id = t.id AND e.profile_id = t.profile_id
         WHERE 1=1{pwhere}
           AND COALESCE(e.user_reviewed, 0) = 0
         ORDER BY CASE WHEN e.transaction_id IS NULL THEN 0 ELSE 1 END,
                  t.date DESC, t.id DESC
         LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def _repair_changes(stored: dict[str, Any] | None, fresh: dict[str, Any]) -> list[dict[str, Any]]:
    if not stored:
        return [
            {
                "field": field,
                "old": "",
                "new": str(fresh.get(field) or ""),
                "confidence": (fresh.get("confidence") or {}).get(field),
            }
            for field in CORRECTABLE_FIELDS
            if str(fresh.get(field) or "")
        ]
    changes: list[dict[str, Any]] = []
    for field in CORRECTABLE_FIELDS:
        old = str(stored.get(field) or "")
        new = str(fresh.get(field) or "")
        if old == new:
            continue
        changes.append(
            {
                "field": field,
                "old": old,
                "new": new,
                "confidence": (fresh.get("confidence") or {}).get(field),
            }
        )
    return changes


def _repair_reason(changes: list[dict[str, Any]], stored: dict[str, Any] | None) -> str:
    if not stored:
        return "missing_enrichment"
    fields = {str(change.get("field") or "") for change in changes}
    if fields & {"top_level_category", "leaf_category", "purpose_category", "essentiality"}:
        return "taxonomy_rule_update"
    if fields & {"semantic_type"}:
        return "semantic_type_rule_update"
    if fields & {"canonical_counterparty", "display_counterparty"}:
        return "counterparty_rule_update"
    return "deterministic_rule_update"


def _relation_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
            (name,),
        ).fetchone()
    except Exception:
        return False
    return bool(row)


def _confidence_value(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 1:
        return None
    return number


def classify_ambiguous_with_local_model(tx: dict[str, Any]) -> None:
    """Reserved Layer 1 hook; current production slice stays deterministic."""
    return None


def _counterparty(
    tx: dict[str, Any],
    conn: sqlite3.Connection | None,
    profile_id: str,
    confidence: dict[str, float],
    evidence: dict[str, Any],
) -> dict[str, str]:
    kind = str(tx.get("merchant_kind") or "").strip()
    merchant_key = str(tx.get("merchant_key") or "").strip()
    merchant_name = str(tx.get("merchant_name") or "").strip()
    counterparty = str(tx.get("counterparty_name") or "").strip()
    description = str(tx.get("description") or tx.get("raw_description") or "").strip()

    display = merchant_name or counterparty or description
    canonical = merchant_key or canonicalize_merchant_key(display) or display.upper()
    source = "merchant_identity" if merchant_key or merchant_name else "description"
    conf = _merchant_confidence(tx.get("merchant_confidence"), source)
    if kind in NON_MERCHANT_KINDS and not merchant_name and _kind_matches_transaction(kind, tx):
        canonical = kind
        display = counterparty or _title_from_description(description) or kind.replace("_", " ").title()
        conf = 0.86
        source = "non_merchant_kind"

    alias = _merchant_alias(conn, canonical, profile_id) if conn is not None and canonical else ""
    if alias:
        display = alias
        evidence["layers"].append("merchant_alias")

    confidence["canonical_counterparty"] = conf
    confidence["display_counterparty"] = conf if display else 0.35
    evidence["counterparty_source"] = source
    if merchant_key:
        evidence["merchant_key"] = merchant_key
    return {"canonical_counterparty": canonical, "display_counterparty": display}


def _kind_matches_transaction(kind: str, tx: dict[str, Any]) -> bool:
    category = str(tx.get("category") or "").strip().lower()
    try:
        amount = float(tx.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if kind == "income":
        return amount > 0 or category == "income"
    if kind == "bank_fee":
        return amount < 0 and category in {"fees", "fees & charges", "bank fees"}
    if kind == "tax":
        return category == "taxes"
    if kind == "credit_card_payment":
        return category == "credit card payment"
    if kind == "personal_transfer":
        return category in {"personal transfer", "savings transfer", "cash withdrawal", "cash deposit", "investment transfer"}
    return False


def _category(tx: dict[str, Any], confidence: dict[str, float], evidence: dict[str, Any]) -> dict[str, str]:
    raw_category = str(tx.get("category") or "").strip()
    category_source = "existing_category"
    category_conf = 0.82 if raw_category and raw_category.lower() not in {"other", "uncategorized"} else 0.45

    if not raw_category:
        provider = str(tx.get("teller_category") or "").strip().lower()
        raw_category = PROVIDER_CATEGORY_MAP.get(provider, "")
        if raw_category:
            category_source = "provider_category"
            category_conf = 0.66

    if not raw_category:
        industry = str(tx.get("merchant_industry") or "").strip().lower()
        raw_category = INDUSTRY_CATEGORY_MAP.get(industry, "")
        if raw_category:
            category_source = "merchant_industry"
            category_conf = 0.62

    mapped = _map_category(raw_category)
    if mapped.get("custom"):
        category_conf = min(category_conf, 0.58)
        category_source = "custom_category_alias"

    confidence["top_level_category"] = category_conf
    confidence["leaf_category"] = category_conf
    confidence["purpose_category"] = max(0.5, category_conf - 0.05)
    confidence["essentiality"] = _essentiality_confidence(mapped["essentiality"], category_source)
    evidence["category_source"] = category_source
    evidence["original_category"] = raw_category
    evidence["layers"].append(category_source)
    return mapped


def _map_category(category: str) -> dict[str, Any]:
    key = str(category or "").strip().lower()
    if key in FOLIO_CATEGORY_MAP:
        top, leaf, purpose, essentiality = FOLIO_CATEGORY_MAP[key]
        return {
            "top_level_category": top,
            "leaf_category": leaf,
            "purpose_category": purpose,
            "essentiality": essentiality,
            "custom": False,
        }
    if not key:
        return {
            "top_level_category": "Other",
            "leaf_category": "Uncategorized",
            "purpose_category": "Needs review",
            "essentiality": "unknown",
            "custom": True,
        }
    inferred = _infer_custom_top_level(key)
    return {
        "top_level_category": inferred,
        "leaf_category": category.strip(),
        "purpose_category": category.strip(),
        "essentiality": _essentiality_for_top(inferred),
        "custom": True,
    }


def _semantic_type(tx: dict[str, Any], category: dict[str, str]) -> tuple[str, float]:
    cat = str(tx.get("category") or category.get("leaf_category") or "").strip().lower()
    if cat in SEMANTIC_NON_EXPENSE_CATEGORIES:
        return SEMANTIC_NON_EXPENSE_CATEGORIES[cat], 0.96
    expense_type = str(tx.get("expense_type") or "").strip().lower()
    if expense_type.startswith("transfer"):
        return "transfer", 0.95
    try:
        amount = float(tx.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    merchant_kind = str(tx.get("merchant_kind") or "").strip().lower()
    description = f"{tx.get('description') or ''} {tx.get('raw_description') or ''}".lower()
    tax_refund_text = (
        "tax ref" in description
        or "tax refund" in description
        or "treas tax ref" in description
        or ("irs treas" in description and "ref" in description)
    )
    if amount > 0 and cat in {"credits & refunds", "refunds"}:
        return "refund", 0.9
    if amount > 0 and (merchant_kind == "tax" or cat == "taxes") and tax_refund_text:
        return "refund", 0.9
    if merchant_kind in {"personal_transfer", "credit_card_payment", "income", "tax", "bank_fee"}:
        mapped = {"personal_transfer": "transfer", "credit_card_payment": "payment", "income": "income", "tax": "spending", "bank_fee": "fee"}
        return mapped[merchant_kind], 0.9
    if amount > 0 and cat not in {"income"}:
        return "refund", 0.62
    return "spending", 0.82 if cat else 0.55


def _recurrence(
    tx: dict[str, Any],
    conn: sqlite3.Connection | None,
    profile_id: str,
    canonical_counterparty: str,
) -> tuple[str, float, dict[str, Any]]:
    key = canonical_counterparty or str(tx.get("merchant_key") or "")
    if conn is not None and key:
        try:
            row = conn.execute(
                """
                SELECT display_name, state, confidence_score, confidence_label, frequency
                  FROM recurring_obligations
                 WHERE profile_id = ?
                   AND merchant_key = ?
                   AND state IN ('active', 'confirmed', 'candidate')
                 ORDER BY CASE state WHEN 'confirmed' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
                          confidence_score DESC
                 LIMIT 1
                """,
                (profile_id, key),
            ).fetchone()
            if row:
                score = max(0.0, min(float(row["confidence_score"] or 0) / 100.0, 1.0))
                state = str(row["state"] or "")
                recurrence = "recurring" if state in {"active", "confirmed"} else "likely_recurring"
                return recurrence, max(score, 0.75), {
                    "display_name": row["display_name"],
                    "state": state,
                    "confidence_label": row["confidence_label"],
                    "frequency": row["frequency"],
                }
        except Exception:
            pass
    category = str(tx.get("category") or "").strip().lower()
    if category == "subscriptions":
        return "likely_recurring", 0.76, {"category": "Subscriptions"}
    return "one_off", 0.68, {}


def _apply_latest_corrections(conn: sqlite3.Connection, enrichment: dict[str, Any]) -> None:
    rows = _correction_rows(conn, enrichment["transaction_id"], enrichment["profile_id"])
    if not rows:
        return
    for row in rows:
        field = row.get("corrected_field")
        if field in CORRECTABLE_FIELDS:
            enrichment[field] = row.get("new_value") or ""
    enrichment["user_reviewed"] = 1
    evidence = enrichment.get("evidence")
    if isinstance(evidence, dict):
        evidence["user_corrections"] = rows
    enrichment["evidence_summary"] = (enrichment.get("evidence_summary") or "") + " User-reviewed correction applied."


def _correction_rows(conn: sqlite3.Connection, transaction_id: str, profile_id: str | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT corrected_field, old_value, new_value, source, created_at
          FROM transaction_enrichment_corrections
         WHERE transaction_id = ? AND profile_id = ?
         ORDER BY created_at DESC, id DESC
        """,
        (transaction_id, profile_id),
    ).fetchall()
    latest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        field = str(row["corrected_field"] or "")
        if field in seen:
            continue
        seen.add(field)
        latest.append(dict(row))
    return latest


def _corrected_fields(conn: sqlite3.Connection, transaction_id: str, profile_id: str | None) -> set[str]:
    if not transaction_id:
        return set()
    return {
        str(row.get("corrected_field") or "")
        for row in _correction_rows(conn, transaction_id, profile_id)
        if row.get("corrected_field") in CORRECTABLE_FIELDS
    }


def _load_transaction(conn: sqlite3.Connection, transaction_id: str, profile_id: str | None = None) -> dict[str, Any] | None:
    params: list[Any] = [transaction_id]
    where = "id = ?"
    if profile_id and profile_id != "household":
        where += " AND profile_id = ?"
        params.append(profile_id)
    row = conn.execute(f"SELECT * FROM transactions WHERE {where} LIMIT 1", params).fetchone()
    return dict(row) if row else None


def _candidate_transactions(conn: sqlite3.Connection, profile_id: str | None, limit: int) -> list[dict[str, Any]]:
    pwhere, params = _profile_where(profile_id, "t")
    rows = conn.execute(
        f"""
        SELECT t.*
          FROM transactions t
          LEFT JOIN transaction_enrichment e
            ON e.transaction_id = t.id AND e.profile_id = t.profile_id
         WHERE 1=1{pwhere}
         ORDER BY CASE WHEN e.transaction_id IS NULL THEN 0 ELSE 1 END,
                  t.date DESC, t.id DESC
         LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(row) for row in rows]


def _row_to_enrichment(row: sqlite3.Row, *, persisted: bool) -> dict[str, Any]:
    data = dict(row)
    data["confidence"] = _json_dict(data.pop("confidence_json", "{}"))
    data["evidence"] = _json_dict(data.pop("evidence_json", "{}"))
    data["user_reviewed"] = bool(data.get("user_reviewed"))
    data["persisted"] = persisted
    return data


def _compact_enrichment(enrichment: dict[str, Any]) -> dict[str, Any]:
    return {
        key: enrichment.get(key)
        for key in (
            "canonical_counterparty",
            "display_counterparty",
            "top_level_category",
            "leaf_category",
            "purpose_category",
            "essentiality",
            "recurrence",
            "semantic_type",
            "user_reviewed",
        )
    } | {"confidence": enrichment.get("confidence", {})}


def _public_transaction(tx: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": tx.get("id"),
        "date": tx.get("date"),
        "description": tx.get("description"),
        "amount": tx.get("amount"),
        "category": tx.get("category"),
        "merchant_name": tx.get("merchant_name"),
        "merchant_key": tx.get("merchant_key"),
        "merchant_kind": tx.get("merchant_kind"),
        "is_excluded": bool(tx.get("is_excluded")),
    }


def _merchant_alias(conn: sqlite3.Connection | None, merchant_key: str, profile_id: str) -> str:
    if conn is None or not merchant_key:
        return ""
    try:
        row = conn.execute(
            "SELECT display_name FROM merchant_aliases WHERE merchant_key = ? AND profile_id = ?",
            (merchant_key, profile_id),
        ).fetchone()
    except Exception:
        return ""
    return str(row["display_name"] or "") if row else ""


def _merchant_confidence(raw: Any, source: str) -> float:
    value = str(raw or "").strip().lower()
    if value == "high":
        return 0.9
    if value == "medium":
        return 0.72
    if value == "low":
        return 0.45
    return 0.78 if source == "merchant_identity" else 0.48


def _essentiality_confidence(essentiality: str, source: str) -> float:
    if essentiality == "unknown":
        return 0.45
    return 0.84 if source in {"existing_category", "custom_category_alias"} else 0.68


def _infer_custom_top_level(key: str) -> str:
    keyword_map = (
        (("rent", "mortgage", "home"), "Housing"),
        (("electric", "water", "internet", "phone"), "Utilities"),
        (("grocery", "market", "costco"), "Groceries"),
        (("restaurant", "coffee", "bar", "dining", "food"), "Dining"),
        (("gas", "uber", "lyft", "parking", "transit"), "Transportation"),
        (("doctor", "pharmacy", "medical", "health"), "Healthcare"),
        (("insurance",), "Insurance"),
        (("subscription", "streaming", "software"), "Subscriptions"),
        (("movie", "music", "game"), "Entertainment"),
        (("flight", "hotel", "travel"), "Travel"),
        (("tax", "irs"), "Taxes"),
        (("fee", "bank"), "Fees & Financial"),
        (("salary", "payroll", "income"), "Income"),
        (("transfer", "zelle", "venmo"), "Transfers"),
    )
    for needles, top in keyword_map:
        if any(needle in key for needle in needles):
            return top
    return "Other"


def _essentiality_for_top(top: str) -> str:
    if top in {"Income", "Transfers", "Debt & Payments"}:
        return "non_expense"
    if top in {"Housing", "Utilities", "Groceries", "Transportation", "Healthcare", "Insurance", "Taxes", "Fees & Financial"}:
        return "essential"
    if top in {"Dining", "Entertainment", "Shopping", "Travel", "Personal Care"}:
        return "discretionary"
    return "unknown"


def _evidence_summary(
    tx: dict[str, Any],
    counterparty: dict[str, str],
    category: dict[str, Any],
    semantic_type: str,
    recurrence: str,
    evidence: dict[str, Any],
) -> str:
    parts = [
        f"counterparty={counterparty.get('display_counterparty') or 'unknown'}",
        f"category={category.get('top_level_category')}/{category.get('leaf_category')}",
        f"semantic_type={semantic_type}",
        f"recurrence={recurrence}",
    ]
    if tx.get("category"):
        parts.append(f"existing_category={tx.get('category')}")
    if tx.get("teller_category"):
        parts.append(f"provider_category={tx.get('teller_category')}")
    layers = ", ".join(evidence.get("layers") or [])
    if layers:
        parts.append(f"layers={layers}")
    return "; ".join(parts)


def _title_from_description(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value or "")
    return " ".join(words[:5]).title()


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _round_confidence(confidence: dict[str, float]) -> dict[str, float]:
    return {key: round(max(0.0, min(float(value), 1.0)), 3) for key, value in confidence.items()}


def _profile_where(profile_id: str | None, alias: str) -> tuple[str, list[Any]]:
    if profile_id and profile_id != "household":
        return f" AND {alias}.profile_id = ?", [profile_id]
    return "", []
