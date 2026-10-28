#!/usr/bin/env bash
# Installe (ou retire) le demarrage automatique de la pile au boot.
#
#   ./scripts/install-autostart.sh            installe et active le service
#   ./scripts/install-autostart.sh --now      installe, active et demarre tout de suite
#   ./scripts/install-autostart.sh --remove   desactive et retire le service
#
# Pourquoi un service systemd *utilisateur* : pas besoin de sudo, il survit aux
# mises a jour du projet (il appelle scripts/start.sh), et il redemarre avec
# WSL. Limite assumee : WSL ne demarre pas tout seul au boot de Windows. Voir
# deploy/register-windows-autostart.ps1 pour declencher WSL au login Windows.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

UNITE="ia-mcp-stack.service"
DEST="$HOME/.config/systemd/user/$UNITE"
TEMPLATE="$PROJECT_ROOT/deploy/ia-mcp-stack.service.in"

if [[ "${1:-}" == "--remove" ]]; then
  systemctl --user disable --now "$UNITE" 2>/dev/null || true
  rm -f "$DEST"
  systemctl --user daemon-reload
  echo "Service $UNITE retire. La pile reste pilotable avec ./scripts/start.sh"
  exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "ERREUR : systemctl absent. Activer systemd dans WSL (/etc/wsl.conf : [boot] systemd=true)" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"
sed "s|@PROJECT_ROOT@|$PROJECT_ROOT|g" "$TEMPLATE" >"$DEST"
echo "  unite ecrite : $DEST"

systemctl --user daemon-reload
systemctl --user enable "$UNITE" >/dev/null
echo "  service active au demarrage : $UNITE"

# Sans linger, le systemd utilisateur ne demarre qu'a la premiere ouverture de
# session : le service ne partirait pas au boot.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  echo
  echo "  ATTENTION : linger desactive pour $USER."
  echo "  Le service ne demarrera qu'a l'ouverture d'une session. Activer :"
  echo "    sudo loginctl enable-linger $USER"
else
  echo "  linger deja actif (demarrage sans session ouverte)"
fi

if [[ "${1:-}" == "--now" ]]; then
  systemctl --user restart "$UNITE"
  echo "  demarrage lance, suivi : journalctl --user -u $UNITE -f"
fi

echo
echo "Utile :"
echo "  ./scripts/start.sh                 demarrer a la main"
echo "  ./scripts/status.sh                verifier l'etat"
echo "  systemctl --user status $UNITE     etat du service"
echo "  journalctl --user -u $UNITE -n 50  derniers evenements"
