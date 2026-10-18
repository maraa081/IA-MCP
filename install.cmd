@echo off
rem ===========================================================================
rem Installeur Windows : lance install.sh dans WSL2.
rem
rem La pile tourne sous Linux (bash, venv, Docker Desktop). Ce lanceur evite de
rem convertir le chemin a la main et rappelle les prerequis propres a Windows.
rem
rem   install.cmd                         installation guidee
rem   install.cmd --model qwen3.5:2b      mode non guide (PC 8 Go, CPU seul)
rem   install.cmd --dry-run               affiche le plan sans rien modifier
rem
rem Prerequis :
rem   1. WSL2 avec une distribution Linux      wsl --install
rem   2. Docker Desktop demarre, integration WSL activee pour la distribution
rem   3. Ollama installe DANS WSL :
rem        wsl -e bash -lc "curl -fsSL https://ollama.com/install.sh | sh"
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
if errorlevel 1 goto :sans_wsl

wsl.exe -l -q >nul 2>&1
if errorlevel 1 goto :sans_distro

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

rem --- Rappels des deux dependances qui viennent de Windows -----------------
wsl.exe bash -lc "command -v docker >/dev/null 2>&1"
if errorlevel 1 (
  echo   [ko] Docker absent dans WSL.
  echo        Installez Docker Desktop, demarrez-le, puis activez
  echo        Settings ^> Resources ^> WSL integration pour cette distribution.
  echo.
)

wsl.exe bash -lc "command -v ollama >/dev/null 2>&1"
if errorlevel 1 (
  echo   [ko] Ollama absent dans WSL.
  echo        Installez-le dans la distribution, pas seulement sous Windows :
  echo          wsl -e bash -lc "curl -fsSL https://ollama.com/install.sh ^| sh"
  echo.
)

echo   Docker Desktop doit etre demarre AVANT la fin de l'installation,
echo   sinon la gateway LiteLLM ne peut pas joindre PostgreSQL.
echo.

rem --- Lancement --------------------------------------------------------------
wsl.exe bash -lc "cd '%WSLCWD%' && bash install.sh %*"
if errorlevel 1 goto :fin_erreur

echo.
echo == Termine. Interface : http://localhost:3000
if /i "%cmdcmdline%"=="%cmdcmdline:/c=%" pause
exit /b 0

:sans_wsl
echo   [ko] WSL2 n'est pas installe.
echo.
echo        Dans un PowerShell administrateur :
echo          wsl --install
echo        puis redemarrez la machine et relancez ce script.
echo.
pause
exit /b 1

:sans_distro
echo   [ko] Aucune distribution Linux installee dans WSL.
echo.
echo        wsl --install -d Ubuntu
echo.
pause
exit /b 1

:fin_erreur
echo.
echo == Installation interrompue. Lisez le message ci-dessus.
echo.
pause
exit /b 1
