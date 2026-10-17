#!/usr/bin/env bash
# Demonstration de bout en bout : ce script raconte l'architecture en executant
# chaque etape. Il sert aussi de test de non-regression manuel.
#
#   ./scripts/demo.sh
#
# Duree : comptez plusieurs dizaines de secondes par appel de modele
# (inference sur CPU).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# L'orchestrateur n'a plus de port publie : les appels passent par le conteneur.
curl() { curl_api "$@"; }

need() { command -v "$1" >/dev/null || { echo "Outil manquant : $1"; exit 1; }; }
need curl
need jq

banner() {
  echo
  echo "==================================================================="
  echo " $1"
  echo "==================================================================="
}

note() {
  echo
  echo "[a retenir] $1"
}

api() {
  # api <methode> <chemin> [<jeton>] [<corps-json>]
  local method="$1" path="$2" token="${3:-}" body="${4:-}"
  local args=(-sS -X "$method" "$ORCHESTRATOR_URL$path" -H 'Content-Type: application/json')
  [[ -n "$token" ]] && args+=(-H "Authorization: Bearer $token")
  [[ -n "$body" ]] && args+=(-d "$body")
  curl "${args[@]}"
}

banner "ETAPE 0 - Etat des dependances"
api GET /health | jq .
note "Un serveur MCP injoignable n'empeche pas le demarrage : il est marque
'ko' et ses outils sortent du catalogue."

banner "ETAPE 1 - Sans jeton, l'API refuse"
echo "POST /v1/chat sans en-tete Authorization :"
curl -sS -o /dev/null -w "  code HTTP = %{http_code}\n" -X POST "$ORCHESTRATOR_URL/v1/chat" \
  -H 'Content-Type: application/json' -d '{"message":"bonjour"}'
note "Le refus est immediat : il ne depend d'aucune logique metier."

banner "ETAPE 2 - Authentification (emission d'un jeton)"
LOGIN_BODY=$(jq -n --arg u "$DEV_USERNAME" --arg p "$DEV_PASSWORD" '{username:$u,password:$p}')
LOGIN_RESPONSE=$(api POST /auth/token "" "$LOGIN_BODY")
TOKEN=$(echo "$LOGIN_RESPONSE" | jq -r .access_token)
echo "Jeton recu : ${TOKEN:0:40}... (roles : $(echo "$LOGIN_RESPONSE" | jq -c .roles))"
echo "Claims du jeton :"
"$PY" - "$TOKEN" <<'PYTHON'
import base64, json, sys
payload = sys.argv[1].split(".")[1]
payload += "=" * (-len(payload) % 4)
print(json.dumps(json.loads(base64.urlsafe_b64decode(payload)), indent=2, ensure_ascii=False))
PYTHON
note "Le jeton est autoporteur : aucune consultation d'annuaire a chaque appel.
'jti' permet de le revoquer precisement. En production il serait emis par un
fournisseur d'identite, pas par cette route de demonstration."

banner "ETAPE 3 - Droits effectifs portes par le jeton (GPO)"
api GET /auth/me "$TOKEN" | jq .
note "Lecture directe de config/policies.yaml. Aucune valeur inventee : c'est le
fichier qui fait foi, et le modele n'a aucun acces a cette decision."

banner "ETAPE 4 - Outils visibles pour cet utilisateur"
api GET /tools "$TOKEN" | jq '[.[] | {name, server}]'
note "Ces outils sont exactement les schemas transmis au modele. Les valeurs
fermees (framework, audience, style) y figurent comme listes de choix : le modele
les voit au lieu de les deviner."

banner "ETAPE 5 - Cas 1 : 'resume moi ce document' -> outil summarize_text"
DOC=$(cat <<'EOF'
Le service de facturation a subi une interruption de 3 heures le 12 octobre,
cause par la saturation du pool de connexions a la base de donnees. Le pic de
charge provenait d'un import nocturne de 2,4 millions de lignes, lance sans
limitation de debit. Une premiere mitigation a consiste a augmenter la taille du
pool de 50 a 200 connexions, ce qui a retabli le service en 22 minutes. Deux
actions correctives sont retenues : limiter le debit de l'import a 5 000 lignes
par seconde, et declencher une alerte lorsque l'attente moyenne sur le pool
depasse 200 ms pendant plus de 5 minutes. Cout estime de l'incident : 41 000
euros de perte de chiffre d'affaires. Responsable du suivi : la plateforme data.
EOF
)
SUMMARY_BODY=$(jq -n --arg text "$DOC" '{message:("Resume moi ce document :\n\n" + $text), synthesize:true}')
api POST /v1/chat "$TOKEN" "$SUMMARY_BODY" | jq '{answer, tools_used, planner_mode, tool_trace: [.tool_trace[] | {tool, decision, duration_ms}], latency_ms}'
note "Regardez 'tools_used' plus que 'answer'. Un modele peut produire une bonne
reponse sans appeler l'outil : c'est arrive lors du premier essai, et c'est un
defaut (resultat non garanti, aucun passage par l'outil, trace incomplete).
Si 'tools_used' est vide ici, c'est ce comportement qui revient."

banner "ETAPE 6 - Cas 2 : du code a documenter -> outil code_to_technical_doc"
CODE=$(sed -n '1,40p' "$PROJECT_ROOT/orchestrator/app/policy/rate_limit.py")
CODE_BODY=$(jq -n --arg code "$CODE" \
  '{message:("Documente ce code en suivant le framework diataxis pour un public developpeur :\n\n```python\n" + $code + "\n```"), synthesize:true}')
api POST /v1/chat "$TOKEN" "$CODE_BODY" \
  | jq '{tools_used, planner_mode, framework: (.tool_trace[0].output_preview // "" | fromjson? // {} | .framework_label), decision: .tool_trace[0].decision, reason: .tool_trace[0].reason, answer, latency_ms}'
note "L'outil impose la structure du framework section par section, et verifie
ensuite dans le texte produit quelles sections manquent. Le modele ne
s'auto-certifie pas."

banner "ETAPE 7 - Contre-epreuve 1 : un role moins permissif voit moins d'outils"
READER_TOKEN=$("$PY" "$PROJECT_ROOT/scripts/mint_token.py" --sub lecteur --roles reader --quiet)
echo "Jeton emis pour le role 'reader'. Outils visibles :"
api GET /tools "$READER_TOKEN" | jq -c '[.[].name]'
note "'code_to_technical_doc' est absent. Ce n'est pas une mention dans un prompt :
le modele n'a jamais recu ce schema."

banner "ETAPE 8 - Contre-epreuve 2 : le role 'reader' tente l'outil interdit"
DENIED_BODY=$(jq -n --arg code "$CODE" \
  '{message:("Utilise loutil de documentation technique pour documenter ce code :\n```python\n" + $code + "\n```"), synthesize:false}')
api POST /v1/chat "$READER_TOKEN" "$DENIED_BODY" \
  | jq '{tools_used, policy_denials, tool_trace: [.tool_trace[] | {tool, decision, reason}], answer}'
note "Deux cas de figure, tous les deux acceptables :
- 'policy_denials' est rempli : la GPO a refuse, l'appel n'a jamais atteint le
  serveur MCP ;
- 'policy_denials' est vide : le modele n'a rien propose, il a repondu lui-meme.
  Cela ne prouve pas que la GPO fonctionne. L'absence de refus n'est pas une
  preuve de securite : la garantie vient des tests unitaires
  (test_appel_hors_droits_est_refuse_a_l_execution)."

banner "ETAPE 9 - Contre-epreuve 3 : un plafond global, refuse sans appeler le modele"
LONG_MESSAGE=$("$PY" -c "print('a' * 40001)")
LONG_BODY=$(jq -n --arg m "$LONG_MESSAGE" '{message:$m}')
echo "POST /v1/chat avec un message de 40001 caracteres (plafond : 40000) :"
curl -sS -D- -o /tmp/denied_body.txt -X POST "$ORCHESTRATOR_URL/v1/chat" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$LONG_BODY" \
  | grep -i -E '^HTTP|^x-policy-rule' | sed 's/^/  /'
cat /tmp/denied_body.txt | jq -c . | sed 's/^/  /'
note "Refus en 403 avec la regle exacte dans l'en-tete X-Policy-Rule. Le modele
n'a meme pas ete appele : inutile de payer une inference pour un message hors
plafond."

banner "ETAPE 10 - Trace d'audit"
tail -n 4 "$LOG_DIR/audit.jsonl" \
  | jq -c '{event, sub, roles, tools_executed, denials, errors, allowed, latency_ms}'
note "La comparaison entre 'tools_proposed' et 'tools_executed' est le meilleur
indicateur en production : elle montre soit une erreur de configuration des
droits, soit une tentative de contournement."

banner "TERMINE"
cat <<EOF
Fichiers a consulter :
  - journal d'audit      : var/logs/audit.jsonl
  - journaux applicatifs : var/logs/*.log
  - politique appliquee  : config/policies.yaml
  - routage des modeles  : gateway/config.yaml
  - diagrammes           : docs/uml/
EOF
