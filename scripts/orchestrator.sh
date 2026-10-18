#!/usr/bin/env bash
# Orchestrateur en conteneur : backend OpenAI d'Open WebUI.
#
# Pourquoi un conteneur et plus un processus sur l'hote :
#   - aucun port publie sur la machine : le 8090 n'est plus un "service" visible
#     ni une interface a ouvrir ;
#   - Open WebUI le joint par son nom sur le reseau Docker (orchestrator-lab) ;
#   - la configuration et le journal d'audit restent sur l'hote (volumes).
#
#   ./scripts/orchestrator.sh up        construit si besoin, (re)demarre
#   ./scripts/orchestrator.sh down      arrete
#   ./scripts/orchestrator.sh status    etat + sante vue de l'interieur
#   ./scripts/orchestrator.sh logs      suit le journal
#   ./scripts/orchestrator.sh rebuild   reconstruit l'image puis redemarre
#
# Le conteneur joint les services de WSL (gateway 4000, MCP 9101-9103) par l'IP
# de WSL : Docker Desktop ne partage pas le namespace reseau de WSL. Cette IP
# change au redemarrage de Windows ; `up` recree le conteneur si elle a change.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CONTENEUR="orchestrator-lab"
IMAGE="mcp-orchestrator-lab:latest"
RESEAU="mcp-lab"
IP_FICHIER="$VAR_DIR/orchestrator-ip.txt"

ip_wsl() { hostname -I | awk '{print $1}'; }

reseau_pret() {
  docker network inspect "$RESEAU" >/dev/null 2>&1 \
    || docker network create "$RESEAU" >/dev/null
}

image_pret() {
  docker image inspect "$IMAGE" >/dev/null 2>&1
}

construire() {
  echo "  construction de l'image $IMAGE (une seule fois, puis sur rebuild)"
  docker build -f "$PROJECT_ROOT/orchestrator/Dockerfile" -t "$IMAGE" "$PROJECT_ROOT"
}

demarrer() {
  local ip
  ip="$(ip_wsl)"
  reseau_pret

  if ! image_pret; then
    construire || { echo "  ERREUR : construction de l'image impossible"; return 1; }
  fi

  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
    local ancienne
    ancienne="$(cat "$IP_FICHIER" 2>/dev/null || true)"
    if [[ "$ip" != "$ancienne" ]]; then
      echo "  IP de WSL changee ($ancienne -> $ip) : recreation du conteneur"
      docker rm -f "$CONTENEUR" >/dev/null 2>&1
    else
      docker start "$CONTENEUR" >/dev/null 2>&1 && echo "  conteneur existant demarre"
    fi
  fi

  if ! docker ps -a --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
    if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
      echo "  ERREUR : .env absent (voir .env.example)"; return 1
    fi
    docker run -d --name "$CONTENEUR" \
      --network "$RESEAU" \
      --env-file "$PROJECT_ROOT/.env" \
      -e GATEWAY_URL="http://$ip:${GATEWAY_PORT:-4000}" \
      -e MCP_SUMMARIZER_URL="http://$ip:9101/mcp" \
      -e MCP_CODEDOC_URL="http://$ip:9102/mcp" \
      -e MCP_CODEDB_URL="http://$ip:9103/mcp" \
      -e ORCHESTRATOR_HOST=0.0.0.0 \
      -e ORCHESTRATOR_PORT=8090 \
      -e POLICIES_PATH=/app/config/policies.yaml \
      -e KEYS_DIR=/app/config/keys \
      -e REVOCATION_PATH=/app/config/revoked_tokens.json \
      -e AUDIT_LOG_PATH=/app/var/logs/audit.jsonl \
      -v "$PROJECT_ROOT/config:/app/config" \
      -v "$PROJECT_ROOT/var/logs:/app/var/logs" \
      --restart unless-stopped \
      "$IMAGE" >/dev/null 2>&1 \
      && echo "  conteneur cree (reseau $RESEAU, aucun port publie)"
  fi
  printf '%s\n' "$ip" > "$IP_FICHIER"

  # Sante vue de l'interieur du conteneur : c'est exactement le chemin qu'utilise
  # Open WebUI. Un test depuis l'hote ne prouverait rien (pas de port publie).
  for _ in $(seq 1 45); do
    if docker exec "$CONTENEUR" python -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; then
      echo "  [ok]   orchestrateur joignable sur $RESEAU:$CONTENEUR:8090"
      return 0
    fi
    sleep 2
  done
  echo "  [ko]   orchestrateur pas pret : ./scripts/orchestrator.sh logs"
  return 1
}

case "${1:-status}" in
  up) demarrer ;;
  rebuild)
    # Une image reconstruite ne remplace pas le conteneur existant : Docker
    # redemarre l'ancien. Il faut donc le supprimer pour repartir de l'image.
    if construire; then
      docker rm -f "$CONTENEUR" >/dev/null 2>&1
      demarrer
    fi
    ;;
  down)
    docker stop "$CONTENEUR" >/dev/null 2>&1 && echo "  [stop] orchestrateur" || echo "  [note] deja arrete"
    ;;
  status)
    docker ps -a --filter "name=$CONTENEUR" --format '  etat : {{.Status}}  ports : {{.Ports}}'
    echo "  reseau : $RESEAU | IP de WSL : $(cat "$IP_FICHIER" 2>/dev/null || echo '(pas encore lancee)')  | actuelle : $(ip_wsl)"
    if docker ps --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
      docker exec "$CONTENEUR" python -c "
import json, urllib.request
d = json.load(urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3))
print('  sante :', d)
" 2>/dev/null || echo "  sante : indisponible"
    fi
    ;;
  logs)
    docker logs -f --tail 50 "$CONTENEUR"
    ;;
  *)
    echo "Usage : $0 {up|down|status|logs|rebuild}"
    exit 1
    ;;
esac
