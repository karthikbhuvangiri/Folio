#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_NAME="${FOLIO_OLLAMA_TMUX_SESSION:-folio-mira-ollama}"
OLLAMA_PORT="${FOLIO_OLLAMA_PORT:-11434}"
START_OLLAMA="${ROOT_DIR}/scripts/start_mira_ollama.sh"
STATE_DIR="${ROOT_DIR}/.folio"
DOCKER_BUILD_STAMP="${STATE_DIR}/docker-build.stamp"
DOCKER_IMAGE_STAMP="${STATE_DIR}/docker-image.stamp"

usage() {
  cat <<EOF
Usage:
  ./folio.sh [start]
  ./folio.sh rebuild
  ./folio.sh fast
  ./folio.sh stop
  ./folio.sh restart
  ./folio.sh status
  ./folio.sh ollama-start
  ./folio.sh ollama-cleanup

EOF
}

require_tmux() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required for this helper. Install tmux or run scripts/start_mira_ollama.sh manually." >&2
    exit 127
  fi
}

port_in_use() {
  command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${OLLAMA_PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

ollama_port_pid() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi

  lsof -nP -t -iTCP:"${OLLAMA_PORT}" -sTCP:LISTEN 2>/dev/null | head -1
}

print_ollama_port_owner() {
  local pid
  pid="$(ollama_port_pid || true)"
  if [[ -z "${pid}" ]]; then
    echo "No process is listening on port ${OLLAMA_PORT}."
    return
  fi

  echo "Process listening on port ${OLLAMA_PORT}:"
  if command -v ps >/dev/null 2>&1; then
    ps -o pid,ppid,lstart,command -p "${pid}" || true
    echo
    echo "Related Ollama processes:"
    ps -axo pid,ppid,lstart,command | awk 'tolower($0) ~ /ollama/ && $0 !~ /awk/ { print }' || true
  else
    lsof -nP -iTCP:"${OLLAMA_PORT}" -sTCP:LISTEN || true
  fi
}

orphaned_ollama_runner_pids() {
  if ! command -v ps >/dev/null 2>&1; then
    return 0
  fi

  ps -axo pid=,ppid=,command= \
    | awk '$2 == 1 && $0 ~ /ollama runner/ { print $1 }'
}

cleanup_orphaned_ollama_runners() {
  local pids
  pids="$(orphaned_ollama_runner_pids | tr '\n' ' ' | xargs 2>/dev/null || true)"
  if [[ -z "${pids}" ]]; then
    return
  fi

  echo "Stopping orphaned Ollama runner process(es): ${pids}"
  # These runners have already lost their parent server and only hold RAM/ports.
  kill ${pids} 2>/dev/null || true
  sleep 1
}

print_useful_commands() {
  cat <<EOF
Useful commands:

View tuned Ollama logs:
tmux attach -t ${SESSION_NAME}

Detach from Ollama logs without stopping it:
Ctrl+b, then d

View Docker logs:
docker compose logs -f

Stop everything:
./folio.sh stop

Restart everything:
./folio.sh restart
EOF
}

build_baseline_stamp() {
  mkdir -p "${STATE_DIR}"

  local created_values
  created_values="$(docker image inspect folio-backend folio-frontend --format '{{.Created}}' 2>/dev/null || true)"
  if [[ -n "${created_values}" ]] && command -v python3 >/dev/null 2>&1; then
    CREATED_VALUES="${created_values}" DOCKER_IMAGE_STAMP="${DOCKER_IMAGE_STAMP}" python3 - <<'PY' >/dev/null 2>&1 || true
import datetime
import os

values = [line.strip() for line in os.environ.get("CREATED_VALUES", "").splitlines() if line.strip()]
if not values:
    raise SystemExit(1)

epochs = []
for value in values:
    normalized = value.replace("Z", "+00:00")
    if "." in normalized:
        head, tail = normalized.split(".", 1)
        if "+" in tail:
            fraction, zone = tail.split("+", 1)
            normalized = f"{head}.{fraction[:6]}+{zone}"
    epochs.append(datetime.datetime.fromisoformat(normalized).timestamp())

path = os.environ["DOCKER_IMAGE_STAMP"]
oldest = min(epochs)
open(path, "a", encoding="utf-8").close()
os.utime(path, (oldest, oldest))
PY
    if [[ -f "${DOCKER_IMAGE_STAMP}" ]]; then
      echo "${DOCKER_IMAGE_STAMP}"
      return
    fi
  fi

  echo "${DOCKER_BUILD_STAMP}"
}

source_changed_since_baseline() {
  local baseline_stamp
  baseline_stamp="$(build_baseline_stamp)"

  if [[ ! -f "${baseline_stamp}" ]]; then
    return 0
  fi

  if [[ "${ROOT_DIR}/docker-compose.yml" -nt "${baseline_stamp}" ]]; then
    return 0
  fi

  if [[ -f "${ROOT_DIR}/.env" && "${ROOT_DIR}/.env" -nt "${baseline_stamp}" ]]; then
    return 0
  fi

  if find "${ROOT_DIR}/backend" "${ROOT_DIR}/frontend" \
    \( -name .venv -o -name venv -o -name __pycache__ -o -name .pytest_cache -o -name node_modules -o -name build -o -name .svelte-kit -o -name .vite \) -prune \
    -o -type f -newer "${baseline_stamp}" -print -quit | grep -q .; then
    return 0
  fi

  return 1
}

start_ollama() {
  require_tmux
  if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    echo "Tuned Ollama tmux session already running: ${SESSION_NAME}"
    cleanup_orphaned_ollama_runners
    return
  fi

  cleanup_orphaned_ollama_runners

  if port_in_use; then
    echo "Port ${OLLAMA_PORT} is already in use, and tmux session ${SESSION_NAME} is not running." >&2
    print_ollama_port_owner >&2
    echo "Stop the existing Ollama server/app first, or run ./folio.sh restart after stopping it." >&2
    exit 1
  fi

  echo "Starting tuned Ollama in tmux session: ${SESSION_NAME}"
  tmux new-session -d -s "${SESSION_NAME}" "cd $(printf '%q' "${ROOT_DIR}") && exec $(printf '%q' "${START_OLLAMA}")"
  sleep 1

  if ! tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    echo "Ollama tmux session exited immediately. Run scripts/start_mira_ollama.sh directly to see the error." >&2
    exit 1
  fi
}

docker_up() {
  local mode="${1:-auto}"
  shift || true

  mkdir -p "${STATE_DIR}"
  case "${mode}" in
    rebuild)
      echo "Starting Folio Docker services with rebuild..."
      docker compose up --build -d "$@"
      touch "${DOCKER_BUILD_STAMP}"
      ;;
    fast)
      echo "Starting Folio Docker services without rebuild..."
      docker compose up -d "$@"
      ;;
    auto)
      if source_changed_since_baseline; then
        echo "Backend/frontend changes detected; rebuilding Docker services..."
        docker compose up --build -d "$@"
        touch "${DOCKER_BUILD_STAMP}"
      else
        echo "No backend/frontend changes detected; starting Docker services..."
        docker compose up -d "$@"
      fi
      ;;
    *)
      echo "Unknown Docker start mode: ${mode}" >&2
      exit 2
      ;;
  esac
}

start_all() {
  local mode="${1:-auto}"
  shift || true
  start_ollama
  docker_up "${mode}" "$@"

  echo
  echo "Folio is starting."
  echo
  print_useful_commands
}

stop_all() {
  require_tmux
  echo "Stopping Folio Docker services..."
  docker compose down

  if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    echo "Stopping tuned Ollama tmux session: ${SESSION_NAME}"
    tmux kill-session -t "${SESSION_NAME}"
  else
    echo "Tuned Ollama tmux session is not running: ${SESSION_NAME}"
  fi
}

status_all() {
  require_tmux
  if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    echo "Tuned Ollama tmux session: running (${SESSION_NAME})"
  else
    echo "Tuned Ollama tmux session: not running (${SESSION_NAME})"
  fi

  if port_in_use; then
    echo "Ollama port ${OLLAMA_PORT}: in use"
  else
    echo "Ollama port ${OLLAMA_PORT}: free"
  fi

  echo
  local orphaned
  orphaned="$(orphaned_ollama_runner_pids | tr '\n' ' ' | xargs 2>/dev/null || true)"
  if [[ -n "${orphaned}" ]]; then
    echo "Orphaned Ollama runner process(es): ${orphaned}"
    echo
  fi

  docker compose ps
}

command="${1:-start}"
case "${command}" in
  start)
    shift || true
    start_all auto "$@"
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    echo
    start_all auto
    ;;
  rebuild)
    shift || true
    start_all rebuild "$@"
    ;;
  fast)
    shift || true
    start_all fast "$@"
    ;;
  status)
    status_all
    ;;
  ollama-start)
    start_ollama
    ;;
  ollama-cleanup)
    cleanup_orphaned_ollama_runners
    ;;
  -h|--help|help)
    usage
    print_useful_commands
    ;;
  *)
    start_all auto "$@"
    ;;
esac
