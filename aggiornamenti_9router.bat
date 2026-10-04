@echo off
rem ============================================================
rem  Sync Video: manutenzione 9Router (combo "comboact")
rem
rem  Sta in un file a parte da aggiornamenti.bat perche' questa operazione
rem  MODIFICA quali modelli il servizio espone: con -AutoReplace toglie dalla
rem  combo i modelli che falliscono, con -AddFreeModels aggiunge quelli
rem  gratuiti. Sono effetti automatici e non annullabili: vanno chiesti, non
rem  nascosti dietro "aggiorna le dipendenze".
rem
rem  Il percorso di default della sincronizzazione NON usa 9Router (--llm off):
rem  questo script serve se si usa --llm 9router.
rem
rem  NOTA: mantenere questo file in SOLO ASCII (vedi _python.bat).
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Manutenzione 9Router
cd /d "%~dp0"

echo ========================================
echo    Sync Video: Manutenzione 9Router (comboact)
echo    Aggiorna la combo di modelli esposta da 9Router
echo ========================================
echo.
echo  ATTENZIONE: toglie dalla combo i modelli che falliscono e aggiunge
echo  quelli gratuiti. Modifica il servizio, non i pacchetti python.
echo.

set "SCRIPT=%~dp09router-maintenance\update-comboact.ps1"
set "REPORT=%~dp09router-maintenance\report-html.ps1"

where pwsh >NUL 2>&1
if not %ERRORLEVEL% EQU 0 (
    echo [ERRORE] PowerShell 7 ^(pwsh^) non trovato nel PATH.
    echo Serve pwsh, non il PowerShell di Windows ^(che e' 5.1^).
    echo Il vecchio aggiornamenti.bat saltava questa parte in silenzio:
    echo ora si dice cosa manca invece di sembrare che sia andato tutto bene.
    goto fallito_pwsh
)
if not exist "%SCRIPT%" (
    echo [ERRORE] Non trovo lo script: "%SCRIPT%".
    goto fallito_assente
)

echo   Aggiornamento della combo di modelli...
pwsh -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -AddFreeModels -AutoReplace
set "EXIT=%ERRORLEVEL%"

rem Il report non viene aperto nel browser: un aggiornamento non deve
rem navigare il desktop. Il percorso del file viene stampato sotto.
if exist "%REPORT%" (
    echo.
    echo   Generazione report HTML...
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%REPORT%"
)

echo.
echo ========================================
echo Codice di uscita: %EXIT%
echo Il report HTML, se generato, e' in 9router-maintenance\logs\
echo ========================================
echo.
pause
exit /b %EXIT%

:fallito_pwsh
echo.
pause
exit /b 9009

:fallito_assente
echo.
pause
exit /b 2