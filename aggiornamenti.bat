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

python aggiornamenti.py

set "EXIT=%ERRORLEVEL%"
echo.
echo ========================================
echo Codice di uscita: %EXIT%
echo ========================================
echo.

echo ========================================
echo    Manutenzione 9Router (comboact)
echo ========================================
echo.
if exist "9router-maintenance\update-comboact.ps1" (
    echo   Aggiornamento della combo di modelli...
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp09router-maintenance\update-comboact.ps1" -AddFreeModels -AutoReplace
) else (
    echo   Cartella 9router-maintenance non trovata: salto la manutenzione.
)
echo.
if exist "9router-maintenance\report-html.ps1" (
    echo   Generazione report HTML...
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp09router-maintenance\report-html.ps1" -Open
)
echo.
echo ========================================
echo    Manutenzione 9Router completata.
echo ========================================

endlocal
exit /b %EXIT%