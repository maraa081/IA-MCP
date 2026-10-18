#!/usr/bin/env bash
# =============================================================================
# Installeur du labo MCP (orchestrateur + gateway + Open WebUI).
#
# A lancer depuis un clone frais, avant toute autre commande :
#
#   ./install.sh                      mode guide : verifie les prerequis, propose
#                                     un modele, installe et demarre la pile
#   ./install.sh --model qwen3.5:2b   mode non guide (PC 8 Go, CPU seul)
#   ./install.sh --yes                accepte les valeurs par defaut
#
# Options :
#   --model <nom>       modele Ollama a utiliser (defaut : qwen3.5:4b)
#   --planner-ctx <n>   num_ctx de l'alias planner (defaut selon le modele)
#   --writer-ctx <n>    num_ctx de l'alias writer  (defaut selon le modele)
#   --yes               ne pose aucune question (defauts partout)
#   --no-docker         saute PostgreSQL (sans Docker, LiteLLM marche en mode
#                       cle maitresse seule)
#   --skip-openwebui    installe la pile mais pas l'interface Open WebUI
#   --dry-run           affiche le plan, ne modifie rien
#   --help              cette aide
#
# Le script est idempotent : on peut le relancer sur une installation existante,
# il ne refait que ce qui manque. Il ne contient aucun chemin absolu : il marche
# depuis n'importe quel dossier de clone.
# =============================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

MODELE=""
CTX_PLANNER=""
CTX_WRITER=""
REPONDRE_OUI=0
AVEC_DOCKER=1
AVEC_OPENWEBUI=1
DRY_RUN=0

# --- Arguments ---------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODELE="${2:-}"; shift 2 ;;
    --planner-ctx) CTX_PLANNER="${2:-}"; shift 2 ;;
    --writer-ctx) CTX_WRITER="${2:-}"; shift 2 ;;
    --yes|-y) REPONDRE_OUI=1; shift ;;
    --no-docker) AVEC_DOCKER=0; shift ;;
    --skip-openwebui) AVEC_OPENWEBUI=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "option inconnue : $1 (voir --help)" >&2; exit 2 ;;
  esac
done

# --- Sortie lisible ----------------------------------------------------------
titre() { printf '\n== %s\n' "$1"; }
ok()    { printf '  [ok]   %s\n' "$1"; }
note()  { printf '  [note] %s\n' "$1"; }
ko()    { printf '  [ko]   %s\n' "$1"; }
info()  { printf '  %s\n' "$1"; }

demander() {
  # demander <question> <defaut> -> repond dans REPONSE
  local question="$1" defaut="$2"
  if [[ "$REPONDRE_OUI" -eq 1 || ! -t 0 ]]; then
    REPONSE="$defaut"; return 0
  fi
  read -r -p "$question [$defaut] : " REPONSE || true
  REPONSE="${REPONSE:-$defaut}"
}

# --- 1. Prerequis ------------------------------------------------------------
titre "1/7  Verification des prerequis"

MANQUANT=0
exiger() {
  # exiger <commande> <message si absent>
  if command -v "$1" >/dev/null 2>&1; then
    ok "$1 present"
  else
    ko "$1 absent : $2"; MANQUANT=1
  fi
}

exiger python3 "installez Python 3.11 ou plus (python3-venv inclus)"
exiger curl "installez curl (paquet standard)"

if command -v python3 >/dev/null 2>&1; then
  PY_OK=$(python3 -c 'import sys,venv; print(1 if sys.version_info >= (3,11) else 0)' 2>/dev/null || echo 0)
  if [[ "$PY_OK" == "1" ]]; then
    ok "python3 $(python3 -V 2>&1 | awk '{print $2}') avec venv"
  else
    ko "Python 3.11+ avec le module venv est requis"
    MANQUANT=1
  fi
fi

exiger ollama "installez Ollama : https://ollama.com/download (ou 'curl -fsSL https://ollama.com/install.sh | sh')"

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "docker actif"
  else
    ko "docker installe mais le demon ne repond pas (Docker Desktop lance ?)"
    AVEC_DOCKER=0
  fi
else
  if [[ "$AVEC_DOCKER" -eq 1 ]]; then
    note "docker absent : PostgreSQL sera saute (mode cle maitresse seule)"
    AVEC_DOCKER=0
  fi
fi

for outil in jq tar; do
  command -v "$outil" >/dev/null 2>&1 && ok "$outil present" || note "$outil absent (utile seulement a demo.sh)"
done

if [[ "$MANQUANT" -eq 1 ]]; then
  printf '\nInstallation interrompue : corrigez les points [ko] ci-dessus puis relancez ./install.sh\n' >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  note "mode --dry-run : aucune modification ne sera faite"
fi

# --- 2. Choix du modele ------------------------------------------------------
titre "2/7  Choix du modele d'inference"

RAM_GO=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
GPU=0
if command -v nvidia-smi >/dev/null 2>&1 || [[ -e /dev/kfd ]] || [[ -e /dev/dri ]]; then
  GPU=1
fi

if [[ -n "$MODELE" ]]; then
  info "modele impose par --model : $MODELE"
else
  info "memoire detectee : ${RAM_GO:-?} Go | GPU : $([[ $GPU -eq 1 ]] && echo oui || echo non)"
  cat <<'EOF'

  Quel modele local utiliser ?

    1) qwen3.5:4b    CPU correct, 3.3 Go   ctx 8192/16384   (defaut)
    2) qwen3.5:2b    CPU modeste ou 8 Go RAM, 2.7 Go   ctx 4096/8192
    3) qwen3.5:0.8b  CPU tres faible, 1.2 Go   ctx 4096/4096
    4) qwen3.5:9b    GPU recommande, 6.6 Go   ctx 8192/16384
    5) autre         saisir un nom exact present sur ollama.com/library

EOF
  # Preselection selon la machine.
  DEFAUT=1
  if [[ "$GPU" -eq 0 ]]; then
    if [[ "$RAM_GO" -gt 0 && "$RAM_GO" -lt 8 ]]; then DEFAUT=2; fi
    if [[ "$RAM_GO" -gt 0 && "$RAM_GO" -lt 4 ]]; then DEFAUT=3; fi
  fi

  demander "Choix" "$DEFAUT"
  case "$REPONSE" in
    1) MODELE="qwen3.5:4b";   CTX_PLANNER="${CTX_PLANNER:-8192}"; CTX_WRITER="${CTX_WRITER:-16384}" ;;
    2) MODELE="qwen3.5:2b";   CTX_PLANNER="${CTX_PLANNER:-4096}"; CTX_WRITER="${CTX_WRITER:-8192}" ;;
    3) MODELE="qwen3.5:0.8b"; CTX_PLANNER="${CTX_PLANNER:-4096}"; CTX_WRITER="${CTX_WRITER:-4096}" ;;
    4) MODELE="qwen3.5:9b";   CTX_PLANNER="${CTX_PLANNER:-8192}"; CTX_WRITER="${CTX_WRITER:-16384}" ;;
    5) demander "Nom exact du modele (ex : qwen3.5:4b)" "qwen3.5:4b"; MODELE="$REPONSE" ;;
    *) ko "choix invalide : $REPONSE"; exit 2 ;;
  esac
fi

# Valeurs par defaut si un --model direct a ete donne sans contexte.
if [[ -z "$CTX_PLANNER" ]]; then
  case "$MODELE" in
    *:0.8b|*:2b) CTX_PLANNER=4096 ;;
    *)           CTX_PLANNER=8192 ;;
  esac
fi
if [[ -z "$CTX_WRITER" ]]; then
  case "$MODELE" in
    *:0.8b)      CTX_WRITER=4096 ;;
    *:2b)        CTX_WRITER=8192 ;;
    *)           CTX_WRITER=16384 ;;
  esac
fi

ok "modele : $MODELE (planner ctx $CTX_PLANNER, writer ctx $CTX_WRITER)"

# --- 3. Recapitulatif --------------------------------------------------------
titre "3/7  Recapitulatif"
info "dossier          : $PROJECT_ROOT"
info "modele           : $MODELE"
info "postgresql       : $([[ $AVEC_DOCKER -eq 1 ]] && echo avec Docker || echo saute)"
info "open webui       : $([[ $AVEC_OPENWEBUI -eq 1 ]] && echo installe || echo saute)"

if [[ "$DRY_RUN" -eq 1 ]]; then
  note "fin du --dry-run : rien n'a ete modifie"
  exit 0
fi

if [[ "$REPONDRE_OUI" -eq 0 ]]; then
  demander "Continuer l'installation ? (o/n)" "o"
  case "$REPONSE" in
    o|O|oui|y|Y) ;;
    *) echo "Annule."; exit 0 ;;
  esac
fi

# --- 4. Configuration --------------------------------------------------------
titre "4/7  Configuration (.env et gateway/config.yaml)"

if [[ ! -f .env ]]; then
  cp .env.example .env
  ok ".env cree depuis .env.example"
else
  note ".env deja present (conserve)"
fi

definir_env() {
  # definir_env <nom> <valeur> : remplace la ligne ou l'ajoute, sans doublon.
  local nom="$1" valeur="$2"
  awk -v n="$nom" -v v="$valeur" '
    BEGIN { fait = 0 }
    index($0, n "=") == 1 { print n "=" v; fait = 1; next }
    { print }
    END { if (!fait) print n "=" v }
  ' .env > .env.tmp && mv .env.tmp .env
}

definir_env OLLAMA_MODEL "$MODELE"
ok ".env : OLLAMA_MODEL=$MODELE"

python3 scripts/set_model.py --model "$MODELE" \
  --planner-ctx "$CTX_PLANNER" --writer-ctx "$CTX_WRITER"

# --- 5. Environments Python et modele ---------------------------------------
titre "5/7  Environnements Python, cles JWT, modele Ollama"
if ! "$PROJECT_ROOT/scripts/bootstrap.sh"; then
  printf '\nEchec de bootstrap.sh : lire le message ci-dessus.\n' >&2
  exit 1
fi

# --- 6. Base de donnees, demarrage ------------------------------------------
titre "6/7  Base PostgreSQL et demarrage de la pile"
if [[ "$AVEC_DOCKER" -eq 1 ]]; then
  "$PROJECT_ROOT/scripts/setup_db.sh" || note "setup_db.sh a echoue : la pile demarrera sans base"
else
  note "PostgreSQL saute (--no-docker ou Docker indisponible)"
fi

"$PROJECT_ROOT/scripts/up.sh"

# --- 7. Open WebUI et verification ------------------------------------------
titre "7/7  Interface et verification"
if [[ "$AVEC_OPENWEBUI" -eq 1 ]]; then
  "$PROJECT_ROOT/scripts/openwebui.sh" up || note "Open WebUI n'a pas demarre (Docker requis)"
  "$PROJECT_ROOT/scripts/openwebui.sh" setup || note "bootstrap Open WebUI a echoue (relancer : ./scripts/openwebui.sh setup)"
else
  note "Open WebUI saute (--skip-openwebui)"
fi

source "$PROJECT_ROOT/scripts/common.sh"

titre "Verification de bout en bout"
if curl -fsS --max-time 5 "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
  ok "gateway LiteLLM repond sur $GATEWAY_URL"
else
  ko "gateway muette : voir var/logs/gateway.log"
fi

AUTH_SCHEME="Bearer"
AUTH_HDR="Authorization: $AUTH_SCHEME ${GATEWAY_API_KEY}"
MODELES=$(curl -sS --max-time 10 -H "$AUTH_HDR" "$GATEWAY_URL/v1/models" 2>/dev/null || true)
if printf '%s' "$MODELES" | grep -q '"id"'; then
  ok "modeles exposes : $(printf '%s' "$MODELES" | grep -o '"id":"[^"]*"' | cut -d'"' -f4 | paste -sd', ' -)"
else
  ko "liste des modeles vide ou refusee"
fi

note "premier appel : sur CPU un 4b peut prendre 30 a 90 s, c'est normal"
REPONSE_CHAT=$(curl -sS --max-time 300 -X POST "$GATEWAY_URL/v1/chat/completions" \
  -H "$AUTH_HDR" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"planner\",\"messages\":[{\"role\":\"user\",\"content\":\"Reponds exactement: OK\"}],\"max_tokens\":16}" \
  2>/dev/null || true)
if printf '%s' "$REPONSE_CHAT" | grep -q '"content"'; then
  ok "le modele repond (alias planner)"
else
  ko "pas de reponse du modele : voir var/logs/gateway.log et 'ollama list'"
fi

cat <<EOF

Installation terminee.

  Interface        : http://localhost:3000
  Modele           : $MODELE
  Comptes          : var/openwebui-admin.txt (cree au premier setup)
  Demarrage        : ./scripts/up.sh   (apres un redemarrage de la machine)
  Arret            : ./scripts/down.sh
  Journaux         : var/logs/
  Diagnostic       : ./scripts/test-infra.sh

Rappel : Docker Desktop doit etre lance AVANT ./scripts/up.sh, sinon la gateway
LiteLLM ne peut pas joindre PostgreSQL et meurt au demarrage.
EOF
