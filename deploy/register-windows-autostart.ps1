# Demarre WSL (donc la pile IA-MCP) au login Windows.
#
# Pourquoi ce fichier : WSL2 ne demarre pas tout seul quand Windows demarre.
# Le service systemd utilisateur (scripts/install-autostart.sh) ne peut donc
# s'executer qu'une fois WSL lance. Cette tache planifiee declenche WSL au
# login, puis systemd et le service ia-mcp-stack font le reste.
#
# Installation, dans PowerShell (pas besoin d'administrateur) :
#
#   powershell -ExecutionPolicy Bypass -File .\deploy\register-windows-autostart.ps1
#
# Desinstallation :
#
#   powershell -ExecutionPolicy Bypass -File .\deploy\register-windows-autostart.ps1 -Remove
#
param(
  [string]$Distro = "",
  [string]$TaskName = "IA-MCP Autostart",
  [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
  schtasks /Delete /TN $TaskName /F 2>$null | Out-Null
  Write-Host "Tache '$TaskName' supprimee."
  exit 0
}

# Distro par defaut si non fournie : la premiere listee par WSL.
if ([string]::IsNullOrWhiteSpace($Distro)) {
  $Distro = (wsl.exe -l -q | Select-Object -First 1)
  if ($Distro) { $Distro = $Distro.Trim() }
}
if ([string]::IsNullOrWhiteSpace($Distro)) {
  throw "Aucune distribution WSL detectee. Passer -Distro <nom>."
}

# Commande : demarre la distro sans ouvrir de fenetre, en tant qu'utilisateur
# par defaut, et lance le demarrage de la pile.
$action = "wsl.exe -d $Distro -u user --cd /home/user/IA-MCP -- bash -lc './scripts/start.sh --no-index'"

Write-Host "Distribution : $Distro"
Write-Host "Commande     : $action"

schtasks /Create /TN $TaskName /TR $action /SC ONLOGON /RL LIMITED /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Creation de la tache impossible (code $LASTEXITCODE)." }

Write-Host "Tache '$TaskName' creee : WSL et la pile demarreront au login."
Write-Host "Tester maintenant : schtasks /Run /TN `"$TaskName`""
Write-Host "Etat de la pile  : wsl -d $Distro -- bash -lc 'cd /home/user/IA-MCP && ./scripts/status.sh'"
