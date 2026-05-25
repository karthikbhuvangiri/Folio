#!/usr/bin/env python3
"""
Interactive setup script for Folio.

Primary supported onboarding paths:
- Docker Desktop + Local AI (host Ollama on macOS/Windows)
- Docker Desktop + No AI

Local development remains available for contributors who want to run the
backend/frontend outside Docker.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from setup_helpers import (
    detect_system_profile,
    format_system_profile,
    load_model_presets,
    recommend_advisor_model,
    recommend_model_preset,
)
from setup_ui import ui

ROOT_DIR = Path(__file__).parent
ENV_FILE = ROOT_DIR / ".env"
CERTS_DIR = ROOT_DIR / "certs"
DATA_DIR = ROOT_DIR / "data"
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIR = ROOT_DIR / "frontend"
FOLIO_LAUNCHER = ROOT_DIR / "folio.sh"
DEPENDENCY_COOLDOWN_DAYS = int(os.environ.get("FOLIO_DEPENDENCY_COOLDOWN_DAYS", "7"))
PIP_VERSION = os.environ.get("FOLIO_PIP_VERSION", "26.0.1")

OLLAMA_DOWNLOAD_URLS = {
    "macos": "https://ollama.com/download/mac",
    "windows": "https://ollama.com/download/windows",
}

DOCKER_DOWNLOAD_URLS = {
    "macos": "https://www.docker.com/products/docker-desktop/",
    "windows": "https://www.docker.com/products/docker-desktop/",
}

NODE_DOWNLOAD_URLS = {
    "macos": "https://nodejs.org/en/download",
    "windows": "https://nodejs.org/en/download",
}
MODEL_PRESETS = load_model_presets(ROOT_DIR)
DISTILBERT_HF_MODEL = "DoDataThings/distilbert-us-transaction-classifier-v2"
DISTILBERT_MODEL_DIR = ROOT_DIR / "models" / "distilbert-us-transaction-classifier-v2"
DISTILBERT_CONTAINER_MODEL_PATH = "/models/distilbert-us-transaction-classifier-v2"
DISTILBERT_MODEL_FILES = (
    "config.json",
    "label_mapping.json",
    "onnx/model_quantized.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)


def dependency_cutoff_iso(days: int = DEPENDENCY_COOLDOWN_DAYS) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ask(prompt: str, default: str | None = None, required: bool = False) -> str:
    display = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        if ui.enabled and ui.palette["key"]:
            sys.stdout.write(ui.palette["key"])
            if ui.BOLD:
                sys.stdout.write(ui.BOLD)
            sys.stdout.write(display)
            sys.stdout.flush()
            value = input().strip()
            sys.stdout.write(ui.RESET)
            sys.stdout.flush()
        else:
            value = input(display).strip()
        if not value and default is not None:
            return default
        if not value and required:
            ui.warning("This field is required.")
            continue
        return value


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    default_label = "Y/n" if default else "y/N"
    display = f"{prompt} [{default_label}]: "
    while True:
        if ui.enabled and ui.palette["key"]:
            sys.stdout.write(ui.palette["key"])
            if ui.BOLD:
                sys.stdout.write(ui.BOLD)
            sys.stdout.write(display)
            sys.stdout.flush()
            value = input().strip().lower()
            sys.stdout.write(ui.RESET)
            sys.stdout.flush()
        else:
            value = input(display).strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        ui.warning("Please answer yes or no.")


def detect_os() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "other"


def check_docker():
    docker_available = shutil.which("docker") is not None
    compose_available = False
    daemon_available = False

    if docker_available:
        try:
            compose_result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            compose_available = compose_result.returncode == 0
        except Exception:
            pass

        try:
            daemon_result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            daemon_available = daemon_result.returncode == 0
        except Exception:
            pass

    return docker_available, compose_available, daemon_available


def check_python() -> bool:
    return sys.version_info >= (3, 11)


def check_node() -> bool:
    return shutil.which("node") is not None


def check_tmux() -> bool:
    return shutil.which("tmux") is not None


def folio_launcher_available(host_os: str) -> bool:
    return host_os != "windows" and FOLIO_LAUNCHER.exists()


def tuned_ollama_session_running() -> bool:
    if not check_tmux():
        return False
    session_name = os.environ.get("FOLIO_OLLAMA_TMUX_SESSION", "folio-mira-ollama")
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def env_file_uses_local_ai() -> bool:
    if not ENV_FILE.exists():
        return False

    values = read_env_values()
    truthy = {"1", "true", "yes", "on"}
    return (
        values.get("MIRA_ENABLED", "").lower() in truthy
        or bool(values.get("OLLAMA_MODEL_COPILOT"))
        or values.get("ENABLE_LLM_CATEGORIZATION", "").lower() in truthy
        or values.get("RECEIPT_INTELLIGENCE_ENABLED", "").lower() in truthy
    )


def env_file_uses_distilbert() -> bool:
    if not ENV_FILE.exists():
        return False

    values = read_env_values()
    truthy = {"1", "true", "yes", "on"}
    return (
        values.get("CATEGORIZATION_BACKEND", "").lower() == "distilbert"
        or values.get("INSTALL_DISTILBERT", "").lower() in truthy
    )


def read_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def maybe_install_node(host_os: str) -> bool:
    if check_node():
        return True

    print()
    ui.warning("Node.js is not installed.")
    installed = False

    if host_os == "macos" and shutil.which("brew"):
        installed = run_install_command(["brew", "install", "node"], "Node.js")
    elif host_os == "windows" and shutil.which("winget"):
        installed = run_install_command(
            ["winget", "install", "-e", "--id", "OpenJS.NodeJS.LTS"],
            "Node.js",
        )

    if installed:
        print()
        ui.warning(
            "Node.js was installed. You may need to reopen your terminal so the node command is available."
        )
        return check_node()

    url = NODE_DOWNLOAD_URLS.get(host_os, "https://nodejs.org/en/download")
    ui.warning(f"Install Node.js 18+ from: {url}")
    if host_os == "macos":
        ui.muted("If you already use Homebrew, you can also run: brew install node")
    elif host_os == "windows":
        ui.muted("If winget is available, you can also run: winget install OpenJS.NodeJS.LTS")
    ui.muted("Then reopen your terminal and rerun setup.py.")
    return False


def setup_directories():
    DATA_DIR.mkdir(exist_ok=True)
    CERTS_DIR.mkdir(exist_ok=True)
    ui.success("Verified data/ and certs/ directories")


def setup_runtime_choice(has_docker: bool, has_local: bool) -> str:
    if has_docker and has_local:
        ui.panel(
            "Runtime",
            [
                "1. Docker (recommended)",
                "2. Local development",
            ],
            ui.BLUE,
        )
        choice = ask("  Runtime", default="1")
        return "docker" if choice in ("1", "docker", "d", "") else "local"
    if has_docker:
        return "docker"
    return "local"


def gather_bank_provider_choice() -> str:
    ui.panel(
        "Bank Connection",
        [
            "Choose how you want to set up bank connectivity right now.",
            "1. Teller",
            "2. SimpleFIN",
            "3. Both",
        ],
        ui.BLUE,
    )
    choice = ask("  Provider", default="1").lower()
    return {
        "1": "teller",
        "2": "simplefin",
        "3": "both",
        "teller": "teller",
        "simplefin": "simplefin",
        "both": "both",
    }.get(choice, "teller")


def gather_teller_config():
    ui.panel(
        "Teller Setup",
        [
            "You're setting up Teller for bank connectivity.",
            "Have these ready before continuing:",
            "• your Teller mTLS certificate",
            "• your Teller private key",
            "• your Teller App ID for the recommended Teller Connect flow",
            "• a plan to link in the UI, or manual access tokens",
        ],
        ui.BLUE,
    )
    ui.panel(
        "Quick Checklist",
        [
            "Sign up at https://teller.io",
            "Create an application and copy its App ID",
            "Download your mTLS certificate and private key",
            "Place them in ./certs as teller-cert.pem and teller-key.pem",
            "If you prefer manual setup, create access tokens in the Teller dashboard",
        ],
        ui.CYAN,
    )

    ready = ask_yes_no("  Do you have your Teller credentials ready and want to continue?", default=True)
    if not ready:
        print()
        ui.warning("Please gather your Teller credentials first, then rerun setup.py.")
        sys.exit(1)

    print()
    ui.panel(
        "Certificate Files",
        [
            "Folio expects these files in ./certs before first sync:",
            "certs/teller-cert.pem",
            "certs/teller-key.pem",
        ],
        ui.BLUE,
    )

    cert_path = "certs/teller-cert.pem"
    key_path = "certs/teller-key.pem"

    cert_full = ROOT_DIR / cert_path
    key_full = ROOT_DIR / key_path
    if not cert_full.exists():
        ui.warning(f"Certificate not found at {cert_path}")
    if not key_full.exists():
        ui.warning(f"Key not found at {key_path}")

    print()
    ui.panel(
        "Account Linking",
        [
            "1. Recommended: connect accounts through the UI with Teller Connect",
            "   - easier setup",
            "   - Folio does not store your bank username or password",
            "   - linked Teller tokens are encrypted before storage in the database",
            "2. Advanced: add Teller access tokens manually in setup/.env",
            "   - useful if you prefer managing tokens yourself",
        ],
        ui.CYAN,
    )
    teller_mode_choice = ask("  Teller setup mode", default="1").lower()
    teller_mode = "connect" if teller_mode_choice in ("1", "connect", "ui", "") else "manual"
    tokens: dict[str, str] = {}

    if teller_mode == "connect":
        print()
        ui.success("Teller Connect is the recommended setup path.")
        teller_app_id = ask("  Teller App ID", required=True)
    else:
        print()
        ui.warning("Manual token mode selected.")
        ui.panel(
            "Manual Token Guide",
            [
                "Create or manage access tokens from the Teller dashboard",
                "Add them now, or later in .env as FIRSTNAME_BANKNAME_TOKEN=value",
                "Example: JOHN_BOFA_TOKEN=test_tok_abc123",
                "You can still add a Teller App ID later for UI-based linking",
            ],
            ui.YELLOW,
        )
        ui.muted("You can skip Teller App ID now if you plan to stay on manual tokens.")
        ui.muted("If you want the UI-based Teller Connect flow later, add the App ID and rebuild the frontend.")
        teller_app_id = ask("  Teller App ID (optional for manual mode)")
        print()
        ui.info("Add Teller access tokens manually now if you want.")
        while True:
            token_name = ask(
                "  Token variable name (e.g. JOHN_BOFA_TOKEN, press Enter to finish)",
            )
            if not token_name:
                break
            token_value = ask(f"  Value for {token_name}", required=True)
            tokens[token_name] = token_value

    teller_env = "development"

    return cert_path, key_path, tokens, teller_app_id, teller_env


def gather_simplefin_config():
    print()
    ui.panel(
        "SimpleFIN Setup",
        [
            "SimpleFIN does not require any certificates.",
            "You will finish the connection in the Folio UI after setup.",
            "What you need later:",
            "• register with SimpleFIN Bridge via https://beta-bridge.simplefin.org/",
            "• get your base64 setup token",
            "• open Folio and paste it into the SimpleFIN connect modal",
        ],
        ui.BLUE,
    )
    ui.panel(
        "What Happens Next",
        [
            "Start Folio normally after setup",
            "Use the dashboard + button or Control Center to choose SimpleFIN",
            "Paste the base64 setup token in the UI",
            "Folio will claim the connection and start the initial sync in the background",
        ],
        ui.CYAN,
    )


def generate_token_encryption_key() -> str:
    print()
    ui.info("Connected bank credentials enrolled through the UI are encrypted before storage.")
    try:
        from cryptography.fernet import Fernet

        encryption_key = Fernet.generate_key().decode()
    except ImportError:
        import base64
        import secrets

        encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

    ui.success("Generated token encryption key and will store it in .env")
    return encryption_key


def choose_ai_mode(system_profile: dict) -> str:
    recommended_preset = MODEL_PRESETS[recommend_model_preset(system_profile, MODEL_PRESETS)]
    ui.panel(
        "AI And Categorization Modes",
        [
            f"1. Local AI with Mira ({recommended_preset['label']})",
            "2. No Ollama: DistilBERT categorization, Mira off",
            "3. Rules only: no local model, Mira off",
        ],
        ui.BLUE,
    )
    choice = ask("  AI mode", default="1").lower()
    if choice in ("1", "local", "local ai", ""):
        return "local"
    if choice in ("2", "distilbert", "ml", "no ollama"):
        return "distilbert"
    return "rules_only"


def choose_model_preset(system_profile: dict) -> dict:
    recommended_key = recommend_model_preset(system_profile, MODEL_PRESETS)
    recommended_preset = dict(MODEL_PRESETS[recommended_key])
    advisor_model = recommend_advisor_model(system_profile)
    recommended_preset["advisor_model"] = advisor_model
    recommended_preset["controller_model"] = recommended_preset.get(
        "controller_model",
        recommended_preset.get("categorize_model"),
    )

    ui.info("Detected system profile:")
    ui.kv("System", format_system_profile(system_profile))
    ui.kv(
        "Mira default",
        f"{recommended_preset['label']} (~{recommended_preset['disk_gb']} GB chat model download)",
    )
    ui.kv("Chat model", recommended_preset["copilot_model"])
    ui.kv("Advisor model", advisor_model)
    if system_profile.get("ram_gb") is not None and system_profile["ram_gb"] < 16:
        ui.warning("This local model can be memory-tight on 8 GB machines. Use No AI if the laptop is under pressure.")
    if advisor_model != recommended_preset["copilot_model"]:
        ui.muted("Advisor uses 26B only on systems with at least 30 GB RAM; chat stays on the tested E4B default.")
    ui.muted("Advanced model experiments are available later in Control Center.")
    return recommended_preset


def check_ollama_cli() -> bool:
    return shutil.which("ollama") is not None


def check_ollama_server(base_url: str = "http://localhost:11434", timeout: float = 2.0) -> bool:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def get_ollama_models(base_url: str = "http://localhost:11434", timeout: float = 3.0) -> set[str]:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return set()

    models = set()
    for item in payload.get("models", []):
        name = item.get("name")
        if isinstance(name, str) and name:
            models.add(name)
    return models


def run_install_command(command: list[str], label: str) -> bool:
    print()
    ui.info(f"Installing {label}...")
    try:
        subprocess.run(command, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        ui.error(f"Failed to install {label}: {exc}")
        return False


def maybe_install_ollama(host_os: str) -> bool:
    if check_ollama_cli():
        return True

    print()
    ui.warning("Ollama is not installed yet.")
    installed = False

    if host_os == "macos" and shutil.which("brew"):
        installed = run_install_command(["brew", "install", "--cask", "ollama"], "Ollama")
    elif host_os == "windows" and shutil.which("winget"):
        installed = run_install_command(
            ["winget", "install", "-e", "--id", "Ollama.Ollama"],
            "Ollama",
        )

    if installed:
        print()
        ui.warning(
            "Ollama was installed. You may need to restart your shell, launch the Ollama app, and in some cases restart the system before setup can finish cleanly."
        )
        return check_ollama_cli()

    url = OLLAMA_DOWNLOAD_URLS.get(host_os, "https://ollama.com/download")
    ui.warning(f"Please install Ollama from: {url}")
    ui.muted("Then rerun setup.py.")
    return False


def ensure_ollama_running(host_os: str, prefer_folio_launcher: bool = False) -> bool:
    if prefer_folio_launcher and folio_launcher_available(host_os):
        if not check_tmux():
            ui.error("tmux is required for ./folio.sh to start tuned Ollama.")
            ui.muted("Install tmux, then rerun setup.py. On macOS with Homebrew: brew install tmux")
            return False

        if check_ollama_server():
            if tuned_ollama_session_running():
                ui.success("Tuned Folio Ollama tmux session is already running.")
                return True

            ui.warning("Ollama is already listening on http://localhost:11434, but not from Folio's tuned tmux session.")
            ui.muted("For Mira's dual-slot prompt-cache behavior, stop the normal Ollama app/server and rerun setup.py.")
            return False

        ui.info("Starting tuned Ollama through ./folio.sh for Mira's dual-slot cache behavior...")
        try:
            launcher_env = os.environ.copy()
            launcher_env["FOLIO_ENV_FILE"] = "/dev/null"
            launcher_env["OLLAMA_HOST"] = "127.0.0.1:11434"
            launcher_env["OLLAMA_NUM_PARALLEL"] = "2"
            launcher_env["OLLAMA_MULTIUSER_CACHE"] = "1"
            launcher_env["OLLAMA_KEEP_ALIVE"] = "30m"
            subprocess.run(
                [str(FOLIO_LAUNCHER), "ollama-start"],
                cwd=str(ROOT_DIR),
                env=launcher_env,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            ui.error(f"Could not start tuned Ollama with ./folio.sh: {exc}")
            return False

        for _ in range(15):
            if check_ollama_server():
                return True
            time.sleep(1)

        ui.error("Tuned Ollama did not become reachable on http://localhost:11434.")
        return False

    if check_ollama_server():
        return True

    print()
    ui.warning("Ollama is installed but its local API is not responding on http://localhost:11434.")
    if host_os == "macos":
        ui.muted("Launch the Ollama app once so it can start its background server and link the CLI.")
    elif host_os == "windows":
        ui.muted("Launch Ollama once from the Start Menu so the background server can start.")

    if check_ollama_cli():
        ui.info("Trying to start Ollama server automatically...")
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(["ollama", "serve"], **kwargs)
            for _ in range(15):
                if check_ollama_server():
                    return True
                time.sleep(1)
        except Exception:
            pass

    ui.error("Ollama still is not reachable. Start/restart Ollama manually and rerun setup if needed.")
    return False


def pull_ollama_model(model: str):
    ui.info(f"Pulling model {model}...")
    subprocess.run(["ollama", "pull", model], check=True)


def ensure_ollama_model(model: str, available_models: set[str]) -> set[str]:
    if model in available_models:
        ui.success(f"Found existing model: {model} (skipping download)")
        return available_models

    pull_ollama_model(model)
    refreshed_models = get_ollama_models()
    if model in refreshed_models:
        return refreshed_models
    available_models.add(model)
    return available_models


def distilbert_model_present(model_dir: Path = DISTILBERT_MODEL_DIR) -> bool:
    return all((model_dir / file_path).is_file() for file_path in DISTILBERT_MODEL_FILES)


def distilbert_resolve_url(model_id: str, file_path: str) -> str:
    repo = urllib.parse.quote(model_id, safe="/")
    path = urllib.parse.quote(file_path, safe="/")
    return f"https://huggingface.co/{repo}/resolve/main/{path}"


def download_distilbert_model(
    model_id: str = DISTILBERT_HF_MODEL,
    target_dir: Path = DISTILBERT_MODEL_DIR,
) -> bool:
    if distilbert_model_present(target_dir):
        ui.success(f"Found existing DistilBERT model cache: {target_dir}")
        return True

    print()
    ui.info(f"Downloading DistilBERT model from Hugging Face: {model_id}")
    ui.muted(f"Target: {target_dir}")
    ui.muted("Credit: DoDataThings / Winston B. Model license: Apache-2.0.")

    tmp_dir = target_dir.with_name(f".{target_dir.name}.tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        for file_path in DISTILBERT_MODEL_FILES:
            destination = tmp_dir / file_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            url = distilbert_resolve_url(model_id, file_path)
            ui.muted(f"  fetching {file_path}")
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Folio-setup/1.0"},
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                with destination.open("wb") as handle:
                    shutil.copyfileobj(response, handle)

        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmp_dir, target_dir, dirs_exist_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        ui.warning(f"Could not download DistilBERT during setup: {exc}")
        ui.muted("Folio will leave runtime Hugging Face download enabled and will still fall back safely if the model is unavailable.")
        return False

    ui.success("DistilBERT model downloaded.")
    return True


def gather_ai_config(ai_mode: str, runtime_mode: str, host_os: str) -> dict:
    internal_trove_seed = hashlib.sha256(str(ROOT_DIR).encode("utf-8")).hexdigest()[:24]
    config = {
        "ai_mode": ai_mode,
        "llm_provider": "ollama",
        "trove_key": "",
        "trove_seed": internal_trove_seed,
        "enable_trove": "false",
        "enable_local_enrichment": "false",
        "enable_llm_categorization": "false",
        "categorization_backend": "rules_only",
        "enable_experimental_local_models": "false",
        "receipt_intelligence_enabled": "false",
        "ollama_base_url": "",
        "llamacpp_base_url": "",
        "llamacpp_model": "local",
        "ollama_model_categorize": "",
        "ollama_model_controller": "",
        "ollama_model_copilot": "",
        "mira_advisor_lens_model": "",
        "local_llm_memory_tier": "",
        "local_llm_ram_gb": "",
        "local_enrichment_batch_size": "20",
        "local_enrichment_min_confidence": "medium",
        "mira_enabled": "false",
        "install_distilbert": "false",
        "distilbert_model": DISTILBERT_HF_MODEL,
        "distilbert_model_path": DISTILBERT_CONTAINER_MODEL_PATH,
        "distilbert_local_files_only": "true",
        "distilbert_allow_download": "false",
        "distilbert_required": "false",
        "distilbert_confidence_threshold": "0.90",
        "distilbert_batch_size": "64",
        "distilbert_shadow": "false",
    }

    if ai_mode == "local":
        if host_os not in {"macos", "windows"}:
            ui.warning("Local AI installer flow currently targets macOS and Windows.")
            ui.info("Falling back to No AI mode on this platform.")
            return config

        if not maybe_install_ollama(host_os):
            ui.error("Local AI setup could not continue without Ollama.")
            sys.exit(1)

        if not ensure_ollama_running(
            host_os,
            prefer_folio_launcher=runtime_mode == "docker",
        ):
            ui.error("Ollama is required for Local AI mode.")
            sys.exit(1)

        system_profile = detect_system_profile(host_os)
        preset = choose_model_preset(system_profile)
        ui.panel(
            "Local AI Preset",
            [
                f"Preset: {preset['label']}",
                f"Categorization/enrichment: {preset['categorize_model']}",
                f"Mira chat: {preset['copilot_model']}",
                f"Advisor: {preset['advisor_model']}",
                f"Chat model download: ~{preset['disk_gb']} GB",
            ],
            ui.CYAN,
        )

        available_models = get_ollama_models()
        for model in dict.fromkeys(
            [
                preset["categorize_model"],
                preset.get("controller_model") or preset["categorize_model"],
                preset["copilot_model"],
                preset["advisor_model"],
            ]
        ):
            available_models = ensure_ollama_model(model, available_models)

        config.update(
            {
                "llm_provider": "ollama",
                "enable_local_enrichment": "true",
                "enable_llm_categorization": "true",
                "categorization_backend": "local_llm",
                "enable_trove": "false",
                "receipt_intelligence_enabled": "true",
                "ollama_base_url": (
                    "http://host.docker.internal:11434"
                    if runtime_mode == "docker"
                    else "http://localhost:11434"
                ),
                "llamacpp_base_url": (
                    "http://host.docker.internal:8081"
                    if runtime_mode == "docker"
                    else "http://localhost:8081"
                ),
                "ollama_model_categorize": preset["categorize_model"],
                "ollama_model_controller": preset.get("controller_model") or preset["categorize_model"],
                "ollama_model_copilot": preset["copilot_model"],
                "mira_advisor_lens_model": preset["advisor_model"],
                "local_llm_memory_tier": (
                    "32gb" if (system_profile.get("ram_gb") or 0) >= 30
                    else "16gb" if (system_profile.get("ram_gb") or 0) >= 14
                    else "8gb" if system_profile.get("ram_gb")
                    else ""
                ),
                "local_llm_ram_gb": str(system_profile["ram_gb"]) if system_profile.get("ram_gb") else "",
                "mira_enabled": "true",
            }
        )
        return config

    if ai_mode == "distilbert":
        print()
        ui.info("No-Ollama mode will use deterministic rules plus optional local DistilBERT categorization.")
        ui.muted("Mira, receipt parsing, and LLM merchant enrichment stay disabled without a local LLM.")
        ui.muted("DistilBERT is conservative: if the model is unavailable, Folio falls back to rules and 'Other'.")
        model_path = (
            DISTILBERT_CONTAINER_MODEL_PATH
            if runtime_mode == "docker"
            else str(DISTILBERT_MODEL_DIR)
        )
        pull_now = ask_yes_no(
            "  Download the DistilBERT transaction model now into ./models? (~260 MB)",
            default=True,
        )
        if pull_now and download_distilbert_model(config["distilbert_model"], DISTILBERT_MODEL_DIR):
            distilbert_model_path = model_path
            distilbert_local_files_only = "true"
            distilbert_allow_download = "false"
        else:
            distilbert_model_path = ""
            distilbert_local_files_only = "false"
            distilbert_allow_download = "true"
            ui.muted("The backend can download the model later from Hugging Face, or fall back to rules if unavailable.")
        config.update(
            {
                "categorization_backend": "distilbert",
                "install_distilbert": "true",
                "distilbert_model_path": distilbert_model_path,
                "distilbert_local_files_only": distilbert_local_files_only,
                "distilbert_allow_download": distilbert_allow_download,
                "mira_enabled": "false",
            }
        )
        return config

    print()
    ui.info("Rules-only mode keeps deterministic rules and manual categorization.")
    return config


def gather_security_config():
    ui.info("An API key protects your backend from unauthorized access.")
    ui.muted("Folio will generate one automatically and write it to .env.")
    print()
    import secrets

    api_key = secrets.token_urlsafe(32)
    ui.success("Generated backend API key automatically.")
    return api_key


def write_env_file(config: dict):
    ai_mode = config["ai_mode"]
    lines = [
        "# ==============================================================",
        "# Folio Configuration",
        "# Generated by setup.py",
        "# ==============================================================",
        "",
        f"# AI mode selected during setup: {ai_mode}",
        "",
        "# -- Teller Certificates (optional unless using Teller) --",
        f"TELLER_CERT_PATH={config['cert_path']}",
        f"TELLER_KEY_PATH={config['key_path']}",
        "",
        "# -- Teller Connect (optional unless using Teller) --",
        f"TELLER_APPLICATION_ID={config.get('teller_app_id', '')}",
        f"TELLER_ENVIRONMENT={config.get('teller_env', 'sandbox')}",
        "",
        "# -- Token Encryption (used for Teller UI enrollments and SimpleFIN connections) --",
        f"TOKEN_ENCRYPTION_KEY={config.get('encryption_key', '')}",
        "",
        "# -- Teller Access Tokens (legacy / manual) --",
    ]

    for name, value in config.get("tokens", {}).items():
        lines.append(f"{name}={value}")
    if not config.get("tokens"):
        lines.extend([
            "# Add tokens here: FIRSTNAME_BANKNAME_TOKEN=value",
            "# Or link accounts from the UI after setup",
        ])

    lines.extend(
        [
            "",
            "# -- SimpleFIN --",
            "# SimpleFIN does not require .env credentials during setup.",
            "# Connect it later in the UI by pasting your base64 setup token.",
            "",
            "# -- Security --",
            f"Folio_API_KEY={config.get('api_key', '')}",
            "",
            "# -- Frontend --",
            f"VITE_API_KEY={config.get('api_key', '')}",
            f"VITE_TELLER_APP_ID={config.get('teller_app_id', '')}",
            f"VITE_TELLER_ENVIRONMENT={config.get('teller_env', 'sandbox')}",
            "",
            "# -- Feature Toggles --",
            "DEMO_MODE=false",
            f"ENABLE_TROVE={config.get('enable_trove', 'false')}",
            f"ENABLE_LOCAL_ENRICHMENT={config.get('enable_local_enrichment', 'false')}",
            f"ENABLE_LLM_CATEGORIZATION={config.get('enable_llm_categorization', 'false')}",
            f"CATEGORIZATION_BACKEND={config.get('categorization_backend', 'rules_only')}",
            f"ENABLE_EXPERIMENTAL_LOCAL_MODELS={config.get('enable_experimental_local_models', 'false')}",
            f"RECEIPT_INTELLIGENCE_ENABLED={config.get('receipt_intelligence_enabled', 'false')}",
            f"MIRA_ENABLED={config.get('mira_enabled', 'false')}",
            "COPILOT_MAX_WRITE_ROWS=5000",
            "MIRA_AGENTIC_RUNTIME=vnext",
            "MIRA_VNEXT_EVIDENCE_MAX_TOKENS=180",
            "MIRA_VNEXT_GENERAL_MAX_TOKENS=1000",
            "MIRA_SELECTOR_DECISION_CACHE_ENABLED=1",
            "MIRA_SELECTOR_DECISION_CACHE_TTL_SECONDS=300",
            "MIRA_SELECTOR_DECISION_CACHE_MAX=128",
            "MIRA_SELECTOR_COMPACT_OUTPUT_ENABLED=1",
            "MIRA_FRONT_CONTROLLER_PROTOCOL_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_CHAT_FAST_LANE_ENABLED=1",
            "MIRA_FRONT_CONTROLLER_FINANCE_SCALAR_FAST_LANE_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_READ_ONLY_TABLE_FAST_LANE_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_CHART_FAST_LANE_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_EXPLAIN_COMPARE_FAST_LANE_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_WRITE_PREVIEW_FAST_LANE_ENABLED=0",
            "MIRA_EXPLAIN_LAST_FAST_LANE_ENABLED=0",
            "MIRA_MEMORY_SLASH_BYPASS_ENABLED=1",
            "MIRA_VNEXT_SELECTOR_FALLBACK_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_COMPLEX_FINANCE_REACT_LITE_ENABLED=0",
            "MIRA_COMPLEX_FINANCE_PREVIEW_LLM_ENABLED=0",
            "MIRA_COMPLEX_FINANCE_PREVIEW_MAX_TOKENS=80",
            "MIRA_COMPLEX_FINANCE_PREVIEW_ONLY_ENABLED=1",
            "MIRA_FRONT_CONTROLLER_MAX_TOKENS=900",
            "MIRA_FRONT_CONTROLLER_BACKGROUND_DRAIN_ENABLED=0",
            "MIRA_MEMORY_SUGGESTIONS_ON_EVIDENCE_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_REWARM_AFTER_EVIDENCE_ENABLED=0",
            "MIRA_FRONT_CONTROLLER_REWARM_MAX_TOKENS=16",
            "MIRA_FRONT_CONTROLLER_REWARM_DELAY_MS=3000",
            "MIRA_FRONT_CONTROLLER_REWARM_MIN_QUIET_MS=1200",
            "MIRA_FRONT_CONTROLLER_REWARM_MIN_INTERVAL_SECONDS=8",
            "MIRA_SESSION_SUMMARIES_ENABLED=1",
            "MIRA_SESSION_SUMMARY_CONTEXT_ENABLED=0",
            "MIRA_SESSION_SUMMARY_IDLE_SECONDS=90",
            "MIRA_PENDING_STATE_FAST_RESOLVER_ENABLED=1",
            "MIRA_MEMORY_SUGGESTIONS_ENABLED=true",
            "MIRA_MEMORY_PREFERENCE_CONTEXT_ENABLED=true",
            "MIRA_LLM_TEMPORAL_PARSER_ENABLED=1",
            "MIRA_PERSONA_TEMPLATES_ENABLED=true",
            "MIRA_PERSONA_V2_ENABLED=true",
            "MIRA_CONFIDENCE_CAVEATS_ENABLED=true",
            "",
            "# -- LLM Provider --",
            f"LLM_PROVIDER={config.get('llm_provider', 'ollama')}",
            "",
            "# -- Ollama (Local AI mode) --",
            f"OLLAMA_BASE_URL={config.get('ollama_base_url', '')}",
            "# Host Ollama server launch settings. Use scripts/start_mira_ollama.sh",
            "# or set these in the process that runs `ollama serve`.",
            "OLLAMA_HOST=127.0.0.1:11434",
            "OLLAMA_NUM_PARALLEL=2",
            "OLLAMA_MULTIUSER_CACHE=1",
            "OLLAMA_KEEP_ALIVE=30m",
            "OLLAMA_PREWARM_KEEP_ALIVE=30m",
            "OLLAMA_CONTROLLER_KEEP_ALIVE=30m",
            "OLLAMA_COPILOT_KEEP_ALIVE=30m",
            f"LLAMACPP_BASE_URL={config.get('llamacpp_base_url', '')}",
            f"LLAMACPP_MODEL={config.get('llamacpp_model', 'local')}",
            "LLAMACPP_TIMEOUT=240",
            "LLAMACPP_TEMPERATURE=1.0",
            "LLAMACPP_TOP_P=0.95",
            "LLAMACPP_TOP_K=64",
            "LLAMACPP_THINK=false",
            f"OLLAMA_MODEL_CATEGORIZE={config.get('ollama_model_categorize', '')}",
            f"OLLAMA_MODEL_CONTROLLER={config.get('ollama_model_controller', '')}",
            f"OLLAMA_MODEL_COPILOT={config.get('ollama_model_copilot', '')}",
            f"OLLAMA_MODEL_RECEIPT={config.get('ollama_model_copilot', '')}",
            f"MIRA_ADVISOR_LENS_MODEL={config.get('mira_advisor_lens_model', '')}",
            f"LOCAL_LLM_MEMORY_TIER={config.get('local_llm_memory_tier', '')}",
            f"LOCAL_LLM_RAM_GB={config.get('local_llm_ram_gb', '')}",
            f"LOCAL_ENRICHMENT_BATCH_SIZE={config.get('local_enrichment_batch_size', '20')}",
            f"LOCAL_ENRICHMENT_MIN_CONFIDENCE={config.get('local_enrichment_min_confidence', 'medium')}",
            "OLLAMA_TIMEOUT_CATEGORIZE=600",
            "OLLAMA_TIMEOUT_CONTROLLER=90",
            "OLLAMA_TIMEOUT_COPILOT=240",
            "OLLAMA_TIMEOUT_ADVISOR=600",
            "OLLAMA_PREWARM_TTL_SECONDS=240",
            "",
            "# -- Mira Background And Advisor --",
            "MIRA_BACKGROUND_ANALYST_ENABLED=1",
            "MIRA_BACKGROUND_ANALYST_STORE_ENABLED=1",
            "MIRA_BACKGROUND_ANALYST_AUTO_ENABLED=0",
            "MIRA_BACKGROUND_ANALYST_MIN_INTERVAL_MINUTES=360",
            "MIRA_BACKGROUND_ANALYST_MAX_TOKENS=900",
            "MIRA_BACKGROUND_ANALYST_MAX_DRAFTS=3",
            "MIRA_FINANCIAL_UNDERSTANDING_ENABLED=1",
            "MIRA_FINANCIAL_UNDERSTANDING_LLM_ENABLED=0",
            "MIRA_FINANCIAL_UNDERSTANDING_MAX_FACTS=8",
            "MIRA_FINANCIAL_UNDERSTANDING_MAX_TOKENS=900",
            "MIRA_FINANCIAL_CONTEXT_TOOL_ENABLED=1",
            "MIRA_LIFESTYLE_CONTEXT_PROMPT_ENABLED=1",
            "MIRA_FINANCIAL_CONTEXT_MAX_FACTS=5",
            "MIRA_ADVISOR_CASES_ENABLED=0",
            "MIRA_ADVISOR_CARDS_ENABLED=0",
            "MIRA_ADVISOR_BACKGROUND_AUTO_ENABLED=0",
            "MIRA_ADVISOR_CARD_MAX_COUNT=4",
            "MIRA_ADVISOR_SYNTHESIS_ENABLED=0",
            "MIRA_ADVISOR_SYNTHESIS_STORE_ENABLED=0",
            "MIRA_ADVISOR_SYNTHESIS_MAX_OBSERVATIONS=5",
            "MIRA_ADVISOR_SYNTHESIS_MAX_TOKENS=1400",
            "MIRA_ADVISOR_LENS_SYNTHESIS_ENABLED=0",
            "MIRA_ADVISOR_LENS_STORE_ENABLED=0",
            "MIRA_ADVISOR_LENS_BACKGROUND_AUTO_ENABLED=0",
            "MIRA_ADVISOR_LENS_THINK=0",
            "MIRA_ADVISOR_LENS_KEEP_ALIVE=2m",
            "MIRA_ADVISOR_LENS_TIMEOUT=600",
            "MIRA_ADVISOR_LENS_MIN_INTERVAL_MINUTES=1440",
            "MIRA_ADVISOR_LENS_MIN_MEMO_CHARS=2200",
            "MIRA_ADVISOR_LENS_MAX_TOKENS=1800",
            "MIRA_ADVISOR_LENS_FINAL_MAX_TOKENS=2600",
            "MIRA_ADVISOR_LENS_CONTEXT_ENABLED=0",
            "MIRA_ADVISOR_LENS_CONTEXT_MAX_CHARS=2200",
            "MIRA_ADVISOR_LENS_CONTEXT_MAX_TOKENS=520",
            "MIRA_ADVISOR_LENS_UI_ENABLED=0",
            "MIRA_ADVISOR_LENS_POST_REWARM_ENABLED=0",
            "MIRA_ADVISOR_LENS_POST_REWARM_PURPOSES=controller",
            "MIRA_ADVISOR_LENS_POST_REWARM_MAX_TOKENS=8",
            "MIRA_FINANCIAL_FEEDBACK_LOOP_ENABLED=0",
            "MIRA_MONEY_OUTLOOK_ENABLED=1",
            "MIRA_SAFE_TO_SPEND_ENABLED=1",
            "MIRA_CASH_LOW_POINT_RADAR_ENABLED=1",
            "MIRA_STATED_INTENT_MEMORY_ENABLED=0",
            "MIRA_HABIT_STREAKS_ENABLED=0",
            "MIRA_MONTHLY_RETROSPECTIVE_ENABLED=0",
            "MIRA_ENRICHMENT_REPAIR_ENABLED=0",
            "",
            "# -- DistilBERT Categorization (No-Ollama local ML mode) --",
            f"DISTILBERT_CATEGORIZATION_MODEL={config.get('distilbert_model', 'DoDataThings/distilbert-us-transaction-classifier-v2')}",
            f"DISTILBERT_MODEL_PATH={config.get('distilbert_model_path', '')}",
            f"DISTILBERT_LOCAL_FILES_ONLY={config.get('distilbert_local_files_only', 'true')}",
            f"DISTILBERT_ALLOW_DOWNLOAD={config.get('distilbert_allow_download', 'false')}",
            f"DISTILBERT_REQUIRED={config.get('distilbert_required', 'false')}",
            f"DISTILBERT_CONFIDENCE_THRESHOLD={config.get('distilbert_confidence_threshold', '0.90')}",
            f"DISTILBERT_BATCH_SIZE={config.get('distilbert_batch_size', '64')}",
            f"DISTILBERT_SHADOW={config.get('distilbert_shadow', 'false')}",
            "",
            "# -- Trove Merchant Enrichment --",
            f"TROVE_API_KEY={config.get('trove_key', '')}",
            f"TROVE_USER_SEED={config.get('trove_seed', 'Folio-self-hosted')}",
            "",
            "# -- App Settings --",
            "FOLIO_AUTO_SYNC_ENABLED=true",
            "FOLIO_AUTO_SYNC_INTERVAL_HOURS=12",
            "FOLIO_AUTO_SYNC_STARTUP_DELAY_SECONDS=30",
            "FOLIO_AUTO_SYNC_CHECK_SECONDS=300",
            "FOLIO_AUTO_SYNC_FAILURE_BACKOFF_MINUTES=60",
            "CORS_ORIGINS=http://localhost:5173,http://localhost:3000",
            "",
            "# -- Docker Settings --",
            f"INSTALL_DISTILBERT={config.get('install_distilbert', 'false')}",
            "BACKEND_PORT=8000",
            "FRONTEND_PORT=3000",
            "DB_FILE=Folio.db",
            "",
        ]
    )

    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
    ui.success("Configuration written to .env")


def copy_env_for_local():
    backend_env = BACKEND_DIR / ".env"
    frontend_env = FRONTEND_DIR / ".env"
    shutil.copy(str(ENV_FILE), str(backend_env))
    shutil.copy(str(ENV_FILE), str(frontend_env))
    ui.success("Copied .env to backend/ and frontend/")


def start_docker(use_folio_launcher: bool = False):
    print()
    using_folio_launcher = use_folio_launcher and folio_launcher_available(detect_os())
    if using_folio_launcher:
        ui.info("Starting Folio through ./folio.sh so tuned Ollama is active...")
        command = [str(FOLIO_LAUNCHER), "rebuild"]
    else:
        if use_folio_launcher:
            ui.warning("./folio.sh is not available on this platform; falling back to Docker Compose.")
        ui.info("Building and starting containers...")
        command = ["docker", "compose", "up", "--build", "-d"]
    ui.muted("This may take a few minutes on first run.")
    print()
    try:
        subprocess.run(
            command,
            cwd=str(ROOT_DIR),
            check=True,
        )
        print()
        ui.success("Folio is running.")
        ui.panel(
            "Services",
            [
                "Frontend:  http://localhost:3000",
                "Backend:   internal Docker service via /api",
                "",
                "Stop:      ./folio.sh stop" if using_folio_launcher else "Stop:      docker compose down",
                "Logs:      docker compose logs -f",
                "Restart:   ./folio.sh restart" if using_folio_launcher else "Restart:   docker compose restart",
            ],
            ui.CYAN,
        )
    except subprocess.CalledProcessError as exc:
        print()
        ui.error(f"Docker startup failed: {exc}")
        ui.muted("If Docker Desktop was just installed, start/restart it and rerun setup.")


def start_local():
    print()
    ui.info("Preparing local development setup...")
    print()

    if not check_node():
        if not maybe_install_node(detect_os()):
            sys.exit(1)

    copy_env_for_local()

    venv_dir = BACKEND_DIR / ".venv"
    if not venv_dir.exists():
        ui.info("Creating Python virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    if sys.platform.startswith("win"):
        python = str(venv_dir / "Scripts" / "python")
    else:
        python = str(venv_dir / "bin" / "python")

    cutoff = dependency_cutoff_iso()

    ui.info(f"Installing backend dependencies with a {DEPENDENCY_COOLDOWN_DAYS}-day PyPI cooldown...")
    subprocess.run([python, "-m", "pip", "install", "--upgrade", f"pip=={PIP_VERSION}"], check=True)
    subprocess.run(
        [
            python,
            "-m",
            "pip",
            "install",
            "--require-hashes",
            f"--uploaded-prior-to={cutoff}",
            "-r",
            str(BACKEND_DIR / "requirements.lock"),
        ],
        check=True,
    )
    if env_file_uses_distilbert():
        ui.info("Installing optional DistilBERT categorization dependencies...")
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--require-hashes",
                "-r",
                str(BACKEND_DIR / "requirements-distilbert.lock"),
            ],
            check=True,
        )

    if not (FRONTEND_DIR / "node_modules").exists():
        ui.info(f"Installing frontend dependencies with a {DEPENDENCY_COOLDOWN_DAYS}-day npm cooldown...")
        lockfile = FRONTEND_DIR / "package-lock.json"
        if not lockfile.exists():
            raise SystemExit("Missing frontend/package-lock.json; refusing npm install without a lockfile.")
        npm_before = subprocess.run(
            ["npm", "config", "get", "before"],
            cwd=str(FRONTEND_DIR),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if npm_before in {"", "null", "undefined"}:
            raise SystemExit("npm did not apply frontend/.npmrc min-release-age; upgrade npm before installing.")
        npm_cmd = ["npm", "ci"]
        subprocess.run(npm_cmd, cwd=str(FRONTEND_DIR), check=True)

    print()
    ui.success("Setup complete.")
    ui.panel(
        "Run Locally",
        [
            f"Backend:  cd backend && {python} -m uvicorn main:app --port 8000",
            "Frontend: cd frontend && npm run dev",
            "Open:     http://localhost:5173",
        ],
        ui.CYAN,
    )


def main():
    ui.banner()

    host_os = detect_os()
    system_profile = detect_system_profile(host_os)
    docker_ok, compose_ok, docker_daemon_ok = check_docker()
    python_ok = check_python()
    node_ok = check_node()

    ui.info("Checking prerequisites...")
    print()
    ui.kv("Operating system", host_os)
    ui.kv("Docker CLI", "installed" if docker_ok else "not found")
    ui.kv("Docker Compose", "installed" if compose_ok else "not found")
    ui.kv("Docker daemon", "running" if docker_daemon_ok else "not ready")
    ui.kv("Python 3.11+", "yes" if python_ok else "no")
    ui.kv("Node.js", "installed" if node_ok else "not found")

    has_docker = docker_ok and compose_ok
    has_local = python_ok and node_ok

    if not has_docker and not has_local:
        print()
        ui.error("Neither Docker nor Python+Node were detected.")
        if host_os in DOCKER_DOWNLOAD_URLS:
            ui.muted(f"Install Docker Desktop: {DOCKER_DOWNLOAD_URLS[host_os]}")
            ui.muted("Then reopen your terminal and rerun setup.py.")
        if host_os in NODE_DOWNLOAD_URLS:
            ui.muted(f"Install Node.js 18+: {NODE_DOWNLOAD_URLS[host_os]}")
        ui.muted("For local development, install Python 3.11+ and Node.js 18+.")
        sys.exit(1)

    if ENV_FILE.exists():
        overwrite = ask("\n  .env already exists. Overwrite it?", default="no").lower()
        if overwrite not in ("y", "yes"):
            ui.info("Keeping existing .env.")
            runtime_mode = setup_runtime_choice(has_docker, has_local)
            if runtime_mode == "docker":
                if not docker_daemon_ok:
                    ui.warning("Docker Desktop is installed but not ready. Start/restart Docker Desktop first.")
                    sys.exit(1)
                start_docker(use_folio_launcher=env_file_uses_local_ai())
            else:
                start_local()
            return

    if has_docker and not docker_daemon_ok:
        print()
        ui.warning(
            "Docker Desktop looks installed but its daemon is not ready. If you just installed Docker Desktop, start it and restart your shell or system if needed."
        )

    ui.step(1, "Directory Setup")
    setup_directories()

    runtime_mode = setup_runtime_choice(has_docker, has_local)

    ui.step(2, "Bank Connection")
    bank_provider = gather_bank_provider_choice()
    cert_path = ""
    key_path = ""
    tokens: dict[str, str] = {}
    teller_app_id = ""
    teller_env = "development"

    if bank_provider in {"teller", "both"}:
        cert_path, key_path, tokens, teller_app_id, teller_env = gather_teller_config()
    if bank_provider in {"simplefin", "both"}:
        gather_simplefin_config()

    encryption_key = generate_token_encryption_key()

    ui.step(3, "AI Mode")
    ai_mode = choose_ai_mode(system_profile)
    ai_config = gather_ai_config(ai_mode, runtime_mode, host_os)

    ui.step(4, "Security")
    api_key = gather_security_config()

    ui.step(5, "Writing Configuration")
    write_env_file(
        {
            "cert_path": cert_path,
            "key_path": key_path,
            "tokens": tokens,
            "teller_app_id": teller_app_id,
            "teller_env": teller_env,
            "encryption_key": encryption_key,
            "api_key": api_key,
            **ai_config,
        }
    )

    ui.step(6, "Start Application")
    if runtime_mode == "docker":
        if not docker_daemon_ok:
            ui.warning("Docker Desktop is not ready yet.")
            next_command = "./folio.sh" if ai_mode == "local" else "docker compose up --build -d"
            ui.panel(
                "Next Step",
                ["Start/restart Docker Desktop, then run:", next_command],
                ui.YELLOW,
            )
            return
        start_docker(use_folio_launcher=ai_mode == "local")
    else:
        start_local()


if __name__ == "__main__":
    main()
