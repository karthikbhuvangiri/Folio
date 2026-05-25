#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${FOLIO_ENV_FILE:-${ROOT_DIR}/.env}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-2}"
export OLLAMA_MULTIUSER_CACHE="${OLLAMA_MULTIUSER_CACHE:-1}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30m}"

print_config() {
  cat <<EOF
OLLAMA_HOST=${OLLAMA_HOST}
OLLAMA_NUM_PARALLEL=${OLLAMA_NUM_PARALLEL}
OLLAMA_MULTIUSER_CACHE=${OLLAMA_MULTIUSER_CACHE}
OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE}
OLLAMA_PREWARM_KEEP_ALIVE=${OLLAMA_PREWARM_KEEP_ALIVE:-}
OLLAMA_CONTROLLER_KEEP_ALIVE=${OLLAMA_CONTROLLER_KEEP_ALIVE:-}
OLLAMA_COPILOT_KEEP_ALIVE=${OLLAMA_COPILOT_KEEP_ALIVE:-}
EOF
}

if [[ "${1:-}" == "--print-env" ]]; then
  print_config
  exit 0
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama is not on PATH. Install Ollama or add it to PATH first." >&2
  exit 127
fi

host_port="${OLLAMA_HOST##*:}"
if [[ "${host_port}" =~ ^[0-9]+$ ]] && command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"${host_port}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port ${host_port} is already in use. Stop the existing Ollama server/app first." >&2
    echo "Current target config would be:" >&2
    print_config >&2
    exit 1
  fi
fi

echo "Starting tuned Ollama for Mira:"
print_config
exec ollama serve
