#!/usr/bin/env bash
# Commande unique : ./start.sh
#
# Lance toute la pile IA-MCP dans l'ordre des dependances, attend que chaque
# service reponde, verifie que l'orchestrateur voit bien les outils MCP, puis
# affiche le tableau d'etat. Idempotent : relancable sans risque.
#
#   ./start.sh              demarrage complet
#   ./start.sh --no-index   sans reindexer le code (demarrage rapide)
#   ./start.sh --no-webui   sans Open WebUI
#
# Code de sortie : 0 si tout repond, 1 sinon.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/start.sh" "$@"
