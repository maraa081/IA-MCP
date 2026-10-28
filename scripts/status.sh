#!/usr/bin/env bash
# Etat compact de la pile : un tableau, une ligne par service.
#
#   ./scripts/status.sh          tableau + code de sortie (1 si un service clé est KO)
#   ./scripts/status.sh --quiet  pas de sortie, seulement le code de sortie
#
# Complement leger de doctor.sh (diagnostic detaille et reparations).
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
say() { (( QUIET )) || printf '%s\n' "$*"; }

KO=0

ligne() {
  # ligne <nom> <etat> <detail>
  if (( QUIET )); then return 0; fi
  local etat="$1" nom="$2" detail="$3" marque
  case "$etat" in
    ok)   marque="ok  " ;;
    ko)   marque="KO  " ;;
    *)    marque="--  " ;;
  esac
  printf '%-10s %-22s %s\n' "$marque" "$nom" "$detail"
}

say "SERVICE    ETAT                   DETAIL"

# --- Ollama -------------------------------------------------------------------
if version="$(curl -fsS --max-time 3 "$OLLAMA_URL/api/version" 2>/dev/null)"; then
  ligne ok "ollama" "$OLLAMA_URL ($(printf '%s' "$version" | tr -d '{}"' | sed 's/version://'))"
else
  ligne ko "ollama" "$OLLAMA_URL ne repond pas"
  KO=1
fi

# --- Gateway LiteLLM ----------------------------------------------------------
if curl -fsS --max-time 3 "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
  pid="$(pid_of gateway)"
  ligne ok "gateway (litellm)" "$GATEWAY_URL${pid:+ (pid $pid)}"
else
  ligne ko "gateway (litellm)" "$GATEWAY_URL ne repond pas"
  KO=1
fi

# --- Serveurs MCP -------------------------------------------------------------
check_mcp() {
  local nom="$1" port="$2" pid
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    exec 3<&- 2>/dev/null || true
    pid="$(pid_of "$nom")"
    ligne ok "$nom" ":$port${pid:+ (pid $pid)}"
  else
    ligne ko "$nom" ":$port ferme"
    KO=1
  fi
}
check_mcp mcp-summarizer 9101
check_mcp mcp-codedoc 9102
check_mcp mcp-codedb 9103

# --- Index de code ------------------------------------------------------------
if [[ -f "$VAR_DIR/codedb.sqlite" ]]; then
  ligne ok "index de code" "$VAR_DIR/codedb.sqlite ($(du -h "$VAR_DIR/codedb.sqlite" | cut -f1))"

else
  ligne -- "index de code" "absent (python scripts/index_code.py)"
fi

# --- Orchestrateur (conteneur) ------------------------------------------------
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx orchestrator-lab; then
  if docker exec orchestrator-lab python -c \
      "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=3)" \
      >/dev/null 2>&1; then
    ligne ok "orchestrateur" "conteneur orchestrator-lab /health"
  else
    ligne ko "orchestrateur" "conteneur actif mais /health muet"
    KO=1
  fi
else
  ligne ko "orchestrateur" "conteneur orchestrator-lab arrete"
  KO=1
fi

# --- Open WebUI ---------------------------------------------------------------
if curl -fsS --max-time 3 "$OPENWEBUI_URL/health" >/dev/null 2>&1 \
   || curl -fsS --max-time 3 "$OPENWEBUI_URL" >/dev/null 2>&1; then
  ligne ok "open-webui" "$OPENWEBUI_URL"
else
  ligne ko "open-webui" "$OPENWEBUI_URL ne repond pas"
  KO=1
fi

(( KO )) && say "" && say "Au moins un service est indisponible : ./scripts/start.sh pour relancer."
exit "$KO"
