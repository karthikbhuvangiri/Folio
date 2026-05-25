from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from log_config import get_logger

load_dotenv()

logger = get_logger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATHS = [
    Path(__file__).resolve().parent / "model_presets.json",
    ROOT_DIR / "model_presets.json",
]

DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower() or "ollama"
DEFAULT_OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
DEFAULT_LLAMACPP_BASE_URL = os.getenv("LLAMACPP_BASE_URL", "http://host.docker.internal:8081")
DEFAULT_LLAMACPP_MODEL = os.getenv("LLAMACPP_MODEL", "local")
DEFAULT_CATEGORIZE_MODEL = os.getenv("OLLAMA_MODEL_CATEGORIZE", "gemma4:e4b")
DEFAULT_CONTROLLER_MODEL = os.getenv("OLLAMA_MODEL_CONTROLLER", DEFAULT_CATEGORIZE_MODEL)
DEFAULT_COPILOT_MODEL = os.getenv("OLLAMA_MODEL_COPILOT", "gemma4:e4b")
DEFAULT_ADVISOR_MODEL = "gemma4:e4b"
HIGH_MEMORY_ADVISOR_MODEL = "gemma4:26b"
ADVISOR_HIGH_MEMORY_MIN_RAM_GB = 30
DEFAULT_MEMORY_TIER = os.getenv("LOCAL_LLM_MEMORY_TIER", "16gb").strip().lower()
ENABLE_EXPERIMENTAL_LOCAL_MODELS = os.getenv(
    "ENABLE_EXPERIMENTAL_LOCAL_MODELS",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}

SETTINGS_DEFAULTS = {
    "llm_provider": None,
    "local_ai_profile": None,
    "categorize_model": None,
    "controller_model": None,
    "copilot_model": None,
    "categorize_batch_size": None,
    "inter_batch_delay_ms": None,
    "low_power_mode": False,
    "expert_mode": False,
}

_OLLAMA_CACHE = {"ts": 0.0, "payload": None}
_OLLAMA_CACHE_TTL = 8.0
_LLAMACPP_CACHE = {"ts": 0.0, "payload": None}
_LLAMACPP_CACHE_TTL = 8.0
_PREWARM_LOCK = threading.Lock()
_PREWARM_IN_FLIGHT: set[tuple[str, str]] = set()
_PREWARM_LAST_RUN: dict[tuple[str, str], float] = {}
_PREWARM_TTL_SECONDS = max(30, int(os.getenv("OLLAMA_PREWARM_TTL_SECONDS", "240")))
_PREWARM_KEEP_ALIVE = os.getenv("OLLAMA_PREWARM_KEEP_ALIVE", "15m").strip() or "15m"


def _load_catalog() -> dict:
    for path in CATALOG_PATHS:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                catalog = json.load(fh)
            if not ENABLE_EXPERIMENTAL_LOCAL_MODELS:
                catalog = dict(catalog)
                catalog["models"] = {
                    model_id: meta
                    for model_id, meta in catalog.get("models", {}).items()
                    if not meta.get("experimental")
                }
            return catalog
    raise FileNotFoundError(f"Could not find model catalog in any of: {', '.join(str(p) for p in CATALOG_PATHS)}")


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _detect_total_ram_bytes() -> int | None:
    try:
        if os.name == "posix":
            if sys_platform := os.getenv("OSTYPE", "").lower():
                _ = sys_platform  # noop to avoid lint noise in environments without sys module checks
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    check=True,
                )
                return int(result.stdout.strip())
            except Exception:
                page_size = os.sysconf("SC_PAGE_SIZE")
                phys_pages = os.sysconf("SC_PHYS_PAGES")
                if page_size > 0 and phys_pages > 0:
                    return int(page_size * phys_pages)

        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            memory_status = MEMORYSTATUSEX()
            memory_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):  # type: ignore[attr-defined]
                return int(memory_status.ullTotalPhys)
    except Exception:
        return None
    return None


def detect_memory_profile() -> dict:
    env_ram_gb = _coerce_int(os.getenv("LOCAL_LLM_RAM_GB"))
    total_ram_bytes = None if env_ram_gb else _detect_total_ram_bytes()
    ram_gb = env_ram_gb or (round(total_ram_bytes / (1024 ** 3)) if total_ram_bytes else None)

    if ram_gb is None:
        tier = DEFAULT_MEMORY_TIER if DEFAULT_MEMORY_TIER in {"8gb", "16gb", "32gb"} else "16gb"
    elif ram_gb >= ADVISOR_HIGH_MEMORY_MIN_RAM_GB:
        tier = "32gb"
    elif ram_gb >= 14:
        tier = "16gb"
    else:
        tier = "8gb"

    return {
        "ramGb": ram_gb,
        "memoryTier": tier,
        "memoryLabel": {"8gb": "8 GB", "16gb": "16 GB", "32gb": "32 GB"}[tier],
    }


def _tier_sort_key(tier: str) -> tuple[int, str]:
    order = {"8gb": 0, "16gb": 1, "32gb": 2}
    return (order.get(tier, 99), tier)


def _configured_advisor_model() -> str:
    return (
        os.getenv("MIRA_ADVISOR_LENS_MODEL")
        or os.getenv("OLLAMA_MODEL_ADVISOR")
        or ""
    ).strip()


def _recommended_advisor_model(memory: dict, defaults: dict | None = None) -> str:
    configured = _configured_advisor_model()
    if configured:
        return configured
    ram_gb = memory.get("ramGb")
    if ram_gb is not None and ram_gb >= ADVISOR_HIGH_MEMORY_MIN_RAM_GB:
        return (defaults or {}).get("advisor_model") or HIGH_MEMORY_ADVISOR_MODEL
    return DEFAULT_ADVISOR_MODEL


def _model_requires_advanced(meta: dict | None) -> bool:
    if not meta:
        return False
    if meta.get("expert_only") or meta.get("advanced_only"):
        return True
    return meta.get("validated_for_mira") is not True


def _model_supports(meta: dict | None, task: str) -> bool:
    return task in (meta or {}).get("task_fit", [])


def _fetch_ollama_state(base_url: str) -> dict:
    now = time.time()
    cached = _OLLAMA_CACHE["payload"]
    if cached and now - _OLLAMA_CACHE["ts"] < _OLLAMA_CACHE_TTL and cached.get("baseUrl") == base_url:
        return cached

    candidates = [base_url]
    stripped = base_url.rstrip("/")
    if "host.docker.internal" in stripped:
        candidates.append(stripped.replace("host.docker.internal", "localhost"))
    elif "localhost" in stripped:
        candidates.append(stripped.replace("localhost", "host.docker.internal"))

    payload = {
        "baseUrl": base_url,
        "requestedBaseUrl": base_url,
        "reachable": False,
        "installedModels": [],
        "error": None,
    }

    seen = set()
    last_error = None
    for candidate in candidates:
        candidate = candidate.rstrip("/")
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            resp = httpx.get(f"{candidate}/api/tags", timeout=2.5)
            resp.raise_for_status()
            data = resp.json()
            payload["reachable"] = True
            payload["baseUrl"] = candidate
            payload["installedModels"] = sorted(
                item.get("name")
                for item in data.get("models", [])
                if isinstance(item.get("name"), str) and item.get("name")
            )
            payload["error"] = None
            break
        except Exception as exc:
            last_error = exc

    if not payload["reachable"] and last_error is not None:
        payload["error"] = str(last_error)

    _OLLAMA_CACHE["ts"] = now
    _OLLAMA_CACHE["payload"] = payload
    return payload


def _fetch_llamacpp_state(base_url: str) -> dict:
    now = time.time()
    cached = _LLAMACPP_CACHE["payload"]
    if cached and now - _LLAMACPP_CACHE["ts"] < _LLAMACPP_CACHE_TTL and cached.get("baseUrl") == base_url:
        return cached

    candidates = [base_url]
    stripped = base_url.rstrip("/")
    if "host.docker.internal" in stripped:
        candidates.append(stripped.replace("host.docker.internal", "localhost"))
    elif "localhost" in stripped:
        candidates.append(stripped.replace("localhost", "host.docker.internal"))

    payload = {
        "baseUrl": base_url,
        "requestedBaseUrl": base_url,
        "reachable": False,
        "models": [],
        "error": None,
    }
    seen = set()
    last_error = None
    for candidate in candidates:
        candidate = candidate.rstrip("/")
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            health = httpx.get(f"{candidate}/health", timeout=2.5)
            health.raise_for_status()
            models_resp = httpx.get(f"{candidate}/v1/models", timeout=2.5)
            models_resp.raise_for_status()
            data = models_resp.json()
            model_items = data.get("data") or data.get("models") or []
            models = []
            for item in model_items:
                if isinstance(item, dict):
                    name = item.get("id") or item.get("model") or item.get("name")
                    if isinstance(name, str) and name:
                        models.append(name)
            payload.update({
                "baseUrl": candidate,
                "reachable": True,
                "models": sorted(models),
                "error": None,
            })
            break
        except Exception as exc:
            last_error = exc

    if not payload["reachable"] and last_error is not None:
        payload["error"] = str(last_error)

    _LLAMACPP_CACHE["ts"] = now
    _LLAMACPP_CACHE["payload"] = payload
    return payload


def _invalidate_ollama_cache() -> None:
    _OLLAMA_CACHE["ts"] = 0.0
    _OLLAMA_CACHE["payload"] = None


def _model_installed(installed_models: list[str] | set[str], model: str | None) -> bool:
    if not model:
        return False
    installed = {item.lower() for item in installed_models if isinstance(item, str)}
    return model.lower() in installed


def _prewarm_model(base_url: str, model: str) -> None:
    key = (base_url.rstrip("/"), model)
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": _PREWARM_KEEP_ALIVE,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        result = resp.json()
        load_ns = result.get("load_duration")
        total_ns = result.get("total_duration")
        if isinstance(load_ns, int) and isinstance(total_ns, int):
            logger.info(
                "Prewarmed Ollama model %s at %s (load %.2fs, total %.2fs)",
                model,
                base_url,
                load_ns / 1_000_000_000,
                total_ns / 1_000_000_000,
            )
        else:
            logger.info("Prewarmed Ollama model %s at %s", model, base_url)
    except Exception as exc:
        logger.debug("Ollama prewarm skipped for %s at %s: %s", model, base_url, exc)
    finally:
        with _PREWARM_LOCK:
            _PREWARM_LAST_RUN[key] = time.time()
            _PREWARM_IN_FLIGHT.discard(key)


def schedule_prewarm_selected_model(purpose: str = "copilot", conn=None, force: bool = False) -> bool:
    resolved = resolve_runtime_settings(conn)
    if resolved.get("provider") != "ollama":
        return False

    if purpose == "categorize":
        model = resolved.get("selectedCategorizeModel")
    elif purpose == "controller":
        model = resolved.get("selectedControllerModel")
    else:
        model = resolved.get("selectedCopilotModel")
    if not model:
        return False

    ollama_state = _fetch_ollama_state(resolved["ollamaBaseUrl"])
    if not ollama_state.get("reachable"):
        return False
    if not _model_installed(ollama_state.get("installedModels", []), model):
        return False

    base_url = ollama_state["baseUrl"].rstrip("/")
    key = (base_url, model)
    now = time.time()
    with _PREWARM_LOCK:
        if key in _PREWARM_IN_FLIGHT:
            return False
        last_run = _PREWARM_LAST_RUN.get(key, 0.0)
        if not force and now - last_run < _PREWARM_TTL_SECONDS:
            return False
        _PREWARM_IN_FLIGHT.add(key)

    worker = threading.Thread(
        target=_prewarm_model,
        args=(base_url, model),
        name=f"ollama-prewarm-{purpose}",
        daemon=True,
    )
    worker.start()
    return True


def _read_settings(conn) -> dict:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    raw = {row["key"]: row["value"] for row in rows}

    settings = dict(SETTINGS_DEFAULTS)
    settings.update({k: raw.get(k) for k in settings.keys()})
    settings["low_power_mode"] = _coerce_bool(settings.get("low_power_mode"))
    settings["expert_mode"] = _coerce_bool(settings.get("expert_mode"))
    settings["categorize_batch_size"] = _coerce_int(settings.get("categorize_batch_size"))
    settings["inter_batch_delay_ms"] = _coerce_int(settings.get("inter_batch_delay_ms"))
    return settings


def update_settings(conn, payload: dict) -> dict:
    payload_keys = set((payload or {}).keys())
    current = _read_settings(conn)
    next_settings = dict(current)
    next_settings.update(payload or {})

    for key in ["low_power_mode", "expert_mode"]:
        if key in next_settings:
            next_settings[key] = _coerce_bool(next_settings.get(key))
    for key in ["categorize_batch_size", "inter_batch_delay_ms"]:
        if key in next_settings:
            next_settings[key] = _coerce_int(next_settings.get(key))

    catalog = _load_catalog()
    presets = catalog.get("presets", {})
    models = catalog.get("models", {})

    provider = (next_settings.get("llm_provider") or DEFAULT_PROVIDER).strip().lower()
    if provider not in {"ollama", "llamacpp"}:
        raise ValueError("Mira only supports local Ollama or experimental llama.cpp LLMs.")
    next_settings["llm_provider"] = provider

    preset = next_settings.get("local_ai_profile")
    if preset and preset not in presets:
        if preset in {"battery_saver", "balanced", "quality", "light"}:
            next_settings["local_ai_profile"] = "default"
        else:
            raise ValueError("Unknown local_ai_profile.")

    expert_mode = bool(next_settings.get("expert_mode"))
    disabling_advanced_mode = "expert_mode" in payload_keys and not expert_mode
    for key in ["categorize_model", "controller_model", "copilot_model"]:
        selected = next_settings.get(key)
        if not selected:
            continue
        if selected not in models:
            raise ValueError(f"Unsupported model: {selected}")
        task = "copilot" if key == "copilot_model" else "categorize"
        if not _model_supports(models[selected], task):
            if key in payload_keys and not disabling_advanced_mode:
                raise ValueError(f"{selected} is not supported for {task}.")
            next_settings[key] = None
            continue
        if _model_requires_advanced(models[selected]) and not expert_mode:
            if key in payload_keys and not disabling_advanced_mode:
                raise ValueError(f"{selected} requires Advanced mode.")
            next_settings[key] = None

    if next_settings.get("categorize_model"):
        model_meta = models[next_settings["categorize_model"]]
        if model_meta.get("categorize_default") == "avoid" and not expert_mode:
            if "categorize_model" in payload_keys and not disabling_advanced_mode:
                raise ValueError("Selected categorize_model requires Advanced mode.")
            next_settings["categorize_model"] = None

    for key, value in next_settings.items():
        stored = None if value is None else str(int(value)) if isinstance(value, bool) else str(value)
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, stored),
        )
    return resolve_runtime_settings(conn)


def resolve_runtime_settings(conn=None) -> dict:
    catalog = _load_catalog()
    presets = catalog.get("presets", {})
    recommended = catalog.get("recommendedDefaults", {})
    models = catalog.get("models", {})
    memory = detect_memory_profile()
    tier = memory["memoryTier"]
    defaults = recommended.get(tier, recommended.get("16gb", {}))

    if conn is None:
        from database import get_db
        with get_db() as temp_conn:
            settings = _read_settings(temp_conn)
    else:
        settings = _read_settings(conn)

    provider = (settings.get("llm_provider") or DEFAULT_PROVIDER).strip().lower()
    if provider not in {"ollama", "llamacpp"}:
        provider = "ollama"

    ollama_state = _fetch_ollama_state(DEFAULT_OLLAMA_BASE_URL)
    active_ollama_base_url = ollama_state["baseUrl"] if ollama_state.get("reachable") else DEFAULT_OLLAMA_BASE_URL
    llamacpp_state = _fetch_llamacpp_state(DEFAULT_LLAMACPP_BASE_URL)
    active_llamacpp_base_url = (
        llamacpp_state["baseUrl"] if llamacpp_state.get("reachable") else DEFAULT_LLAMACPP_BASE_URL
    )

    explicit_preset_key = settings.get("local_ai_profile")
    if explicit_preset_key not in presets:
        explicit_preset_key = None
    preset_key = explicit_preset_key or defaults.get("preset") or "default"
    preset = presets.get(preset_key, presets.get("default", {}))
    expert_mode = bool(settings.get("expert_mode"))

    default_categorize_model = (
        preset.get("categorize_model")
        if explicit_preset_key
        else defaults.get("categorize_model") or preset.get("categorize_model")
    )
    default_copilot_model = (
        preset.get("copilot_model")
        if explicit_preset_key
        else defaults.get("copilot_model") or preset.get("copilot_model")
    )
    default_advisor_model = _recommended_advisor_model(memory, defaults)

    categorize_model = settings.get("categorize_model") or default_categorize_model or DEFAULT_CATEGORIZE_MODEL
    controller_model = settings.get("controller_model") or DEFAULT_CONTROLLER_MODEL or categorize_model
    copilot_model = settings.get("copilot_model") or default_copilot_model or DEFAULT_COPILOT_MODEL
    advisor_model = default_advisor_model

    if categorize_model in models:
        if not _model_supports(models[categorize_model], "categorize"):
            categorize_model = preset.get("categorize_model") or defaults.get("categorize_model") or DEFAULT_CATEGORIZE_MODEL
        elif _model_requires_advanced(models[categorize_model]) and not expert_mode:
            categorize_model = preset.get("categorize_model") or defaults.get("categorize_model") or DEFAULT_CATEGORIZE_MODEL
        elif models[categorize_model].get("categorize_default") == "avoid" and not expert_mode:
            categorize_model = preset.get("categorize_model") or defaults.get("categorize_model") or DEFAULT_CATEGORIZE_MODEL

    if copilot_model in models:
        if not _model_supports(models[copilot_model], "copilot"):
            copilot_model = preset.get("copilot_model") or defaults.get("copilot_model") or DEFAULT_COPILOT_MODEL
        elif _model_requires_advanced(models[copilot_model]) and not expert_mode:
            copilot_model = preset.get("copilot_model") or defaults.get("copilot_model") or DEFAULT_COPILOT_MODEL
    if controller_model in models:
        if _model_requires_advanced(models[controller_model]) and not expert_mode:
            controller_model = categorize_model
        elif models[controller_model].get("categorize_default") == "avoid" and not expert_mode:
            controller_model = categorize_model

    batch_size = settings.get("categorize_batch_size") or preset.get("default_batch_size") or 20
    delay_ms = settings.get("inter_batch_delay_ms") or preset.get("inter_batch_delay_ms") or 600
    low_power_mode = bool(settings.get("low_power_mode"))
    if low_power_mode:
        batch_size = min(batch_size, 10)
        delay_ms = max(delay_ms, 1400)

    return {
        "provider": provider,
        "ollamaBaseUrl": active_ollama_base_url,
        "llamaCppBaseUrl": active_llamacpp_base_url,
        "llamaCppModel": DEFAULT_LLAMACPP_MODEL,
        "memory": memory,
        "preset": preset_key,
        "presetMeta": preset,
        "expertMode": expert_mode,
        "lowPowerMode": low_power_mode,
        "selectedCategorizeModel": categorize_model,
        "selectedControllerModel": controller_model,
        "selectedCopilotModel": copilot_model,
        "selectedAdvisorModel": advisor_model,
        "categorizeBatchSize": max(1, int(batch_size)),
        "interBatchDelayMs": max(0, int(delay_ms)),
    }


def get_catalog_response(conn=None) -> dict:
    catalog = _load_catalog()
    resolved = resolve_runtime_settings(conn)
    ollama_state = _fetch_ollama_state(resolved["ollamaBaseUrl"])
    installed = set(ollama_state.get("installedModels", []))

    model_list = []
    model_map = {}
    tiers = []
    for tier in sorted({meta["ram_tier"] for meta in catalog["models"].values()}, key=_tier_sort_key):
        tier_models = []
        for model_id, meta in catalog["models"].items():
            if meta.get("ram_tier") != tier:
                continue
            enriched = {
                "id": model_id,
                **meta,
                "installed": _model_installed(installed, model_id),
                "selectedForCategorize": resolved["selectedCategorizeModel"] == model_id,
                "selectedForController": resolved["selectedControllerModel"] == model_id,
                "selectedForCopilot": resolved["selectedCopilotModel"] == model_id,
                "selectedForAdvisor": resolved["selectedAdvisorModel"] == model_id,
            }
            tier_models.append(enriched)
            model_list.append(enriched)
            model_map[model_id] = enriched

        tiers.append({
            "key": tier,
            "label": {"8gb": "Best on 8 GB", "16gb": "Best on 16 GB", "32gb": "Best on 32 GB"}[tier],
            "models": tier_models,
        })

    return {
        "tiers": tiers,
        "models": model_map,
        "modelList": model_list,
        "recommendedDefaults": catalog.get("recommendedDefaults", {}),
        "presets": catalog.get("presets", {}),
        "expertModeAvailable": True,
    }


def get_status_response(conn=None) -> dict:
    resolved = resolve_runtime_settings(conn)
    ollama_state = _fetch_ollama_state(resolved["ollamaBaseUrl"])
    llamacpp_state = _fetch_llamacpp_state(resolved["llamaCppBaseUrl"])
    return {
        "provider": resolved["provider"],
        "ollamaReachable": ollama_state.get("reachable", False),
        "llamaCppReachable": llamacpp_state.get("reachable", False),
        "llamaCppModels": llamacpp_state.get("models", []),
        "llamaCppBaseUrl": resolved["llamaCppBaseUrl"],
        "llamaCppModel": resolved["llamaCppModel"],
        "memoryTier": resolved["memory"]["memoryTier"],
        "memoryLabel": resolved["memory"]["memoryLabel"],
        "ramGb": resolved["memory"]["ramGb"],
        "installedModels": ollama_state.get("installedModels", []),
        "selectedCategorizeModel": resolved["selectedCategorizeModel"],
        "selectedControllerModel": resolved["selectedControllerModel"],
        "selectedCopilotModel": resolved["selectedCopilotModel"],
        "selectedAdvisorModel": resolved["selectedAdvisorModel"],
        "preset": resolved["preset"],
        "lowPowerMode": resolved["lowPowerMode"],
        "expertMode": resolved["expertMode"],
        "categorizeBatchSize": resolved["categorizeBatchSize"],
        "interBatchDelayMs": resolved["interBatchDelayMs"],
        "ollamaBaseUrl": resolved["ollamaBaseUrl"],
    }


def get_frontend_flags(conn=None) -> dict:
    resolved = resolve_runtime_settings(conn)
    local_llm_configured = (
        (resolved["provider"] == "ollama" and bool(resolved["ollamaBaseUrl"]))
        or (resolved["provider"] == "llamacpp" and bool(resolved["llamaCppBaseUrl"]))
    )
    return {
        "localLlmEnabled": local_llm_configured,
        "localLlmProvider": resolved["provider"],
        "memoryTier": resolved["memory"]["memoryTier"],
        "localAiProfile": resolved["preset"],
        "lowPowerMode": resolved["lowPowerMode"],
        "expertMode": resolved["expertMode"],
        "selectedCategorizeModel": resolved["selectedCategorizeModel"],
        "selectedControllerModel": resolved["selectedControllerModel"],
        "selectedCopilotModel": resolved["selectedCopilotModel"],
        "selectedAdvisorModel": resolved["selectedAdvisorModel"],
    }


def install_model(model_id: str, conn=None) -> dict[str, Any]:
    catalog = _load_catalog()
    models = catalog.get("models", {})
    if model_id not in models:
        raise ValueError(f"Unsupported model: {model_id}")

    resolved = resolve_runtime_settings(conn)
    if resolved.get("provider") != "ollama":
        raise ValueError("Install is only supported for Ollama models. Start llama.cpp models on the host.")
    ollama_state = _fetch_ollama_state(resolved["ollamaBaseUrl"])
    if not ollama_state.get("reachable"):
        raise ValueError("Ollama is not reachable from the backend container.")

    final_status = None
    last_completed = 0
    try:
        with httpx.stream(
            "POST",
            f"{ollama_state['baseUrl'].rstrip('/')}/api/pull",
            json={"name": model_id, "stream": True},
            timeout=600.0,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                payload = json.loads(line)
                final_status = payload
                if isinstance(payload.get("completed"), int):
                    last_completed = payload["completed"]
    except httpx.HTTPError as exc:
        raise ValueError(f"Failed to install {model_id}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Ollama returned an unexpected response while installing {model_id}.") from exc

    _invalidate_ollama_cache()
    status = get_status_response(conn)
    return {
        "model": model_id,
        "completed": _model_installed(status.get("installedModels", []), model_id),
        "bytesCompleted": last_completed,
        "finalStatus": final_status,
        "status": status,
    }


def get_provider() -> str:
    return resolve_runtime_settings().get("provider", DEFAULT_PROVIDER)


def get_ollama_config() -> dict:
    resolved = resolve_runtime_settings()
    return {
        "base_url": resolved["ollamaBaseUrl"],
        "categorize_model": resolved["selectedCategorizeModel"],
        "controller_model": resolved["selectedControllerModel"],
        "copilot_model": resolved["selectedCopilotModel"],
        "advisor_model": resolved["selectedAdvisorModel"],
    }


def get_llamacpp_config() -> dict:
    resolved = resolve_runtime_settings()
    return {
        "base_url": resolved["llamaCppBaseUrl"],
        "model": resolved["llamaCppModel"],
    }


def get_categorization_policy() -> dict:
    resolved = resolve_runtime_settings()
    return {
        "batch_size": resolved["categorizeBatchSize"],
        "inter_batch_delay_ms": resolved["interBatchDelayMs"],
    }
