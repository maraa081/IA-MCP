#!/usr/bin/env bash
# =============================================================================
# Diagnostic et reparation de la pile.
#
#   ./scripts/doctor.sh                 diagnostic en lecture seule
#   ./scripts/doctor.sh --fix           repare ce qui peut l'etre, puis re-verifie
#   ./scripts/doctor.sh --full          ajoute un appel reel au modele (lent sur CPU)
#   ./scripts/doctor.sh --gateway-timeout 300
#   ./scripts/doctor.sh --model-timeout 900
#
# Le script parcourt la pile dans l'ordre des dependances. Une panne en bas
# explique presque toujours les erreurs en haut :
#
#   Docker arrete -> PostgreSQL absent -> gateway LiteLLM morte
#   -> Open WebUI affiche "gateway injoignable"
#
# Chaque couche affiche [ok], [ko] ou [note]. Une couche [ko] n'arrete pas le
# diagnostic : on veut la photo complete, pas la premiere erreur.
#
# Sortie : 0 si tout est vert, 1 sinon (utilisable dans une chaine).
# =============================================================================
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

REPARER=0
FULL=0
DELAI_GATEWAY="${DELAI_GATEWAY:-180}"
DELAI_DOCKER="${DELAI_DOCKER:-120}"
DELAI_MODELE="${DELAI_MODELE:-600}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fix|--repare) REPARER=1; shift ;;
    --full) FULL=1; shift ;;
    --gateway-timeout) DELAI_GATEWAY="${2:-180}"; shift 2 ;;
    --model-timeout) DELAI_MODELE="${2:-600}"; shift 2 ;;
    --help|-h) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "option inconnue : $1 (voir --help)" >&2; exit 2 ;;
  esac
done

# shellcheck disable=SC1091
source scripts/common.sh

CURL_REEL="${CURL_REEL:-$(command -v curl)}"
ECHECS=0
REPARATIONS=0

titre() { printf '\n== %s\n' "$1"; }
ok()    { printf '  [ok]   %s\n' "$1"; }
ko()    { printf '  [ko]   %s\n' "$1"; ECHECS=$((ECHECS + 1)); }
note()  { printf '  [note] %s\n' "$1"; }
info()  { printf '  %s\n' "$1"; }

reparer() { [[ "$REPARER" -eq 1 ]]; }

reparation() {
  # reparation <description> <commande...>
  local description="$1"; shift
  note "reparation : $description"
  if "$@"; then
    REPARATIONS=$((REPARATIONS + 1))
    return 0
  fi
  note "echec de la reparation : $description"
  return 1
}

est_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

port_ouvert() {
  # port_ouvert <port> : teste une connexion TCP locale, sans curl.
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 1
  exec 3<&- 3>&-
  return 0
}

attendre_http() {
  # attendre_http <url> <nom> <secondes> : affiche l'avancement toutes les 15 s.
  local url="$1" nom="$2" secondes="$3" i
  for ((i = 1; i <= secondes; i++)); do
    if "$CURL_REEL" -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    if (( i % 15 == 0 )); then
      printf '  ...    %s : toujours en cours (%ds)\n' "$nom" "$i"
    fi
    sleep 1
  done
  return 1
}

# --- 1. Systeme --------------------------------------------------------------
titre "1. Systeme"

command -v curl >/dev/null 2>&1 && ok "curl present" || ko "curl absent"
if command -v python3 >/dev/null 2>&1 \
   && python3 -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  ok "python3 3.11+ avec venv"
else
  ko "python3 3.11+ (module venv) absent"
fi
command -v ollama >/dev/null 2>&1 && ok "cli ollama present" || ko "cli ollama absent"
command -v docker >/dev/null 2>&1 && ok "cli docker present" || ko "cli docker absent"

# --- 2. Docker ---------------------------------------------------------------
titre "2. Docker"

DEMON_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "demon Docker actif"
  DEMON_DOCKER=1
else
  ko "demon Docker injoignable"
  if reparer; then
    if est_wsl; then
      PS_EXE="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
      DESKTOP_EXE="/mnt/c/Program Files/Docker/Docker/Docker Desktop.exe"
      if [[ -x "$PS_EXE" && -f "$DESKTOP_EXE" ]]; then
        reparation "demarrage de Docker Desktop" \
          "$PS_EXE" -NoProfile -Command "Start-Process 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe'"
        for ((i = 1; i <= DELAI_DOCKER; i++)); do
          docker info >/dev/null 2>&1 && break
          (( i % 20 == 0 )) && printf '  ...    Docker Desktop demarre (%ds)\n' "$i"
          sleep 1
        done
        if docker info >/dev/null 2>&1; then
          ok "Docker Desktop demarre"; DEMON_DOCKER=1
        else
          ko "Docker Desktop ne repond toujours pas apres ${DELAI_DOCKER}s"
        fi
      else
        note "Docker Desktop introuvable depuis WSL : demarrez-le a la main"
      fi
    else
      if command -v systemctl >/dev/null 2>&1; then
        reparation "demarrage du service docker" sudo -n systemctl start docker || \
          note "sudo demande un mot de passe : lancez 'sudo systemctl start docker'"
        docker info >/dev/null 2>&1 && { ok "service docker demarre"; DEMON_DOCKER=1; }
      fi
    fi
  fi
fi

# --- 3. PostgreSQL -----------------------------------------------------------
titre "3. PostgreSQL (base de la gateway)"

if [[ "$DEMON_DOCKER" -eq 1 ]]; then
  if docker ps --format '{{.Names}}' | grep -qx "litellm-postgres"; then
    ok "conteneur litellm-postgres en marche"
  else
    if docker ps -a --format '{{.Names}}' | grep -qx "litellm-postgres"; then
      ko "conteneur litellm-postgres arrete"
    else
      ko "conteneur litellm-postgres absent"
    fi
    if reparer; then
      reparation "creation ou demarrage du conteneur PostgreSQL" scripts/setup_db.sh
    fi
  fi

  if docker exec litellm-postgres pg_isready -U litellm -d litellm >/dev/null 2>&1; then
    ok "PostgreSQL accepte les connexions"
  else
    ko "PostgreSQL ne repond pas (voir : docker logs litellm-postgres)"
  fi

  port_ouvert 5433 && ok "port 5433 joignable depuis l'hote" \
    || ko "port 5433 ferme : la gateway ne pourra pas faire ses migrations"
else
  note "Docker indisponible : PostgreSQL et les cles virtuelles sont hors jeu"
  note "la pile peut tourner sans base, avec la cle maitresse seule"
fi

# --- 4. Gateway LiteLLM ------------------------------------------------------
titre "4. Gateway LiteLLM (port 4000)"

diagnostic_gateway() {
  local journal="$LOG_DIR/gateway.log"
  [[ -f "$journal" ]] || { note "aucun journal ($journal)"; return; }
  if grep -q "P1001" "$journal"; then
    note "P1001 dans le journal : PostgreSQL etait injoignable au demarrage"
    note "  ordre correct : Docker Desktop, puis PostgreSQL, puis la gateway"
  fi
  if grep -qi "address already in use" "$journal"; then
    note "port 4000 deja utilise : ss -ltnp | grep :4000"
  fi
  if grep -qi "migration" "$journal"; then
    note "des migrations Prisma apparaissent dans le journal"
    note "  au premier demarrage elles peuvent prendre plusieurs minutes"
  fi
  if grep -qiE "prisma CLI is neither on PATH|hasn.t been generated|Unable to find Prisma binaries|prisma-client-py: not found" "$journal"; then
    note "client prisma absent : LiteLLM n'a pas pu preparer la base (migrations)"
    note "  correction : ./scripts/enable_db.sh (installe litellm[extra_proxy] + prisma generate)"
  fi
  if grep -qi "traceback" "$journal"; then
    note "derniere erreur du journal :"
    grep -i -A4 "traceback" "$journal" | tail -6 | sed 's/^/         /'
  fi
}

relancer_gateway() {
  stop_background gateway
  sleep 1
  rm -f "$PID_DIR/gateway.pid"
  start_background gateway "$LOG_DIR/gateway.log" \
    "$VENV_GATEWAY/bin/litellm" --config "$PROJECT_ROOT/gateway/config.yaml" \
    --host "${GATEWAY_HOST:-127.0.0.1}" --port "${GATEWAY_PORT:-4000}"
}

GATEWAY_OK=0
if "$CURL_REEL" -fsS --max-time 3 "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
  ok "gateway vivante sur $GATEWAY_URL"
  GATEWAY_OK=1
else
  ko "gateway muette sur $GATEWAY_URL"

  if [[ -f "$PID_DIR/gateway.pid" ]] && ! kill -0 "$(cat "$PID_DIR/gateway.pid")" 2>/dev/null; then
    note "fichier PID perime (le processus est mort) : $PID_DIR/gateway.pid"
    if reparer; then rm -f "$PID_DIR/gateway.pid"; fi
  fi

  diagnostic_gateway

  if reparer; then
    reparation "redemarrage de la gateway" relancer_gateway
    info "attente de la gateway (jusqu'a ${DELAI_GATEWAY}s)"
    if attendre_http "$GATEWAY_URL/health/liveliness" "gateway" "$DELAI_GATEWAY"; then
      ok "gateway repond apres redemarrage"
      GATEWAY_OK=1
    else
      ko "gateway toujours muette apres ${DELAI_GATEWAY}s"
      diagnostic_gateway
      info "journal complet : tail -60 $LOG_DIR/gateway.log"
    fi
  else
    info "relancer avec --fix pour redemarrer la gateway"
  fi
fi

# La gateway a besoin du client prisma des que LITELLM_DATABASE_URL est defini
# (base des cles virtuelles, budgets). Sans lui, LiteLLM demarre mais n'applique
# jamais ses migrations : les cles virtuelles restent inutilisables, sans erreur
# nette. C'est le piege classique d'une installation LiteLLM par pip.
if [[ -n "${LITELLM_DATABASE_URL:-}" ]]; then
  if "$VENV_GATEWAY/bin/python" -c "from prisma import Prisma" >/dev/null 2>&1; then
    ok "client prisma genere (migrations de la gateway possibles)"
  else
    ko "client prisma absent : migrations de la gateway impossibles"
    if reparer; then
      # enable_db.sh installe litellm[extra_proxy] si besoin, genere le client
      # (prisma generate), redemarre la gateway et verifie les migrations.
      reparation "installation + generation du client prisma" scripts/enable_db.sh
      if attendre_http "$GATEWAY_URL/health/liveliness" "gateway" "$DELAI_GATEWAY"; then
        ok "gateway repond apres reparation prisma"
        GATEWAY_OK=1
      else
        ko "gateway toujours muette apres reparation prisma"
      fi
    else
      info "corriger avec : ./scripts/enable_db.sh"
      info "ou a la main : pip install 'litellm[extra_proxy]' puis prisma generate"
    fi
  fi
fi

# --- 5. Ollama ---------------------------------------------------------------
titre "5. Ollama (moteur d'inference)"

MODELE=$(grep -m1 'model: ollama_chat/' gateway/config.yaml \
  | sed -E 's/.*ollama_chat\/([^ ]+).*/\1/')
[[ -n "$MODELE" ]] && info "modele attendu par la gateway : $MODELE"

OLLAMA_OK=0
if "$CURL_REEL" -fsS --max-time 5 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  ok "Ollama repond sur $OLLAMA_URL"
  OLLAMA_OK=1
else
  ko "Ollama ne repond pas sur $OLLAMA_URL"
  if reparer; then
    mkdir -p "$LOG_DIR"
    if command -v systemctl >/dev/null 2>&1 \
       && systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
      reparation "demarrage du service ollama" sudo -n systemctl start ollama || true
    fi
    if ! "$CURL_REEL" -fsS --max-time 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
      reparation "demarrage de 'ollama serve'" \
        setsid nohup ollama serve >"$LOG_DIR/ollama.log" 2>&1 < /dev/null &
    fi
    if attendre_http "$OLLAMA_URL/api/version" "Ollama" 60; then
      ok "Ollama demarre"
      OLLAMA_OK=1
    else
      ko "Ollama ne demarre pas : voir $LOG_DIR/ollama.log"
    fi
  fi
fi

if [[ "$OLLAMA_OK" -eq 1 && -n "$MODELE" ]]; then
  if "$CURL_REEL" -fsS --max-time 15 "$OLLAMA_URL/api/tags" \
       | grep -q "\"name\":\"$MODELE\""; then
    ok "modele present : $MODELE"
  else
    ko "modele absent : $MODELE"
    if reparer; then
      info "telechargement du modele (peut prendre plusieurs minutes)"
      reparation "ollama pull $MODELE" ollama pull "$MODELE"
    else
      info "corriger avec : ollama pull $MODELE"
    fi
  fi
fi

# --- 6. Serveurs MCP ---------------------------------------------------------
titre "6. Serveurs MCP (9101-9103)"

demarrer_mcp() {
  start_background mcp-summarizer "$LOG_DIR/mcp-summarizer.log" \
    "$PY" -m mcp_servers.summarizer.server --host 0.0.0.0 --port 9101
  start_background mcp-codedoc "$LOG_DIR/mcp-codedoc.log" \
    "$PY" -m mcp_servers.codedoc.server --host 0.0.0.0 --port 9102
  start_background mcp-codedb "$LOG_DIR/mcp-codedb.log" \
    "$PY" -m mcp_servers.codedb.server --host 0.0.0.0 --port 9103
}

MCP_KO=0
for port in 9101 9102 9103; do
  if port_ouvert "$port"; then
    ok "port $port ouvert"
  else
    ko "port $port ferme"
    MCP_KO=1
  fi
done

if [[ "$MCP_KO" -eq 1 ]]; then
  if reparer; then
    reparation "demarrage des serveurs MCP" demarrer_mcp
    sleep 2
    for port in 9101 9102 9103; do
      port_ouvert "$port" && ok "port $port ouvert" || ko "port $port toujours ferme"
    done
  else
    info "corriger avec : ./scripts/up.sh"
  fi
fi

# L'index de code du serveur codedb : un port ouvert ne dit rien du contenu de
# la base. Sans entree, les outils repondent mais ne trouvent rien (panne
# silencieuse qui laisse croire a un index vide cote modele).
INDEX_SQLITE="$PROJECT_ROOT/var/codedb.sqlite"
if [[ -f "$INDEX_SQLITE" ]]; then
  ENTREES_INDEX=$("$PY" -c "import sys; sys.path.insert(0, '$PROJECT_ROOT'); from mcp_servers.codedb.database import CodeDatabase; print(CodeDatabase('$INDEX_SQLITE').inventaire()['entrees'])" 2>/dev/null)
  if [[ "${ENTREES_INDEX:-0}" -gt 0 ]]; then
    ok "index de code : ${ENTREES_INDEX} entree(s)"
  else
    ko "index de code vide"
    if reparer; then
      reparation "indexation du code" "$PY" scripts/index_code.py
    else
      info "corriger avec : python scripts/index_code.py"
    fi
  fi
else
  ko "index de code absent ($INDEX_SQLITE)"
  if reparer; then
    reparation "indexation du code" "$PY" scripts/index_code.py
  else
    info "corriger avec : python scripts/index_code.py"
  fi
fi

# --- 7. Orchestrateur --------------------------------------------------------
titre "7. Orchestrateur (conteneur, port interne 8090)"

if [[ "$DEMON_DOCKER" -eq 1 ]]; then
  if docker ps --format '{{.Names}}' | grep -qx "orchestrator-lab"; then
    ok "conteneur orchestrator-lab en marche"
    if docker exec orchestrator-lab python -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1; then
      ok "sante de l'orchestrateur vue de l'interieur"
      titre_health=$(docker exec orchestrator-lab python -c "
import json, urllib.request
print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3))))
" 2>/dev/null)
      [[ -n "$titre_health" ]] && info "$titre_health"
    else
      ko "orchestrateur muet sur 8090"
      diagnostic=""
      if reparer; then
        reparation "redemarrage de l'orchestrateur" scripts/orchestrator.sh up
        sleep 2
        docker exec orchestrator-lab python -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3)
" >/dev/null 2>&1 && ok "orchestrateur repond" || ko "orchestrateur toujours muet"
      fi
      [[ -n "$diagnostic" ]] && info "$diagnostic"
    fi
  else
    ko "conteneur orchestrator-lab arrete ou absent"
    if reparer; then
      reparation "demarrage de l'orchestrateur" scripts/orchestrator.sh up
    else
      info "corriger avec : ./scripts/orchestrator.sh up"
    fi
  fi
else
  note "Docker indisponible : orchestrateur non verifiable"
fi

# --- 8. Open WebUI -----------------------------------------------------------
titre "8. Open WebUI (port 3000)"

OUVERT_WEBUI=0
if "$CURL_REEL" -fsS --max-time 5 "http://127.0.0.1:${OPENWEBUI_PORT:-3000}/health" >/dev/null 2>&1; then
  ok "Open WebUI repond sur le port ${OPENWEBUI_PORT:-3000}"
  OUVERT_WEBUI=1
else
  ko "Open WebUI ne repond pas sur le port ${OPENWEBUI_PORT:-3000}"
  if reparer; then
    reparation "demarrage d'Open WebUI" scripts/openwebui.sh up
    if attendre_http "http://127.0.0.1:${OPENWEBUI_PORT:-3000}/health" "Open WebUI" 120; then
      ok "Open WebUI demarre"
      OUVERT_WEBUI=1
    else
      ko "Open WebUI ne demarre pas : docker logs open-webui"
    fi
  else
    info "corriger avec : ./scripts/openwebui.sh up"
  fi
fi

# Derive d'IP : sous WSL, l'IP change au redemarrage de Windows. Les conteneurs
# gardent l'ancienne et ne joignent plus les services de l'hote.
if est_wsl && [[ "$DEMON_DOCKER" -eq 1 ]]; then
  IP_ACTUELLE=$(hostname -I | awk '{print $1}')
  for fichier in "$VAR_DIR/openwebui-ip.txt" "$VAR_DIR/orchestrator-ip.txt"; do
    [[ -f "$fichier" ]] || continue
    IP_ENREGISTREE=$(cat "$fichier" 2>/dev/null || true)
    if [[ -n "$IP_ACTUELLE" && "$IP_ACTUELLE" != "$IP_ENREGISTREE" ]]; then
      ko "IP de WSL changee ($IP_ENREGISTREE -> $IP_ACTUELLE) pour $(basename "$fichier")"
      if reparer; then
        note "recreation des conteneurs pour la nouvelle IP"
        reparation "mise a jour d'Open WebUI" scripts/openwebui.sh up
        reparation "mise a jour de l'orchestrateur" scripts/orchestrator.sh up
      else
        info "corriger avec : ./scripts/openwebui.sh up && ./scripts/orchestrator.sh up"
      fi
    else
      ok "IP de WSL coherente pour $(basename "$fichier")"
    fi
  done
fi

# --- 9. Bbout en bout (facultatif) -------------------------------------------
titre "9. Appel de bout en bout"

if [[ "$FULL" -eq 0 ]]; then
  note "saute : relancer avec --full pour un appel reel au modele (lent sur CPU)"
elif [[ "$GATEWAY_OK" -eq 0 ]]; then
  note "saute : la gateway ne repond pas"
else
  AUTH_SCHEME="Bearer"
  ENTETE="Authorization: $AUTH_SCHEME $GATEWAY_API_KEY"
  DEBUT=$(date +%s)
  REPONSE=$("$CURL_REEL" -sS --max-time "$DELAI_MODELE" -X POST "$GATEWAY_URL/v1/chat/completions" \
    -H "$ENTETE" -H 'Content-Type: application/json' \
    -d "{\"model\":\"planner\",\"messages\":[{\"role\":\"user\",\"content\":\"Reponds exactement: OK\"}],\"max_tokens\":16}" \
    2>&1 || true)
  DUREE=$(($(date +%s) - DEBUT))
  if printf '%s' "$REPONSE" | grep -q '"content"'; then
    ok "le modele a repondu en ${DUREE}s"
    if [[ "$DUREE" -gt 90 ]]; then
      note "c'est lent : normal sur CPU avec un modele 4b, surtout au premier appel"
      note "pour accelerer : python3 scripts/set_model.py --model qwen3.5:2b"
    fi
  else
    ko "pas de reponse du modele apres ${DUREE}s"
    info "$(printf '%s' "$REPONSE" | head -c 300)"
  fi
fi

# --- Resume ------------------------------------------------------------------
titre "Resume"
info "controles en echec : $ECHECS"
info "reparations appliquees : $REPARATIONS"

if [[ "$ECHECS" -eq 0 ]]; then
  echo "  Tout est vert."
  exit 0
fi

echo
echo "  Pannes restantes. Ordre a respecter :"
echo "    1. Docker Desktop (ou sudo systemctl start docker)"
echo "    2. ./scripts/setup_db.sh"
echo "    3. ./scripts/up.sh"
echo "    4. ./scripts/openwebui.sh setup"
echo "  Journaux : var/logs/ et docker logs <conteneur>"
exit 1
