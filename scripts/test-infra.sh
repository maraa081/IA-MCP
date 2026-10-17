#!/usr/bin/env bash
# Test de bon fonctionnement de l'infrastructure.
#
#   ./scripts/test-infra.sh           verifications rapides (aucun appel au modele)
#   ./scripts/test-infra.sh --full    ajoute un appel de bout en bout (lent sur CPU)
#
# Chaque ligne affichee est un controle qui passe ou qui echoue. Le script sort
# en erreur si au moins un controle echoue : il est utilisable dans une chaine
# d'integration.
#
# Ce script complete les tests unitaires, il ne les remplace pas :
#   - pytest (./.venv/bin/python -m pytest) verifie la logique en isolation ;
#   - ce script verifie que la pile reelle est branchee et applique ses regles.
#
# Note d'implementation : les appels authentifies utilisent l'option
# --oauth2-bearer de curl plutot qu'un en-tete ecrit a la main. C'est plus court
# et cela evite de recopier un jeton dans une ligne de commande.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source scripts/common.sh

FULL=0
[[ "${1:-}" == "--full" ]] && FULL=1

command -v curl >/dev/null || { echo "curl est necessaire"; exit 1; }
command -v jq >/dev/null || { echo "jq est necessaire"; exit 1; }

PASSES=0
FAILURES=0
AUDIT_LOG="$PROJECT_ROOT/var/logs/audit.jsonl"

ok()    { echo "  [ok]   $1"; PASSES=$((PASSES + 1)); }
ko()    { echo "  [ko]   $1"; FAILURES=$((FAILURES + 1)); }
title() { echo; echo "-- $1"; }
code()  { curl -sS -o /dev/null -w '%{http_code}' --max-time "${2:-10}" "$1"; }

# --- 1. Moteur d'inference ----------------------------------------------------
title "1. Moteur d'inference (Ollama)"

if curl -fsS --max-time 5 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  ok "Ollama repond sur $OLLAMA_URL"
else
  ko "Ollama ne repond pas sur $OLLAMA_URL (lancer 'ollama serve')"
fi

MODELE=$(grep -m1 'model: ollama_chat/' "$PROJECT_ROOT/gateway/config.yaml" \
  | sed -E 's/.*ollama_chat\/([^ ]+).*/\1/')
if [[ -n "$MODELE" ]]; then
  if curl -fsS --max-time 10 "$OLLAMA_URL/api/tags" \
       | jq -e --arg m "$MODELE" '.models[] | select(.name == $m)' >/dev/null 2>&1; then
    ok "modele present : $MODELE"
  else
    ko "modele absent : $MODELE (lancer 'ollama pull $MODELE')"
  fi
else
  ko "aucun modele declare dans gateway/config.yaml"
fi

# --- 2. Gateway LiteLLM -------------------------------------------------------
title "2. Gateway LiteLLM (point d'entree des modeles)"

if curl -fsS --max-time 5 "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
  ok "gateway joignable sur $GATEWAY_URL"
else
  ko "gateway injoignable sur $GATEWAY_URL"
fi

CODE_SANS_CLE=$(code "$GATEWAY_URL/v1/models")
# Tout code superieur ou egal a 400 est un refus. On ne fixe pas 401 en dur :
# sans base de donnees, LiteLLM repond 500 sur son propre chemin de controle.
if [[ "$CODE_SANS_CLE" -ge 400 ]] 2>/dev/null; then
  ok "la gateway refuse un appel sans cle ($CODE_SANS_CLE)"
else
  ko "la gateway a repondu $CODE_SANS_CLE sans cle : verifier GATEWAY_MASTER_KEY"
fi

GATEWAY_MODELS=$(curl -sS --max-time 10 --oauth2-bearer "$GATEWAY_API_KEY" \
  "$GATEWAY_URL/v1/models" 2>/dev/null)
if echo "$GATEWAY_MODELS" | jq -e --arg m "$PLANNER_MODEL" \
     '.data[] | select(.id == $m)' >/dev/null 2>&1; then
  ok "alias '$PLANNER_MODEL' declare dans la gateway"
elif echo "$GATEWAY_MODELS" | grep -q 'no_db_connection'; then
  # Toujours la meme limite : sans base, LiteLLM refuse /v1/models meme avec la
  # cle maitresse. On retombe sur le fichier de configuration, en le disant.
  if grep -q "model_name: $PLANNER_MODEL" "$PROJECT_ROOT/gateway/config.yaml"; then
    ok "alias '$PLANNER_MODEL' declare dans gateway/config.yaml (API non verifiable sans base)"
  else
    ko "alias '$PLANNER_MODEL' introuvable dans gateway/config.yaml"
  fi
else
  ko "alias '$PLANNER_MODEL' absent de la gateway"
fi

# --- 3. Orchestrateur et serveurs MCP ----------------------------------------
title "3. Orchestrateur et serveurs MCP"

HEALTH=$(curl -sS --max-time 15 "$ORCHESTRATOR_URL/health" 2>/dev/null)
if [[ -z "$HEALTH" ]]; then
  ko "orchestrateur injoignable sur $ORCHESTRATOR_URL (lancer ./scripts/up.sh)"
  echo
  echo "Resultat : $PASSES reussi(s), $FAILURES echoue(s) (arret : la pile n'est pas demarree)"
  exit 1
fi

[[ "$(echo "$HEALTH" | jq -r '.status')" == "ok" ]] \
  && ok "etat global : ok" \
  || ko "etat global degrade : $(echo "$HEALTH" | jq -c .)"

[[ "$(echo "$HEALTH" | jq -r '.gateway')" == "true" ]] \
  && ok "l'orchestrateur joint la gateway" \
  || ko "l'orchestrateur ne joint pas la gateway"

for serveur in $(echo "$HEALTH" | jq -r '.mcp_servers | keys[]'); do
  etat=$(echo "$HEALTH" | jq -r ".mcp_servers[\"$serveur\"]")
  [[ "$etat" == "true" ]] \
    && ok "serveur MCP '$serveur' joignable" \
    || ko "serveur MCP '$serveur' injoignable"
done

[[ "$(echo "$HEALTH" | jq -r '.tools_known')" -ge 2 ]] \
  && ok "outils MCP decouverts : $(echo "$HEALTH" | jq -r '.tools_known')" \
  || ko "moins de deux outils decouverts"

# --- 4. Authentification ------------------------------------------------------
title "4. Authentification JWT"

CODE_SANS_JETON=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
  -X POST "$ORCHESTRATOR_URL/v1/chat" -H 'Content-Type: application/json' \
  -d '{"message":"bonjour"}')
[[ "$CODE_SANS_JETON" == "401" ]] \
  && ok "sans jeton : 401" \
  || ko "sans jeton : 401 attendu, obtenu $CODE_SANS_JETON"

CODE_MAUVAIS=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
  -X POST "$ORCHESTRATOR_URL/auth/token" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$DEV_USERNAME\",\"password\":\"mauvais-mot-de-passe\"}")
[[ "$CODE_MAUVAIS" == "401" ]] \
  && ok "mauvais mot de passe : 401" \
  || ko "mauvais mot de passe : 401 attendu, obtenu $CODE_MAUVAIS"

LOGIN=$("$PY" -c 'import json,os;print(json.dumps({"username":os.environ["DEV_USERNAME"],"password":os.environ["DEV_PASSWORD"]}))')
TOKEN=$(curl -sS --max-time 10 -X POST "$ORCHESTRATOR_URL/auth/token" \
  -H 'Content-Type: application/json' -d "$LOGIN" | jq -r '.access_token // empty')

if [[ -n "$TOKEN" ]]; then
  ok "jeton emis pour '$DEV_USERNAME'"
else
  ko "impossible d'emettre un jeton"
  echo
  echo "  Arret : sans jeton valide, les controles suivants n'ont pas de sens."
  echo "  Cause la plus frequente : le .env a change (DEV_USERNAME, DEV_PASSWORD)"
  echo "  sans que l'orchestrateur soit redemarre. Il lit la configuration une"
  echo "  seule fois, au demarrage."
  echo "  Correction : ./scripts/down.sh && ./scripts/up.sh"
  echo
  echo "Resultat : $PASSES reussi(s), $FAILURES echoue(s)"
  exit 1
fi

[[ "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 --oauth2-bearer "pas-un-jeton" \
    "$ORCHESTRATOR_URL/auth/me")" == "401" ]] \
  && ok "jeton malforme : 401" \
  || ko "jeton malforme : 401 attendu"

# --- 5. Politique de droits ---------------------------------------------------
title "5. Politique de droits (GPO)"

reponse_outils() {
  # reponse_outils <jeton>
  curl -sS --max-time 10 --oauth2-bearer "$1" "$ORCHESTRATOR_URL/tools"
}

OUTILS_ANALYST=$(reponse_outils "$TOKEN")
if echo "$OUTILS_ANALYST" | jq -e 'type == "array"' >/dev/null 2>&1; then
  NOMS_ANALYST=$(echo "$OUTILS_ANALYST" | jq -r '[.[].name] | sort | join(",")')
  if [[ "$NOMS_ANALYST" == "code_to_technical_doc,summarize_text" ]]; then
    ok "role analyst : deux outils visibles"
  else
    ko "role analyst : outils inattendus ($NOMS_ANALYST)"
  fi
else
  ko "role analyst : reponse inattendue ($(echo "$OUTILS_ANALYST" | head -c 100))"
fi

TOKEN_READER=$("$PY" scripts/mint_token.py --sub lecteur --roles reader --quiet 2>/dev/null)
NOMS_READER=$(reponse_outils "$TOKEN_READER" | jq -r '[.[].name] | join(",")' 2>/dev/null)
[[ "$NOMS_READER" == "summarize_text" ]] \
  && ok "role reader : un seul outil visible (code_to_technical_doc masque)" \
  || ko "role reader : outils inattendus ($NOMS_READER)"

TOKEN_INCONNU=$("$PY" scripts/mint_token.py --sub inconnu --roles role-inexistant --quiet 2>/dev/null)
NB_INCONNU=$(reponse_outils "$TOKEN_INCONNU" | jq -r 'length' 2>/dev/null)
[[ "$NB_INCONNU" == "0" ]] \
  && ok "role inexistant : aucun outil (refus par defaut)" \
  || ko "role inexistant : $NB_INCONNU outil(s) visible(s), attendu 0"

# Plafond global de taille du message, verifie jusque dans l'en-tete de refus.
GROS_MESSAGE=$("$PY" -c "print('a' * 40001)")
CORPS=$(jq -n --arg m "$GROS_MESSAGE" '{message:$m}')
ENTETES=$(curl -sS -D- -o /dev/null --max-time 20 -X POST "$ORCHESTRATOR_URL/v1/chat" \
  --oauth2-bearer "$TOKEN" -H 'Content-Type: application/json' -d "$CORPS")
echo "$ENTETES" | grep -q '^HTTP/.* 403' \
  && ok "message de 40001 caracteres : 403" \
  || ko "message hors plafond : 403 attendu"
echo "$ENTETES" | grep -qi '^x-policy-rule: limits.max_prompt_chars' \
  && ok "la regle refusee est indiquee dans X-Policy-Rule" \
  || ko "en-tete X-Policy-Rule absent ou inattendu"

# --- 6. Revocation ------------------------------------------------------------
title "6. Revocation d'un jeton"

TOKEN_A_REVOQUER=$("$PY" scripts/mint_token.py --sub revoque --roles reader --quiet 2>/dev/null)
AVANT=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
  --oauth2-bearer "$TOKEN_A_REVOQUER" "$ORCHESTRATOR_URL/auth/me")
REVOCATION=$(curl -sS --max-time 10 -X POST --oauth2-bearer "$TOKEN_A_REVOQUER" \
  "$ORCHESTRATOR_URL/auth/revoke" | jq -r '.revoked // false')
APRES=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
  --oauth2-bearer "$TOKEN_A_REVOQUER" "$ORCHESTRATOR_URL/auth/me")

[[ "$AVANT" == "200" ]] \
  && ok "jeton valide avant revocation : 200" \
  || ko "jeton avant revocation : 200 attendu, obtenu $AVANT"
[[ "$REVOCATION" == "true" ]] \
  && ok "revocation enregistree" \
  || ko "revocation non confirmee"
[[ "$APRES" == "401" ]] \
  && ok "jeton revoque : 401 (jeton pourtant non expire refuse)" \
  || ko "jeton revoque : 401 attendu, obtenu $APRES"

# --- 7. Journal d'audit -------------------------------------------------------
title "7. Journal d'audit"

if [[ -s "$AUDIT_LOG" ]]; then
  DERNIER=$(tail -n 1 "$AUDIT_LOG")
  echo "$DERNIER" | jq -e '.trace_id and .event and .sub' >/dev/null 2>&1 \
    && ok "evenements d'audit lisibles ($(wc -l < "$AUDIT_LOG") lignes)" \
    || ko "dernier evenement d'audit illisible"

  # Attention : la derniere ligne n'est pas forcement un evenement de dialogue
  # (une revocation de jeton en produit un autre). On cherche explicitement le
  # dernier chat.completed avant de verifier son contenu. Le filtrage se fait
  # avec jq et non grep : le formatage du JSON n'est pas un contrat.
  DERNIER_CHAT=$(jq -c 'select(.event == "chat.completed")' "$AUDIT_LOG" 2>/dev/null | tail -n 1)
  if [[ -n "$DERNIER_CHAT" ]]; then
    echo "$DERNIER_CHAT" | jq -e '.prompt_fingerprint and .prompt_chars' >/dev/null 2>&1 \
      && ok "le contenu du message n'est pas journalise en clair (empreinte seulement)" \
      || ko "l'evenement de dialogue ne porte pas d'empreinte"
    echo "$DERNIER_CHAT" | jq -e 'has("tools_proposed") and has("tools_executed")' >/dev/null 2>&1 \
      && ok "outils proposes et executes journalises (comparaison possible)" \
      || ko "outils non journalises"
  else
    ko "aucun evenement de dialogue dans l'audit (lancer un appel d'abord)"
  fi
else
  ko "journal d'audit vide ($AUDIT_LOG)"
fi

# --- 8. Appel de bout en bout (option --full) --------------------------------
if [[ "$FULL" == "1" ]]; then
  title "8. Appel de bout en bout (planification, outil MCP, modele)"
  echo "  ... appel en cours, comptez une minute sur CPU"

  DOC="Le service a subi une interruption de 3 heures. Cout estime : 41 000 euros."
  CORPS_CHAT=$(jq -n --arg t "$DOC" '{message:("Resume moi ce document : " + $t), synthesize:false}')
  REPONSE=$(curl -sS --max-time 600 -X POST "$ORCHESTRATOR_URL/v1/chat" \
    --oauth2-bearer "$TOKEN" -H 'Content-Type: application/json' -d "$CORPS_CHAT")

  OUTILS_UTILISES=$(echo "$REPONSE" | jq -r '(.tools_used // []) | join(",")' 2>/dev/null)
  [[ "$OUTILS_UTILISES" == "summarize_text" ]] \
    && ok "le modele a appele l'outil summarize_text" \
    || ko "outil appele : '$OUTILS_UTILISES' (attendu summarize_text)"

  LONGUEUR_REPONSE=$(echo "$REPONSE" | jq -r '(.answer // "") | length' 2>/dev/null)
  [[ "$LONGUEUR_REPONSE" -gt 80 ]] 2>/dev/null \
    && ok "reponse non vide ($LONGUEUR_REPONSE caracteres)" \
    || ko "reponse trop courte ou vide ($LONGUEUR_REPONSE caracteres)"

  echo "$REPONSE" | jq -e '.tool_trace[0].decision == "allowed"' >/dev/null 2>&1 \
    && ok "appel d'outil autorise par la GPO" \
    || ko "appel d'outil refuse"

  echo "  latence totale : $(echo "$REPONSE" | jq -r '.latency_ms') ms"
else
  title "8. Appel de bout en bout"
  echo "  (ignore : relancer avec --full pour inclure un appel au modele)"
fi

# --- Resultat -----------------------------------------------------------------
echo
echo "==================================================================="
echo " $PASSES controle(s) reussi(s), $FAILURES echoue(s)"
echo "==================================================================="

if [[ "$FAILURES" -gt 0 ]]; then
  echo "Journaux utiles : $LOG_DIR/*.log"
  exit 1
fi

echo "L'infrastructure repond et applique bien ses regles."
exit 0
