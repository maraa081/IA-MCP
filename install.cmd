@echo off
rem ===========================================================================
rem Installeur Windows : lance install.sh dans WSL2.
rem
rem La pile tourne sous Linux (bash, venv, Docker Desktop). Ce lanceur convertit
rem le chemin, verifie WSL2, et propose de l'installer si la machine est vierge.
rem Les dependances Linux (python3-venv, Ollama, jq) sont installees par
rem install.sh lui-meme, apres confirmation.
rem
rem   install.cmd                         installation guidee
rem   install.cmd --model qwen3.5:2b      mode non guide (PC 8 Go, CPU seul)
rem   install.cmd --dry-run               affiche le plan sans rien modifier
rem   install.cmd --no-auto-install       n'installe aucune dependance
rem
rem Prerequis cote Windows :
rem   1. WSL2 avec une distribution Linux      wsl --install
rem   2. Docker Desktop demarre, integration WSL activee pour la distribution
rem      (facultatif : sans Docker, la pile tourne sans PostgreSQL)
rem ===========================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

rem %~dp0 finit par un antislash : dans "C:\...\" le guillemet serait echappe
rem et wslpath recevrait un chemin tronque. On retire l'antislash final.
set "RACINE=%~dp0"
if "%RACINE:~-1%"=="\" set "RACINE=%RACINE:~0,-1%"
cd /d "%RACINE%"

echo.
echo == Installeur du labo MCP (Windows vers WSL2)
echo.

where wsl.exe >nul 2>&1
if errorlevel 1 goto :proposer_wsl

wsl.exe -l -q >nul 2>&1
if errorlevel 1 goto :proposer_distro

set "WSLCWD="
for /f "usebackq delims=" %%i in (`wsl.exe wslpath -a "%RACINE%" 2^>nul`) do set "WSLCWD=%%i"
if not defined WSLCWD (
  echo   [ko] Conversion du chemin vers WSL impossible pour : %RACINE%
  echo        Verifiez que la distribution par defaut repond : wsl.exe -l -v
  goto :fin_erreur
)

echo   dossier Windows : %RACINE%
echo   dossier WSL     : %WSLCWD%
echo.

wsl.exe bash -lc "cd '%WSLCWD%' && test -f install.sh"
if errorlevel 1 (
  echo   [ko] install.sh introuvable dans %WSLCWD%
  goto :fin_erreur
)

rem --- Rappel de la seule dependance non installable en ligne de commande -----
rem Redirection laissee hors des guillemets : cmd l'interprete meme a l'interieur.
wsl.exe bash -lc "docker info" >nul 2>&1
if errorlevel 1 (
  echo   [note] Docker Desktop ne repond pas.
  echo          Pour PostgreSQL : demarrez Docker Desktop et activez
  echo          Settings ^> Resources ^> WSL integration pour cette distribution.
  echo          Sans Docker, la pile fonctionne quand meme, sans base de cles.
  echo.
)

echo   Les dependances Linux manquantes seront installees par install.sh,
echo   apres confirmation. Un mot de passe sudo peut etre demande.
echo.

rem --- Lancement --------------------------------------------------------------
wsl.exe bash -lc "cd '%WSLCWD%' && bash install.sh %*"
if errorlevel 1 goto :fin_erreur

echo.
echo == Termine. Interface : http://localhost:3000
if /i "%cmdcmdline%"=="%cmdcmdline:/c=%" pause
exit /b 0

:proposer_wsl
echo   [ko] WSL2 n'est pas installe sur cette machine.
echo.
set "REP="
set /p "REP=Installer WSL2 maintenant ? (droits admin et redemarrage necessaires) [o/n] : "
if /i not "%REP%"=="o" goto :fin_erreur
echo.
echo   Ouverture de PowerShell en administrateur...
powershell -NoProfile -Command "Start-Process -Verb RunAs wsl.exe -ArgumentList '--install'"
echo.
echo   Apres l'installation : redemarrez la machine, puis relancez install.cmd.
pause
exit /b 1

:proposer_distro
echo   [ko] WSL2 est present mais aucune distribution Linux n'est installee.
echo.
set "REP="
set /p "REP=Installer Ubuntu maintenant ? (droits admin et redemarrage possibles) [o/n] : "
if /i not "%REP%"=="o" goto :fin_erreur
echo.
powershell -NoProfile -Command "Start-Process -Verb RunAs wsl.exe -ArgumentList '--install','-d','Ubuntu'"
echo.
echo   Apres l'installation : redemarrez si demande, puis relancez install.cmd.
pause
exit /b 1

:fin_erreur
echo.
echo == Installation interrompue. Lisez le message ci-dessus.
echo.
pause
exit /b 1
