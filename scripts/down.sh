#!/usr/bin/env bash
# Arrete proprement les services lances par up.sh.
# Ollama est laisse actif (il sert d'autres usages) : passez --all pour l'arreter.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "Arret des services"
stop_background orchestrator          # ancien processus hote, s'il en reste un
"$PROJECT_ROOT/scripts/orchestrator.sh" down || true
stop_background mcpo
stop_background mcp-codedb
stop_background mcp-codedoc
stop_background mcp-summarizer
stop_background laya
stop_background gateway

if [[ "${1:-}" == "--all" ]]; then
  stop_background ollama
else
  echo "  [note] Ollama laisse actif (utilisez --all pour l'arreter)"
fi
