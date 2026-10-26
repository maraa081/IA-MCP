#!/usr/bin/env bash
# Sidecar du modele de decision Laya (planificateur).
#
#   ./scripts/laya.sh up       charge le modele et demarre le service
#   ./scripts/laya.sh down     l'arrete
#   ./scripts/laya.sh status   etat + sante
#   ./scripts/laya.sh logs     suit le journal
#
# Le service n'est utile que si PLANNER_MODE=laya dans .env. S'il est arrete,
# l'orchestrateur retombe automatiquement sur le planificateur qwen.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

VENV_LAYA="$PROJECT_ROOT/.venv-laya"
PY_LAYA="$VENV_LAYA/bin/python"
PORT="${LAYA_PORT:-9104}"
MODELE="${LAYA_MODEL:-convaiinnovations/laya-multilingual}"

case "${1:-status}" in
  up)
    if [[ ! -x "$PY_LAYA" ]]; then
      echo "  ERREUR : $VENV_LAYA absent."
      echo "  Creer l'environnement :"
      echo "    python3 -m venv .venv-laya"
      echo "    ./.venv-laya/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch"
      echo "    ./.venv-laya/bin/pip install laya"
      exit 1
    fi
    start_background laya "$LOG_DIR/laya.log" \
      "$PY_LAYA" "$PROJECT_ROOT/scripts/laya_server.py" \
      --host 0.0.0.0 --port "$PORT" --model "$MODELE" --max-len "${LAYA_MAX_LEN:-8192}"
    # Le premier demarrage telecharge le checkpoint (plusieurs centaines de Mo)
    # et le charge : on laisse jusqu'a 5 minutes avant de conclure a un echec.
    wait_for_http "http://127.0.0.1:$PORT/health" "Laya" 300 || true
    ;;
  down)
    stop_background laya
    ;;
  status)
    echo "  modele : $MODELE"
    echo "  url    : http://127.0.0.1:$PORT"
    if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" 2>/dev/null; then
      echo
    else
      echo "  etat   : non demarre (ou en cours de chargement)"
    fi
    ;;
  logs)
    tail -f "$LOG_DIR/laya.log"
    ;;
  *)
    echo "Usage : $0 {up|down|status|logs}"
    exit 1
    ;;
esac
