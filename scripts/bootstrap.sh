#!/usr/bin/env bash
# Installation complete du labo. Idempotent : on peut le relancer sans risque.
#
#   ./scripts/bootstrap.sh
#
# Etapes : environnements Python, cle JWT, fichier .env, modeles Ollama.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "== 1/5  Environnement Python de l'orchestrateur et des serveurs MCP"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
  echo "  venv cree : $VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PROJECT_ROOT/orchestrator/requirements.txt"
echo "  dependances installees"

echo "== 2/5  Environnement Python de la gateway LiteLLM"
if [[ ! -d "$VENV_GATEWAY" ]]; then
  python3 -m venv "$VENV_GATEWAY"
  echo "  venv cree : $VENV_GATEWAY"
fi
"$VENV_GATEWAY/bin/pip" install --quiet --upgrade pip
"$VENV_GATEWAY/bin/pip" install --quiet -r "$PROJECT_ROOT/gateway/requirements.txt"
echo "  dependances installees"

echo "== 3/5  Cles de signature JWT"
"$PY" "$PROJECT_ROOT/scripts/gen_keys.py"

echo "== 4/5  Fichier de configuration"
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
  echo "  .env cree depuis .env.example"
else
  echo "  .env deja present (non modifie)"
fi

echo "== 5/5  Modeles Ollama"
MODELE_OLLAMA="${OLLAMA_MODEL:-qwen3.5:4b}"
if [[ "${1:-}" == "--no-model" ]]; then
  echo "  [skip] telechargement du modele (--no-model)"
elif ! curl -fsS --max-time 3 "$OLLAMA_URL/api/version" >/dev/null 2>&1; then
  echo "  Ollama ne repond pas sur $OLLAMA_URL : lancez 'ollama serve' puis relancez."
else
  # Le meme modele sert aux deux alias (planner et writer) : on economise la
  # memoire. Le nom vient de OLLAMA_MODEL (.env) ; install.sh le choisit et
  # scripts/set_model.py reporte ce choix dans gateway/config.yaml.
  echo "  modele : $MODELE_OLLAMA"
  ollama pull "$MODELE_OLLAMA"
fi

cat <<'EOF'

Installation terminee.

Pour demarrer la pile complete :
  ./scripts/up.sh

Pour voir la demonstration de bout en bout :
  ./scripts/demo.sh
EOF
