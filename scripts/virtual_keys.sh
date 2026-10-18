#!/usr/bin/env bash
# Redemarre la gateway avec la base, cree deux cles virtuelles (une par service)
# et les enregistre dans .env. Aucune cle n'est affichee.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source scripts/common.sh

echo "=== 1. redemarrage de la gateway ==="
stop_background gateway
sleep 2
start_background gateway "$LOG_DIR/gateway.log" \
  "$VENV_GATEWAY/bin/litellm" --config "$PROJECT_ROOT/gateway/config.yaml" \
  --host 127.0.0.1 --port 4000
for _ in $(seq 1 90); do
  curl -fsS --max-time 3 "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1 && break
  sleep 2
done
curl -sS --max-time 5 -o /dev/null -w '  sante : %{http_code}\n' "$GATEWAY_URL/health/liveliness" || echo "  gateway absente"
grep -iE 'error|traceback|exception' "$LOG_DIR/gateway.log" | tail -3 | sed 's/^/  /'

echo "=== 2. creation des cles virtuelles ==="
creer_cle() {
  # creer_cle <alias> <modeles json> <rpm> <budget>
  local alias="$1" modeles="$2" rpm="$3" budget="$4"
  local corps
  corps=$(jq -n --arg a "$alias" --argjson m "$modeles" --argjson r "$rpm" --argjson b "$budget" \
    '{key_alias:$a, models:$m, rpm_limit:$r, max_budget:$b, budget_duration:"30d",
      metadata:{service:$a, environnement:"labo"}}')
  local reponse
  reponse=$(curl -sS --max-time 30 -X POST "$GATEWAY_URL/key/generate" \
    --oauth2-bearer "$GATEWAY_MASTER_KEY" -H 'Content-Type: application/json' -d "$corps")
  if [[ "$(echo "$reponse" | jq -r '.key // empty')" == "" ]]; then
    echo "  echec pour $alias : $(echo "$reponse" | head -c 200)"
    return 1
  fi
  echo "$reponse" | jq -r '.key'
}

if [[ -z "${VIRTUAL_KEY_ORCH:-}" ]]; then
  CLE_ORCH=$(creer_cle "orchestrateur" '["planner","writer"]' 120 20) || exit 1
  echo "  cle 'orchestrateur' creee (modeles planner+writer, 120 rpm, budget 20)"
else
  CLE_ORCH="$VIRTUAL_KEY_ORCH"
fi

if [[ -z "${VIRTUAL_KEY_MCP:-}" ]]; then
  CLE_MCP=$(creer_cle "mcp-serveurs" '["writer"]' 60 10) || exit 1
  echo "  cle 'mcp-serveurs' creee (modele writer uniquement, 60 rpm, budget 10)"
else
  CLE_MCP="$VIRTUAL_KEY_MCP"
fi

echo "=== 3. enregistrement dans .env (valeurs non affichees) ==="
if grep -q '^GATEWAY_API_KEY=' .env; then
  grep -v '^GATEWAY_API_KEY=' .env > .env.tmp && mv .env.tmp .env
fi
if grep -q '^MCP_GATEWAY_API_KEY=' .env; then
  grep -v '^MCP_GATEWAY_API_KEY=' .env > .env.tmp && mv .env.tmp .env
fi
{
  echo ""
  echo "# Cles virtuelles LiteLLM, une par service (gerees en base)."
  echo "# Voir la sortie de : curl -s \$GATEWAY_URL/key/info -H ... (cle maitresse)"
  echo "GATEWAY_API_KEY=$CLE_ORCH"
  echo "# Les serveurs MCP utilisent une cle plus restrictive (modele 'writer' seul)."
  echo "MCP_GATEWAY_API_KEY=$CLE_MCP"
} >> .env
echo "  GATEWAY_API_KEY et MCP_GATEWAY_API_KEY ecrits"

echo "=== 4. controle de restriction : la cle MCP doit refuser 'planner' ==="
REPONSE=$(curl -sS --max-time 60 -X POST "$GATEWAY_URL/v1/chat/completions" \
  --oauth2-bearer "$CLE_MCP" -H 'Content-Type: application/json' \
  -d '{"model":"planner","messages":[{"role":"user","content":"OK"}],"max_tokens":5,"think":false}')
echo "$REPONSE" | jq -c '{erreur: (.error.message // "aucune"), type: (.error.type // "-")}' 2>/dev/null \
  || echo "  $(echo "$REPONSE" | head -c 160)"

echo "=== 5. controle de restriction : la cle MCP doit accepter 'writer' ==="
REPONSE=$(curl -sS --max-time 120 -X POST "$GATEWAY_URL/v1/chat/completions" \
  --oauth2-bearer "$CLE_MCP" -H 'Content-Type: application/json' \
  -d '{"model":"writer","messages":[{"role":"user","content":"Reponds juste : OK"}],"max_tokens":10,"think":false}')
echo "$REPONSE" | jq -c '{modele: .model, contenu: .choices[0].message.content}' 2>/dev/null \
  || echo "  $(echo "$REPONSE" | head -c 160)"
