from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any, Iterable

from range_parser import words


GROUND_KINDS = {"exact", "approximate", "ambiguous", "missing"}
ENTITY_TYPES = {"merchant", "category", "account", "transaction", "goal", "recurring"}

STOP_MERCHANT_WORDS = {"and", "the", "of", "for", "at", "to", "in", "on", "my"}
STOP_QUERY_WORDS = STOP_MERCHANT_WORDS | {
    "a", "about", "an", "all", "can", "could", "did", "do", "does", "how", "i", "is",
    "last", "me", "month", "months", "much", "same", "show", "spend", "spending",
    "spent", "this", "transaction", "transactions", "versus", "vs", "what",
    "you", "compare", "compared", "current", "past", "previous", "prior",
    "year", "paid", "pay", "charges", "charge", "expenses", "expense",
    "money", "wasted", "list", "display", "find", "pull", "please", "that",
    "then", "there",
}

CATEGORY_SYNONYMS = {
    "food": "Food & Dining",
    "grocery": "Groceries",
    "groceries": "Groceries",
    "dining": "Food & Dining",
    "foodanddining": "Food & Dining",
    "food and dining": "Food & Dining",
    "food dining": "Food & Dining",
    "restaurant": "Food & Dining",
    "restaurants": "Food & Dining",
    "shopping": "Shopping",
    "subscription": "Subscriptions",
    "subscriptions": "Subscriptions",
    "tax": "Taxes",
    "taxes": "Taxes",
    "rent": "Housing",
    "medical": "Healthcare",
    "health": "Healthcare",
    "healthcare": "Healthcare",
}

BROAD_CATEGORY_NAMES = {
    "groceries",
    "food & dining",
    "dining",
    "restaurant",
    "restaurants",
    "subscriptions",
    "subscription",
    "shopping",
    "travel",
    "utilities",
    "housing",
    "rent",
    "healthcare",
    "medical",
    "transportation",
    "taxes",
    "tax",
    "income",
    "entertainment",
}


@dataclass(frozen=True)
class GroundCandidate:
    id: str
    entity_type: str
    value: str
    canonical_id: str
    display_name: str
    confidence: float
    match_type: str
    matched_text: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity_type": self.entity_type,
            "value": self.value,
            "canonical_id": self.canonical_id,
            "display_name": self.display_name,
            "confidence": round(float(self.confidence), 4),
            "match_type": self.match_type,
            "matched_text": self.matched_text,
            "evidence": dict(self.evidence or {}),
        }


@dataclass(frozen=True)
class GroundResult:
    kind: str
    entity_type: str
    value: str | None
    canonical_id: str | None
    display_name: str | None
    confidence: float
    candidates: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    rejected_candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entity_type": self.entity_type,
            "value": self.value,
            "canonical_id": self.canonical_id,
            "display_name": self.display_name,
            "confidence": round(float(self.confidence), 4),
            "candidates": list(self.candidates or []),
            "evidence": dict(self.evidence or {}),
            "rejected_candidates": list(self.rejected_candidates or []),
        }


@dataclass(frozen=True)
class _Entity:
    entity_type: str
    display_name: str
    canonical_id: str
    aliases: tuple[str, ...] = ()
    frequency: int = 0
    latest_date: str = ""
    description_blob: str = ""


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def compact_token_text(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def canonical_key(text: str) -> str:
    return " ".join(token for token in normalize_text(text).split() if token != "and")


def significant_tokens(text: str, *, query: bool = False) -> list[str]:
    stop = STOP_QUERY_WORDS if query else STOP_MERCHANT_WORDS
    return [token for token in words(text) if token and token not in stop]


def _contains_phrase(normalized_haystack: str, phrase: str) -> bool:
    normalized = normalize_text(phrase)
    if not normalized:
        return False
    return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", normalized_haystack) is not None


def _safe_canonical_merchant_key(value: str) -> str:
    try:
        from merchant_identity import canonicalize_merchant_key

        return canonicalize_merchant_key(value) or value
    except Exception:
        return compact_token_text(value).upper()


def _entity_id(entity_type: str, canonical_id: str, display_name: str) -> str:
    raw = canonical_id or display_name
    normalized = compact_token_text(raw) or compact_token_text(display_name) or "unknown"
    return f"{entity_type}:{normalized}"


def _entities_from_names(entity_type: str, names: Iterable[str] | None) -> list[_Entity]:
    entities: list[_Entity] = []
    seen: set[str] = set()
    source_names = [str(item) for item in (names or [])]
    if entity_type == "category":
        for category in CATEGORY_SYNONYMS.values():
            if category not in source_names:
                source_names.append(category)
    for raw in source_names:
        display = str(raw or "").strip()
        if not display:
            continue
        canonical_id = _safe_canonical_merchant_key(display) if entity_type == "merchant" else display
        key = f"{entity_type}:{canonical_id.lower()}:{display.lower()}"
        if key in seen:
            continue
        seen.add(key)
        entities.append(_Entity(entity_type=entity_type, display_name=display, canonical_id=canonical_id))
    return entities


def _profile_clause(profile: str | None) -> tuple[str, list[Any]]:
    if profile and profile != "household":
        return " AND profile_id = ?", [profile]
    return "", []


def load_entities(entity_type: str, profile: str | None = None) -> list[_Entity]:
    def _load_uncached() -> list[_Entity]:
        if entity_type == "merchant":
            return _load_merchant_entities(profile)
        if entity_type == "category":
            return _load_category_entities(profile)
        return []

    try:
        import copilot_cache

        fingerprint = _data_fingerprint(profile)
        return copilot_cache.get_entity_index(entity_type, profile, fingerprint, _load_uncached)
    except Exception:
        return _load_uncached()


def _data_fingerprint(profile: str | None) -> str:
    try:
        import copilot_cache
        from database import get_db

        with get_db() as conn:
            return copilot_cache.db_fingerprint(conn, profile)
    except Exception:
        return "unknown"


def _load_entities_uncached(entity_type: str, profile: str | None = None) -> list[_Entity]:
    if entity_type == "merchant":
        return _load_merchant_entities(profile)
    if entity_type == "category":
        return _load_category_entities(profile)
    return []


def _load_merchant_entities(profile: str | None = None) -> list[_Entity]:
    try:
        from database import get_db
    except Exception:
        return []

    p_clause, p_params = _profile_clause(profile)
    where = f"""
        COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, '')) IS NOT NULL
        AND COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, '')) != ''
        AND amount < 0
        AND is_excluded = 0
        AND category NOT IN ('Savings Transfer','Personal Transfer','Credit Card Payment','Income','Credits & Refunds')
        AND (expense_type IS NULL OR expense_type NOT IN ('transfer_internal','transfer_household'))
        {p_clause}
    """
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(merchant_name, ''), merchant_key) AS display_name,
                    COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, '')) AS canonical_id,
                    COUNT(*) AS frequency,
                    MAX(date) AS latest_date,
                    GROUP_CONCAT(
                        COALESCE(description, '') || ' ' || COALESCE(raw_description, ''),
                        ' '
                    ) AS description_blob
                FROM transactions_visible
                WHERE {where}
                GROUP BY COALESCE(NULLIF(merchant_key, ''), NULLIF(merchant_name, ''))
                ORDER BY frequency DESC, LENGTH(display_name) DESC, display_name
                """,
                p_params,
            ).fetchall()
    except Exception:
        return []

    entities: list[_Entity] = []
    for row in rows:
        display = str(row["display_name"] if hasattr(row, "keys") else row[0] or "").strip()
        if not display or len(normalize_text(display)) < 3:
            continue
        canonical = str(row["canonical_id"] if hasattr(row, "keys") else row[1] or "").strip()
        if not canonical:
            canonical = _safe_canonical_merchant_key(display)
        entities.append(
            _Entity(
                entity_type="merchant",
                display_name=display,
                canonical_id=canonical,
                frequency=int((row["frequency"] if hasattr(row, "keys") else row[2]) or 0),
                latest_date=str((row["latest_date"] if hasattr(row, "keys") else row[3]) or ""),
                description_blob=str((row["description_blob"] if hasattr(row, "keys") else row[4]) or ""),
            )
        )
    return entities


def _load_category_entities(profile: str | None = None) -> list[_Entity]:
    try:
        from database import get_db
    except Exception:
        return []

    p_clause, p_params = _profile_clause(profile)
    by_name: dict[str, _Entity] = {}
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT category, COUNT(*) AS frequency, MAX(date) AS latest_date
                FROM transactions_visible
                WHERE category IS NOT NULL AND category != ''{p_clause}
                GROUP BY category
                ORDER BY frequency DESC, category
                """,
                p_params,
            ).fetchall()
            for row in rows:
                name = str(row["category"] if hasattr(row, "keys") else row[0] or "").strip()
                if not name:
                    continue
                by_name[name.lower()] = _Entity(
                    entity_type="category",
                    display_name=name,
                    canonical_id=name,
                    frequency=int((row["frequency"] if hasattr(row, "keys") else row[1]) or 0),
                    latest_date=str((row["latest_date"] if hasattr(row, "keys") else row[2]) or ""),
                )
            try:
                category_rows = conn.execute("SELECT name FROM categories WHERE COALESCE(name, '') != ''").fetchall()
            except Exception:
                category_rows = []
            for row in category_rows:
                name = str(row["name"] if hasattr(row, "keys") else row[0] or "").strip()
                if name and name.lower() not in by_name:
                    by_name[name.lower()] = _Entity(entity_type="category", display_name=name, canonical_id=name)
    except Exception:
        return []
    return list(by_name.values())


def _merge_transaction_evidence(entities: list[_Entity], entity_type: str, profile: str | None) -> list[_Entity]:
    if entity_type != "merchant" or not entities:
        return entities
    loaded = load_entities("merchant", profile)
    if not loaded:
        return entities
    by_display = {normalize_text(entity.display_name): entity for entity in loaded}
    by_canonical = {str(entity.canonical_id).lower(): entity for entity in loaded}
    merged: list[_Entity] = []
    for entity in entities:
        enriched = by_canonical.get(str(entity.canonical_id).lower()) or by_display.get(normalize_text(entity.display_name))
        if not enriched:
            merged.append(entity)
            continue
        merged.append(
            _Entity(
                entity_type=entity.entity_type,
                display_name=entity.display_name,
                canonical_id=enriched.canonical_id or entity.canonical_id,
                aliases=entity.aliases,
                frequency=enriched.frequency,
                latest_date=enriched.latest_date,
                description_blob=enriched.description_blob,
            )
        )
    return merged


def _merchant_nicknames(name: str) -> set[str]:
    tokens = significant_tokens(name)
    nicknames: set[str] = set()
    if not tokens:
        return nicknames
    if len(tokens) == 1:
        token = tokens[0]
        for size in (3, 4, 5):
            if len(token) >= size:
                nicknames.add(token[:size])
        return nicknames

    nicknames.add("".join(token[0] for token in tokens if token))
    first, second = tokens[0], tokens[1]
    for size in (3, 4, 5):
        if len(first) >= size:
            nicknames.add(first[:size])
    for first_size in (2, 3, 4):
        for second_size in (1, 2, 3):
            if len(first) >= first_size and len(second) >= second_size:
                nicknames.add(first[:first_size] + second[:second_size])
    return {item for item in nicknames if len(item) >= 2}


def _char_ngrams(text: str, size: int = 3) -> set[str]:
    clean = compact_token_text(text)
    if not clean:
        return set()
    if len(clean) <= size:
        return {clean}
    return {clean[idx:idx + size] for idx in range(len(clean) - size + 1)}


def _ngram_similarity(left: str, right: str) -> float:
    a = _char_ngrams(left)
    b = _char_ngrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _soundex_token(token: str) -> str:
    token = re.sub(r"[^a-z]", "", (token or "").lower())
    if not token:
        return ""
    first = token[0].upper()
    groups = {
        "b": "1", "f": "1", "p": "1", "v": "1",
        "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2", "x": "2", "z": "2",
        "d": "3", "t": "3",
        "l": "4",
        "m": "5", "n": "5",
        "r": "6",
    }
    digits: list[str] = []
    previous = groups.get(token[0], "")
    for char in token[1:]:
        digit = groups.get(char, "")
        if digit and digit != previous:
            digits.append(digit)
        previous = digit
    return (first + "".join(digits) + "000")[:4]


def _frequency_boost(entity: _Entity) -> float:
    if entity.frequency <= 0:
        return 0.0
    return min(math.log10(entity.frequency + 1) * 0.015, 0.045)


def _recency_boost(entity: _Entity) -> float:
    if not entity.latest_date:
        return 0.0
    try:
        latest = date.fromisoformat(str(entity.latest_date)[:10])
    except Exception:
        return 0.0
    days = (date.today() - latest).days
    if days <= 45:
        return 0.025
    if days <= 180:
        return 0.015
    if days <= 365:
        return 0.008
    return 0.0


def _score_category_synonym(query_norm: str, query_compact: str, entity: _Entity) -> tuple[float, str, dict[str, Any]]:
    entity_norm = normalize_text(entity.display_name)
    for phrase, category in sorted(CATEGORY_SYNONYMS.items(), key=lambda item: len(item[0]), reverse=True):
        if normalize_text(category) != entity_norm:
            continue
        phrase_norm = normalize_text(phrase)
        phrase_compact = compact_token_text(phrase)
        if _contains_phrase(query_norm, phrase_norm) or (phrase_compact and phrase_compact in query_compact):
            return 1.0, phrase, {"synonym": phrase, "target": category}
    return 0.0, "", {}


def _score_entity(text: str, entity: _Entity) -> GroundCandidate | None:
    query_norm = normalize_text(text)
    query_tokens = significant_tokens(text, query=True)
    if not query_tokens:
        query_tokens = words(text)
    query_set = set(query_tokens)
    query_compact = compact_token_text("".join(query_tokens)) or compact_token_text(text)
    raw_token_compacts = {compact_token_text(token) for token in words(text)}

    name_norm = normalize_text(entity.display_name)
    name_key = canonical_key(entity.display_name)
    name_tokens = significant_tokens(entity.display_name)
    if not name_tokens:
        name_tokens = words(entity.display_name)
    name_set = set(name_tokens)
    name_compact = compact_token_text("".join(name_tokens)) or compact_token_text(entity.display_name)

    best_score = 0.0
    match_type = ""
    matched_text = ""
    evidence: dict[str, Any] = {
        "frequency": entity.frequency,
        "latest_date": entity.latest_date,
    }

    def keep(score: float, kind: str, matched: str, extra: dict[str, Any] | None = None) -> None:
        nonlocal best_score, match_type, matched_text, evidence
        if score > best_score:
            best_score = score
            match_type = kind
            matched_text = matched
            evidence = {**evidence, **(extra or {})}

    if entity.entity_type == "category":
        score, matched, extra = _score_category_synonym(query_norm, query_compact, entity)
        if score:
            keep(score, "synonym", matched, extra)

    if name_norm and _contains_phrase(query_norm, name_norm):
        keep(1.0, "exact", entity.display_name, {"exact": "normalized_phrase"})
    if name_key and name_key != name_norm and _contains_phrase(canonical_key(text), name_key):
        keep(0.99, "exact", entity.display_name, {"exact": "canonical_phrase"})
    if name_set and name_set <= query_set:
        keep(1.0, "exact", entity.display_name, {"exact": "token_subset"})
    if name_compact and len(name_compact) >= 5 and name_compact in query_compact:
        keep(0.98, "exact", entity.display_name, {"exact": "compact_phrase"})

    if entity.entity_type == "merchant":
        nicknames = _merchant_nicknames(entity.display_name)
        nickname_hit = next((token for token in raw_token_compacts if token in nicknames), "")
        if not nickname_hit and query_compact in nicknames:
            nickname_hit = query_compact
        if nickname_hit:
            keep(0.82, "nickname", nickname_hit, {"nickname": nickname_hit})

    if query_tokens and name_tokens:
        overlap = query_set & name_set
        if overlap:
            coverage = len(overlap) / max(1, min(len(query_set), len(name_set)))
            jaccard = len(overlap) / max(1, len(query_set | name_set))
            keep(0.48 + (coverage * 0.26) + (jaccard * 0.12), "token_overlap", " ".join(sorted(overlap)), {
                "overlap_tokens": sorted(overlap),
                "coverage": round(coverage, 4),
                "jaccard": round(jaccard, 4),
            })

        prefix_hits = 0
        for query_token in query_tokens:
            if len(query_token) >= 3 and any(token.startswith(query_token) for token in name_tokens):
                prefix_hits += 1
        if prefix_hits and prefix_hits >= min(2, len(name_tokens)):
            keep(0.74, "partial", " ".join(query_tokens), {"prefix_hits": prefix_hits})

    comparison_strings = {name_compact, compact_token_text(entity.display_name)}
    if entity.entity_type == "merchant":
        comparison_strings.update(_merchant_nicknames(entity.display_name))
    best_ratio = 0.0
    best_token = ""
    for query_token in query_tokens:
        if len(query_token) < 4:
            continue
        for candidate_text in comparison_strings:
            if len(candidate_text) < 4:
                continue
            ratio = SequenceMatcher(None, query_token, candidate_text).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_token = query_token
    if best_ratio >= 0.78:
        keep(min(0.78, best_ratio), "fuzzy", best_token, {"similarity": round(best_ratio, 4)})

    ngram = _ngram_similarity(query_compact, name_compact)
    if ngram >= 0.45:
        keep(min(0.72, 0.48 + ngram * 0.28), "ngram", " ".join(query_tokens), {"ngram_similarity": round(ngram, 4)})

    query_soundex = {_soundex_token(token) for token in query_tokens if len(token) >= 4}
    name_soundex = {_soundex_token(token) for token in name_tokens if len(token) >= 4}
    phonetic_hits = {code for code in query_soundex & name_soundex if code}
    if phonetic_hits:
        keep(0.68, "phonetic", " ".join(query_tokens), {"phonetic": sorted(phonetic_hits)})

    if entity.description_blob and query_tokens:
        desc_norm = normalize_text(entity.description_blob)
        desc_tokens = set(words(entity.description_blob))
        raw_hits = [token for token in query_tokens if len(token) >= 3 and token in desc_tokens]
        if raw_hits and not set(raw_hits) <= name_set:
            coverage = len(raw_hits) / max(1, len(query_tokens))
            score = min(0.88, 0.68 + coverage * 0.16 + min(len(raw_hits), 3) * 0.02)
            keep(score, "raw_description", " ".join(raw_hits), {
                "raw_description_tokens": raw_hits,
                "raw_description_match": True,
            })
        if query_norm and len(query_norm) >= 4 and query_norm in desc_norm and not _contains_phrase(name_norm, query_norm):
            keep(0.86, "raw_description", query_norm, {"raw_description_phrase": True})

    if best_score <= 0:
        return None

    if best_score < 0.98:
        best_score = min(0.97, best_score + _frequency_boost(entity) + _recency_boost(entity))

    candidate_id = _entity_id(entity.entity_type, entity.canonical_id, entity.display_name)
    return GroundCandidate(
        id=candidate_id,
        entity_type=entity.entity_type,
        value=entity.display_name,
        canonical_id=entity.canonical_id,
        display_name=entity.display_name,
        confidence=max(0.0, min(best_score, 1.0)),
        match_type=match_type or "unknown",
        matched_text=matched_text,
        evidence=evidence,
    )


def _short_merchant_family_ambiguity(
    text: str,
    top: GroundCandidate,
    ranked: list[GroundCandidate],
    limit: int,
) -> list[GroundCandidate]:
    query_tokens = significant_tokens(text, query=True)
    if len(query_tokens) != 1:
        return []
    token = query_tokens[0]
    top_tokens = set(significant_tokens(top.display_name) or words(top.display_name))
    if token not in top_tokens:
        return []

    family: list[GroundCandidate] = []
    seen: set[str] = set()
    for candidate in ranked:
        if candidate.entity_type != "merchant":
            continue
        key = normalize_text(candidate.display_name)
        if not key or key in seen:
            continue
        candidate_tokens = set(significant_tokens(candidate.display_name) or words(candidate.display_name))
        if token not in candidate_tokens:
            continue
        if candidate is not top and candidate.confidence < 0.74:
            continue
        seen.add(key)
        family.append(candidate)
        if len(family) >= limit:
            break
    if len(family) <= 1:
        return []
    family.sort(key=lambda item: (item.confidence, item.evidence.get("frequency") or 0), reverse=True)
    return family[:limit]


def ground_text(
    text: str,
    entity_type: str,
    names: Iterable[str] | None = None,
    *,
    profile: str | None = None,
    rejected_candidates: Iterable[str] | None = None,
    include_transaction_evidence: bool = False,
    limit: int = 8,
    approximate_threshold: float = 0.62,
) -> GroundResult:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unsupported entity_type: {entity_type}")
    rejected = [str(item).strip() for item in (rejected_candidates or []) if str(item or "").strip()]
    rejected_norm = {normalize_text(item) for item in rejected}

    entities = _entities_from_names(entity_type, names) if names is not None else load_entities(entity_type, profile)
    if entity_type == "category":
        existing = {normalize_text(entity.display_name) for entity in entities}
        for fallback in _entities_from_names("category", CATEGORY_SYNONYMS.values()):
            if normalize_text(fallback.display_name) not in existing:
                entities.append(fallback)
                existing.add(normalize_text(fallback.display_name))
    if include_transaction_evidence:
        entities = _merge_transaction_evidence(entities, entity_type, profile)
    if not entities and names is None:
        entities = []

    scored: dict[str, GroundCandidate] = {}
    for entity in entities:
        if normalize_text(entity.display_name) in rejected_norm or normalize_text(entity.canonical_id) in rejected_norm:
            continue
        candidate = _score_entity(text, entity)
        if not candidate:
            continue
        current = scored.get(candidate.id)
        if current is None or candidate.confidence > current.confidence:
            scored[candidate.id] = candidate

    ranked = sorted(
        scored.values(),
        key=lambda item: (
            item.confidence,
            item.evidence.get("frequency") or 0,
            len(normalize_text(item.display_name)),
        ),
        reverse=True,
    )
    candidate_dicts = [candidate.as_dict() for candidate in ranked[:limit]]

    if not ranked or ranked[0].confidence < 0.45:
        return GroundResult(
            kind="missing",
            entity_type=entity_type,
            value=None,
            canonical_id=None,
            display_name=None,
            confidence=0.0,
            candidates=candidate_dicts,
            evidence={"query": text},
            rejected_candidates=rejected,
        )

    top = ranked[0]
    exact_matches = [
        candidate for candidate in ranked
        if candidate.confidence >= 0.98 and candidate.match_type in {"exact", "synonym"}
    ]
    if exact_matches:
        if entity_type == "category":
            direct = [candidate for candidate in exact_matches if candidate.match_type == "exact"]
            if direct:
                exact_matches = direct
        unique = []
        seen_values = set()
        for candidate in exact_matches:
            key = (candidate.entity_type, normalize_text(candidate.display_name))
            if key not in seen_values:
                unique.append(candidate)
                seen_values.add(key)
        if entity_type == "merchant":
            specific = _most_specific_exact_merchant_matches(text, unique)
            if specific:
                unique = specific
        if len(unique) > 1:
            return GroundResult(
                kind="ambiguous",
                entity_type=entity_type,
                value=None,
                canonical_id=None,
                display_name=None,
                confidence=unique[0].confidence,
                candidates=[candidate.as_dict() for candidate in unique[:limit]],
                evidence={"query": text, "reason": "multiple exact matches"},
                rejected_candidates=rejected,
            )
        top = unique[0]
        if entity_type == "merchant":
            family = _short_merchant_family_ambiguity(text, top, ranked, limit)
            if family:
                return GroundResult(
                    kind="ambiguous",
                    entity_type=entity_type,
                    value=None,
                    canonical_id=None,
                    display_name=None,
                    confidence=family[0].confidence,
                    candidates=[candidate.as_dict() for candidate in family],
                    evidence={"query": text, "reason": "short merchant name matched multiple live merchants"},
                    rejected_candidates=rejected,
                )
        return GroundResult(
            kind="exact",
            entity_type=entity_type,
            value=top.value,
            canonical_id=top.canonical_id,
            display_name=top.display_name,
            confidence=top.confidence,
            candidates=candidate_dicts,
            evidence=top.evidence,
            rejected_candidates=rejected,
        )

    close = [
        candidate for candidate in ranked
        if candidate.confidence >= max(approximate_threshold, top.confidence - 0.03)
    ]
    if len(close) > 1 and top.confidence < 0.9:
        return GroundResult(
            kind="ambiguous",
            entity_type=entity_type,
            value=None,
            canonical_id=None,
            display_name=None,
            confidence=top.confidence,
            candidates=[candidate.as_dict() for candidate in close[:limit]],
            evidence={"query": text, "reason": "close candidate scores"},
            rejected_candidates=rejected,
        )

    if top.confidence >= approximate_threshold:
        return GroundResult(
            kind="approximate",
            entity_type=entity_type,
            value=top.value,
            canonical_id=top.canonical_id,
            display_name=top.display_name,
            confidence=top.confidence,
            candidates=candidate_dicts,
            evidence=top.evidence,
            rejected_candidates=rejected,
        )

    return GroundResult(
        kind="missing",
        entity_type=entity_type,
        value=None,
        canonical_id=None,
        display_name=None,
        confidence=0.0,
        candidates=candidate_dicts,
        evidence={"query": text, "reason": "below confidence threshold"},
        rejected_candidates=rejected,
    )


def _most_specific_exact_merchant_matches(
    text: str,
    candidates: list[GroundCandidate],
) -> list[GroundCandidate]:
    query_tokens = set(significant_tokens(text, query=True) or words(text))
    if not query_tokens:
        return []
    covered: list[tuple[int, GroundCandidate]] = []
    for candidate in candidates:
        name_tokens = set(significant_tokens(candidate.display_name) or words(candidate.display_name))
        if name_tokens and name_tokens <= query_tokens:
            covered.append((len(name_tokens), candidate))
    if not covered:
        return []
    max_size = max(size for size, _candidate in covered)
    specific = [candidate for size, candidate in covered if size == max_size]
    return specific if len(specific) == 1 else []


def _names_digest(names: Iterable[str] | None) -> str:
    if names is None:
        return "live"
    values = [str(item) for item in names]
    raw = json.dumps(values, sort_keys=False, ensure_ascii=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cached_ground_result(
    *,
    entity_type: str,
    text: str,
    names: Iterable[str] | None,
    profile: str | None,
    rejected_candidates: Iterable[str] | None,
    include_transaction_evidence: bool,
    limit: int,
    factory,
) -> GroundResult:
    rejected = [str(item).strip() for item in (rejected_candidates or []) if str(item or "").strip()]
    fingerprint = _data_fingerprint(profile) if names is None or include_transaction_evidence else ""
    try:
        import copilot_cache

        key = copilot_cache.make_key(
            entity_type,
            normalize_text(text),
            _names_digest(names),
            profile or "household",
            rejected,
            bool(include_transaction_evidence),
            limit,
            fingerprint,
        )
        result = copilot_cache.get_resolver_result(key, factory)
        return result if isinstance(result, GroundResult) else copy.deepcopy(factory())
    except Exception:
        return factory()


def ground_merchant(
    text: str,
    merchant_names: Iterable[str] | None = None,
    *,
    profile: str | None = None,
    rejected_candidates: Iterable[str] | None = None,
    include_transaction_evidence: bool = False,
    limit: int = 8,
) -> GroundResult:
    if merchant_names is not None and not isinstance(merchant_names, (list, tuple)):
        merchant_names = list(merchant_names)
    return _cached_ground_result(
        entity_type="merchant",
        text=text,
        names=merchant_names,
        profile=profile,
        rejected_candidates=rejected_candidates,
        include_transaction_evidence=include_transaction_evidence,
        limit=limit,
        factory=lambda: ground_text(
            text,
            "merchant",
            merchant_names,
            profile=profile,
            rejected_candidates=rejected_candidates,
            include_transaction_evidence=include_transaction_evidence,
            limit=limit,
        ),
    )


def ground_category(
    text: str,
    category_names: Iterable[str] | None = None,
    *,
    profile: str | None = None,
    rejected_candidates: Iterable[str] | None = None,
    limit: int = 8,
) -> GroundResult:
    if category_names is not None and not isinstance(category_names, (list, tuple)):
        category_names = list(category_names)
    return _cached_ground_result(
        entity_type="category",
        text=text,
        names=category_names,
        profile=profile,
        rejected_candidates=rejected_candidates,
        include_transaction_evidence=False,
        limit=limit,
        factory=lambda: ground_text(
            text,
            "category",
            category_names,
            profile=profile,
            rejected_candidates=rejected_candidates,
            limit=limit,
        ),
    )


def ground_entity(
    text: str,
    *,
    entity_types: tuple[str, ...] = ("merchant", "category"),
    merchant_names: Iterable[str] | None = None,
    category_names: Iterable[str] | None = None,
    profile: str | None = None,
    prefer_entity_type: str | None = None,
    rejected_candidates: Iterable[str] | None = None,
    include_transaction_evidence: bool = False,
    limit: int = 8,
) -> GroundResult:
    results: list[GroundResult] = []
    for entity_type in entity_types:
        if entity_type == "merchant":
            results.append(
                ground_merchant(
                    text,
                    merchant_names,
                    profile=profile,
                    rejected_candidates=rejected_candidates,
                    include_transaction_evidence=include_transaction_evidence,
                    limit=limit,
                )
            )
        elif entity_type == "category":
            results.append(
                ground_category(
                    text,
                    category_names,
                    profile=profile,
                    rejected_candidates=rejected_candidates,
                    limit=limit,
                )
            )

    all_candidates = []
    for result in results:
        all_candidates.extend(result.candidates or [])
    all_candidates.sort(key=lambda item: float(item.get("confidence") or 0), reverse=True)

    preferred = next((result for result in results if result.entity_type == prefer_entity_type), None)
    if preferred and preferred.kind in {"exact", "approximate"}:
        return GroundResult(
            kind=preferred.kind,
            entity_type=preferred.entity_type,
            value=preferred.value,
            canonical_id=preferred.canonical_id,
            display_name=preferred.display_name,
            confidence=preferred.confidence,
            candidates=all_candidates[:limit],
            evidence=preferred.evidence,
            rejected_candidates=preferred.rejected_candidates,
        )

    usable = [result for result in results if result.kind in {"exact", "approximate"}]
    if not usable:
        entity_type = prefer_entity_type or (entity_types[0] if entity_types else "merchant")
        return GroundResult(
            kind="missing",
            entity_type=entity_type,
            value=None,
            canonical_id=None,
            display_name=None,
            confidence=0.0,
            candidates=all_candidates[:limit],
            evidence={"query": text},
            rejected_candidates=[str(item) for item in rejected_candidates or []],
        )
    usable.sort(key=lambda result: result.confidence, reverse=True)
    top = usable[0]
    close = [result for result in usable if result.confidence >= top.confidence - 0.05]
    if len(close) > 1 and close[0].entity_type != close[1].entity_type and not prefer_entity_type:
        return GroundResult(
            kind="ambiguous",
            entity_type=top.entity_type,
            value=None,
            canonical_id=None,
            display_name=None,
            confidence=top.confidence,
            candidates=all_candidates[:limit],
            evidence={"query": text, "reason": "multiple entity types matched"},
            rejected_candidates=top.rejected_candidates,
        )
    return GroundResult(
        kind=top.kind,
        entity_type=top.entity_type,
        value=top.value,
        canonical_id=top.canonical_id,
        display_name=top.display_name,
        confidence=top.confidence,
        candidates=all_candidates[:limit],
        evidence=top.evidence,
        rejected_candidates=top.rejected_candidates,
    )


def exact_merchant_for_text(text: str, merchant_names: Iterable[str]) -> str | None:
    result = ground_merchant(text, merchant_names, limit=1)
    return result.value if result.kind == "exact" else None


def exact_category_for_text(text: str, category_names: Iterable[str] | None = None, *, profile: str | None = None) -> str | None:
    result = ground_category(text, category_names, profile=profile, limit=1)
    return result.value if result.kind == "exact" else None


def candidate_names_for_text(text: str, names: Iterable[str], *, entity_type: str = "merchant", limit: int = 20) -> list[str]:
    result = ground_text(text, entity_type, names, limit=limit)
    if result.candidates:
        return [str(candidate.get("display_name") or candidate.get("value") or "") for candidate in result.candidates[:limit]]
    return []


def resolve_category_name(text: str, category_names: Iterable[str] | None = None, *, profile: str | None = None) -> str | None:
    result = ground_category(text, category_names, profile=profile, limit=1)
    return result.value if result.kind in {"exact", "approximate"} else None


def _jsonish(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class MerchantResolution:
    name: str | None
    confidence: float
    reason: str = ""
    candidates: tuple[str, ...] = ()
    matched_text: str = ""


def resolve_merchant_with_llm(
    text: str,
    merchant_names: list[str],
    limit: int = 24,
    *,
    profile: str | None = None,
    rejected_candidates: Iterable[str] | None = None,
    include_transaction_evidence: bool = False,
) -> MerchantResolution:
    if not merchant_names:
        return MerchantResolution(None, 0.0)

    deterministic = ground_merchant(
        text,
        merchant_names,
        profile=profile,
        rejected_candidates=rejected_candidates,
        include_transaction_evidence=include_transaction_evidence,
        limit=limit,
    )
    candidate_names = tuple(
        str(candidate.get("display_name") or candidate.get("value") or "")
        for candidate in deterministic.candidates
        if candidate.get("display_name") or candidate.get("value")
    )
    if deterministic.kind == "exact" and deterministic.value:
        return MerchantResolution(
            deterministic.value,
            deterministic.confidence,
            "exact merchant mention",
            candidate_names or (deterministic.value,),
            deterministic.display_name or deterministic.value,
        )

    def fallback_resolution(reason: str) -> MerchantResolution:
        if deterministic.kind in {"approximate", "ambiguous"} and deterministic.candidates:
            top = deterministic.candidates[0]
            confidence = min(float(top.get("confidence") or deterministic.confidence), 0.7)
            if confidence >= 0.55:
                return MerchantResolution(
                    str(top.get("display_name") or top.get("value") or ""),
                    confidence,
                    reason,
                    candidate_names,
                    str(top.get("matched_text") or " ".join(significant_tokens(text, query=True)) or text),
                )
        return MerchantResolution(None, 0.0, reason, candidate_names)

    if not deterministic.candidates:
        return MerchantResolution(None, 0.0, "no resolver candidates", ())

    try:
        import llm_client

        if not llm_client.is_available():
            return fallback_resolution("local LLM unavailable")

        llm_candidates = []
        for idx, candidate in enumerate(deterministic.candidates[:limit]):
            llm_candidates.append(
                {
                    "id": f"m{idx}",
                    "display_name": candidate.get("display_name"),
                    "canonical_id": candidate.get("canonical_id"),
                    "resolver_confidence": candidate.get("confidence"),
                    "match_type": candidate.get("match_type"),
                    "matched_text": candidate.get("matched_text"),
                    "evidence": candidate.get("evidence") or {},
                }
            )
        by_id = {str(candidate["id"]): candidate for candidate in llm_candidates}
        by_name = {str(candidate.get("display_name") or ""): candidate for candidate in llm_candidates}
        prompt = f"""You resolve a user's merchant wording to one merchant from Folio's live transaction merchant candidates.
Return JSON only:
{{"candidate_id": string|null, "confidence": number, "reason": "short"}}

Rules:
- Choose a match only if the user's wording plausibly refers to the same real-world merchant.
- You may use brand knowledge, abbreviations, nicknames, acronyms, and common shorthand.
- candidate_id must be copied exactly from the candidate list, or null.
- Do not return a merchant name outside the candidate list.
- If none fit, return candidate_id=null and confidence=0.
- Do not answer the finance question.

User wording: {text}

Candidate merchants:
{json.dumps(llm_candidates, ensure_ascii=True)}

JSON:"""
        parsed = _jsonish(llm_client.complete(prompt, max_tokens=160, purpose="controller"))
    except Exception:
        return fallback_resolution("local LLM resolver failed")

    candidate_id = str(parsed.get("candidate_id") or "").strip()
    selected = by_id.get(candidate_id)
    legacy_match = str(parsed.get("match") or "").strip()
    if not selected and legacy_match:
        selected = by_name.get(legacy_match)

    try:
        confidence = max(0.0, min(float(parsed.get("confidence", 0)), 1.0))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(parsed.get("reason") or "").strip()
    if selected:
        return MerchantResolution(
            str(selected.get("display_name") or ""),
            confidence,
            reason,
            candidate_names,
            str(selected.get("matched_text") or " ".join(significant_tokens(text, query=True)) or text),
        )
    if "candidate_id" not in parsed and "match" not in parsed:
        return fallback_resolution("local LLM resolver returned malformed JSON")
    return MerchantResolution(None, confidence, reason, candidate_names)
