#!/usr/bin/env bash
# Envoie un prompt a l'orchestrateur et affiche tout ce qui s'est passe.
#
#   ./scripts/prompt.sh "Resume moi ce document : <votre texte>"
#   ./scripts/prompt.sh --role reader "Documente ce code : <votre code>"
#   ./scripts/prompt.sh --no-synthesis "Resume ceci : ..."     # plus rapide
#   cat document.txt | ./scripts/prompt.sh "Resume ce document"
#   ./scripts/prompt.sh --json "..."                            # reponse brute
#
# Le jeton est signe localement (scripts/mint_token.py) : pas besoin de mot de
# passe, et le role est choisi librement pour observer l'effet de la GPO.
#
# Le script affiche la reponse, mais aussi la trace technique : outils proposes,
# outils reellement appeles, refus, latence, et la ligne d'audit correspondante.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source scripts/common.sh

ROLE="${PROMPT_ROLE:-analyst}"
SUB="${PROMPT_SUB:-demo}"
SYNTHESE=""
SORTIE_JSON=0
DELAI=600

aide() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)    aide ;;
    --role)       ROLE="$2"; shift 2 ;;
    --sub)        SUB="$2"; shift 2 ;;
    --reader)     ROLE="reader"; shift ;;
    --no-synthesis) SYNTHESE="false"; shift ;;
    --json)       SORTIE_JSON=1; shift ;;
    --timeout)    DELAI="$2"; shift 2 ;;
    --)           shift; break ;;
    *)            break ;;
  esac
done

# Le message : les arguments, sinon l'entree standard.
if [[ $# -gt 0 ]]; then
  MESSAGE="$*"
else
  if [[ -t 0 ]]; then
    echo "Aucun prompt fourni." >&2
    echo "Exemple : ./scripts/prompt.sh \"Resume moi ce document : ...\"" >&2
    exit 2
  fi
  MESSAGE=$(cat)
fi

if [[ -z "${MESSAGE// /}" ]]; then
  echo "Le prompt est vide." >&2
  exit 2
fi

command -v jq >/dev/null || { echo "jq est necessaire"; exit 1; }

# --- Preparation --------------------------------------------------------------
TOKEN=$("$PY" scripts/mint_token.py --sub "$SUB" --roles "$ROLE" --quiet 2>/dev/null)
if [[ -z "$TOKEN" ]]; then
  echo "Impossible de signer un jeton (les cles sont-elles presentes ? ./scripts/gen_keys.py)" >&2
  exit 1
fi

CORPS=$(jq -n --arg m "$MESSAGE" --arg s "$SYNTHESE" \
  '{message: $m} + (if $s == "" then {} else {synthesize: ($s == "true")} end)')

echo "utilisateur : $SUB   role(s) : $ROLE   modele : $PLANNER_MODEL -> $WRITER_MODEL"
echo "prompt : ${#MESSAGE} caracteres"
echo "envoi en cours... (une reponse prend couramment 30 a 90 s, l'inference est sur CPU)"
echo

DEBUT=$(date +%s)
REPONSE=$(curl -sS --max-time "$DELAI" -X POST "$ORCHESTRATOR_URL/v1/chat" \
  --oauth2-bearer "$TOKEN" -H 'Content-Type: application/json' -d "$CORPS" -w $'\n%{http_code}')
CODE=$(printf '%s' "$REPONSE" | tail -n 1)
CORPS_REPONSE=$(printf '%s' "$REPONSE" | sed '$d')
FIN=$(date +%s)
DUREE=$((FIN - DEBUT))

if [[ "$SORTIE_JSON" == "1" ]]; then
  printf '%s\n' "$CORPS_REPONSE" | jq .
  exit 0
fi

# --- Cas d'erreur -------------------------------------------------------------
if [[ "$CODE" != "200" ]]; then
  echo "L'API a repondu $CODE :"
  printf '%s\n' "$CORPS_REPONSE" | jq -r '.detail // .' 2>/dev/null | sed 's/^/  /'
  case "$CODE" in
    401) echo "  -> jeton refuse. Verifier que l'orchestrateur tourne et que la cle publique est a jour." ;;
    403) echo "  -> refuse par la GPO. La reponse ci-dessus indique la regle et la raison exacte." ;;
    502) echo "  -> echec cote traitement : voir var/logs/orchestrator.log" ;;
  esac
  exit 1
fi

# --- Reponse ------------------------------------------------------------------
echo "==================================================================="
echo " REPONSE"
echo "==================================================================="
printf '%s\n' "$CORPS_REPONSE" | jq -r '.answer'
echo
echo "==================================================================="
echo " TRACE DE LA DECISION"
echo "==================================================================="
printf '%s\n' "$CORPS_REPONSE" | jq -r '
  "trace_id ......... \(.trace_id)",
  "mode de decision . \(.planner_mode)",
  "modele ........... \(.model_used)",
  "latence .......... \(.latency_ms) ms (mesuree ici : '"$DUREE"' s)",
  "outils appeles ... \(if (.tools_used | length) == 0 then "(aucun)" else (.tools_used | join(", ")) end)",
  "",
  (if (.tool_trace | length) == 0
   then "aucun appel d outil n a ete propose par le modele"
   else (.tool_trace[] | "  [\(.decision)] \(.tool)\(if .duration_ms then " (\(.duration_ms) ms)" else "" end)"
        + (if .reason and .reason != "execute" then "\n      raison : \(.reason)" else "" end))
   end),
  (if (.policy_denials | length) > 0
   then "\nrefus de la politique :\n" + (.policy_denials | map("  - " + .) | join("\n"))
   else "" end)
'

# --- Ligne d'audit ------------------------------------------------------------
TRACE_ID=$(printf '%s\n' "$CORPS_REPONSE" | jq -r '.trace_id')
LIGNE=$(jq -c --arg t "$TRACE_ID" 'select(.trace_id == $t)' "$AUDIT_LOG" 2>/dev/null | tail -n 1)
if [[ -n "$LIGNE" ]]; then
  echo
  echo "ligne d'audit correspondante (var/logs/audit.jsonl) :"
  printf '%s\n' "$LIGNE" | jq -c '{event, sub, roles, tools_proposed, tools_executed, denials, errors, allowed, latency_ms}' | sed 's/^/  /'
fi

# Aide au diagnostic quand le modele a repondu sans appeler l'outil.
if [[ "$(printf '%s\n' "$CORPS_REPONSE" | jq -r '.tools_used | length')" == "0" ]]; then
  echo
  echo "note : le modele a repondu sans passer par un outil. C'est normal pour une"
  echo "       question de conversation. Pour une demande de resume ou de"
  echo "       documentation, cela signifie qu'il a redige lui-meme : reformulez en"
  echo "       nommant explicitement l'action attendue."
fi
