"""Static Mira advisor reads for public demo mode."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


DEMO_ADVISOR_READ_VERSION = "demo_static_advisor_read_v1"
_DEMO_READ_DIR = Path(__file__).resolve().parents[1] / "demo_mira_reads"
_META_RE = re.compile(r"\A\s*<!--\s*folio-demo-advisor-read\s*(\{.*?\})\s*-->\s*", re.DOTALL)


def _profile_key(profile: str | None) -> str:
    value = str(profile or "").strip().lower()
    if not value or value == "household":
        return "household"
    safe = re.sub(r"[^a-z0-9_-]+", "", value)
    return safe or "household"


@lru_cache(maxsize=8)
def _load_fixture(profile_key: str) -> dict[str, Any] | None:
    path = _DEMO_READ_DIR / f"{profile_key}.md"
    if not path.exists() and profile_key != "household":
        path = _DEMO_READ_DIR / "household.md"
    if not path.exists():
        return None

    raw = path.read_text(encoding="utf-8")
    match = _META_RE.match(raw)
    metadata: dict[str, Any] = {}
    body = raw
    if match:
        metadata = json.loads(match.group(1))
        body = raw[match.end():]
    body = body.strip()
    if not body:
        return None

    quality = metadata.get("quality") if isinstance(metadata.get("quality"), dict) else {}
    payload = {
        "memo_markdown": body,
        "theses": metadata.get("theses") if isinstance(metadata.get("theses"), list) else [],
        "action_plan": metadata.get("action_plan") if isinstance(metadata.get("action_plan"), list) else [],
        "cards": metadata.get("cards") if isinstance(metadata.get("cards"), list) else [],
        "source": metadata.get("source") or "static_demo_markdown",
        "model": metadata.get("model"),
        "run_status": metadata.get("run_status"),
    }
    return {
        "id": metadata.get("id") or f"demo-advisor-read-{profile_key}",
        "profile": None if profile_key == "household" else profile_key,
        "generated_at": metadata.get("generated_at") or "2026-05-20T12:00:00Z",
        "valid_until": metadata.get("valid_until") or "2099-01-01T00:00:00Z",
        "version": metadata.get("version") or DEMO_ADVISOR_READ_VERSION,
        "memo_markdown": body,
        "theses": payload["theses"],
        "quality": {
            "ok": bool(quality.get("ok", True)),
            "score": quality.get("score", 0.9),
            "coverage_count": quality.get("coverage_count", len(payload["theses"])),
            "required_count": quality.get("required_count", len(payload["theses"])),
        },
        "payload": payload,
        "cards": payload["cards"],
        "action_plan": payload["action_plan"],
    }


def load_demo_advisor_read(profile: str | None) -> dict[str, Any] | None:
    """Return a static demo memo matching the stored advisor memo shape."""
    return _load_fixture(_profile_key(profile))
