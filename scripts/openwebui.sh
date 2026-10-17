#!/usr/bin/env bash
# Open WebUI : facade de chat branchee sur la gateway LiteLLM.
#
# Chaine : navigateur -> Open WebUI :3000 -> gateway LiteLLM :4000 -> Ollama.
# Les outils MCP arrivent par la passerelle d'outils (scripts/bridge.sh).
#
#   ./scripts/openwebui.sh up       demarre (ou recree) le conteneur
#   ./scripts/openwebui.sh down     l'arrete
#   ./scripts/openwebui.sh status   etat, URL, adresse des modeles
#   ./scripts/openwebui.sh logs     suit le journal du conteneur
#
# Le conteneur tourne dans Docker Desktop : pour joindre les services de WSL, il
# passe par l'IP de WSL (et non par 127.0.0.1, qui est le conteneur lui-meme).
# Cette IP change au redemarrage de Windows : relancer `up` la remet a jour.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CONTENEUR="open-webui"
IMAGE="ghcr.io/open-webui/open-webui:main"
PORT="${OPENWEBUI_PORT:-3000}"
VOLUME="open-webui-data"
IP_FICHIER="$VAR_DIR/openwebui-ip.txt"
RESEAU="mcp-lab"

# --- Cle de session (persistante : sinon les sessions sautent a chaque restart)
if ! grep -q '^WEBUI_SECRET_KEY=' .env 2>/dev/null; then
  SECRET=$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  printf '\n# Secret de session d_Open WebUI (genere une fois).\nWEBUI_SECRET_KEY=%s\n' "$SECRET" >> .env
  unset SECRET
  echo "  .env : WEBUI_SECRET_KEY generee (valeur non affichee)"
fi
WEBUI_SECRET_KEY="$(sed -n 's/^WEBUI_SECRET_KEY=//p' .env | head -1)"

if [[ -z "${GATEWAY_API_KEY:-}" ]]; then
  echo "  ERREUR : GATEWAY_API_KEY absente de .env (cle virtuelle de la gateway)"; exit 1
fi

ip_wsl() { hostname -I | awk '{print $1}'; }

case "${1:-status}" in
  up)
    IP="$(ip_wsl)"
    ANCIENNE_IP="$(cat "$IP_FICHIER" 2>/dev/null || true)"
    docker network inspect "$RESEAU" >/dev/null 2>&1 \
      || docker network create "$RESEAU" >/dev/null

    if docker ps -a --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
      if [[ "$IP" != "$ANCIENNE_IP" ]]; then
        echo "  IP de WSL changee ($ANCIENNE_IP -> $IP) : recreation du conteneur"
        docker rm -f "$CONTENEUR" >/dev/null 2>&1
      else
        docker start "$CONTENEUR" >/dev/null 2>&1 && echo "  conteneur existant demarre"
      fi
    fi

    if ! docker ps -a --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
      docker volume create "$VOLUME" >/dev/null 2>&1 || true
      docker run -d --name "$CONTENEUR" \
        --network "$RESEAU" \
        -p "$PORT:8080" \
        -e PORT=8080 \
        -e OPENAI_API_BASE_URL="http://$IP:4000/v1" \
        -e OPENAI_API_KEY="$GATEWAY_API_KEY" \
        -e ENABLE_OPENAI_API=true \
        -e ENABLE_OLLAMA_API=false \
        -e WEBUI_SECRET_KEY="$WEBUI_SECRET_KEY" \
        -e WEBUI_URL="http://localhost:$PORT" \
        -e ENABLE_SIGNUP=true \
        -v "$VOLUME:/app/backend/data" \
        --restart unless-stopped \
        "$IMAGE" >/dev/null 2>&1 \
        && echo "  conteneur cree (reseau $RESEAU)"
    fi
    # Un conteneur cree avant l'arrivee du reseau doit y etre rattache une fois.
    docker network connect "$RESEAU" "$CONTENEUR" >/dev/null 2>&1 || true
    printf '%s' "$IP" > "$IP_FICHIER"

    for _ in $(seq 1 60); do
      if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
      sleep 2
    done
    echo "  Open WebUI : http://localhost:$PORT"
    ;;

  down)
    docker stop "$CONTENEUR" >/dev/null 2>&1 && echo "  [stop] Open WebUI" || echo "  [note] conteneur deja arrete"
    ;;

  status)
    docker ps -a --filter "name=$CONTENEUR" --format '  etat : {{.Status}}  ports : {{.Ports}}'
    echo "  URL navigateur : http://localhost:$PORT"
    echo "  IP de WSL utilisee par le conteneur : $(cat "$IP_FICHIER" 2>/dev/null || echo '(pas encore lancee)')  | IP actuelle : $(ip_wsl)"
    echo "  modeles : $(curl -s --max-time 5 "http://127.0.0.1:$PORT/api/models" -o /dev/null -w '%{http_code}' 2>/dev/null) (401 = normal sans session)"
    ;;

  logs)
    docker logs -f --tail 50 "$CONTENEUR"
    ;;

  setup)
    # Premier compte admin, puis branchement sur l'orchestrateur (idempotent).
    "$PY" "$PROJECT_ROOT/scripts/openwebui_bootstrap.py" \
      --base "http://127.0.0.1:$PORT" \
      --ip-wsl "$(ip_wsl)" \
      --credentials "$VAR_DIR/openwebui-admin.txt"
    TOKEN="$("$PY" "$PROJECT_ROOT/scripts/mint_token.py" \
      --sub openwebui --roles analyst --ttl 31536000 --quiet)"
    printf '%s' "$TOKEN" > "$VAR_DIR/openwebui-token.txt"
    chmod 600 "$VAR_DIR/openwebui-token.txt"
    "$PY" "$PROJECT_ROOT/scripts/openwebui_connect.py" \
      --base "http://127.0.0.1:$PORT" --orchestrateur "http://orchestrator-lab:8090/v1" \
      --token "$TOKEN" --credentials "$VAR_DIR/openwebui-admin.txt"
    unset TOKEN
    ;;

  connect)
    # Branche Open WebUI sur l'orchestrateur : fournisseur OpenAI unique, cle =
    # JWT longue duree verifie a chaque requete. Retire les anciens serveurs
    # d'outils (chemin sans JWT, sans GPO, sans audit).
    TOKEN="$("$PY" "$PROJECT_ROOT/scripts/mint_token.py" \
      --sub openwebui --roles analyst --ttl 31536000 --quiet)"
    printf '%s' "$TOKEN" > "$VAR_DIR/openwebui-token.txt"
    chmod 600 "$VAR_DIR/openwebui-token.txt"
    "$PY" "$PROJECT_ROOT/scripts/openwebui_connect.py" \
      --base "http://127.0.0.1:$PORT" \
      --orchestrateur "http://orchestrator-lab:8090/v1" \
      --token "$TOKEN" \
      --credentials "$VAR_DIR/openwebui-admin.txt"
    unset TOKEN
    ;;

  *)
    echo "Usage : $0 {up|down|status|logs|setup|connect}"
    exit 1
    ;;
esac
