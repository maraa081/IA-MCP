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
export ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:8090}"
export MCPO_URL="${MCPO_URL:-http://127.0.0.1:9300}"
export OPENWEBUI_URL="${OPENWEBUI_URL:-http://127.0.0.1:3000}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
export PLANNER_MODEL="${PLANNER_MODEL:-planner}"
export WRITER_MODEL="${WRITER_MODEL:-writer}"
export MCP_SUMMARIZER_URL="${MCP_SUMMARIZER_URL:-http://127.0.0.1:9101/mcp}"
export MCP_CODEDOC_URL="${MCP_CODEDOC_URL:-http://127.0.0.1:9102/mcp}"
export LAYA_PORT="${LAYA_PORT:-9104}"
export LAYA_URL="${LAYA_URL:-http://127.0.0.1:9104}"
export LAYA_MODEL="${LAYA_MODEL:-convaiinnovations/laya-multilingual}"

# Chemins de travail.
export VENV="$PROJECT_ROOT/.venv"
export VENV_GATEWAY="$PROJECT_ROOT/.venv-gateway"
export VENV_BRIDGE="$PROJECT_ROOT/.venv-bridge"
export PY="$VENV/bin/python"
export VAR_DIR="$PROJECT_ROOT/var"
export PID_DIR="$VAR_DIR/run"
export TMP_DIR="$VAR_DIR/tmp"
export LOG_DIR="$VAR_DIR/logs"
export AUDIT_LOG="$LOG_DIR/audit.jsonl"
mkdir -p "$PID_DIR" "$LOG_DIR" "$TMP_DIR"

# Chemin du vrai binaire curl, avant que les scripts ne definissent une fonction
# du meme nom (voir curl_api).
CURL_REEL="$(type -P curl || true)"

# --- Fonctions utilitaires ----------------------------------------------------

wait_for_http() {
  local url="$1" name="$2" attempts="${3:-60}"
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      echo "  [ok]   $name"
      return 0
    fi
    # Un premier demarrage peut etre long (migrations LiteLLM, chargement du
    # modele) : on montre que le script avance au lieu de laisser croire a un gel.
    if (( i % 15 == 0 )); then
      printf '  ...    %s : toujours en cours (%ds)\n' "$name" "$i"
    fi
    sleep 1
  done
  echo "  [ko]   $name ($url) : pas de reponse apres ${attempts}s"
  return 1
}

wait_for_port() {
  # Un serveur MCP repond en HTTP mais refuse un GET sans session : tester le
  # port est plus fiable qu'un GET qui renverrait 400/406 sur un service sain.
  local host="$1" port="$2" name="$3" attempts="${4:-30}"
  for ((i = 1; i <= attempts; i++)); do
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      exec 3<&- 2>/dev/null || true
      echo "  [ok]   $name ($host:$port)"
      return 0
    fi
    if (( i % 15 == 0 )); then
      printf '  ...    %s : toujours en cours (%ds)\n' "$name" "$i"
    fi
    sleep 1
  done
  echo "  [ko]   $name ($host:$port) : port ferme apres ${attempts}s"
  return 1
}

# --- Identification fiable des processus -------------------------------------
# Un fichier PID seul ne suffit pas. Apres un redemarrage, les numeros de PID
# sont reattribues : `kill -0` sur un PID recycle reussit, le script conclut
# "deja lance" et le service ne redemarre jamais. C'est exactement le symptome
# des serveurs MCP absents au reboot. On enregistre donc AUSSI l'heure de
# demarrage du processus (/proc/<pid>/stat, champ 22) : elle reste unique meme
# quand le PID est reutilise.

proc_starttime() {
  local stat
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  [[ -n "$stat" ]] || return 1
  stat="${stat##*) }"   # le champ comm peut contenir des espaces
  # shellcheck disable=SC2086
  set -- $stat          # reindexe : champ 22 du stat = position 20 ici
  printf '%s' "${20}"
}

pid_of() { tr -dc '0-9' <"$PID_DIR/$1.pid" 2>/dev/null || true; }

pid_is_ours() {
  local name="$1" pid startfile="$PID_DIR/$1.start"
  [[ -f "$PID_DIR/$name.pid" ]] || return 1
  pid="$(pid_of "$name")"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # Fichier d'heure absent (ancien format) : on ne peut pas distinguer un PID
  # recycle, on fait confiance au fichier une derniere fois.
  [[ -f "$startfile" ]] || return 0
  [[ "$(proc_starttime "$pid")" == "$(cat "$startfile")" ]]
}

forget_pid() { rm -f "$PID_DIR/$1.pid" "$PID_DIR/$1.start"; }

prune_stale_pids() {
  # Supprime les fichiers PID dont le processus n'est plus le notre. A lancer
  # au demarrage : les PID ecrits avant le reboot ne doivent pas bloquer.
  local f name n=0
  for f in "$PID_DIR"/*.pid; do
    [[ -e "$f" ]] || continue
    name="$(basename "$f" .pid)"
    if ! pid_is_ours "$name"; then
      forget_pid "$name"
      n=$((n + 1))
    fi
  done
  (( n > 0 )) && echo "  [clean] $n fichier(s) PID perime(s) supprime(s)"
  return 0
}

start_background() {
  # start_background <nom> <fichier-journal> <commande...>
  local name="$1" logfile="$2" pid
  shift 2
  if pid_is_ours "$name"; then
    echo "  [deja] $name deja lance (pid $(pid_of "$name"))"
    return 0
  fi
  if [[ -f "$PID_DIR/$name.pid" ]]; then
    echo "  [stale] $name : pid $(cat "$PID_DIR/$name.pid") inactif ou recycle, nettoyage"
    forget_pid "$name"
  fi
  setsid nohup "$@" >"$logfile" 2>&1 < /dev/null &
  pid=$!
  echo "$pid" >"$PID_DIR/$name.pid"
  proc_starttime "$pid" >"$PID_DIR/$name.start" 2>/dev/null || true
  echo "  [go]   $name (pid $pid) -> $logfile"
}

# Les scripts de test appellent l'orchestrateur par son URL habituelle, mais il
# n'a plus de port publie : il tourne en conteneur, joignable seulement depuis le
# reseau Docker. On rejoue donc les requetes DANS le conteneur. Le code des
# scripts ne change pas ; seul l'aiguillage est centralise ici.
#
#   curl_api <arguments curl...>
#
# Les appels qui ne visent pas l'orchestrateur (gateway, Ollama, MCP) partent
# normalement depuis l'hote.
curl_api() {
  local args=("$@") argument
  for argument in "${args[@]}"; do
    case "$argument" in
      *127.0.0.1:8090*|*localhost:8090*)
        if docker ps --format '{{.Names}}' | grep -qx orchestrator-lab; then
          docker exec -i orchestrator-lab "$CURL_REEL" "${args[@]}"
          return $?
        fi
        echo "orchestrateur non demarre (./scripts/orchestrator.sh up)" >&2
        return 7
        ;;
    esac
  done
  "$CURL_REEL" "${args[@]}"
}

stop_background() {
  local name="$1" pid
  [[ -f "$PID_DIR/$name.pid" ]] || return 0
  pid="$(pid_of "$name")"
  # pid_is_ours evite de tuer un processus innocent qui aurait herite du PID.
  if [[ -n "$pid" ]] && pid_is_ours "$name"; then
    # On tue le groupe de processus : uvicorn et les serveurs MCP lancent des
    # enfants, et on ne veut pas laisser de processus orphelins.
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    echo "  [stop] $name (pid $pid)"
  fi
  forget_pid "$name"
}
