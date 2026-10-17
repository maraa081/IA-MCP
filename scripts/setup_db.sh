#!/usr/bin/env bash
# Met en place la base PostgreSQL de la gateway LiteLLM.
# Le mot de passe est genere ici et n'est jamais affiche : il ne sort que par le
# fichier .env (non versionne) et par les variables du conteneur.
set -uo pipefail
cd /home/maxim/.openclaw/workspace/mcp-orchestrator-lab || exit 1

CONTENEUR="litellm-postgres"
PORT="5433"

# --- 1. Chaine de connexion dans .env ----------------------------------------
if grep -q '^LITELLM_DATABASE_URL=' .env; then
  echo "  .env : LITELLM_DATABASE_URL deja present"
else
  MOT_DE_PASSE=$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  printf '\n# Base PostgreSQL de la gateway (cles virtuelles, budgets, journal des appels).\nLITELLM_DATABASE_URL=postgresql://litellm:%s@127.0.0.1:%s/litellm\n' \
    "$MOT_DE_PASSE" "$PORT" >> .env
  unset MOT_DE_PASSE
  echo "  .env : chaine de connexion ajoutee (mot de passe genere, non affiche)"
fi

MOT_DE_PASSE=$(sed -n "s#^LITELLM_DATABASE_URL=postgresql://litellm:\([^@]*\)@.*#\1#p" .env)
if [[ -z "$MOT_DE_PASSE" ]]; then
  echo "  ERREUR : impossible de relire la chaine de connexion"; exit 1
fi

# --- 2. Conteneur -------------------------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTENEUR"; then
  docker start "$CONTENEUR" >/dev/null 2>&1 && echo "  conteneur existant demarre"
else
  docker run -d --name "$CONTENEUR" \
    -e POSTGRES_USER=litellm \
    -e POSTGRES_PASSWORD="$MOT_DE_PASSE" \
    -e POSTGRES_DB=litellm \
    -p "127.0.0.1:$PORT:5432" \
    -v litellm-pgdata:/var/lib/postgresql/data \
    --restart unless-stopped \
    postgres:16-alpine >/dev/null 2>&1 && echo "  conteneur cree sur 127.0.0.1:$PORT"
fi
unset MOT_DE_PASSE

# --- 3. Attente de disponibilite ---------------------------------------------
for _ in $(seq 1 40); do
  if docker exec "$CONTENEUR" pg_isready -U litellm -d litellm >/dev/null 2>&1; then
    echo "  base prete"
    break
  fi
  sleep 1
done

# --- 4. Etat ------------------------------------------------------------------
docker ps --filter "name=$CONTENEUR" --format '  etat : {{.Status}}  ports : {{.Ports}}'
echo "  volume : litellm-pgdata ($(docker volume inspect litellm-pgdata --format '{{.Mountpoint}}' 2>/dev/null))"
