#!/usr/bin/env bash
# COMMANDE UNIQUE : demarre toute la pile IA-MCP et verifie qu'elle repond.
#
#   ./scripts/start.sh              tout demarrer (idempotent, relancable)
#   ./scripts/start.sh --no-index   sans reindexer le code (demarrage rapide)
#   ./scripts/start.sh --no-webui   sans Open WebUI
#
# Ordre : Docker -> Ollama -> gateway -> serveurs MCP -> orchestrateur -> WebUI
# Chaque etape est independante : un service en echec n'empeche pas les autres.
# Code de sortie : 0 si tous les services cles repondent, 1 sinon.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

UP_ARGS=()
AVEC_WEBUI=1
for arg in "$@"; do
  case "$arg" in
    --no-index) UP_ARGS+=(--no-index) ;;
    --no-webui) AVEC_WEBUI=0 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "option inconnue : $arg" >&2; exit 2 ;;
  esac
done

echo "=== IA-MCP : demarrage de la pile ==="

# Au reboot de Windows, Docker Desktop peut demarrer apres WSL. Sans ceinture
# d'attente, l'orchestrateur et les conteneurs echouent une fois puis restent
# arretes jusqu'a une intervention manuelle.
if command -v docker >/dev/null 2>&1; then
  echo "== Docker"
  pret=0
  for ((i = 1; i <= 60; i++)); do
    if docker info >/dev/null 2>&1; then pret=1; break; fi
    (( i % 15 == 0 )) && printf '  ...    attente du demon Docker (%ds)\n' "$i"
    sleep 1
  done
  if (( pret )); then
    echo "  [ok]   demon Docker joignable"
  else
    echo "  [ko]   Docker indisponible : conteneurs ignores (demarrer Docker Desktop)"
    echo "         les serveurs MCP et la gateway seront quand meme lances"
  fi
else
  echo "== Docker : absent, conteneurs ignores"
fi

echo
"$PROJECT_ROOT/scripts/up.sh" "${UP_ARGS[@]}"
UP_RC=$?

if (( AVEC_WEBUI )); then
  echo
  echo "== Open WebUI (interface de chat)"
  # `up` recree le conteneur si l'IP de WSL a change au reboot : sans cela
  # l'interface pointe vers l'ancienne adresse et ne joint plus l'orchestrateur.
  if docker info >/dev/null 2>&1; then
    "$PROJECT_ROOT/scripts/openwebui.sh" up || echo "  [note] Open WebUI non demarre : ./scripts/openwebui.sh up"
  else
    echo "  [note] Docker indisponible : Open WebUI non demarre"
  fi
fi

echo
echo "=== Etat ==="
"$PROJECT_ROOT/scripts/status.sh"
STATUS_RC=$?

if (( UP_RC != 0 || STATUS_RC != 0 )); then
  echo
  echo "Des services manquent. Diagnostic detaille : ./scripts/doctor.sh"
  exit 1
fi
echo
echo "Tout est pret : $OPENWEBUI_URL"
exit 0
