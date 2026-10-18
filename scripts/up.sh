#!/usr/bin/env bash
# Demarre la pile complete, dans l'ordre des dependances :
#   Ollama -> gateway LiteLLM -> serveurs MCP -> orchestrateur
#
#   ./scripts/up.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "== Moteur d'inference (Ollama)"
if curl -fsS --max-time 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  echo "  [ok]   Ollama deja actif"
else
  start_background ollama "$LOG_DIR/ollama.log" ollama serve
  wait_for_http "$OLLAMA_URL/api/version" "Ollama" 90
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
wait_for_http "$GATEWAY_URL/health/liveliness" "Gateway" 180 || \
  wait_for_http "$GATEWAY_URL/v1/models" "Gateway (v1/models)" 10 || true

echo "== Serveurs MCP"
start_background mcp-summarizer "$LOG_DIR/mcp-summarizer.log" \
  "$PY" -m mcp_servers.summarizer.server --host 0.0.0.0 --port 9101
start_background mcp-codedoc "$LOG_DIR/mcp-codedoc.log" \
  "$PY" -m mcp_servers.codedoc.server --host 0.0.0.0 --port 9102
start_background mcp-codedb "$LOG_DIR/mcp-codedb.log" \
  "$PY" -m mcp_servers.codedb.server --host 0.0.0.0 --port 9103

sleep 2

# Le serveur codedb sert un index SQLite : sans lui, ses outils repondent mais
# ne trouvent rien (« index du code est vide »). L'indexation est idempotente
# (empreinte sha256 : les fichiers inchanges ne sont pas reecrits).
echo "== Index de code (serveur codedb)"
if "$PY" scripts/index_code.py; then
  echo "  [ok]   index de code a jour"
else
  echo "  [note] indexation en echec : lancer 'python scripts/index_code.py'"
fi

echo "== Orchestrateur (conteneur, backend OpenAI d'Open WebUI)"
# Plus de processus sur l'hote et plus de port publie : Open WebUI joint le
# conteneur par son nom sur le reseau Docker (voir scripts/orchestrator.sh).
"$PROJECT_ROOT/scripts/orchestrator.sh" up || true

echo
echo "Etat du systeme :"
docker exec orchestrator-lab python -c "
import json, urllib.request
print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)), indent=4))
" 2>/dev/null || echo "  (orchestrateur pas encore pret : ./scripts/orchestrator.sh logs)"
echo
echo "Interface unique : ./scripts/openwebui.sh up  ->  $OPENWEBUI_URL"
echo "Connexion des modeles : ./scripts/openwebui.sh connect"
echo "Journaux : $LOG_DIR"
