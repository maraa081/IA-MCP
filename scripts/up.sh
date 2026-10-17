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
  wait_for_http "$OLLAMA_URL/api/version" "Ollama" 30
fi

echo "== Gateway LiteLLM (point d'entree unique des modeles)"
start_background gateway "$LOG_DIR/gateway.log" \
  "$VENV_GATEWAY/bin/litellm" --config "$PROJECT_ROOT/gateway/config.yaml" \
  --host "${GATEWAY_HOST:-127.0.0.1}" --port "${GATEWAY_PORT:-4000}"
wait_for_http "$GATEWAY_URL/health/liveliness" "Gateway" 60 || \
  wait_for_http "$GATEWAY_URL/v1/models" "Gateway (v1/models)" 10 || true

echo "== Serveurs MCP"
start_background mcp-summarizer "$LOG_DIR/mcp-summarizer.log" \
  "$PY" -m mcp_servers.summarizer.server --port 9101
start_background mcp-codedoc "$LOG_DIR/mcp-codedoc.log" \
  "$PY" -m mcp_servers.codedoc.server --port 9102
start_background mcp-codedb "$LOG_DIR/mcp-codedb.log" \
  "$PY" -m mcp_servers.codedb.server --port 9103
echo "== Passerelle d'outils (MCP -> OpenAPI, pour Open WebUI)"
"$PROJECT_ROOT/scripts/bridge.sh" up

sleep 2

echo "== Orchestrateur"
start_background orchestrator "$LOG_DIR/orchestrator.log" \
  "$PY" -m uvicorn orchestrator.app.main:app \
  --host "${ORCHESTRATOR_HOST:-127.0.0.1}" --port "${ORCHESTRATOR_PORT:-8090}"
wait_for_http "$ORCHESTRATOR_URL/health" "Orchestrateur" 60 || true

echo
echo "Etat du systeme :"
curl -fsS "$ORCHESTRATOR_URL/health" | "${PY}" -m json.tool 2>/dev/null || \
  echo "  (orchestrateur pas encore pret : voir $LOG_DIR/orchestrator.log)"
echo
echo "Documentation interactive : $ORCHESTRATOR_URL/docs"
echo "Facade de chat (facultative) : ./scripts/openwebui.sh up  ->  $OPENWEBUI_URL"
echo "Journaux : $LOG_DIR"
