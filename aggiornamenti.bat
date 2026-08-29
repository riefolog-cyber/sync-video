@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Aggiornamento pacchetti
cd /d "%~dp0"

echo ========================================
echo    Sync Video: Aggiornamento pacchetti
echo    Verifica versioni PyPI e installa
echo ========================================
echo.

rem --- Scelta Python: helper condiviso (preferisce 3.11, vedi _python.bat) ---
call "%~dp0_python.bat"
!PY_CMD! --version >NUL 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRORE] Python non e' stato trovato nel PATH.
    echo Installa Python oppure abilita "Add Python to PATH".
    set "EXIT=9009"
    goto after_python
)
echo Python scelto: !PY_CMD!
!PY_CMD! aggiornamenti.py
set "EXIT=%ERRORLEVEL%"
:after_python
echo.
echo ========================================
echo Codice di uscita: %EXIT%
echo ========================================
echo.

echo ========================================
echo    Manutenzione 9Router (comboact)
echo ========================================
echo.
where pwsh >NUL 2>&1
if not %ERRORLEVEL% EQU 0 (
    echo   PowerShell 7 ^(pwsh^) non trovato: salto la manutenzione 9Router.
    goto skip_router
)
if exist "9router-maintenance\update-comboact.ps1" (
    echo   Aggiornamento della combo di modelli...
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp09router-maintenance\update-comboact.ps1" -AddFreeModels -AutoReplace
) else (
    echo   Cartella 9router-maintenance non trovata: salto la manutenzione.
)
echo.
:skip_router
where pwsh >NUL 2>&1
if %ERRORLEVEL% EQU 0 if exist "9router-maintenance\report-html.ps1" (
    echo   Generazione report HTML...
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp09router-maintenance\report-html.ps1" -Open
)
echo.
echo ========================================
echo    Manutenzione 9Router completata.
echo ========================================
echo.
pause
exit /b %EXIT%