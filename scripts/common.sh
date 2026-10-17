#!/usr/bin/env bash
# Configuration partagee par tous les scripts du labo.
# Usage : source scripts/common.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_ROOT

# Le fichier .env porte toute la configuration (voir .env.example).
if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

# Valeurs par defaut, pour que les scripts marchent meme sans .env.
export GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:4000}"
export GATEWAY_API_KEY="${GATEWAY_API_KEY:-sk-lab-internal-0001}"
export GATEWAY_MASTER_KEY="${GATEWAY_MASTER_KEY:-sk-master-lab-0001}"
export ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:8080}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
export PLANNER_MODEL="${PLANNER_MODEL:-planner}"
export WRITER_MODEL="${WRITER_MODEL:-writer}"
export MCP_SUMMARIZER_URL="${MCP_SUMMARIZER_URL:-http://127.0.0.1:9101/mcp}"
export MCP_CODEDOC_URL="${MCP_CODEDOC_URL:-http://127.0.0.1:9102/mcp}"

# Chemins de travail.
export VENV="$PROJECT_ROOT/.venv"
export VENV_GATEWAY="$PROJECT_ROOT/.venv-gateway"
export PY="$VENV/bin/python"
export VAR_DIR="$PROJECT_ROOT/var"
export PID_DIR="$VAR_DIR/run"
export LOG_DIR="$VAR_DIR/logs"
export AUDIT_LOG="$LOG_DIR/audit.jsonl"
mkdir -p "$PID_DIR" "$LOG_DIR"

# --- Fonctions utilitaires ----------------------------------------------------

wait_for_http() {
  local url="$1" name="$2" attempts="${3:-60}"
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      echo "  [ok]   $name"
      return 0
    fi
    sleep 1
  done
  echo "  [ko]   $name ($url) : pas de reponse apres ${attempts}s"
  return 1
}

start_background() {
  # start_background <nom> <fichier-journal> <commande...>
  local name="$1" logfile="$2"
  shift 2
  if [[ -f "$PID_DIR/$name.pid" ]] && kill -0 "$(cat "$PID_DIR/$name.pid")" 2>/dev/null; then
    echo "  [deja] $name deja lance (pid $(cat "$PID_DIR/$name.pid"))"
    return 0
  fi
  setsid nohup "$@" >"$logfile" 2>&1 < /dev/null &
  echo $! >"$PID_DIR/$name.pid"
  echo "  [go]   $name (pid $(cat "$PID_DIR/$name.pid")) -> $logfile"
}

stop_background() {
  local name="$1"
  local pidfile="$PID_DIR/$name.pid"
  if [[ ! -f "$pidfile" ]]; then
    return 0
  fi
  local pid
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    # On tue le groupe de processus : uvicorn et les serveurs MCP lancent des
    # enfants, et on ne veut pas laisser de processus orphelins.
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    echo "  [stop] $name (pid $pid)"
  fi
  rm -f "$pidfile"
}
