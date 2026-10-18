#!/usr/bin/env bash
# Garantit le client prisma (via litellm[extra_proxy]) si besoin, redemarre la
# gateway et verifie les migrations.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source scripts/common.sh

echo "=== prisma ==="
# Deux conditions pour que la gateway puisse utiliser la base :
#   1. le paquet prisma (installe par litellm[extra_proxy]) ;
#   2. le client genere : LiteLLM fournit schema.prisma mais pas le client, et
#      un pip install ne le cuit pas (contrairement a l'image Docker). Sans
#      generate, la gateway refuse de demarrer des qu'une base est configuree.
if ./.venv-gateway/bin/python -c "import prisma" 2>/dev/null; then
  echo "  deja installe : $(./.venv-gateway/bin/python -c 'import importlib.metadata as m; print(m.version("prisma"))')"
else
  echo "  installation..."
  # litellm[extra_proxy] (et non prisma seul) : la version de prisma est alors
  # alignee sur celle attendue par LiteLLM. Un prisma isole peut etre trop recent
  # ou trop ancien et rendre le client inutilisable.
  ./.venv-gateway/bin/pip install --quiet 'litellm[extra_proxy]' 2>&1 | tail -3
  ./.venv-gateway/bin/python -c "import prisma, importlib.metadata as m; print('  installe :', m.version('prisma'))" || echo "  ECHEC"
fi

if ./.venv-gateway/bin/python -c "from prisma import Prisma" 2>/dev/null; then
  echo "  client prisma deja genere"
else
  SCHEMA="$(./.venv-gateway/bin/python -c "import litellm, os; print(os.path.join(os.path.dirname(litellm.__file__), 'proxy', 'schema.prisma'))")"
  echo "  generation du client prisma (schema : $SCHEMA)..."
  # Le generateur prisma-client-py doit etre sur le PATH : la CLI prisma (Node)
  # le lance comme sous-processus, et sans le venv/bin sur le PATH elle echoue
  # avec 'prisma-client-py: not found'.
  export PATH="$VENV_GATEWAY/bin:$PATH"
  if prisma generate --schema "$SCHEMA"; then
    echo "  client prisma genere"
  else
    echo "  ECHEC de la generation : voir le message ci-dessus"
    exit 1
  fi
fi

echo "=== redemarrage de la gateway ==="
stop_background gateway
sleep 2
start_background gateway "$LOG_DIR/gateway.log" \
  "$VENV_GATEWAY/bin/litellm" --config "$PROJECT_ROOT/gateway/config.yaml" \
  --host "${GATEWAY_HOST:-127.0.0.1}" --port "${GATEWAY_PORT:-4000}"

echo "=== attente ==="
for _ in $(seq 1 90); do
  if curl -fsS --max-time 3 "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
    echo "  gateway prete"
    break
  fi
  sleep 2
done

echo "=== journal : migrations et base ==="
grep -iE 'migration|prisma|database|db |error|traceback' "$LOG_DIR/gateway.log" | tail -12

echo "=== tables creees ==="
docker exec litellm-postgres psql -U litellm -d litellm -tAc \
  "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null \
  | sed 's/^/  tables dans le schema public : /'
docker exec litellm-postgres psql -U litellm -d litellm -tAc \
  "select table_name from information_schema.tables where table_schema='public' order by 1 limit 8" 2>/dev/null \
  | sed 's/^/  - /'
