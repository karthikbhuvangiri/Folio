"""Deterministic advisor fact snapshots for incremental Mira portraits.

This layer is intentionally offline/internal. It does not write to the live DB
and it does not call an LLM. Its job is to make history mutations visible:
new transactions create deltas; recategorized past transactions invalidate the
old portrait buckets that depended on them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from mira.safe_finance_query import NON_SPENDING_CATEGORIES, PRIVATE_DISCRETIONARY_CATEGORIES


ADVISOR_FACT_SNAPSHOT_VERSION = "mira_advisor_fact_snapshot_v1"
ADVISOR_FACT_DELTA_VERSION = "mira_advisor_fact_delta_v1"

PORTRAIT_SECTIONS = {
    "my_read": "The Read",
    "underwrite_month": "The Month I Would Plan Around",
    "noise_verdict": "What I Am Keeping Out Of The Verdict",
    "money_map": "The Money Map",
    "next_moves": "Moves I Would Make First",
    "do_not_overcorrect": "What I Would Leave Alone",
    "caveats": "What Could Change This Read",
}

HISTORICAL_REBUILD_MONTH_THRESHOLD = 3
HISTORICAL_REBUILD_BUCKET_THRESHOLD = 8
MATERIAL_MONTH_DELTA = 250.0


def build_advisor_fact_snapshot(
    conn,
    *,
    profile: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build month/category/merchant facts plus deterministic fingerprints."""

    profile_id = str(profile or "household")
    as_of_date = _parse_date(as_of) or date.today()
    tx_rows = _fetch_transactions(conn, profile_id, as_of_date.isoformat())
    months: dict[str, dict[str, Any]] = {}
    category_months: dict[str, dict[str, Any]] = {}
    merchant_months: dict[str, dict[str, Any]] = {}

    for tx in tx_rows:
        month = str(tx.get("date") or "")[:7]
        if not month:
            continue
        month_item = months.setdefault(month, _empty_month(month, as_of_date))
        _apply_month_tx(month_item, tx)

        if _is_spending_tx(tx):
            category = _clean_category(tx.get("category"))
            merchant = _clean_merchant(tx)
            category_key = _bucket_key(month, category)
            merchant_key = _bucket_key(month, merchant)
            category_item = category_months.setdefault(category_key, _empty_category_month(month, category))
            merchant_item = merchant_months.setdefault(merchant_key, _empty_merchant_month(month, merchant, category))
            _apply_spending_bucket_tx(category_item, tx, merchant=merchant)
            _apply_spending_bucket_tx(merchant_item, tx, category=category)

    for item in months.values():
        _finalize_month(item)
    for item in category_months.values():
        _finalize_spending_bucket(item)
    for item in merchant_months.values():
        _finalize_spending_bucket(item)

    payload = {
        "version": ADVISOR_FACT_SNAPSHOT_VERSION,
        "profile": profile_id,
        "as_of": as_of_date.isoformat(),
        "months": dict(sorted(months.items())),
        "category_months": dict(sorted(category_months.items())),
        "merchant_months": dict(sorted(merchant_months.items())),
    }
    payload["summary"] = {
        "month_count": len(months),
        "category_month_count": len(category_months),
        "merchant_month_count": len(merchant_months),
        "latest_month": max(months) if months else None,
        "complete_month_count": sum(1 for item in months.values() if item.get("is_complete")),
    }
    payload["fingerprint"] = _fingerprint(
        {
            "version": payload["version"],
            "profile": payload["profile"],
            "months": _fingerprints(payload["months"]),
            "category_months": _fingerprints(payload["category_months"]),
            "merchant_months": _fingerprints(payload["merchant_months"]),
        }
    )
    return payload


def diff_advisor_fact_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare two snapshots and classify portrait invalidation."""

    month_changes = _diff_bucket_map(before.get("months") or {}, after.get("months") or {})
    category_changes = _diff_bucket_map(before.get("category_months") or {}, after.get("category_months") or {})
    merchant_changes = _diff_bucket_map(before.get("merchant_months") or {}, after.get("merchant_months") or {})
    as_of = _parse_date(after.get("as_of")) or date.today()
    current_month = as_of.isoformat()[:7]

    touched_months = sorted(
        {
            *_changed_month_values(month_changes),
            *_changed_month_values(category_changes),
            *_changed_month_values(merchant_changes),
        }
    )
    historical_months = [month for month in touched_months if month < current_month]
    current_month_touched = current_month in touched_months
    new_months = [item["key"] for item in month_changes if item["change"] == "added"]

    change_types: list[str] = []
    if not (month_changes or category_changes or merchant_changes):
        change_types.append("no_material_change")
    if new_months:
        change_types.append("new_month_delta")
    if current_month_touched:
        change_types.append("current_month_delta")
    if historical_months and category_changes:
        change_types.append("historical_reclassification")
    if historical_months and merchant_changes:
        change_types.append("merchant_history_changed")

    historical_bucket_count = sum(1 for item in [*category_changes, *merchant_changes] if _change_month(item) in historical_months)
    needs_full_rebuild = (
        len(set(historical_months)) >= HISTORICAL_REBUILD_MONTH_THRESHOLD
        or historical_bucket_count >= HISTORICAL_REBUILD_BUCKET_THRESHOLD
    )
    if needs_full_rebuild:
        change_types.append("broad_history_mutation")
        change_types.append("needs_full_rebuild")

    invalidated_sections = _invalidated_sections(
        month_changes=month_changes,
        category_changes=category_changes,
        merchant_changes=merchant_changes,
        historical_months=historical_months,
        current_month_touched=current_month_touched,
        needs_full_rebuild=needs_full_rebuild,
    )
    return {
        "version": ADVISOR_FACT_SNAPSHOT_VERSION,
        "status": "changed" if "no_material_change" not in change_types else "no_material_change",
        "change_types": change_types,
        "current_month": current_month,
        "touched_months": touched_months,
        "historical_months": historical_months,
        "new_months": new_months,
        "needs_full_rebuild": needs_full_rebuild,
        "invalidated_sections": invalidated_sections,
        "month_changes": month_changes,
        "category_month_changes": category_changes,
        "merchant_month_changes": merchant_changes,
    }


def build_portrait_delta_packet(diff: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw snapshot diff into a deterministic advisor-facing delta."""

    change_types = list(diff.get("change_types") or [])
    category_summary = _category_change_summary(diff.get("category_month_changes") or [])
    merchant_summary = _merchant_change_summary(diff.get("merchant_month_changes") or [])
    if "no_material_change" in change_types:
        headline = "No material portrait change."
        action = "Keep the stored portrait as-is."
    elif diff.get("needs_full_rebuild"):
        headline = "The stored portrait needs a full rebuild because historical facts changed broadly."
        action = "Regenerate the full advisor read before showing or answering from it."
    elif "historical_reclassification" in change_types:
        months = ", ".join(diff.get("historical_months") or [])
        headline = f"Historical categorization changed for {months}; the old portrait buckets are stale."
        action = "Refresh the affected portrait sections before treating the old read as current."
    elif "current_month_delta" in change_types:
        headline = "The current month changed, but the core portrait may still stand."
        action = "Update the current-month, money-map, and next-move sections from the new facts."
    else:
        headline = "Advisor facts changed."
        action = "Review the changed buckets before reusing the stored portrait."
    return {
        "version": ADVISOR_FACT_DELTA_VERSION,
        "status": diff.get("status"),
        "headline": headline,
        "action": action,
        "change_types": change_types,
        "needs_full_rebuild": bool(diff.get("needs_full_rebuild")),
        "touched_months": diff.get("touched_months") or [],
        "historical_months": diff.get("historical_months") or [],
        "invalidated_sections": diff.get("invalidated_sections") or [],
        "category_change_summary": category_summary,
        "merchant_change_summary": merchant_summary,
    }


def store_advisor_fact_snapshot(
    conn,
    *,
    profile: str | None = None,
    snapshot: dict[str, Any] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Store a fact snapshot in an internal table and return the stored row."""

    _ensure_snapshot_table(conn)
    profile_id = str(profile or (snapshot or {}).get("profile") or "household")
    payload = snapshot or build_advisor_fact_snapshot(conn, profile=profile_id, as_of=as_of)
    fingerprint = str(payload.get("fingerprint") or _fingerprint(payload))
    generated_at = _now()
    conn.execute(
        """
        INSERT INTO mira_advisor_fact_snapshots
            (profile_id, as_of, snapshot_json, fingerprint, generated_at, status, version)
        VALUES (?, ?, ?, ?, ?, 'active', ?)
        ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
            as_of = excluded.as_of,
            snapshot_json = excluded.snapshot_json,
            generated_at = excluded.generated_at,
            status = excluded.status,
            version = excluded.version
        """,
        (
            profile_id,
            payload.get("as_of"),
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            fingerprint,
            generated_at,
            ADVISOR_FACT_SNAPSHOT_VERSION,
        ),
    )
    return {
        "profile": profile_id,
        "as_of": payload.get("as_of"),
        "fingerprint": fingerprint,
        "generated_at": generated_at,
        "snapshot": payload,
    }


def load_latest_advisor_fact_snapshot(conn, *, profile: str | None = None) -> dict[str, Any] | None:
    _ensure_snapshot_table(conn)
    row = conn.execute(
        """
        SELECT profile_id, as_of, snapshot_json, fingerprint, generated_at, version
          FROM mira_advisor_fact_snapshots
         WHERE profile_id = ?
           AND status = 'active'
         ORDER BY generated_at DESC, id DESC
         LIMIT 1
        """,
        (str(profile or "household"),),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        snapshot = json.loads(data.get("snapshot_json") or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    return {
        "profile": data.get("profile_id"),
        "as_of": data.get("as_of"),
        "fingerprint": data.get("fingerprint"),
        "generated_at": data.get("generated_at"),
        "version": data.get("version"),
        "snapshot": snapshot,
    }


def load_advisor_fact_snapshot(
    conn,
    *,
    profile: str | None = None,
    fingerprint: str,
) -> dict[str, Any] | None:
    _ensure_snapshot_table(conn)
    row = conn.execute(
        """
        SELECT profile_id, as_of, snapshot_json, fingerprint, generated_at, version
          FROM mira_advisor_fact_snapshots
         WHERE profile_id = ?
           AND fingerprint = ?
           AND status = 'active'
         LIMIT 1
        """,
        (str(profile or "household"), str(fingerprint or "")),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        snapshot = json.loads(data.get("snapshot_json") or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    return {
        "profile": data.get("profile_id"),
        "as_of": data.get("as_of"),
        "fingerprint": data.get("fingerprint"),
        "generated_at": data.get("generated_at"),
        "version": data.get("version"),
        "snapshot": snapshot,
    }


def store_portrait_delta_packet(
    conn,
    *,
    profile: str | None,
    source_memo_fingerprint: str | None,
    stored_snapshot_fingerprint: str | None,
    current_snapshot_fingerprint: str | None,
    delta_packet: dict[str, Any],
) -> dict[str, Any]:
    _ensure_delta_table(conn)
    profile_id = str(profile or "household")
    packet_fingerprint = _fingerprint(
        {
            "profile": profile_id,
            "source_memo_fingerprint": source_memo_fingerprint,
            "stored_snapshot_fingerprint": stored_snapshot_fingerprint,
            "current_snapshot_fingerprint": current_snapshot_fingerprint,
            "delta_packet": delta_packet,
        }
    )
    existing = _load_portrait_delta_by_fingerprint(conn, profile=profile_id, fingerprint=packet_fingerprint)
    if existing:
        return {
            **existing,
            "inserted": False,
            "duplicate": True,
        }
    generated_at = _now()
    conn.execute(
        """
        INSERT INTO mira_advisor_portrait_deltas (
            profile_id, source_memo_fingerprint, stored_snapshot_fingerprint,
            current_snapshot_fingerprint, delta_json, generated_at, status,
            fingerprint, version
        )
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
        ON CONFLICT(profile_id, fingerprint) DO NOTHING
        """,
        (
            profile_id,
            source_memo_fingerprint,
            stored_snapshot_fingerprint,
            current_snapshot_fingerprint,
            json.dumps(delta_packet, sort_keys=True, separators=(",", ":")),
            generated_at,
            packet_fingerprint,
            ADVISOR_FACT_DELTA_VERSION,
        ),
    )
    return {
        "profile": profile_id,
        "fingerprint": packet_fingerprint,
        "generated_at": generated_at,
        "delta_packet": delta_packet,
        "status": "active",
        "inserted": True,
        "duplicate": False,
    }


def expire_portrait_delta_packets(
    conn,
    *,
    profile: str | None,
    source_memo_fingerprint: str | None = None,
    exclude_source_memo_fingerprint: str | None = None,
) -> int:
    """Mark active portrait deltas inactive after they no longer apply."""

    _ensure_delta_table(conn)
    clauses = ["profile_id = ?", "status = 'active'"]
    params: list[Any] = [str(profile or "household")]
    if source_memo_fingerprint:
        clauses.append("source_memo_fingerprint = ?")
        params.append(str(source_memo_fingerprint))
    if exclude_source_memo_fingerprint:
        clauses.append("(source_memo_fingerprint IS NULL OR source_memo_fingerprint != ?)")
        params.append(str(exclude_source_memo_fingerprint))
    cursor = conn.execute(
        f"""
        UPDATE mira_advisor_portrait_deltas
           SET status = 'superseded'
         WHERE {' AND '.join(clauses)}
        """,
        tuple(params),
    )
    try:
        return int(cursor.rowcount or 0)
    except (TypeError, ValueError):
        return 0


def list_portrait_delta_packets(conn, *, profile: str | None, limit: int = 3) -> list[dict[str, Any]]:
    _ensure_delta_table(conn)
    rows = conn.execute(
        """
        SELECT profile_id, source_memo_fingerprint, stored_snapshot_fingerprint,
               current_snapshot_fingerprint, delta_json, generated_at, status,
               fingerprint, version
          FROM mira_advisor_portrait_deltas
         WHERE profile_id = ?
           AND status = 'active'
         ORDER BY generated_at DESC
         LIMIT ?
        """,
        (str(profile or "household"), max(1, min(int(limit or 3), 20))),
    ).fetchall()
    out = []
    for row in rows:
        out.append(_row_to_portrait_delta(row))
    return out


def _load_portrait_delta_by_fingerprint(conn, *, profile: str | None, fingerprint: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT profile_id, source_memo_fingerprint, stored_snapshot_fingerprint,
               current_snapshot_fingerprint, delta_json, generated_at, status,
               fingerprint, version
          FROM mira_advisor_portrait_deltas
         WHERE profile_id = ?
           AND fingerprint = ?
         LIMIT 1
        """,
        (str(profile or "household"), str(fingerprint or "")),
    ).fetchone()
    return _row_to_portrait_delta(row) if row else None


def _row_to_portrait_delta(row) -> dict[str, Any]:
    data = dict(row)
    try:
        delta_packet = json.loads(data.get("delta_json") or "{}")
    except json.JSONDecodeError:
        delta_packet = {}
    return {**data, "profile": data.get("profile_id"), "delta_packet": delta_packet}


def _ensure_snapshot_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mira_advisor_fact_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            as_of TEXT,
            snapshot_json TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            version TEXT NOT NULL,
            UNIQUE(profile_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mira_advisor_fact_snapshots_profile_time
            ON mira_advisor_fact_snapshots(profile_id, generated_at DESC)
        """
    )


def _ensure_delta_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mira_advisor_portrait_deltas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL,
            source_memo_fingerprint TEXT,
            stored_snapshot_fingerprint TEXT,
            current_snapshot_fingerprint TEXT,
            delta_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            fingerprint TEXT NOT NULL,
            version TEXT NOT NULL,
            UNIQUE(profile_id, fingerprint)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mira_advisor_portrait_deltas_profile_time
            ON mira_advisor_portrait_deltas(profile_id, generated_at DESC)
        """
    )


def _fetch_transactions(conn, profile: str, as_of: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, profile_id, date, amount, category, merchant_name, merchant_key,
               COALESCE(expense_type, '') AS expense_type
          FROM transactions_visible
         WHERE profile_id = ?
           AND date <= ?
         ORDER BY date, id
        """,
        (profile, as_of),
    ).fetchall()
    return [dict(row) for row in rows]


def _empty_month(month: str, as_of: date) -> dict[str, Any]:
    return {
        "month": month,
        "is_complete": _month_end(month) <= as_of,
        "transaction_count": 0,
        "income": 0.0,
        "gross_spending": 0.0,
        "credits_refunds": 0.0,
        "incoming_external_transfers": 0.0,
        "outgoing_external_transfers": 0.0,
        "net_cash_flow": 0.0,
        "contributor_hashes": [],
    }


def _empty_category_month(month: str, category: str) -> dict[str, Any]:
    return {
        "month": month,
        "category": category,
        "spend_role": _spend_role(category),
        "is_private": category in PRIVATE_DISCRETIONARY_CATEGORIES,
        "total": 0.0,
        "count": 0,
        "avg_ticket": 0.0,
        "top_merchants": {},
        "contributor_hashes": [],
    }


def _empty_merchant_month(month: str, merchant: str, category: str) -> dict[str, Any]:
    return {
        "month": month,
        "merchant": merchant,
        "category": category,
        "total": 0.0,
        "count": 0,
        "avg_ticket": 0.0,
        "contributor_hashes": [],
    }


def _apply_month_tx(item: dict[str, Any], tx: dict[str, Any]) -> None:
    amount = _num(tx.get("amount"))
    category = _clean_category(tx.get("category"))
    expense_type = str(tx.get("expense_type") or "")
    item["transaction_count"] += 1
    if category == "Income" and amount > 0:
        item["income"] = _round(_num(item.get("income")) + amount)
    elif amount > 0 and category not in NON_SPENDING_CATEGORIES:
        item["credits_refunds"] = _round(_num(item.get("credits_refunds")) + amount)
    if _is_spending_tx(tx):
        item["gross_spending"] = _round(_num(item.get("gross_spending")) + abs(amount))
    if expense_type == "transfer_external":
        if amount > 0:
            item["incoming_external_transfers"] = _round(_num(item.get("incoming_external_transfers")) + amount)
        elif amount < 0:
            item["outgoing_external_transfers"] = _round(_num(item.get("outgoing_external_transfers")) + abs(amount))
    item["contributor_hashes"].append(_tx_fingerprint(tx))


def _apply_spending_bucket_tx(item: dict[str, Any], tx: dict[str, Any], *, merchant: str | None = None, category: str | None = None) -> None:
    amount = abs(_num(tx.get("amount")))
    item["total"] = _round(_num(item.get("total")) + amount)
    item["count"] = int(item.get("count") or 0) + 1
    if merchant:
        merchants = item.setdefault("top_merchants", {})
        merchants[merchant] = _round(_num(merchants.get(merchant)) + amount)
    if category and not item.get("category"):
        item["category"] = category
    item["contributor_hashes"].append(_tx_fingerprint(tx))


def _finalize_month(item: dict[str, Any]) -> None:
    item["net_cash_flow"] = _round(
        _num(item.get("income"))
        + _num(item.get("credits_refunds"))
        + _num(item.get("incoming_external_transfers"))
        - _num(item.get("gross_spending"))
        - _num(item.get("outgoing_external_transfers"))
    )
    item["fingerprint"] = _fingerprint({k: v for k, v in item.items() if k != "fingerprint"})


def _finalize_spending_bucket(item: dict[str, Any]) -> None:
    count = int(item.get("count") or 0)
    item["avg_ticket"] = _round(_num(item.get("total")) / count) if count else 0.0
    if isinstance(item.get("top_merchants"), dict):
        item["top_merchants"] = [
            {"merchant": merchant, "total": total}
            for merchant, total in sorted(item["top_merchants"].items(), key=lambda pair: _num(pair[1]), reverse=True)[:5]
        ]
    item["fingerprint"] = _fingerprint({k: v for k, v in item.items() if k != "fingerprint"})


def _diff_bucket_map(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            changes.append(_change_row("added", key, None, new))
        elif new is None:
            changes.append(_change_row("removed", key, old, None))
        elif old.get("fingerprint") != new.get("fingerprint"):
            changes.append(_change_row("changed", key, old, new))
    return changes


def _category_change_summary(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_month: dict[str, dict[str, Any]] = {}
    for change in changes:
        month = _change_month(change)
        item = by_month.setdefault(month, {"month": month, "added": [], "removed": [], "changed": []})
        category = change.get("after_category") or change.get("before_category")
        if not category:
            continue
        if change.get("change") == "added":
            item["added"].append(category)
        elif change.get("change") == "removed":
            item["removed"].append(category)
        else:
            item["changed"].append(category)
    return [
        {
            "month": month,
            "added": sorted(set(item["added"])),
            "removed": sorted(set(item["removed"])),
            "changed": sorted(set(item["changed"])),
        }
        for month, item in sorted(by_month.items())
    ]


def _merchant_change_summary(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_month: dict[str, dict[str, Any]] = {}
    for change in changes:
        month = _change_month(change)
        item = by_month.setdefault(month, {"month": month, "added": [], "removed": [], "changed": []})
        merchant = change.get("after_merchant") or change.get("before_merchant")
        if not merchant:
            continue
        if change.get("change") == "added":
            item["added"].append(merchant)
        elif change.get("change") == "removed":
            item["removed"].append(merchant)
        else:
            item["changed"].append(merchant)
    return [
        {
            "month": month,
            "added": sorted(set(item["added"]))[:8],
            "removed": sorted(set(item["removed"]))[:8],
            "changed": sorted(set(item["changed"]))[:8],
        }
        for month, item in sorted(by_month.items())
    ]


def _change_row(change: str, key: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    active = after or before or {}
    out = {
        "change": change,
        "key": key,
        "month": active.get("month") or _key_month(key),
        "before_fingerprint": before.get("fingerprint") if before else None,
        "after_fingerprint": after.get("fingerprint") if after else None,
    }
    for field in ("category", "merchant", "total", "count", "income", "gross_spending", "net_cash_flow"):
        if before and field in before:
            out[f"before_{field}"] = before.get(field)
        if after and field in after:
            out[f"after_{field}"] = after.get(field)
    if before and after:
        for field in ("total", "count", "income", "gross_spending", "net_cash_flow"):
            if field in before or field in after:
                out[f"{field}_delta"] = _round(_num(after.get(field)) - _num(before.get(field)))
    return out


def _invalidated_sections(
    *,
    month_changes: list[dict[str, Any]],
    category_changes: list[dict[str, Any]],
    merchant_changes: list[dict[str, Any]],
    historical_months: list[str],
    current_month_touched: bool,
    needs_full_rebuild: bool,
) -> list[str]:
    if needs_full_rebuild:
        return list(PORTRAIT_SECTIONS.values())
    sections: set[str] = set()
    if month_changes:
        sections.add(PORTRAIT_SECTIONS["underwrite_month"])
    if historical_months or any(_is_material_month_change(item) for item in month_changes):
        sections.update({PORTRAIT_SECTIONS["my_read"], PORTRAIT_SECTIONS["caveats"]})
    if category_changes:
        sections.update({PORTRAIT_SECTIONS["money_map"], PORTRAIT_SECTIONS["next_moves"]})
    if merchant_changes:
        sections.update({PORTRAIT_SECTIONS["money_map"], PORTRAIT_SECTIONS["next_moves"]})
    if historical_months:
        sections.update({PORTRAIT_SECTIONS["noise_verdict"], PORTRAIT_SECTIONS["caveats"]})
    if current_month_touched:
        sections.add(PORTRAIT_SECTIONS["underwrite_month"])
    if any(_is_private_change(item) for item in category_changes):
        sections.add(PORTRAIT_SECTIONS["do_not_overcorrect"])
    return [section for section in PORTRAIT_SECTIONS.values() if section in sections]


def _changed_month_values(changes: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("month") or _key_month(item.get("key"))) for item in changes if item.get("month") or item.get("key")}


def _change_month(change: dict[str, Any]) -> str:
    return str(change.get("month") or _key_month(change.get("key")) or "")


def _is_private_change(change: dict[str, Any]) -> bool:
    return str(change.get("before_category") or change.get("after_category") or "") in PRIVATE_DISCRETIONARY_CATEGORIES


def _is_material_month_change(change: dict[str, Any]) -> bool:
    return any(
        abs(_num(change.get(field))) >= MATERIAL_MONTH_DELTA
        for field in ("income_delta", "gross_spending_delta", "net_cash_flow_delta")
    )


def _is_spending_tx(tx: dict[str, Any]) -> bool:
    return _num(tx.get("amount")) < 0 and _clean_category(tx.get("category")) not in NON_SPENDING_CATEGORIES


def _tx_fingerprint(tx: dict[str, Any]) -> str:
    return _fingerprint(
        {
            "id": tx.get("id"),
            "date": tx.get("date"),
            "amount": _round(tx.get("amount")),
            "category": _clean_category(tx.get("category")),
            "merchant": _clean_merchant(tx),
            "expense_type": str(tx.get("expense_type") or ""),
        }
    )


def _fingerprints(items: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {key: str(value.get("fingerprint") or "") for key, value in sorted(items.items())}


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _bucket_key(month: str, value: str) -> str:
    return f"{month}|{value}"


def _key_month(key: Any) -> str:
    return str(key or "").split("|", 1)[0]


def _clean_category(value: Any) -> str:
    return str(value or "Uncategorized").strip() or "Uncategorized"


def _clean_merchant(tx: dict[str, Any]) -> str:
    return str(tx.get("merchant_name") or tx.get("merchant_key") or tx.get("category") or "Merchant").strip() or "Merchant"


def _spend_role(category: str) -> str:
    lowered = category.lower()
    if category in {"Housing", "Rent", "Mortgage"}:
        return "structural_floor"
    if category in PRIVATE_DISCRETIONARY_CATEGORIES:
        return "private_discretionary"
    if "fee" in lowered or "interest" in lowered:
        return "avoidable_leakage"
    if category in {"Travel", "Entertainment"} or category.startswith("Trip:"):
        return "event_or_irregular"
    if category in {"Insurance", "Subscriptions", "Membership"}:
        return "recurring_or_vendor_review"
    return "flexible_living"


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _month_end(month: str) -> date:
    start = date.fromisoformat(f"{month}-01")
    if start.month == 12:
        return date(start.year, 12, 31)
    return date(start.year, start.month + 1, 1) - timedelta(days=1)


def _round(value: Any, ndigits: int = 2) -> float:
    return round(_num(value), ndigits)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
