#!/usr/bin/env bash
# Passerelle d'outils : traduit les serveurs MCP en API OpenAPI.
#
# Pourquoi ce composant : Open WebUI ne parle pas MCP, il parle OpenAPI. mcpo
# (outil officiel de l'equipe Open WebUI) fait le pont : il se connecte aux
# serveurs MCP en streamable-http et publie chacun de leurs outils comme une
# route REST, avec un document OpenAPI. Open WebUI importe ce document comme
# "serveur d'outils" : chaque outil devient une case a cocher par conversation.
#
#   ./scripts/bridge.sh up       demarre la passerelle
#   ./scripts/bridge.sh down     l'arrete
#   ./scripts/bridge.sh status   etat et URL des documents OpenAPI
#
# La passerelle ne detient aucune cle de modele : ce sont les serveurs MCP qui
# parlent a la gateway LiteLLM, avec leur propre cle (moindre privilege).
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

PORT="${MCPO_PORT:-9300}"
CONFIG="$PROJECT_ROOT/bridge/mcpo.config.json"
MCPO="$VENV_BRIDGE/bin/mcpo"

# --- Cle d'acces a la passerelle ----------------------------------------------
# Generee une seule fois, ecrite dans .env (non versionne), jamais affichee.
if ! grep -q '^MCPO_API_KEY=' .env 2>/dev/null; then
  CLE=$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf '\n# Cle d_acces de la passerelle d_outils (mcpo), lue par Open WebUI.\nMCPO_API_KEY=%s\n' "$CLE" >> .env
  unset CLE
  echo "  .env : MCPO_API_KEY generee (valeur non affichee)"
fi
MCPO_API_KEY="$(sed -n 's/^MCPO_API_KEY=//p' .env | head -1)"
export MCPO_API_KEY

command -v "$MCPO" >/dev/null 2>&1 || { echo "  ERREUR : mcpo absent. Installer : pip install mcpo"; exit 1; }
[[ -f "$CONFIG" ]] || { echo "  ERREUR : $CONFIG introuvable"; exit 1; }

case "${1:-status}" in
  up)
    echo "Passerelle d'outils (mcpo) sur ${MCPO_HOST:-0.0.0.0}:$PORT"
    start_background mcpo "$LOG_DIR/mcpo.log" \
      "$MCPO" --config "$CONFIG" \
      --host "${MCPO_HOST:-0.0.0.0}" --port "$PORT" \
      --api-key "$MCPO_API_KEY" --name "lab-mcp-tools" \
      --description "Outils MCP du labo exposes en OpenAPI"
    wait_for_http "http://127.0.0.1:$PORT/docs" "Passerelle d'outils" 30 || true
    ;;

  down)
    stop_background mcpo
    ;;

  status)
    echo "Etat :"
    if [[ -f "$PID_DIR/mcpo.pid" ]] && kill -0 "$(cat "$PID_DIR/mcpo.pid")" 2>/dev/null; then
      echo "  [ok]   mcpo actif (pid $(cat "$PID_DIR/mcpo.pid"))"
    else
      echo "  [ko]   mcpo arrete"
    fi
    echo
    echo "Documents OpenAPI a declarer dans Open WebUI (Admin -> Outils) :"
    for serveur in summarizer codedoc codedb; do
      printf '  %-11s http://%s:%s/%s/openapi.json\n' \
        "$serveur" "$(hostname -I | awk '{print $1}')" "$PORT" "$serveur"
    done
    echo
    echo "Cle d'acces (a coller dans Open WebUI) :"
    echo "  grep '^MCPO_API_KEY=' .env"
    ;;

  *)
    echo "Usage : $0 {up|down|status}"
    exit 1
    ;;
esac
