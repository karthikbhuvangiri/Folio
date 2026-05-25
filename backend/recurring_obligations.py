"""
recurring_obligations.py
Deterministic obligation model for recurring subscriptions and upcoming bills.
"""

from __future__ import annotations

import calendar
import json
import math
import re
import uuid
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any

from merchant_identity import canonicalize_merchant_key


DETECTOR_VERSION = 2

FREQUENCY_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "semi_annual": 6,
    "semiannual": 6,
    "annual": 12,
    "yearly": 12,
}

FREQUENCY_DAYS = {
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 91,
    "semi_annual": 182,
    "semiannual": 182,
    "annual": 365,
    "yearly": 365,
}

HARD_EXCLUDED_CATEGORIES = {
    "Income",
    "Credits & Refunds",
    "Credit Card Payment",
    "Internal Transfer",
    "Savings Transfer",
    "Personal Transfer",
    "Tax Payment",
    "Tax Refund",
    "Refund",
    "Reversal",
    "Adjustment",
}

SOFT_PENALTY_CATEGORIES = {"Groceries", "Food & Dining", "Dining", "Travel", "Shopping", "Pharmacy"}


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def add_months(base: date, months: int) -> date:
    """Calendar-aware month addition with day clamped to the destination month."""
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def advance_recurring_date(base: date, frequency: str | None) -> date:
    freq = (frequency or "monthly").lower().replace("-", "_")
    if freq in {"weekly", "week"}:
        return base + timedelta(days=7)
    if freq in {"biweekly", "bi_weekly", "fortnightly"}:
        return base + timedelta(days=14)
    return add_months(base, FREQUENCY_MONTHS.get(freq, 1))


def due_from_anchor(
    *,
    last_seen: date | None,
    frequency: str | None,
    anchor_day: int | None,
    anchor_month: int | None = None,
    anchor_mode: str | None = "observed_pattern",
    today: date | None = None,
) -> date | None:
    today = today or date.today()
    freq = (frequency or "monthly").lower().replace("-", "_")
    if last_seen is None and anchor_day is None:
        return None

    if freq in {"annual", "yearly"} and anchor_month:
        month = max(1, min(int(anchor_month), 12))
        day = max(1, min(int(anchor_day or 1), 31))
        year = today.year
        for _ in range(3):
            last_day = calendar.monthrange(year, month)[1]
            candidate = date(year, month, min(day, last_day))
            if candidate >= today:
                return candidate
            year += 1

    if anchor_day:
        day = max(1, min(int(anchor_day), 31))
        seed = today if last_seen is None else max(today, last_seen)
        month_start = seed.replace(day=1)
        for offset in range(0, 24):
            candidate_month = add_months(month_start, offset)
            last_day = calendar.monthrange(candidate_month.year, candidate_month.month)[1]
            if anchor_mode == "end_of_month":
                candidate = candidate_month.replace(day=last_day)
            else:
                candidate = candidate_month.replace(day=min(day, last_day))
            if candidate >= today:
                return candidate

    candidate = last_seen
    if candidate is None:
        return None
    while candidate < today:
        candidate = advance_recurring_date(candidate, freq)
    return candidate


def annualize_amount(amount: float, frequency: str | None) -> float:
    freq = (frequency or "monthly").lower().replace("-", "_")
    multipliers = {"weekly": 52, "biweekly": 26, "monthly": 12, "quarterly": 4, "semi_annual": 2, "annual": 1}
    return float(amount or 0) * multipliers.get(freq, 12)


def cents(amount: float | int | None) -> int:
    return int(round(float(amount or 0) * 100))


def dollars(amount_cents: int | None) -> float:
    return round(float(amount_cents or 0) / 100.0, 2)


def canonical_key(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return (canonicalize_merchant_key(text) or text.upper().strip()).upper().strip()


def merchant_match_keys(value: str | None) -> set[str]:
    text = (value or "").strip()
    if not text:
        return set()
    raw = text.upper().strip()
    canonical = canonical_key(text)
    keys = {raw}
    if canonical:
        keys.add(canonical)
    return keys


def amount_band(amount: float | int | None) -> str:
    """Round to the nearest dollar band to avoid cents-level auto-key churn."""
    value = float(amount or 0)
    return str(int(round(value)))


def hard_excluded_reason(item: dict[str, Any]) -> str | None:
    """Return a deterministic exclusion reason for non-obligation transactions."""
    source = item.get("matched_by") or item.get("source")
    if source == "user" or item.get("confidence") == "user":
        return None
    category = str(item.get("category") or "").strip()
    if category in HARD_EXCLUDED_CATEGORIES:
        return f"hard_excluded_category:{category}"
    expense_type = str(item.get("expense_type") or "").strip().lower()
    if expense_type.startswith("transfer"):
        return f"hard_excluded_expense_type:{expense_type}"
    text = " ".join(
        str(item.get(key) or "")
        for key in ("merchant", "clean_name", "description", "raw_description")
    ).upper()
    if any(token in text for token in ("REFUND", "REVERSAL", "TRANSFER", "PAYMENT THANK YOU")):
        return "hard_excluded_descriptor"
    return None


def _float_list(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except Exception:
            continue
    return out


def _date_list(values: Any) -> list[date]:
    if not isinstance(values, list):
        return []
    parsed = [parse_date(value) for value in values]
    return sorted([value for value in parsed if value is not None])


def _coefficient_of_variation(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    if avg <= 0:
        return None
    variance = sum((value - avg) ** 2 for value in values) / len(values)
    return math.sqrt(variance) / avg


def _median_absolute_deviation(values: list[int]) -> float | None:
    if len(values) < 2:
        return None
    med = median(values)
    return median([abs(value - med) for value in values])


def _frequency_nominal_and_grace(frequency: str | None) -> tuple[int, int, int, int]:
    freq = (frequency or "monthly").lower().replace("-", "_")
    if freq in {"weekly", "week"}:
        return 7, 4, 6, 9
    if freq in {"biweekly", "bi_weekly", "fortnightly"}:
        return 14, 7, 12, 18
    if freq in {"quarterly", "quarter"}:
        return 91, 30, 80, 105
    if freq in {"semi_annual", "semiannual", "semi-annually"}:
        return 182, 45, 160, 210
    if freq in {"annual", "annually", "yearly", "year"}:
        return 365, 45, 340, 400
    return 30, 15, 25, 38


def _percentile_cents(values: list[float], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return cents(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return cents(ordered[lower])
    weight = position - lower
    return cents(ordered[lower] * (1 - weight) + ordered[upper] * weight)


def score_components_for_item(item: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """Deterministic 0-100 recurring confidence components."""
    today = today or date.today()
    source = item.get("matched_by") or item.get("source") or "algorithm"
    if source == "user" or item.get("confidence") == "user":
        return {
            "occurrence_count": 30,
            "interval_consistency": 25,
            "amount_stability": 20,
            "calendar_day_consistency": 10,
            "active_recency": 10,
            "identity_signal": 5,
            "soft_penalty": 0,
            "total": 100,
        }

    occurrences = max(0, int(item.get("occurrences") or item.get("charge_count") or 0))
    dates = _date_list(item.get("dates"))
    if not dates:
        last_seen = parse_date(item.get("last_date") or item.get("last_charge"))
        dates = [last_seen] if last_seen else []
    intervals = _float_list(item.get("intervals"))
    if not intervals and len(dates) >= 2:
        intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]

    segment_amounts = _float_list(item.get("segment_amounts")) or _float_list(item.get("amounts"))
    if not segment_amounts:
        amount = item.get("avg_amount") or item.get("amount")
        segment_amounts = [float(amount)] if amount is not None else []

    frequency = item.get("frequency") or "monthly"
    nominal, grace, low, high = _frequency_nominal_and_grace(frequency)
    occurrence_score = min(30, round(10 * math.log2(occurrences + 1))) if occurrences else 0
    if intervals:
        passed = sum(1 for interval in intervals if low <= interval <= high)
        interval_score = round(25 * passed / len(intervals))
    else:
        interval_score = 0

    cv = _coefficient_of_variation(segment_amounts)
    if cv is None:
        amount_score = 0
    else:
        amount_score = round(20 * max(0.0, 1.0 - cv / 0.20))

    mad = _median_absolute_deviation([d.day for d in dates])
    if mad is None:
        calendar_score = 0
    else:
        calendar_score = round(10 * max(0.0, 1.0 - mad / 4.0))

    recency_score = 0
    last_seen = dates[-1] if dates else parse_date(item.get("last_date") or item.get("last_charge"))
    if last_seen:
        age = max(0, (today - last_seen).days)
        full_until = nominal + grace
        zero_at = nominal + (2 * grace)
        if age <= full_until:
            recency_score = 10
        elif age < zero_at:
            recency_score = round(10 * (zero_at - age) / max(grace, 1))

    if source == "seed":
        identity_score = 5
    elif source == "category":
        identity_score = 3
    else:
        identity_score = 3 if item.get("merchant") or item.get("clean_name") else 1

    category = item.get("category") or ""
    soft_penalty = 15 if category in SOFT_PENALTY_CATEGORIES and source not in {"seed", "user"} else 0
    total = max(0, min(100, occurrence_score + interval_score + amount_score + calendar_score + recency_score + identity_score - soft_penalty))
    return {
        "occurrence_count": occurrence_score,
        "interval_consistency": interval_score,
        "amount_stability": amount_score,
        "calendar_day_consistency": calendar_score,
        "active_recency": recency_score,
        "identity_signal": identity_score,
        "soft_penalty": soft_penalty,
        "total": total,
    }


def obligation_key_for(
    merchant_key: str,
    *,
    source: str | None = None,
    seed_name: str | None = None,
    service_tag: str | None = None,
    amount: float | int | None = None,
    frequency: str | None = None,
) -> str:
    base = canonical_key(merchant_key)
    if source == "user":
        tag = canonical_key(service_tag or merchant_key) or "USER"
        return f"{base}:user:{tag}"
    if seed_name:
        return f"{base}:seed:{canonical_key(seed_name)}"
    return f"{base}:auto:{amount_band(amount)}:{(frequency or 'monthly').lower().replace('-', '_')}"


def confidence_score_for_item(item: dict[str, Any]) -> int:
    return int(score_components_for_item(item).get("total") or 0)


def confidence_label(score: int, source: str | None = None) -> str:
    if source == "user" or score >= 100:
        return "user"
    if score >= 75:
        return "high"
    if score >= 55:
        return "medium"
    if score >= 35:
        return "low"
    return "candidate"


def state_for_item(item: dict[str, Any], score: int) -> str:
    source = item.get("matched_by") or item.get("source")
    if source == "user" or item.get("confidence") == "user":
        return "confirmed"
    if (item.get("status") or "").lower() == "inactive":
        return "inactive"
    if score >= 55:
        return "active"
    if score >= 35:
        return "candidate"
    return "candidate"


def event_period_bucket(event: dict[str, Any]) -> str:
    detail = event.get("detail") or "{}"
    if isinstance(detail, str):
        try:
            detail_obj = json.loads(detail)
        except Exception:
            detail_obj = {}
    else:
        detail_obj = detail if isinstance(detail, dict) else {}
    for key in ("period_bucket", "new_charge_date", "last_charge_date", "latest_date", "date"):
        value = detail_obj.get(key)
        if value:
            return str(value)[:7]
    return datetime.now().strftime("%Y-%m")


def recurring_event_key(event: dict[str, Any], profile_id: str) -> str:
    merchant = event.get("merchant_name") or event.get("merchant") or ""
    return ":".join([
        profile_id,
        canonical_key(merchant),
        str(event.get("event_type") or ""),
        event_period_bucket(event),
    ])


def evidence_from_item(item: dict[str, Any], score: int) -> dict[str, Any]:
    amount = float(item.get("avg_amount") or item.get("amount") or 0)
    segment_amounts = _float_list(item.get("segment_amounts")) or _float_list(item.get("amounts")) or [amount]
    category = item.get("category") or "Subscriptions"
    soft_penalty = category in SOFT_PENALTY_CATEGORIES and item.get("matched_by") not in {"seed", "user"}
    score_breakdown = score_components_for_item(item)
    score_breakdown["total"] = score
    return {
        "detector_version": DETECTOR_VERSION,
        "matched_by": item.get("matched_by") or item.get("source") or "algorithm",
        "occurrences": item.get("occurrences") or 0,
        "months_paid": item.get("months_paid") or 0,
        "score_breakdown": score_breakdown,
        "score_component_max": {
            "occurrence_count": 30,
            "interval_consistency": 25,
            "amount_stability": 20,
            "calendar_day_consistency": 10,
            "active_recency": 10,
            "identity_signal": 5,
        },
        "soft_penalty": "category" if soft_penalty else None,
        "amount_band": amount_band(amount),
        "price_change": item.get("price_change"),
        "matched_transaction_ids": item.get("transaction_ids") or [],
        "dates": item.get("dates") or ([item.get("last_date")] if item.get("last_date") else []),
        "amounts": item.get("amounts") or [],
        "intervals": item.get("intervals") or [],
        "price_segments": item.get("price_segments") or [],
        "seed_pattern": item.get("seed_pattern"),
        "excluded_reasons": item.get("excluded_reasons") or [],
        "scheduling_anchor": {
            "anchor_day": _anchor_day(item),
            "anchor_mode": "observed_pattern",
        },
    }


def _anchor_day(item: dict[str, Any]) -> int | None:
    next_date = parse_date(item.get("next_expected_date") or item.get("next_expected"))
    if next_date:
        return next_date.day
    last_date = parse_date(item.get("last_date") or item.get("last_charge"))
    if last_date:
        return last_date.day
    return None


def _anchor_month(item: dict[str, Any]) -> int | None:
    freq = (item.get("frequency") or "").lower()
    if freq not in {"annual", "yearly"}:
        return None
    last_date = parse_date(item.get("last_date") or item.get("last_charge"))
    return last_date.month if last_date else None


def upsert_obligation_from_item(
    conn,
    item: dict[str, Any],
    *,
    profile_id: str,
    run_id: str | None = None,
) -> str | None:
    merchant = item.get("merchant") or item.get("clean_name") or ""
    merchant_key = canonical_key(merchant)
    if not merchant_key:
        return None
    source = item.get("matched_by") or item.get("source") or "algorithm"
    if hard_excluded_reason(item):
        return None
    seed_name = merchant if source == "seed" else None
    service_tag = merchant if source == "user" else None
    amount = float(item.get("avg_amount") or item.get("amount") or 0)
    segment_amounts = _float_list(item.get("segment_amounts")) or _float_list(item.get("amounts")) or [amount]
    frequency = item.get("frequency") or "monthly"
    key = obligation_key_for(
        merchant_key,
        source="user" if source == "user" else None,
        seed_name=seed_name,
        service_tag=service_tag,
        amount=amount,
        frequency=frequency,
    )
    score = confidence_score_for_item(item)
    state = state_for_item(item, score)
    if int(item.get("occurrences") or 0) == 1 and source == "seed" and state == "active":
        state = "candidate"
        score = min(score, 34)
    evidence = evidence_from_item(item, score)
    anchor_day = _anchor_day(item)
    anchor_month = _anchor_month(item)
    last_seen = parse_date(item.get("last_date") or item.get("last_charge"))
    next_expected = parse_date(item.get("next_expected_date") or item.get("next_expected"))
    if state in {"inactive", "stale", "dismissed", "cancelled"}:
        next_expected = None
    elif next_expected is None:
        next_expected = due_from_anchor(
            last_seen=last_seen,
            frequency=frequency,
            anchor_day=anchor_day,
            anchor_month=anchor_month,
            anchor_mode="observed_pattern",
        )

    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO recurring_obligations
           (profile_id, obligation_key, merchant_key, display_name, service_tag,
            seed_name, category, amount_cents, amount_p10_cents, amount_p90_cents,
            frequency, anchor_day, anchor_month, anchor_mode, next_expected_date,
            state, source, confidence_score, confidence_label, evidence_json,
            first_seen_date, last_seen_date, last_run_id, detector_version, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'observed_pattern', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(profile_id, obligation_key) DO UPDATE SET
               display_name = excluded.display_name,
               category = excluded.category,
               amount_cents = excluded.amount_cents,
               amount_p10_cents = excluded.amount_p10_cents,
               amount_p90_cents = excluded.amount_p90_cents,
               frequency = excluded.frequency,
               anchor_day = excluded.anchor_day,
               anchor_month = excluded.anchor_month,
               anchor_mode = excluded.anchor_mode,
               next_expected_date = CASE
                   WHEN recurring_obligations.state IN ('cancelled', 'dismissed')
                    AND recurring_obligations.last_user_action_at IS NOT NULL
                   THEN NULL
                   ELSE excluded.next_expected_date
               END,
               state = CASE
                   WHEN recurring_obligations.state IN ('confirmed', 'cancelled', 'dismissed')
                    AND recurring_obligations.last_user_action_at IS NOT NULL
                   THEN recurring_obligations.state
                   ELSE excluded.state
               END,
               source = CASE
                   WHEN recurring_obligations.source = 'user' THEN recurring_obligations.source
                   ELSE excluded.source
               END,
               confidence_score = CASE
                   WHEN recurring_obligations.source = 'user' THEN recurring_obligations.confidence_score
                   ELSE excluded.confidence_score
               END,
               confidence_label = CASE
                   WHEN recurring_obligations.source = 'user' THEN recurring_obligations.confidence_label
                   ELSE excluded.confidence_label
               END,
               evidence_json = excluded.evidence_json,
               first_seen_date = COALESCE(recurring_obligations.first_seen_date, excluded.first_seen_date),
               last_seen_date = excluded.last_seen_date,
               last_run_id = excluded.last_run_id,
               detector_version = excluded.detector_version,
               updated_at = excluded.updated_at""",
        (
            profile_id,
            key,
            merchant_key,
            merchant,
            service_tag or "",
            seed_name or "",
            item.get("category") or "Subscriptions",
            cents(amount),
            _percentile_cents(segment_amounts, 0.10),
            _percentile_cents(segment_amounts, 0.90),
            frequency,
            anchor_day,
            anchor_month,
            next_expected.isoformat() if next_expected else None,
            state,
            source,
            score,
            confidence_label(score, source),
            json.dumps(evidence, sort_keys=True),
            item.get("first_seen_date") or item.get("last_date") or item.get("last_charge"),
            item.get("last_date") or item.get("last_charge"),
            run_id,
            DETECTOR_VERSION,
            now,
        ),
    )
    return key


def _orphan_sweep(
    conn,
    *,
    profile_id: str,
    run_id: str,
    seen: set[str],
    today: date | None = None,
) -> dict[str, int]:
    """Apply conservative lifecycle rules for detector-owned rows missed by a run."""
    today = today or date.today()
    params: list[Any] = [profile_id]
    not_seen_clause = ""
    if seen:
        placeholders = ",".join("?" * len(seen))
        not_seen_clause = f"AND obligation_key NOT IN ({placeholders})"
        params.extend(sorted(seen))

    rows = conn.execute(
        f"""SELECT obligation_key, state, frequency, last_seen_date, confidence_score
            FROM recurring_obligations
            WHERE profile_id = ?
              AND source IN ('seed', 'algorithm', 'category')
              AND state IN ('active', 'candidate')
              AND last_user_action_at IS NULL
              {not_seen_clause}""",
        params,
    ).fetchall()

    deleted = transitioned = preserved = 0
    for row in rows:
        obligation_key = row[0]
        state = row[1] or "candidate"
        frequency = row[2] or "monthly"
        score = int(row[4] or 0)
        nominal, _, _, _ = _frequency_nominal_and_grace(frequency)
        last_seen = parse_date(row[3])

        if state == "candidate" and score < 35:
            conn.execute(
                """DELETE FROM recurring_obligations
                   WHERE profile_id = ? AND obligation_key = ?""",
                (profile_id, obligation_key),
            )
            deleted += 1
            continue

        freq_key = frequency.lower().replace("-", "_")
        if freq_key in {"annual", "yearly", "semi_annual", "semiannual"} and last_seen:
            if (today - last_seen).days < (2 * nominal):
                preserved += 1
                continue

        next_state = "stale" if state == "candidate" else "inactive"
        conn.execute(
            """UPDATE recurring_obligations
               SET state = ?,
                   next_expected_date = NULL,
                   last_run_id = ?,
                   updated_at = datetime('now')
               WHERE profile_id = ? AND obligation_key = ?""",
            (next_state, run_id, profile_id, obligation_key),
        )
        transitioned += 1

    return {"deleted": deleted, "transitioned": transitioned, "preserved": preserved}


def sync_detection_results(
    conn,
    items: list[dict[str, Any]],
    *,
    profile_id: str,
    txn_count: int = 0,
    mode: str = "shadow",
) -> str:
    run_id = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT OR REPLACE INTO recurring_detection_runs
           (id, profile_id, mode, detector_version, started_at, txn_count, candidate_count, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'running')""",
        (run_id, profile_id, mode, DETECTOR_VERSION, now, int(txn_count or 0), len(items)),
    )
    seen: set[str] = set()
    for item in items:
        key = upsert_obligation_from_item(conn, item, profile_id=profile_id, run_id=run_id)
        if key:
            seen.add(key)

    sweep = {"deleted": 0, "transitioned": 0, "preserved": 0}
    if mode != "incremental":
        sweep = _orphan_sweep(conn, profile_id=profile_id, run_id=run_id, seen=seen)

    conn.execute(
        """UPDATE recurring_detection_runs
           SET completed_at = datetime('now'),
               status = 'completed',
               candidate_count = ?
           WHERE id = ?""",
        (len(items) + sweep["preserved"], run_id),
    )
    return run_id


def upsert_user_obligation(
    conn,
    *,
    merchant: str,
    amount: float,
    frequency: str,
    profile_id: str,
    category: str = "Subscriptions",
    expected_day: int | None = None,
    service_tag: str | None = None,
) -> str:
    merchant_key = canonical_key(merchant)
    tag = service_tag or merchant
    key = obligation_key_for(merchant_key, source="user", service_tag=tag)
    next_expected = None
    if expected_day:
        next_expected = due_from_anchor(
            last_seen=None,
            frequency=frequency,
            anchor_day=expected_day,
            anchor_mode="exact_day",
        )
    now = datetime.now().isoformat(timespec="seconds")
    evidence = {"source": "user_declared", "service_tag": tag}
    conn.execute(
        """INSERT INTO recurring_obligations
           (profile_id, obligation_key, merchant_key, display_name, service_tag,
            category, amount_cents, frequency, anchor_day, anchor_mode,
            next_expected_date, state, source, confidence_score, confidence_label,
            evidence_json, detector_version, last_user_action_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', 'user', 100, 'user', ?, ?, ?, ?, ?)
           ON CONFLICT(profile_id, obligation_key) DO UPDATE SET
               display_name = excluded.display_name,
               service_tag = excluded.service_tag,
               category = excluded.category,
               amount_cents = excluded.amount_cents,
               frequency = excluded.frequency,
               anchor_day = excluded.anchor_day,
               anchor_mode = excluded.anchor_mode,
               next_expected_date = excluded.next_expected_date,
               state = 'confirmed',
               source = 'user',
               confidence_score = 100,
               confidence_label = 'user',
               evidence_json = excluded.evidence_json,
               last_user_action_at = excluded.last_user_action_at,
               updated_at = excluded.updated_at""",
        (
            profile_id,
            key,
            merchant_key,
            merchant,
            tag,
            category,
            cents(amount),
            frequency,
            expected_day,
            "exact_day" if expected_day else "observed_pattern",
            next_expected.isoformat() if next_expected else None,
            json.dumps(evidence, sort_keys=True),
            DETECTOR_VERSION,
            now,
            now,
            now,
        ),
    )
    supersede_feedback(
        conn,
        profile_id=profile_id,
        merchant_key=merchant_key,
        feedback_types=("dismissed", "cancelled", "amount_review_dismissed"),
    )
    return key


def record_feedback(
    conn,
    *,
    merchant: str,
    profile_id: str,
    feedback_type: str,
    scope: str = "merchant",
    obligation_key: str | None = None,
    payload: dict[str, Any] | None = None,
    expires_at: str | None = None,
) -> str:
    merchant_key = canonical_key(merchant)
    if not obligation_key:
        row = conn.execute(
            """SELECT obligation_key FROM recurring_obligations
               WHERE profile_id = ? AND merchant_key = ?
               ORDER BY CASE state WHEN 'confirmed' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, updated_at DESC
               LIMIT 1""",
            (profile_id, merchant_key),
        ).fetchone()
        obligation_key = row[0] if row else f"{merchant_key}:merchant"
    now = datetime.now().isoformat(timespec="seconds")
    if feedback_type == "confirmed":
        supersede_feedback(
            conn,
            profile_id=profile_id,
            merchant_key=merchant_key,
            feedback_types=("dismissed", "cancelled", "snoozed", "confirmed", "amount_review_dismissed"),
        )
    else:
        supersede_feedback(conn, profile_id=profile_id, merchant_key=merchant_key, feedback_types=(feedback_type,))
    conn.execute(
        """INSERT INTO recurring_feedback
           (profile_id, obligation_key, merchant_key, feedback_type, scope,
            payload_json, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (profile_id, obligation_key, merchant_key, feedback_type, scope, json.dumps(payload or {}, sort_keys=True), expires_at, now),
    )
    if feedback_type == "cancelled":
        conn.execute(
            """UPDATE recurring_obligations
               SET state = 'cancelled', last_user_action_at = ?, updated_at = datetime('now')
               WHERE profile_id = ? AND merchant_key = ?""",
            (now, profile_id, merchant_key),
        )
    elif feedback_type == "dismissed":
        conn.execute(
            """UPDATE recurring_obligations
               SET state = 'dismissed', last_user_action_at = ?, updated_at = datetime('now')
               WHERE profile_id = ? AND merchant_key = ?""",
            (now, profile_id, merchant_key),
        )
    elif feedback_type == "confirmed":
        if obligation_key and not obligation_key.endswith(":merchant"):
            conn.execute(
                """UPDATE recurring_obligations
                   SET state = 'confirmed',
                       source = CASE WHEN source = 'user' THEN source ELSE 'user_confirmed' END,
                       confidence_score = MAX(confidence_score, 100),
                       confidence_label = 'user',
                       last_user_action_at = ?,
                       updated_at = datetime('now')
                   WHERE profile_id = ? AND obligation_key = ?""",
                (now, profile_id, obligation_key),
            )
        else:
            conn.execute(
                """UPDATE recurring_obligations
                   SET state = 'confirmed',
                       source = CASE WHEN source = 'user' THEN source ELSE 'user_confirmed' END,
                       confidence_score = MAX(confidence_score, 100),
                       confidence_label = 'user',
                       last_user_action_at = ?,
                       updated_at = datetime('now')
                   WHERE profile_id = ? AND merchant_key = ?""",
                (now, profile_id, merchant_key),
            )
    return obligation_key


def supersede_feedback(
    conn,
    *,
    profile_id: str,
    merchant_key: str,
    feedback_types: tuple[str, ...] = ("dismissed", "cancelled", "snoozed"),
) -> int:
    placeholders = ",".join("?" * len(feedback_types))
    result = conn.execute(
        f"""UPDATE recurring_feedback
            SET superseded_at = datetime('now')
            WHERE profile_id = ?
              AND merchant_key = ?
              AND superseded_at IS NULL
              AND feedback_type IN ({placeholders})""",
        (profile_id, merchant_key, *feedback_types),
    )
    return result.rowcount


def restore_obligation(conn, *, merchant: str, profile_id: str) -> bool:
    merchant_key = canonical_key(merchant)
    changed = supersede_feedback(
        conn,
        profile_id=profile_id,
        merchant_key=merchant_key,
        feedback_types=("dismissed", "cancelled", "snoozed"),
    )
    result = conn.execute(
        """UPDATE recurring_obligations
           SET state = CASE
                   WHEN source = 'user' THEN 'confirmed'
                   WHEN confidence_score >= 55 THEN 'active'
                   ELSE 'candidate'
               END,
               last_user_action_at = NULL,
               updated_at = datetime('now')
           WHERE profile_id = ? AND merchant_key = ?""",
        (profile_id, merchant_key),
    )
    return bool(changed or result.rowcount)


def sync_legacy_subscription_cache(conn, profile: str | None = None) -> int:
    """
    Derive legacy merchant subscription cache fields from recurring_obligations.

    The old merchants.subscription_* columns remain for compatibility, but this
    keeps them as a projection of v2 state instead of an independent source of
    truth.
    """
    clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        clause = "WHERE profile_id = ?"
        params.append(profile)
    rows = conn.execute(
        f"""SELECT profile_id, merchant_key, display_name, category, source,
                   amount_cents, frequency, state, last_seen_date,
                   next_expected_date, last_user_action_at
            FROM recurring_obligations
            {clause}""",
        params,
    ).fetchall()
    updated = 0
    for row in rows:
        profile_id = row[0] or "household"
        merchant_key = canonical_key(row[1])
        if not merchant_key:
            continue
        state = row[7] or "candidate"
        if state == "dismissed":
            subscription_status = "dismissed"
        elif state == "confirmed":
            subscription_status = "active"
        else:
            subscription_status = state
        next_expected = None if state in {"inactive", "stale", "dismissed", "cancelled"} else row[9]
        conn.execute(
            """INSERT INTO merchants
               (merchant_key, clean_name, category, source, is_subscription,
                subscription_frequency, subscription_amount, subscription_status,
                cancelled_by_user, cancelled_at, last_charge_date,
                next_expected_date, profile_id)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(merchant_key, profile_id) DO UPDATE SET
                   clean_name = COALESCE(NULLIF(excluded.clean_name, ''), merchants.clean_name),
                   category = COALESCE(NULLIF(excluded.category, ''), merchants.category),
                   source = excluded.source,
                   is_subscription = 1,
                   subscription_frequency = excluded.subscription_frequency,
                   subscription_amount = excluded.subscription_amount,
                   subscription_status = excluded.subscription_status,
                   cancelled_by_user = excluded.cancelled_by_user,
                   cancelled_at = excluded.cancelled_at,
                   last_charge_date = excluded.last_charge_date,
                   next_expected_date = excluded.next_expected_date,
                   updated_at = datetime('now')""",
            (
                merchant_key,
                row[2] or merchant_key,
                row[3] or "Subscriptions",
                row[4] or "algorithm",
                row[6] or "monthly",
                dollars(row[5]),
                subscription_status,
                1 if state == "cancelled" else 0,
                row[10] if state == "cancelled" else None,
                row[8],
                next_expected,
                profile_id,
            ),
        )
        updated += 1
    return updated


def validate_backfill(conn, profile: str | None = None) -> dict[str, Any]:
    """Check that legacy user/dismiss/cancel state has a v2 representation."""
    profile_clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        profile_clause = " AND profile_id = ?"
        params.append(profile)

    obligation_rows = conn.execute(
        f"""SELECT profile_id, merchant_key, state
            FROM recurring_obligations
            WHERE 1=1{profile_clause}""",
        params,
    ).fetchall()
    obligations_by_state = {
        (row[0] or "household", canonical_key(row[1]), row[2] or "")
        for row in obligation_rows
    }
    feedback_rows = conn.execute(
        f"""SELECT profile_id, merchant_key, feedback_type
            FROM recurring_feedback
            WHERE superseded_at IS NULL{profile_clause}""",
        params,
    ).fetchall()
    feedback_set = {
        (row[0] or "household", canonical_key(row[1]), row[2] or "")
        for row in feedback_rows
    }

    missing_user_declared = []
    for row in conn.execute(
        f"""SELECT merchant_name, profile_id
            FROM user_declared_subscriptions
            WHERE is_active = 1{profile_clause}""",
        params,
    ).fetchall():
        merchant_key = canonical_key(row[0])
        if (row[1], merchant_key, "confirmed") not in obligations_by_state:
            missing_user_declared.append({"merchant": row[0], "profile": row[1]})

    missing_dismissed = []
    for row in conn.execute(
        f"""SELECT merchant_name, profile_id
            FROM dismissed_recurring
            WHERE 1=1{profile_clause}""",
        params,
    ).fetchall():
        merchant_key = canonical_key(row[0])
        if (row[1], merchant_key, "dismissed") not in feedback_set:
            missing_dismissed.append({"merchant": row[0], "profile": row[1]})

    missing_cancelled = []
    for row in conn.execute(
        f"""SELECT merchant_key, clean_name, profile_id
            FROM merchants
            WHERE is_subscription = 1
              AND COALESCE(cancelled_by_user, 0) = 1{profile_clause}""",
        params,
    ).fetchall():
        profile_id = row[2] or "household"
        merchant_key = canonical_key(row[0] or row[1])
        if (profile_id, merchant_key, "cancelled") not in obligations_by_state:
            missing_cancelled.append({"merchant": row[1] or row[0], "profile": row[2] or "household"})

    return {
        "missing_user_declared": missing_user_declared,
        "missing_dismissed": missing_dismissed,
        "missing_cancelled": missing_cancelled,
        "ok": not (missing_user_declared or missing_dismissed or missing_cancelled),
    }


def backfill_from_legacy(conn, profile: str | None = None) -> dict[str, int]:
    """Idempotently project legacy recurring state into app-computed v2 keys."""
    profile_clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        profile_clause = " AND profile_id = ?"
        params.append(profile)

    counts = {"user_declared": 0, "merchant_rows": 0, "dismissed": 0, "cancelled": 0}

    for row in conn.execute(
        f"""SELECT merchant_name, amount, frequency,
                   COALESCE(NULLIF(category, ''), 'Subscriptions'),
                   expected_day, profile_id
            FROM user_declared_subscriptions
            WHERE is_active = 1{profile_clause}""",
        params,
    ).fetchall():
        upsert_user_obligation(
            conn,
            merchant=row[0],
            amount=float(row[1] or 0),
            frequency=row[2] or "monthly",
            profile_id=row[5] or "household",
            category=row[3] or "Subscriptions",
            expected_day=row[4],
        )
        counts["user_declared"] += 1

    for row in conn.execute(
        f"""SELECT merchant_key, clean_name, category, source,
                   subscription_frequency, subscription_amount,
                   subscription_status, last_charge_date, next_expected_date,
                   charge_count, profile_id, cancelled_by_user, cancelled_at
            FROM merchants
            WHERE is_subscription = 1{profile_clause}""",
        params,
    ).fetchall():
        profile_id = row[10] or "household"
        merchant = row[1] or row[0]
        merchant_key = canonical_key(merchant)
        existing = conn.execute(
            """SELECT 1 FROM recurring_obligations
               WHERE profile_id = ? AND merchant_key = ?
               LIMIT 1""",
            (profile_id, merchant_key),
        ).fetchone()
        if existing:
            continue
        source = row[3] or "algorithm"
        if source == "user":
            continue
        if hard_excluded_reason({"category": row[2], "source": source, "merchant": merchant}):
            continue
        item = {
            "merchant": merchant,
            "avg_amount": float(row[5] or 0),
            "frequency": row[4] or "monthly",
            "occurrences": int(row[9] or 0),
            "category": row[2] or "Subscriptions",
            "confidence": "high" if source == "seed" else "medium",
            "status": row[6] or "inactive",
            "last_date": row[7],
            "next_expected_date": row[8],
            "months_paid": int(row[9] or 0),
            "matched_by": source,
            "annual_cost": annualize_amount(float(row[5] or 0), row[4] or "monthly"),
            "price_change": None,
        }
        key = upsert_obligation_from_item(conn, item, profile_id=profile_id, run_id=None)
        if key and row[11]:
            conn.execute(
                """UPDATE recurring_obligations
                   SET state = 'cancelled',
                       next_expected_date = NULL,
                       last_user_action_at = COALESCE(?, datetime('now')),
                       updated_at = datetime('now')
                   WHERE profile_id = ? AND obligation_key = ?""",
                (row[12], profile_id, key),
            )
            counts["cancelled"] += 1
        elif key and (row[6] or "").lower() == "inactive":
            conn.execute(
                """UPDATE recurring_obligations
                   SET state = 'inactive',
                       next_expected_date = NULL,
                       updated_at = datetime('now')
                   WHERE profile_id = ? AND obligation_key = ?""",
                (profile_id, key),
            )
        counts["merchant_rows"] += 1

    for row in conn.execute(
        f"""SELECT merchant_name, profile_id, dismissed_at
            FROM dismissed_recurring
            WHERE 1=1{profile_clause}""",
        params,
    ).fetchall():
        merchant_key = canonical_key(row[0])
        existing_rows = conn.execute(
            """SELECT merchant_key FROM recurring_feedback
               WHERE profile_id = ?
                 AND feedback_type = 'dismissed'
                 AND superseded_at IS NULL""",
            (row[1],),
        ).fetchall()
        exists = any(canonical_key(existing[0]) == merchant_key for existing in existing_rows)
        if not exists:
            conn.execute(
                """INSERT INTO recurring_feedback
                   (profile_id, obligation_key, merchant_key, feedback_type, scope,
                    payload_json, created_at)
                   VALUES (?, ?, ?, 'dismissed', 'merchant', ?, ?)""",
                (
                    row[1],
                    f"{merchant_key}:merchant",
                    merchant_key,
                    json.dumps({"backfilled_from": "dismissed_recurring"}, sort_keys=True),
                    row[2] or datetime.now().isoformat(timespec="seconds"),
                ),
            )
            counts["dismissed"] += 1

    return counts


def _legacy_recurring_summary(conn, profile: str | None = None) -> dict[str, Any]:
    profile_clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        profile_clause = " AND profile_id = ?"
        params.append(profile)

    rows = conn.execute(
        f"""SELECT subscription_status, cancelled_by_user, subscription_amount,
                   subscription_frequency, source
            FROM merchants
            WHERE is_subscription = 1{profile_clause}""",
        params,
    ).fetchall()
    active_total = 0.0
    active_count = inactive_count = cancelled_count = 0
    for row in rows:
        status = row[0] or "inactive"
        cancelled = bool(row[1])
        annual = annualize_amount(float(row[2] or 0), row[3] or "monthly")
        if cancelled:
            cancelled_count += 1
        elif status == "active":
            active_count += 1
            active_total += annual
        else:
            inactive_count += 1

    user_count = conn.execute(
        f"""SELECT COUNT(*)
            FROM user_declared_subscriptions
            WHERE is_active = 1{profile_clause}""",
        params,
    ).fetchone()[0]
    dismissed_count = conn.execute(
        f"""SELECT COUNT(*)
            FROM dismissed_recurring
            WHERE 1=1{profile_clause}""",
        params,
    ).fetchone()[0]
    return {
        "active_count": active_count,
        "inactive_count": inactive_count,
        "cancelled_count": cancelled_count,
        "user_declared_count": int(user_count or 0),
        "dismissed_count": int(dismissed_count or 0),
        "active_annual_total": round(active_total, 2),
    }


def shadow_comparison(conn, profile: str | None = None, *, days: int = 45) -> dict[str, Any]:
    """Return old-vs-v2 recurring and upcoming-bill comparison metrics."""
    legacy = _legacy_recurring_summary(conn, profile)
    recurring = get_recurring_bundle(conn, profile)
    scheduled = get_scheduled_bundle(conn, days=days, profile=profile)
    confirmed_active_total = 0.0
    inferred_active_total = 0.0
    for item in recurring.get("items", []):
        if item.get("state") not in {"active", "confirmed"}:
            continue
        if item.get("confirmed") or int(item.get("confidence_score") or 0) >= 75:
            confirmed_active_total += float(item.get("annual_cost") or 0)
        else:
            inferred_active_total += float(item.get("annual_cost") or 0)
    return {
        "profile": profile or "household",
        "days": days,
        "legacy": legacy,
        "v2": {
            "active_count": recurring.get("active_count", 0),
            "inactive_count": recurring.get("inactive_count", 0),
            "candidate_count": recurring.get("candidate_count", 0),
            "dismissed_count": recurring.get("dismissed_count", 0),
            "cancelled_count": recurring.get("cancelled_count", 0),
            "confirmed_active_annual_total": round(confirmed_active_total, 2),
            "inferred_active_annual_total": round(inferred_active_total, 2),
            "upcoming_confirmed_total": scheduled.get("confirmed_upcoming_total", 0),
            "upcoming_inferred_total": scheduled.get("inferred_upcoming_total", 0),
            "needs_review_total": scheduled.get("needs_review_total", 0),
        },
        "backfill": validate_backfill(conn, profile),
    }


def _active_feedback(conn, profile: str | None) -> dict[tuple[str, str], dict[str, Any]]:
    clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        clause = " AND profile_id = ?"
        params.append(profile)
    rows = conn.execute(
        f"""SELECT profile_id, obligation_key, merchant_key, feedback_type, scope,
                   payload_json, expires_at, created_at
            FROM recurring_feedback
            WHERE superseded_at IS NULL
              AND (expires_at IS NULL OR expires_at >= date('now'))
              {clause}
            ORDER BY created_at DESC""",
        params,
    ).fetchall()
    feedback: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row[5] or "{}")
        except Exception:
            pass
        key = (row[0], row[2])
        feedback.setdefault(key, {
            "profile_id": row[0],
            "obligation_key": row[1],
            "merchant_key": row[2],
            "feedback_type": row[3],
            "scope": row[4],
            "payload": payload,
            "expires_at": row[6],
            "created_at": row[7],
        })
    return feedback


def _amount_review_dismissals(conn, profile: str | None) -> dict[tuple[str, str], dict[str, Any]]:
    clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        clause = " AND profile_id = ?"
        params.append(profile)
    rows = conn.execute(
        f"""SELECT profile_id, merchant_key, payload_json, created_at
            FROM recurring_feedback
            WHERE superseded_at IS NULL
              AND feedback_type = 'amount_review_dismissed'
              AND (expires_at IS NULL OR expires_at >= date('now'))
              {clause}
            ORDER BY created_at DESC""",
        params,
    ).fetchall()
    dismissals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row[2] or "{}")
        except Exception:
            payload = {}
        profile_id = row[0] or "household"
        merchant_key = canonical_key(row[1])
        dismissals.setdefault((profile_id, merchant_key), {
            "profile_id": profile_id,
            "merchant_key": merchant_key,
            "payload": payload,
            "created_at": row[3],
        })
    return dismissals


def _amount_review_matches_dismissal(review: dict[str, Any] | None, dismissal: dict[str, Any] | None) -> bool:
    if not review or not dismissal:
        return False
    payload = dismissal.get("payload") or {}
    latest_date = str(review.get("latest_date") or "")
    dismissed_latest = str(payload.get("latest_date") or "")
    if latest_date and dismissed_latest and latest_date != dismissed_latest:
        return False
    try:
        review_amount = float(review.get("suggested_amount") or review.get("cycle_net_amount") or 0)
        dismissed_amount = float(payload.get("suggested_amount") or 0)
    except (TypeError, ValueError):
        return False
    return dismissed_amount > 0 and abs(review_amount - dismissed_amount) < 0.01


def obligations_exist(conn, profile: str | None = None) -> bool:
    if profile and profile != "household":
        row = conn.execute(
            "SELECT 1 FROM recurring_obligations WHERE profile_id = ? LIMIT 1",
            (profile,),
        ).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM recurring_obligations LIMIT 1").fetchone()
    return bool(row)


def _row_value(row, idx: int, default=None):
    try:
        return row[idx]
    except Exception:
        return default


def _dedupe_obligation_rows(
    rows,
    profile: str | None = None,
    *,
    service_idx: int,
    seed_idx: int,
) -> list:
    """Hide synthetic household/profile duplicates and same-profile shadow rows."""
    row_list = list(rows or [])

    def explicit_service_key(row) -> str:
        return str(_row_value(row, service_idx) or _row_value(row, seed_idx) or "").upper().strip()

    def service_key(row) -> str:
        return canonical_key(explicit_service_key(row) or row[3] or "")

    explicit_bases_with_profile = {
        ((row[0] or "household"), canonical_key(row[2]), service_key(row), row[6] or "monthly")
        for row in row_list
        if explicit_service_key(row)
    }
    explicit_bases_without_profile = {
        (canonical_key(row[2]), service_key(row), row[6] or "monthly")
        for row in row_list
        if explicit_service_key(row)
    }

    def duplicate_key(row, include_profile: bool) -> tuple:
        # Explicit service/seed names represent one obligation even when the
        # user changes the amount. Legacy rows without service metadata also
        # merge into an explicit service row when the merchant/service/frequency
        # match. Only detector-only fallbacks use amount to avoid collapsing
        # two genuinely separate same-merchant services.
        base_without_amount = (canonical_key(row[2]), service_key(row), row[6] or "monthly")
        if include_profile:
            matching_explicit = ((row[0] or "household"), *base_without_amount) in explicit_bases_with_profile
        else:
            matching_explicit = base_without_amount in explicit_bases_without_profile
        amount_part = None if explicit_service_key(row) or matching_explicit else int(row[5] or 0)
        base = (*base_without_amount, amount_part)
        return ((row[0] or "household"), *base) if include_profile else base

    if not (profile and profile != "household"):
        concrete_keys = {
            duplicate_key(row, include_profile=False)
            for row in row_list
            if (row[0] or "household") != "household"
        }
        row_list = [
            row for row in row_list
            if not ((row[0] or "household") == "household" and duplicate_key(row, include_profile=False) in concrete_keys)
        ]

    state_rank = {"confirmed": 6, "active": 5, "candidate": 4, "inactive": 3, "stale": 2, "dismissed": 1, "cancelled": 0}
    source_rank = {"user": 6, "user_confirmed": 5, "seed": 4, "algorithm": 3, "category": 2}

    def rank(row) -> tuple:
        profile_rank = 0 if (row[0] or "household") == "household" else 1
        score = _row_value(row, 10 if service_idx == 16 else 13) or 0
        last_seen = _row_value(row, 14 if service_idx == 16 else 16) or ""
        return (
            profile_rank,
            state_rank.get(row[8] if service_idx == 16 else row[11], 0),
            source_rank.get(row[9] if service_idx == 16 else row[12], 0),
            int(score),
            str(last_seen),
        )

    best: dict[tuple, Any] = {}
    for row in row_list:
        key = duplicate_key(row, include_profile=True)
        current = best.get(key)
        if current is None or rank(row) > rank(current):
            best[key] = row
    return list(best.values())


def _state_with_recency_sanity(
    state: str,
    *,
    source: str | None,
    confidence_score: int,
    frequency: str | None,
    last_seen: date | None,
    last_user_action_at: str | None,
    today: date | None = None,
) -> str:
    if state not in {"inactive", "stale"}:
        return state
    if last_user_action_at:
        return state
    if source not in {"seed", "algorithm", "category", "user_confirmed"}:
        return state
    if not last_seen:
        return state
    nominal, grace, _, _ = _frequency_nominal_and_grace(frequency)
    if ((today or date.today()) - last_seen).days <= nominal + grace:
        return "active" if confidence_score >= 55 else "candidate"
    return state


def _spend_history_by_merchant(conn, profile: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """Return historical paid totals keyed by (profile_id, canonical merchant key)."""
    clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        clause = " AND profile_id = ?"
        params.append(profile)
    try:
        rows = conn.execute(
            f"""SELECT profile_id, amount,
                       COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''),
                                NULLIF(description_normalized, ''), description) AS merchant,
                       COALESCE(merchant_key, '') || ' ' ||
                       COALESCE(merchant_name, '') || ' ' ||
                       COALESCE(description_normalized, '') || ' ' ||
                       COALESCE(description, '') AS match_text
                FROM transactions_visible
                WHERE amount != 0
                  AND COALESCE(is_excluded, 0) = 0{clause}""",
            params,
        ).fetchall()
    except Exception:
        return {}

    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        amount = float(row[1] or 0)
        row_profile = row[0] or "household"
        keys = _history_keys_for_transaction(row[2], row[3])
        if not keys:
            continue
        for scope in (row_profile, "household"):
            for key in keys:
                bucket = totals.setdefault((scope, key), {"total_spent": 0.0, "charge_count": 0})
                bucket["total_spent"] += -amount
                if amount < 0:
                    bucket["charge_count"] += 1
    for bucket in totals.values():
        bucket["total_spent"] = round(max(float(bucket["total_spent"] or 0), 0), 2)
    return totals


def _recent_history_by_merchant(
    conn,
    profile: str | None = None,
    *,
    today: date | None = None,
    days: int = 450,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return recent charge evidence keyed by (profile_id, canonical merchant key)."""
    today = today or date.today()
    clause = ""
    params: list[Any] = [(today - timedelta(days=max(1, int(days or 450)))).isoformat()]
    if profile and profile != "household":
        clause = " AND profile_id = ?"
        params.append(profile)
    try:
        rows = conn.execute(
            f"""SELECT id, date, amount, category, profile_id,
                       COALESCE(NULLIF(merchant_name, ''), NULLIF(merchant_key, ''),
                                NULLIF(description_normalized, ''), description) AS merchant,
                       COALESCE(merchant_key, '') || ' ' ||
                       COALESCE(merchant_name, '') || ' ' ||
                       COALESCE(description_normalized, '') || ' ' ||
                       COALESCE(description, '') AS match_text
                FROM transactions_visible
                WHERE amount != 0
                  AND COALESCE(is_excluded, 0) = 0
                  AND date >= ?{clause}
                ORDER BY date DESC""",
            params,
        ).fetchall()
    except Exception:
        return {}

    history: dict[tuple[str, str], dict[str, Any]] = {}
    seen_ids: dict[tuple[str, str], set[str]] = {}
    amounts_by_key: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        tx_date = parse_date(row[1])
        if tx_date is None:
            continue
        signed_amount = round(float(row[2] or 0), 2)
        amount = round(abs(signed_amount), 2)
        if amount <= 0:
            continue
        row_profile = row[4] or "household"
        keys = _history_keys_for_transaction(row[5], row[6])
        if not keys:
            continue
        tx = {
            "id": row[0],
            "date": tx_date.isoformat(),
            "amount": amount,
            "signed_amount": signed_amount,
            "category": row[3] or "",
            "merchant": row[5] or "",
            "profile": row_profile,
            "kind": "charge" if signed_amount < 0 else "credit",
        }
        for scope in (row_profile, "household"):
            for key in keys:
                bucket_key = (scope, key)
                ids = seen_ids.setdefault(bucket_key, set())
                if row[0] in ids:
                    continue
                ids.add(row[0])
                bucket = history.setdefault(
                    bucket_key,
                    {
                        "seen_count": 0,
                        "last_paid": None,
                        "amount_min": None,
                        "amount_max": None,
                        "charges": [],
                        "adjustments": [],
                        "transactions": [],
                        "recent_transactions": [],
                    },
                )
                bucket["transactions"].append(tx)
                if signed_amount < 0:
                    bucket["seen_count"] += 1
                    bucket["charges"].append(tx)
                    amounts_by_key.setdefault(bucket_key, []).append(amount)
                    current_last = parse_date(bucket.get("last_paid"))
                    if current_last is None or tx_date > current_last:
                        bucket["last_paid"] = tx_date.isoformat()
                    bucket["recent_transactions"].append(tx)
                else:
                    bucket["adjustments"].append(tx)

    for bucket_key, bucket in history.items():
        amounts = amounts_by_key.get(bucket_key, [])
        if amounts:
            bucket["amount_min"] = round(min(amounts), 2)
            bucket["amount_max"] = round(max(amounts), 2)
        for list_key in ("transactions", "charges", "adjustments", "recent_transactions"):
            bucket[list_key] = sorted(
                bucket[list_key],
                key=lambda item: item.get("date") or "",
                reverse=True,
            )
        bucket["recent_transactions"] = sorted(
            bucket["recent_transactions"],
            key=lambda item: item.get("date") or "",
            reverse=True,
        )[:5]
    return history


def _history_keys_for_transaction(merchant: str | None, match_text: str | None) -> set[str]:
    keys = set()
    merchant_key = canonical_key(merchant)
    if merchant_key:
        keys.add(merchant_key)

    text = (match_text or "").upper()
    if "CLAUDE" in text:
        keys.update({"CLAUDE", "CLAUDE_AI", "CLAUDE_PRO"})
    if "CHATGPT" in text:
        keys.update({"CHATGPT", "CHATGPT_PLUS"})
    if "TWITTER" in text or "X CORP" in text or "ABOUT.X.COM" in text:
        keys.update({"TWITTER", "X_TWITTER", "X_PREMIUM_TWITTER"})

    tokens = [
        token for token in re.findall(r"[A-Z0-9]+", text)
        if len(token) > 1 and token not in {"WWW", "COM", "HTTPS", "HTTP", "THE", "INC", "LLC", "CO"}
    ]
    for size in (1, 2, 3):
        for idx in range(0, max(len(tokens) - size + 1, 0)):
            phrase = " ".join(tokens[idx:idx + size])
            key = canonical_key(phrase)
            if key:
                keys.add(key)
    return keys


def _history_keys_for_obligation(*values: str | None) -> set[str]:
    keys: set[str] = set()
    for value in values:
        if not value:
            continue
        keys.update(_history_keys_for_transaction(value, value))
    return keys


def _direct_history_keys_for_obligation(*values: str | None) -> set[str]:
    keys: set[str] = set()
    for value in values:
        key = canonical_key(value)
        if key:
            keys.add(key)
    return keys


def _best_recent_history(
    history: dict[tuple[str, str], dict[str, Any]],
    profile_id: str | None,
    keys: set[str],
    *,
    preferred_keys: set[str] | None = None,
) -> dict[str, Any]:
    def choose(search_keys: set[str]) -> dict[str, Any]:
        best: dict[str, Any] = {}
        for scope in (profile_id or "household", "household"):
            for key in search_keys:
                candidate = history.get((scope, key))
                if not candidate:
                    continue
                if int(candidate.get("seen_count") or 0) > int(best.get("seen_count") or 0):
                    best = candidate
        return best

    preferred = choose(preferred_keys or set())
    return preferred or choose(keys)


def _same_cycle_window_days(frequency: str | None) -> int:
    freq = (frequency or "monthly").lower().replace("-", "_")
    if freq in {"weekly", "week"}:
        return 2
    if freq in {"biweekly", "bi_weekly", "fortnightly"}:
        return 4
    return 7


def _charge_cycles(charges: list[dict[str, Any]], frequency: str | None) -> list[dict[str, Any]]:
    same_cycle_days = _same_cycle_window_days(frequency)
    cycles: list[dict[str, Any]] = []
    for tx in sorted(charges, key=lambda item: item.get("date") or "", reverse=True):
        tx_date = parse_date(tx.get("date"))
        if tx_date is None:
            continue
        if not cycles:
            cycles.append({"anchor_date": tx_date, "oldest_date": tx_date, "transactions": [tx]})
            continue
        current = cycles[-1]
        oldest = current["oldest_date"]
        if 0 <= (oldest - tx_date).days <= same_cycle_days:
            current["transactions"].append(tx)
            current["oldest_date"] = tx_date
        else:
            cycles.append({"anchor_date": tx_date, "oldest_date": tx_date, "transactions": [tx]})
    return cycles


def _summarize_current_history(
    history: dict[str, Any],
    *,
    frequency: str | None,
    expected_amount: float,
) -> dict[str, Any]:
    charges = history.get("charges") or history.get("recent_transactions") or []
    cycles = _charge_cycles(charges, frequency)
    if not cycles:
        return history

    _, _, low, high = _frequency_nominal_and_grace(frequency)
    current_cycles = [cycles[0]]
    for cycle in cycles[1:]:
        gap = (current_cycles[-1]["anchor_date"] - cycle["anchor_date"]).days
        if low <= gap <= high:
            current_cycles.append(cycle)
        else:
            break

    current_ids = {
        tx.get("id")
        for cycle in current_cycles
        for tx in cycle.get("transactions", [])
    }
    current_transactions = [
        tx for cycle in current_cycles for tx in cycle.get("transactions", [])
    ]
    current_transactions = sorted(
        current_transactions,
        key=lambda item: item.get("date") or "",
        reverse=True,
    )
    prior_transactions = [
        tx for tx in charges
        if tx.get("id") not in current_ids
    ]
    current_amounts = [float(tx.get("amount") or 0) for tx in current_transactions if float(tx.get("amount") or 0) > 0]
    latest_date = current_cycles[0]["anchor_date"].isoformat()
    nearby_adjustments = []
    for tx in history.get("adjustments") or []:
        tx_date = parse_date(tx.get("date"))
        if tx_date and abs((current_cycles[0]["anchor_date"] - tx_date).days) <= _same_cycle_window_days(frequency):
            nearby_adjustments.append(tx)

    summary = {
        **history,
        "seen_count": len(current_cycles),
        "historical_seen_count": len(charges),
        "last_paid": latest_date,
        "amount_min": round(min(current_amounts), 2) if current_amounts else history.get("amount_min"),
        "amount_max": round(max(current_amounts), 2) if current_amounts else history.get("amount_max"),
        "recent_transactions": current_transactions[:5],
        "prior_transactions": sorted(prior_transactions, key=lambda item: item.get("date") or "", reverse=True)[:5],
        "adjustments": sorted(nearby_adjustments, key=lambda item: item.get("date") or "", reverse=True)[:5],
        "current_cycle_charge_count": len(current_cycles[0].get("transactions", [])),
    }
    if prior_transactions:
        prior_date = parse_date(prior_transactions[0].get("date"))
        if prior_date:
            gap_days = (current_cycles[-1]["anchor_date"] - prior_date).days
            summary["history_gap_days"] = gap_days
            summary["restart_detected"] = gap_days > high

    if current_amounts and expected_amount > 0:
        latest_amount = float(current_transactions[0].get("amount") or 0)
        largest_amount = max(current_amounts)
        smallest_amount = min(current_amounts)
        current_cycle_amounts = [
            float(tx.get("amount") or 0)
            for tx in current_cycles[0].get("transactions", [])
            if float(tx.get("amount") or 0) > 0
        ]
        cycle_charge_total = round(sum(current_cycle_amounts), 2)
        cycle_credit_total = round(sum(float(tx.get("amount") or 0) for tx in nearby_adjustments), 2)
        cycle_net_amount = round(max(largest_amount, cycle_charge_total - cycle_credit_total), 2)
        threshold = max(5.0, expected_amount * 0.15)
        if len(current_cycles[0].get("transactions", [])) > 1 and (largest_amount - smallest_amount) >= threshold:
            summary["amount_review"] = {
                "type": "same_cycle_adjustment",
                "basis": "same_cycle_cluster",
                "direction": "down" if cycle_net_amount < expected_amount else "up",
                "current_amount": round(expected_amount, 2),
                "suggested_amount": cycle_net_amount,
                "latest_amount": round(latest_amount, 2),
                "previous_amount": round(smallest_amount, 2),
                "cycle_charge_total": cycle_charge_total,
                "cycle_credit_total": cycle_credit_total,
                "cycle_net_amount": cycle_net_amount,
                "latest_date": latest_date,
                "cycle_charge_count": len(current_cycles[0].get("transactions", [])),
            }
        elif abs(latest_amount - expected_amount) >= threshold:
            summary["amount_review"] = {
                "type": "amount_change",
                "basis": "latest_charge",
                "direction": "down" if latest_amount < expected_amount else "up",
                "current_amount": round(expected_amount, 2),
                "suggested_amount": round(latest_amount, 2),
                "latest_amount": round(latest_amount, 2),
                "latest_date": latest_date,
            }
    return summary


def _legacy_subscription_totals(conn, profile: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        clause = " AND profile_id = ?"
        params.append(profile)
    try:
        rows = conn.execute(
            f"""SELECT COALESCE(profile_id, 'household'), merchant_key, clean_name,
                       subscription_amount, total_spent, charge_count
                FROM merchants
                WHERE is_subscription = 1{clause}""",
            params,
        ).fetchall()
    except Exception:
        return {}

    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        profile_id = row[0] or "household"
        amount = float(row[3] or 0)
        charge_count = int(row[5] or 0)
        total_spent = float(row[4] or 0)
        if total_spent <= 0 and amount > 0 and charge_count > 0:
            total_spent = amount * charge_count
        if total_spent <= 0 and charge_count <= 0:
            continue
        for raw_key in (row[1], row[2]):
            key = canonical_key(raw_key)
            if not key:
                continue
            bucket = totals.setdefault((profile_id, key), {"total_spent": 0.0, "charge_count": 0})
            bucket["total_spent"] = max(float(bucket["total_spent"] or 0), total_spent)
            bucket["charge_count"] = max(int(bucket["charge_count"] or 0), charge_count)
    for bucket in totals.values():
        bucket["total_spent"] = round(bucket["total_spent"], 2)
    return totals


def get_recurring_bundle(conn, profile: str | None = None) -> dict[str, Any]:
    clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        clause = "WHERE profile_id = ?"
        params.append(profile)
    rows = conn.execute(
        f"""SELECT profile_id, obligation_key, merchant_key, display_name, category,
                   amount_cents, frequency, next_expected_date, state, source,
                   confidence_score, confidence_label, evidence_json, first_seen_date,
                   last_seen_date, last_user_action_at, service_tag, seed_name
            FROM recurring_obligations
            {clause}
            ORDER BY
                CASE state WHEN 'confirmed' THEN 0 WHEN 'active' THEN 1 WHEN 'candidate' THEN 2 WHEN 'inactive' THEN 3 ELSE 4 END,
                amount_cents DESC""",
        params,
    ).fetchall()
    rows = _dedupe_obligation_rows(rows, profile, service_idx=16, seed_idx=17)
    feedback = _active_feedback(conn, profile)
    history_totals = _spend_history_by_merchant(conn, profile)
    legacy_totals = _legacy_subscription_totals(conn, profile)
    recent_history = _recent_history_by_merchant(conn, profile)
    items: list[dict[str, Any]] = []
    dismissed: list[dict[str, Any]] = []
    suppressed_event_keys: set[tuple[str, str]] = set()
    suppressed_event_merchants: set[str] = set()
    active_count = inactive_count = cancelled_count = candidate_count = 0
    total_annual = 0.0
    total_monthly = 0.0

    for row in rows:
        profile_id, obligation_key, stored_merchant_key = row[0], row[1], row[2]
        merchant_key = canonical_key(stored_merchant_key) or stored_merchant_key
        effective_state = row[8] or "candidate"
        effective_state = _state_with_recency_sanity(
            effective_state,
            source=row[9],
            confidence_score=int(row[10] or 0),
            frequency=row[6],
            last_seen=parse_date(row[14]),
            last_user_action_at=row[15],
        )
        fb = feedback.get((profile_id, stored_merchant_key)) or feedback.get((profile_id, merchant_key))
        if fb and fb["feedback_type"] in {"dismissed", "cancelled"}:
            effective_state = "cancelled" if fb["feedback_type"] == "cancelled" else "dismissed"
        if effective_state == "dismissed":
            suppressed_event_keys.add((profile_id, merchant_key))
            suppressed_event_merchants.add(merchant_key)
            dismissed.append({
                "merchant": row[3] or merchant_key,
                "dismissed_at": fb["created_at"] if fb else row[15],
                "profile": profile_id,
                "obligation_key": obligation_key,
            })
            continue

        amount = dollars(row[5])
        frequency = row[6] or "monthly"
        score = int(row[10] or 0)
        label = row[11] or confidence_label(score, row[9])
        confirmed = effective_state == "confirmed" or row[9] == "user" or label == "user"
        evidence = {}
        try:
            evidence = json.loads(row[12] or "{}")
        except Exception:
            evidence = {}
        status = "active" if effective_state == "confirmed" else effective_state
        cancelled = effective_state == "cancelled"
        lookup_keys = _history_keys_for_obligation(
            stored_merchant_key,
            merchant_key,
            row[3],
            row[16],
            row[17],
        )
        direct_lookup_keys = _direct_history_keys_for_obligation(
            stored_merchant_key,
            merchant_key,
            row[3],
            row[16],
            row[17],
        )
        current_history = _best_recent_history(
            recent_history,
            profile_id,
            lookup_keys,
            preferred_keys=direct_lookup_keys,
        )
        if current_history:
            current_history = _summarize_current_history(
                current_history,
                frequency=frequency,
                expected_amount=amount,
            )
        amount_review = current_history.get("amount_review") if current_history else None
        effective_amount = amount
        amount_basis = "stored"
        if amount_review:
            try:
                suggested_amount = round(float(amount_review.get("suggested_amount") or 0), 2)
            except (TypeError, ValueError):
                suggested_amount = 0
            if suggested_amount > 0:
                effective_amount = suggested_amount
                amount_basis = amount_review.get("basis") or "recent_history"
        annual = annualize_amount(effective_amount, frequency)
        display_last_charge = current_history.get("last_paid") if current_history else None
        display_last_charge = display_last_charge or row[14]
        display_next_expected = row[7]
        if (
            not display_next_expected
            and display_last_charge
            and effective_state in {"active", "confirmed"}
            and not cancelled
        ):
            last_seen_for_next = parse_date(display_last_charge)
            if last_seen_for_next:
                display_next_expected = advance_recurring_date(last_seen_for_next, frequency).isoformat()
        evidence_for_item = {
            **evidence,
            "last_paid": display_last_charge,
            "amount_min": current_history.get("amount_min") if current_history else evidence.get("amount_min"),
            "amount_max": current_history.get("amount_max") if current_history else evidence.get("amount_max"),
            "historical_seen_count": current_history.get("historical_seen_count") if current_history else evidence.get("historical_seen_count"),
            "current_cycle_charge_count": current_history.get("current_cycle_charge_count") if current_history else evidence.get("current_cycle_charge_count"),
        }
        spend_history = history_totals.get((profile_id, merchant_key), {})
        legacy = legacy_totals.get((profile_id, merchant_key), {})
        paid_total = float(spend_history.get("total_spent") or legacy.get("total_spent") or 0)
        paid_count = int(
            spend_history.get("charge_count")
            or legacy.get("charge_count")
            or current_history.get("historical_seen_count")
            or evidence.get("occurrences")
            or 0
        )
        item = {
            "merchant": row[3] or merchant_key,
            "clean_name": row[3] or merchant_key,
            "logo_url": None,
            "category": row[4] or "Subscriptions",
            "frequency": frequency,
            "amount": effective_amount,
            "stored_amount": amount,
            "amount_basis": amount_basis,
            "annual_cost": round(annual, 2),
            "status": status,
            "state": effective_state,
            "confidence": label,
            "confidence_score": score,
            "confirmed": confirmed,
            "source": row[9] or "algorithm",
            "matched_by": row[9] or "algorithm",
            "last_charge": display_last_charge,
            "next_expected": display_next_expected,
            "charge_count": paid_count,
            "total_spent": round(paid_total or effective_amount * paid_count, 2),
            "price_change": evidence.get("price_change"),
            "cancelled": cancelled,
            "profile": profile_id,
            "obligation_key": obligation_key,
            "merchant_key": merchant_key,
            "evidence": evidence_for_item,
            "service_tag": row[16],
            "seed_name": row[17],
            "recent_transactions": current_history.get("recent_transactions") if current_history else [],
            "amount_review": amount_review,
        }
        items.append(item)

        if cancelled:
            suppressed_event_keys.add((profile_id, merchant_key))
            suppressed_event_merchants.add(merchant_key)
            cancelled_count += 1
        elif effective_state in {"inactive", "stale"}:
            inactive_count += 1
        elif effective_state == "candidate":
            candidate_count += 1
        elif effective_state in {"active", "confirmed"}:
            active_count += 1
            if confirmed or score >= 75:
                total_annual += annual
                total_monthly += annual / 12

    event_rows = conn.execute(
        f"""SELECT id, event_type, merchant_name, detail, created_at, is_read, profile_id
            FROM subscription_events
            {'WHERE profile_id = ?' if profile and profile != 'household' else ''}
            ORDER BY created_at DESC
            LIMIT 50""",
        params,
    ).fetchall()
    events = []
    for row in event_rows:
        event_profile = row[6] or "household"
        event_merchant_key = canonical_key(row[2]) or str(row[2] or "").strip().upper()
        if (event_profile, event_merchant_key) in suppressed_event_keys:
            continue
        if not (profile and profile != "household") and event_merchant_key in suppressed_event_merchants:
            continue
        try:
            detail = json.loads(row[3] or "{}")
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
    unread_event_count = sum(1 for event in events if not event["is_read"])

    return {
        "items": items,
        "active_count": active_count,
        "inactive_count": inactive_count,
        "cancelled_count": cancelled_count,
        "candidate_count": candidate_count,
        "dismissed_count": len(dismissed),
        "total_monthly": round(total_monthly, 2),
        "total_annual": round(total_annual, 2),
        "events": events,
        "unread_event_count": unread_event_count,
        "dismissed": dismissed,
    }


def upcoming_group(merchant: str, category: str | None) -> dict[str, str]:
    text = f"{merchant or ''} {category or ''}".lower()
    category_name = category or "Subscriptions"
    checks = [
        ("housing", "Housing", ("rent", "mortgage", "apartment", "property", "hoa", "landlord", "housing")),
        ("utilities", "Utilities", ("utility", "utilities", "electric", "water", "gas", "internet", "xfinity", "comcast", "verizon", "phone", "mobile")),
        ("debt", "Debt", ("loan", "student loan", "auto loan", "credit card", "visa", "mastercard", "amex", "debt")),
        ("insurance", "Insurance", ("insurance", "geico", "progressive", "state farm", "allstate", "anthem", "kaiser")),
        ("subscriptions", "Subscriptions", ("subscription", "netflix", "spotify", "apple", "google", "claude", "anthropic", "openai", "chatgpt", "x premium", "twitter", "simplefin")),
    ]
    for key, label, needles in checks:
        if category_name.lower() == label.lower() or any(needle in text for needle in needles):
            return {"key": key, "label": label}
    return {"key": "other", "label": "Other"}


VARIABLE_RECURRING_BILL_CATEGORIES = {
    "Insurance",
    "Auto Insurance",
    "Home Insurance",
    "Health Insurance",
    "Renters Insurance",
    "Life Insurance",
    "Utilities",
    "Electric",
    "Gas",
    "Water",
    "Internet",
    "Wireless",
    "Cable",
}


def _is_variable_recurring_bill(category: str | None, group_key: str | None = None) -> bool:
    if group_key in {"insurance", "utilities"}:
        return True
    return (category or "").strip() in VARIABLE_RECURRING_BILL_CATEGORIES


def _variable_bill_forecast_amount(
    *,
    stored_amount: float,
    category: str | None,
    group_key: str | None,
    history: dict[str, Any],
) -> tuple[float, dict[str, Any] | None]:
    """
    Subscriptions forecast from the configured amount. Variable bills forecast
    from the latest current-cycle charge, while the stored amount remains the
    user's confirmation/seed value.
    """
    if not _is_variable_recurring_bill(category, group_key):
        return stored_amount, None

    latest_charge = None
    for tx in history.get("recent_transactions") or []:
        if (tx.get("kind") or "charge") != "charge":
            continue
        amount = float(tx.get("amount") or 0)
        if amount > 0:
            latest_charge = round(amount, 2)
            break

    if latest_charge is None:
        return stored_amount, None

    return latest_charge, {
        "basis": "latest_variable_bill_charge",
        "stored_amount": round(stored_amount, 2),
        "latest_amount": latest_charge,
        "latest_date": history.get("last_paid"),
    }


def get_scheduled_bundle(
    conn,
    *,
    days: int = 45,
    profile: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    window_days = max(1, min(int(days or 45), 180))
    window_end = today + timedelta(days=window_days)
    clause = ""
    params: list[Any] = []
    if profile and profile != "household":
        clause = "WHERE profile_id = ?"
        params.append(profile)
    rows = conn.execute(
        f"""SELECT profile_id, obligation_key, merchant_key, display_name, category,
                   amount_cents, frequency, anchor_day, anchor_month, anchor_mode,
                   next_expected_date, state, source, confidence_score, confidence_label,
                   evidence_json, last_seen_date, service_tag, seed_name, last_user_action_at
            FROM recurring_obligations
            {clause}""",
        params,
    ).fetchall()
    rows = _dedupe_obligation_rows(rows, profile, service_idx=17, seed_idx=18)
    feedback = _active_feedback(conn, profile)
    amount_review_dismissals = _amount_review_dismissals(conn, profile)
    recent_history = _recent_history_by_merchant(conn, profile, today=today)
    items: list[dict[str, Any]] = []
    confirmed_upcoming_total = 0.0
    inferred_upcoming_total = 0.0
    needs_review_total = 0.0

    for row in rows:
        profile_id, obligation_key, stored_merchant_key = row[0], row[1], row[2]
        merchant_key = canonical_key(stored_merchant_key) or stored_merchant_key
        state = row[11] or "candidate"
        state = _state_with_recency_sanity(
            state,
            source=row[12],
            confidence_score=int(row[13] or 0),
            frequency=row[6],
            last_seen=parse_date(row[16]),
            last_user_action_at=row[19],
            today=today,
        )
        fb = feedback.get((profile_id, stored_merchant_key)) or feedback.get((profile_id, merchant_key))
        if fb and fb["feedback_type"] in {"dismissed", "cancelled"}:
            continue
        if state in {"inactive", "stale", "dismissed", "cancelled"}:
            continue
        stored_amount = dollars(row[5])
        if stored_amount <= 0:
            continue
        frequency = row[6] or "monthly"
        score = int(row[13] or 0)
        label = row[14] or confidence_label(score, row[12])
        confirmed = state == "confirmed" or row[12] == "user" or label == "user"
        evidence = {}
        try:
            evidence = json.loads(row[15] or "{}")
        except Exception:
            pass
        lookup_keys = _history_keys_for_obligation(
            stored_merchant_key,
            merchant_key,
            row[3],
            row[17],
            row[18],
        )
        direct_lookup_keys = _direct_history_keys_for_obligation(
            stored_merchant_key,
            merchant_key,
            row[3],
            row[17],
            row[18],
        )
        history = _best_recent_history(
            recent_history,
            profile_id,
            lookup_keys,
            preferred_keys=direct_lookup_keys,
        )
        if history:
            history = _summarize_current_history(
                history,
                frequency=frequency,
                expected_amount=stored_amount,
            )
        history_last_paid = history.get("last_paid")
        last_seen = parse_date(row[16])
        history_last_seen = parse_date(history_last_paid)
        last_paid = row[16]
        if history_last_seen and (last_seen is None or history_last_seen > last_seen):
            last_seen = history_last_seen
            last_paid = history_last_paid
        due = parse_date(row[10])
        if due is None:
            due = due_from_anchor(
                last_seen=last_seen,
                frequency=frequency,
                anchor_day=row[7],
                anchor_month=row[8],
                anchor_mode=row[9],
                today=today,
            )
        while due is not None and due < today:
            due = advance_recurring_date(due, frequency)
        if due is None or due > window_end:
            continue
        days_until = (due - today).days
        if days_until < 0:
            schedule_status = "overdue"
        elif days_until <= 7:
            schedule_status = "due_soon"
        else:
            schedule_status = "expected"
        if state == "candidate":
            schedule_status = "candidate"
        group = upcoming_group(row[3] or merchant_key, row[4])
        evidence_occurrences = int(evidence.get("occurrences") or evidence.get("charge_count") or 0)
        seen_count = int(history.get("seen_count") if history else evidence_occurrences)
        evidence_amounts = _float_list(evidence.get("amounts"))
        amount_min = history.get("amount_min")
        amount_max = history.get("amount_max")
        if amount_min is None:
            amount_min = round(min(evidence_amounts), 2) if evidence_amounts else stored_amount
        if amount_max is None:
            amount_max = round(max(evidence_amounts), 2) if evidence_amounts else stored_amount
        amount, forecast_amount = _variable_bill_forecast_amount(
            stored_amount=stored_amount,
            category=row[4],
            group_key=group["key"],
            history=history,
        )
        monthly = annualize_amount(amount, frequency) / 12
        source = row[12] or "algorithm"
        if confirmed:
            source_label = "User confirmed" if source == "user" else "Confirmed"
            confirmed_upcoming_total += amount
        elif state == "candidate" or score < 55:
            source_label = "Needs review"
            needs_review_total += amount
        else:
            source_label = "Inferred"
            inferred_upcoming_total += amount
        amount_review = history.get("amount_review")
        dismissal = (
            amount_review_dismissals.get((profile_id, merchant_key))
            or amount_review_dismissals.get((profile_id, canonical_key(stored_merchant_key)))
        )
        if _amount_review_matches_dismissal(amount_review, dismissal):
            amount_review = None
        items.append({
            "merchant": row[3] or merchant_key,
            "category": row[4] or "Subscriptions",
            "group": group["key"],
            "group_label": group["label"],
            "amount": amount,
            "monthly_equivalent": round(monthly, 2),
            "frequency": frequency,
            "next_date": due.isoformat(),
            "days_until": days_until,
            "status": schedule_status,
            "state": state,
            "last_charge": last_paid,
            "profile": profile_id,
            "source": source,
            "source_label": source_label,
            "confidence": label,
            "confidence_score": score,
            "confirmed": confirmed,
            "charge_count": seen_count,
            "obligation_key": obligation_key,
            "merchant_key": merchant_key,
            "evidence": {
                **evidence,
                "source_label": source_label,
                "seen_count": seen_count,
                "historical_seen_count": history.get("historical_seen_count", seen_count),
                "restart_detected": bool(history.get("restart_detected")),
                "history_gap_days": history.get("history_gap_days"),
                "last_paid": last_paid,
                "amount_min": amount_min,
                "amount_max": amount_max,
                "forecast_amount": forecast_amount,
                "prior_transactions": history.get("prior_transactions") or [],
                "adjustments": history.get("adjustments") or [],
                "current_cycle_charge_count": history.get("current_cycle_charge_count") or 0,
                "stale": False,
            },
            "recent_transactions": history.get("recent_transactions") or [],
            "amount_review": amount_review,
        })

    items.sort(key=lambda item: (item["next_date"], item["merchant"], item.get("profile") or ""))
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item.get("group") or "other"
        groups.setdefault(key, {
            "key": key,
            "label": item.get("group_label") or "Other",
            "count": 0,
            "scheduled_count": 0,
            "amount": 0.0,
            "monthly_equivalent": 0.0,
        })
        groups[key]["count"] += 1
        groups[key]["scheduled_count"] += 1
        groups[key]["amount"] += item["amount"]
        groups[key]["monthly_equivalent"] += item["monthly_equivalent"]
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
        "scheduled_count": len(items),
        "needs_date_count": 0,
        "due_soon_count": len([item for item in items if item["status"] in {"overdue", "due_soon"}]),
        "upcoming_total": round(confirmed_upcoming_total, 2),
        "confirmed_upcoming_total": round(confirmed_upcoming_total, 2),
        "inferred_upcoming_total": round(inferred_upcoming_total, 2),
        "needs_review_total": round(needs_review_total, 2),
        "monthly_equivalent_total": round(sum(item["monthly_equivalent"] for item in items), 2),
        "groups": group_summary,
    }
