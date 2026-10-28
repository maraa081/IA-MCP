#!/usr/bin/env bash
# Demarre la pile complete, dans l'ordre des dependances :
#   Ollama -> gateway LiteLLM -> serveurs MCP -> index de code -> orchestrateur
#
#   ./scripts/up.sh                demarrage complet
#   ./scripts/up.sh --no-index     sans reindexation du code (demarrage rapide)
#
# Le script est idempotent (relancable sans risque) et NE S'ARRETE PAS si une
# etape echoue : chaque etage est independant. Une gateway lente ou un Ollama
# absent ne doit pas empecher les serveurs MCP de demarrer - c'etait le
# comportement precedent (set -e) et la cause des MCP absents apres un reboot.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

DO_INDEX=1
for arg in "$@"; do
  case "$arg" in
    --no-index) DO_INDEX=0 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "option inconnue : $arg" >&2; exit 2 ;;
  esac
done

FAILED=()
note_fail() { FAILED+=("$1"); }

# Au reboot, les fichiers PID d'avant l'extinction traînent encore. On nettoie
# avant tout : sinon un PID recycle ferait croire le service deja lance.
echo "== Nettoyage des fichiers PID perimes"
prune_stale_pids

echo "== Moteur d'inference (Ollama)"
if curl -fsS --max-time 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  echo "  [ok]   Ollama deja actif"
elif command -v ollama >/dev/null 2>&1; then
  start_background ollama "$LOG_DIR/ollama.log" ollama serve
  wait_for_http "$OLLAMA_URL/api/version" "Ollama" 90 || note_fail "ollama"
else
  echo "  [ko]   binaire ollama introuvable : modeles locaux indisponibles"
  note_fail "ollama"
fi

echo "== Gateway LiteLLM (point d'entree unique des modeles)"
# Si une base est configuree, elle doit etre joignable AVANT la gateway : sans
# elle, LiteLLM meurt au demarrage (erreur P1001). On (re)demarre le conteneur
# PostgreSQL si Docker est disponible, sinon on previent clairement.
if [[ -n "${LITELLM_DATABASE_URL:-}" ]]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    if docker ps -a --format '{{.Names}}' | grep -qx litellm-postgres; then
      docker start litellm-postgres >/dev/null 2>&1 || true
      for _ in $(seq 1 30); do
        docker exec litellm-postgres pg_isready -U litellm -d litellm >/dev/null 2>&1 && break
        sleep 1
      done
      docker exec litellm-postgres pg_isready -U litellm -d litellm >/dev/null 2>&1 \
        && echo "  [ok]   PostgreSQL pret" \
        || echo "  [note] PostgreSQL ne repond pas encore (voir scripts/setup_db.sh)"
    else
      echo "  [note] LITELLM_DATABASE_URL definie mais conteneur litellm-postgres absent"
      echo "         (./scripts/setup_db.sh pour le creer)"
    fi
  else
    echo "  [note] LITELLM_DATABASE_URL definie mais Docker indisponible :"
    echo "         la gateway risque d'echouer (P1001). Demarrer Docker Desktop."
  fi
fi
start_background gateway "$LOG_DIR/gateway.log" \
  "$VENV_GATEWAY/bin/litellm" --config "$PROJECT_ROOT/gateway/config.yaml" \
  --host "${GATEWAY_HOST:-127.0.0.1}" --port "${GATEWAY_PORT:-4000}"
# 180 s : au premier demarrage, LiteLLM applique ses migrations Prisma et
# telecharge sa chaine d'outils. 60 s suffisaient a faire croire a une panne.
if ! wait_for_http "$GATEWAY_URL/health/liveliness" "Gateway" 180; then
  wait_for_http "$GATEWAY_URL/v1/models" "Gateway (v1/models)" 10 || note_fail "gateway"
fi

echo "== Serveurs MCP"
start_background mcp-summarizer "$LOG_DIR/mcp-summarizer.log" \
  "$PY" -m mcp_servers.summarizer.server --host 0.0.0.0 --port 9101
start_background mcp-codedoc "$LOG_DIR/mcp-codedoc.log" \
  "$PY" -m mcp_servers.codedoc.server --host 0.0.0.0 --port 9102
start_background mcp-codedb "$LOG_DIR/mcp-codedb.log" \
  "$PY" -m mcp_servers.codedb.server --host 0.0.0.0 --port 9103

# On verifie le port de chacun : demarrer un processus ne prouve pas qu'il a
# survecu (port deja pris, import casse, dependance manquante).
wait_for_port 127.0.0.1 9101 "mcp-summarizer" 30 || note_fail "mcp-summarizer"
wait_for_port 127.0.0.1 9102 "mcp-codedoc" 30 || note_fail "mcp-codedoc"
wait_for_port 127.0.0.1 9103 "mcp-codedb" 30 || note_fail "mcp-codedb"

# Le serveur codedb sert un index SQLite : sans lui, ses outils repondent mais
# ne trouvent rien (« index du code est vide »). L'indexation est idempotente
# (empreinte sha256 : les fichiers inchanges ne sont pas reecrits).
if (( DO_INDEX )); then
  echo "== Index de code (serveur codedb)"
  if "$PY" scripts/index_code.py; then
    echo "  [ok]   index de code a jour"
  else
    echo "  [note] indexation en echec : lancer 'python scripts/index_code.py'"
  fi
else
  echo "== Index de code : ignore (--no-index)"
fi

if [[ "${PLANNER_MODE:-auto}" == "laya" ]]; then
  echo "== Modele de decision Laya (planificateur)"
  "$PROJECT_ROOT/scripts/laya.sh" up || echo "  [note] Laya indisponible : repli sur qwen"
fi

echo "== Orchestrateur (conteneur, backend OpenAI d'Open WebUI)"
# Plus de processus sur l'hote et plus de port publie : Open WebUI joint le
# conteneur par son nom sur le reseau Docker (voir scripts/orchestrator.sh).
"$PROJECT_ROOT/scripts/orchestrator.sh" up || note_fail "orchestrator"

echo
if ((${#FAILED[@]} > 0)); then
  echo "Demarrage termine AVEC ERREURS : ${FAILED[*]}"
  echo "Diagnostic : ./scripts/status.sh  puis  ./scripts/doctor.sh"
  exit 1
fi

echo "Pile demarree."
echo "Detail : ./scripts/status.sh"
echo "Interface : ./scripts/openwebui.sh up  ->  $OPENWEBUI_URL"
echo "Journaux : $LOG_DIR"
