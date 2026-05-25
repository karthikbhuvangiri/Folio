from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
from pathlib import Path

RECOMMENDED_LOCAL_MODEL = "gemma4:e4b"
HIGH_MEMORY_ADVISOR_MODEL = "gemma4:26b"
ADVISOR_HIGH_MEMORY_MIN_RAM_GB = 30


def load_model_presets(root_dir: Path) -> dict[str, dict]:
    with (root_dir / "model_presets.json").open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if "presets" not in payload:
        return payload

    return {key: dict(value) for key, value in payload["presets"].items()}


def _detect_total_ram_bytes(host_os: str) -> int | None:
    try:
        if host_os == "macos":
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
                return int(result.stdout.strip())
            except Exception:
                page_size = os.sysconf("SC_PAGE_SIZE")
                phys_pages = os.sysconf("SC_PHYS_PAGES")
                if page_size > 0 and phys_pages > 0:
                    return int(page_size * phys_pages)

        if host_os == "windows":
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


def detect_system_profile(host_os: str) -> dict:
    total_ram_bytes = _detect_total_ram_bytes(host_os)
    ram_gb = round(total_ram_bytes / (1024**3)) if total_ram_bytes else None
    architecture = platform.machine().lower() or "unknown"
    cpu_count = os.cpu_count() or 0

    return {
        "host_os": host_os,
        "architecture": architecture,
        "cpu_count": cpu_count,
        "ram_gb": ram_gb,
        "is_apple_silicon": host_os == "macos" and architecture in {"arm64", "aarch64"},
    }


def recommend_model_preset(system_profile: dict, model_presets: dict[str, dict]) -> str:
    if "default" in model_presets:
        return "default"

    ram_gb = system_profile.get("ram_gb")
    cpu_count = system_profile.get("cpu_count", 0)
    architecture = system_profile.get("architecture", "")

    if ram_gb is None:
        return "balanced"

    if ram_gb >= 32 and cpu_count >= 8:
        return "quality"

    if ram_gb >= 16:
        return "balanced"

    if ram_gb >= 8:
        return "light"

    return "light"


def recommend_advisor_model(system_profile: dict) -> str:
    ram_gb = system_profile.get("ram_gb")
    if ram_gb is not None and ram_gb >= ADVISOR_HIGH_MEMORY_MIN_RAM_GB:
        return HIGH_MEMORY_ADVISOR_MODEL
    return RECOMMENDED_LOCAL_MODEL


def format_system_profile(system_profile: dict) -> str:
    ram_label = f"{system_profile['ram_gb']} GB RAM" if system_profile.get("ram_gb") else "RAM unknown"
    cpu_label = f"{system_profile.get('cpu_count', 0)} CPU threads"
    arch_label = system_profile.get("architecture", "unknown")
    return f"{ram_label}, {cpu_label}, {arch_label}"
