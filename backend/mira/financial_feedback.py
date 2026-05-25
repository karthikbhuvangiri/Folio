"""User feedback store for Mira's financial understanding layer.

Phase 28 keeps feedback separate from deterministic finance facts. Feedback can
change ranking, framing, and surfacing; it must not change transaction math.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


FEEDBACK_TYPES = {
    "accepted",
    "dismissed",
    "corrected",
    "snoozed",
    "too_sensitive",
    "more_like_this",
    "less_like_this",
}
NORMALIZED_EFFECTS = {
    "acknowledge",
    "suppress",
    "downrank",
    "uprank",
    "reframe",
    "promote_to_memory",
    "update_operating_preference",
}
TARGET_TYPES = {
    "fact",
    "insight",
    "advisor_card",
    "advisor_read",
    "category",
    "merchant",
    "profile",
    "cashflow",
    "account",
}
SUBJECT_TYPES = {"profile", "category", "merchant", "subscription", "account", "cashflow", "advisor_read"}
SOURCES = {"chat", "dashboard", "memory_ui", "proactive_card", "advisor_read"}
SENSITIVITY_STATES = {"low", "medium", "high"}
STATUS_STATES = {"active", "cleared"}


def financial_feedback_loop_enabled() -> bool:
    return os.getenv("MIRA_FINANCIAL_FEEDBACK_LOOP_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}


def ensure_financial_feedback_tables(conn: sqlite3.Connection) -> None:
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


def record_financial_feedback(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    feedback: dict[str, Any],
) -> dict[str, Any]:
    ensure_financial_feedback_tables(conn)
    clean = _normalize_feedback(profile, feedback)
    cur = conn.execute(
        """
        INSERT INTO mira_financial_feedback (
            profile_id, target_type, target_id, fact_id, insight_id, subject_type,
            subject_key, feedback_type, correction_text, normalized_effect,
            safe_summary, sensitivity, source, metadata_json, expires_at, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            clean["profile_id"],
            clean["target_type"],
            clean["target_id"],
            clean.get("fact_id"),
            clean.get("insight_id"),
            clean["subject_type"],
            clean["subject_key"],
            clean["feedback_type"],
            clean["correction_text"],
            clean["normalized_effect"],
            clean["safe_summary"],
            clean["sensitivity"],
            clean["source"],
            json.dumps(clean.get("metadata") or {}, ensure_ascii=True, sort_keys=True),
            clean.get("expires_at") or None,
        ),
    )
    conn.commit()
    return get_financial_feedback(conn=conn, profile=profile, feedback_id=int(cur.lastrowid)) or {}


def get_financial_feedback(*, conn: sqlite3.Connection, profile: str | None, feedback_id: int) -> dict[str, Any] | None:
    ensure_financial_feedback_tables(conn)
    row = conn.execute(
        """
        SELECT *
          FROM mira_financial_feedback
         WHERE id = ?
           AND profile_id = ?
         LIMIT 1
        """,
        (int(feedback_id), _profile_scope(profile)),
    ).fetchone()
    return _public_feedback(dict(row)) if row else None


def list_financial_feedback(
    *,
    conn: sqlite3.Connection,
    profile: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    include_cleared: bool = False,
    include_expired: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_financial_feedback_tables(conn)
    where = ["profile_id = ?"]
    params: list[Any] = [_profile_scope(profile)]
    if not include_cleared:
        where.append("status = 'active'")
    if not include_expired:
        where.append("(expires_at IS NULL OR expires_at > datetime('now'))")
    if target_type:
        where.append("target_type = ?")
        params.append(_enum(target_type, TARGET_TYPES, "advisor_read"))
    if target_id:
        where.append("target_id = ?")
        params.append(_clean_text(target_id, 120))
    if subject_type:
        where.append("subject_type = ?")
        params.append(_enum(subject_type, SUBJECT_TYPES, "profile"))
    if subject_key:
        where.append("subject_key = ?")
        params.append(_key(subject_key))
    rows = conn.execute(
        f"""
        SELECT *
          FROM mira_financial_feedback
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        [*params, max(1, min(int(limit or 50), 200))],
    ).fetchall()
    return [_public_feedback(dict(row)) for row in rows]


def clear_financial_feedback(*, conn: sqlite3.Connection, profile: str | None, feedback_id: int) -> dict[str, Any] | None:
    ensure_financial_feedback_tables(conn)
    scope = _profile_scope(profile)
    existing = get_financial_feedback(conn=conn, profile=scope, feedback_id=feedback_id)
    if not existing:
        return None
    conn.execute(
        """
        UPDATE mira_financial_feedback
           SET status = 'cleared'
         WHERE id = ?
           AND profile_id = ?
        """,
        (int(feedback_id), scope),
    )
    conn.commit()
    return get_financial_feedback(conn=conn, profile=scope, feedback_id=feedback_id)


def feedback_effect_summary(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    subject_type: str | None = None,
    subject_key: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    rows = list_financial_feedback(
        conn=conn,
        profile=profile,
        subject_type=subject_type,
        subject_key=subject_key,
        target_type=target_type,
        target_id=target_id,
        limit=200,
    )
    effects: dict[str, int] = {}
    types: dict[str, int] = {}
    sensitivities: dict[str, int] = {}
    summaries: list[str] = []
    for row in rows:
        effects[row["normalized_effect"]] = effects.get(row["normalized_effect"], 0) + 1
        types[row["feedback_type"]] = types.get(row["feedback_type"], 0) + 1
        sensitivities[row["sensitivity"]] = sensitivities.get(row["sensitivity"], 0) + 1
        safe_summary = str(row.get("safe_summary") or "").strip()
        if safe_summary and safe_summary not in summaries:
            summaries.append(safe_summary)
    return {
        "count": len(rows),
        "effects": effects,
        "feedback_types": types,
        "sensitivities": sensitivities,
        "safe_summaries": summaries[:6],
    }


def feedback_adjustments_for_subjects(
    *,
    conn: sqlite3.Connection,
    profile: str | None,
    subjects: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not subjects:
        return {}
    wanted = {(_enum(subject_type, SUBJECT_TYPES, "profile"), _key(subject_key)) for subject_type, subject_key in subjects}
    rows = list_financial_feedback(conn=conn, profile=profile, limit=200)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("subject_type") or ""), str(row.get("subject_key") or ""))
        if key not in wanted:
            continue
        item = grouped.setdefault(
            key,
            {
                "count": 0,
                "effects": {},
                "feedback_types": {},
                "safe_summaries": [],
                "sensitivity": "low",
                "suppress": False,
                "downrank": False,
                "uprank": False,
                "reframe": False,
                "too_sensitive": False,
            },
        )
        item["count"] += 1
        effect = str(row.get("normalized_effect") or "")
        feedback_type = str(row.get("feedback_type") or "")
        item["effects"][effect] = item["effects"].get(effect, 0) + 1
        item["feedback_types"][feedback_type] = item["feedback_types"].get(feedback_type, 0) + 1
        if effect == "suppress":
            item["suppress"] = True
        if effect == "downrank":
            item["downrank"] = True
        if effect == "uprank":
            item["uprank"] = True
        if effect == "reframe":
            item["reframe"] = True
        if feedback_type == "too_sensitive":
            item["too_sensitive"] = True
            item["sensitivity"] = "high"
        elif row.get("sensitivity") == "medium" and item["sensitivity"] != "high":
            item["sensitivity"] = "medium"
        safe_summary = str(row.get("safe_summary") or "").strip()
        if safe_summary and safe_summary not in item["safe_summaries"]:
            item["safe_summaries"].append(safe_summary)
    for item in grouped.values():
        item["safe_summaries"] = item["safe_summaries"][:4]
    return grouped


def feedback_memory_candidate(feedback: dict[str, Any]) -> dict[str, Any] | None:
    """Build a visible memory-entry candidate from explicit feedback.

    The caller still decides whether to write it. This function intentionally
    uses safe summaries/metadata instead of raw correction text.
    """
    if not isinstance(feedback, dict) or feedback.get("status") != "active":
        return None
    feedback_type = _enum(feedback.get("feedback_type"), FEEDBACK_TYPES, "")
    if not feedback_type:
        return None
    metadata = feedback.get("metadata") if isinstance(feedback.get("metadata"), dict) else {}
    subject = _clean_text(metadata.get("card_title"), 80) or _human_subject(feedback.get("subject_type") or "", feedback.get("subject_key") or feedback.get("target_id"))
    if feedback_type == "corrected":
        safe_summary = _clean_text(feedback.get("safe_summary"), 180)
        body = safe_summary if safe_summary.lower().startswith("user ") else f"User corrected Mira's financial framing for {subject}."
        section = "preferences"
    elif feedback_type == "too_sensitive":
        body = f"User wants Mira to treat {subject} as sensitive unless directly requested."
        section = "concerns"
    elif feedback_type in {"less_like_this", "dismissed"}:
        body = f"User wants fewer financial reads like {subject}."
        section = "preferences"
    elif feedback_type in {"more_like_this", "accepted"}:
        body = f"User wants more financial reads like {subject}."
        section = "preferences"
    elif feedback_type == "snoozed":
        body = f"User temporarily snoozed Mira financial reads about {subject}."
        section = "preferences"
    else:
        return None
    return {
        "section": section,
        "body": _clean_text(body, 220),
        "confidence": "stated",
        "evidence": f"Mira financial feedback #{feedback.get('id')}",
        "theme": f"mira_financial_feedback:{feedback_type}",
    }


def _normalize_feedback(profile: str | None, feedback: dict[str, Any]) -> dict[str, Any]:
    feedback_type = _enum(feedback.get("feedback_type"), FEEDBACK_TYPES, "")
    if not feedback_type:
        raise ValueError("feedback_type is required.")
    correction_text = _clean_text(feedback.get("correction_text"), 600)
    if feedback_type == "corrected" and not correction_text:
        raise ValueError("correction_text is required for corrected feedback.")
    normalized_effect = _enum(feedback.get("normalized_effect"), NORMALIZED_EFFECTS, "")
    if not normalized_effect:
        normalized_effect = _default_effect(feedback_type)
    target_type = _enum(feedback.get("target_type"), TARGET_TYPES, "advisor_read")
    subject_type = _enum(feedback.get("subject_type"), SUBJECT_TYPES, "profile")
    expires_at = _clean_datetime(feedback.get("expires_at"))
    if feedback_type == "snoozed" and not expires_at:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=14)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    safe_summary = _clean_text(feedback.get("safe_summary"), 240)
    if not safe_summary:
        safe_summary = _default_safe_summary(feedback_type, subject_type, feedback.get("subject_key"))
    metadata = feedback.get("metadata") if isinstance(feedback.get("metadata"), dict) else {}
    return {
        "profile_id": _profile_scope(profile),
        "target_type": target_type,
        "target_id": _clean_text(feedback.get("target_id"), 120),
        "fact_id": _optional_int(feedback.get("fact_id")),
        "insight_id": _optional_int(feedback.get("insight_id")),
        "subject_type": subject_type,
        "subject_key": _key(feedback.get("subject_key")),
        "feedback_type": feedback_type,
        "correction_text": correction_text,
        "normalized_effect": normalized_effect,
        "safe_summary": safe_summary,
        "sensitivity": _enum(feedback.get("sensitivity"), SENSITIVITY_STATES, _sensitivity_for_feedback(feedback_type, correction_text)),
        "source": _enum(feedback.get("source"), SOURCES, "chat"),
        "metadata": _safe_metadata(metadata),
        "expires_at": expires_at,
    }


def _public_feedback(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "metadata": _json_load(row.get("metadata_json"), {}),
    }


def _default_effect(feedback_type: str) -> str:
    return {
        "accepted": "uprank",
        "dismissed": "downrank",
        "corrected": "reframe",
        "snoozed": "suppress",
        "too_sensitive": "suppress",
        "more_like_this": "uprank",
        "less_like_this": "downrank",
    }.get(feedback_type, "acknowledge")


def _default_safe_summary(feedback_type: str, subject_type: str, subject_key: Any) -> str:
    subject = _human_subject(subject_type, subject_key)
    if feedback_type == "corrected":
        return f"User correction recorded for {subject}."
    if feedback_type == "too_sensitive":
        return f"Treat {subject} as sensitive unless directly requested."
    if feedback_type == "more_like_this":
        return f"User wants more reads like this for {subject}."
    if feedback_type == "less_like_this":
        return f"User wants fewer reads like this for {subject}."
    if feedback_type == "snoozed":
        return f"Temporarily suppress this read for {subject}."
    if feedback_type == "dismissed":
        return f"Down-rank this read for {subject}."
    return f"User accepted this read for {subject}."


def _sensitivity_for_feedback(feedback_type: str, correction_text: str) -> str:
    lowered = correction_text.lower()
    if feedback_type == "too_sensitive":
        return "high"
    if any(term in lowered for term in ("vaping", "alcohol", "gambling", "medical", "debt", "addiction")):
        return "high"
    return "low"


def _human_subject(subject_type: str, subject_key: Any) -> str:
    key = " ".join(str(subject_key or "").replace("_", " ").split())
    if not key:
        return subject_type.replace("_", " ")
    return key[:80]


def _safe_metadata(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        clean_key = _key(key)
        if not clean_key:
            continue
        if isinstance(value, bool):
            out[clean_key] = value
        elif isinstance(value, (int, float)):
            out[clean_key] = round(float(value), 4)
        elif isinstance(value, str):
            out[clean_key] = _clean_text(value, 160)
        elif isinstance(value, list):
            out[clean_key] = [_clean_text(item, 80) for item in value[:8] if str(item).strip()]
    return out


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("id fields must be integers.")
    return parsed if parsed > 0 else None


def _clean_datetime(value: Any) -> str:
    text = _clean_text(value, 40)
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("expires_at must be ISO-8601.")
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _profile_scope(profile: str | None) -> str:
    return str(profile or "household").strip() or "household"


def _key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:100] or "profile"


def _enum(value: Any, allowed: set[str], default: str) -> str:
    text = _key(value)
    return text if text in allowed else default


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json_load(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return default
