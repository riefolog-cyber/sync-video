@echo off
rem ============================================================
rem  Sync Video: manutenzione 9Router (combo "comboact")
rem
rem  Sta in un file a parte da aggiornamenti.bat perche' questa operazione
rem  MODIFICA quali modelli il servizio espone: con -AutoReplace toglie dalla
rem  combo i modelli che falliscono, con -AddFreeModels aggiunge quelli
rem  gratuiti. Non e' annullabile, quindi va richiesta e non nascosta dietro
rem  "aggiorna le dipendenze".
rem
rem  Il percorso di default della sincronizzazione NON usa 9Router (--llm off):
rem  questo script serve se si usa --llm 9router.
rem
rem  Uso:
rem    aggiornamenti_9router.bat            chiede conferma, poi applica
rem    aggiornamenti_9router.bat --dry-run  mostra cosa cambierebbe e basta
rem    aggiornamenti_9router.bat --no-pause non fermarsi (automazione)
rem
rem  ATTENZIONE sul codice di uscita: update-comboact.ps1 esce con 0 anche se
rem  ha rimosso modelli falliti (succede solo su eccezione grave). Un 0 qui
rem  vuol dire "lo script e' andato a buon fine", NON "la combo e' sana":
rem  per quello guarda la riga SUCCESS: Kept=.. | Removed=.. .
rem
rem  NOTA: mantenere questo file in SOLO ASCII (vedi _python.bat).
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Manutenzione 9Router
cd /d "%~dp0"

set "PAUSE_IT=1"
set "DRYRUN="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--no-pause" (set "PAUSE_IT=0"& shift & goto parse)
if /i "%~1"=="--dry-run" (set "DRYRUN=1"& shift & goto parse)
echo Opzione sconosciuta: "%~1"
echo Usa --dry-run o --no-pause.
set "EXIT=2"
goto fine
:parsed

echo ========================================
echo    Sync Video: Manutenzione 9Router (comboact)
echo    Aggiorna la combo di modelli esposta da 9Router
echo ========================================
echo.

set "SCRIPT=%~dp09router-maintenance\update-comboact.ps1"
set "REPORT=%~dp09router-maintenance\report-html.ps1"

where pwsh >NUL 2>&1
if not %ERRORLEVEL% EQU 0 (
    echo [ERRORE] PowerShell 7 ^(pwsh^) non trovato nel PATH.
    echo Serve pwsh, non il PowerShell di Windows ^(che e' 5.1^).
    goto fallito_pwsh
)
if not exist "%SCRIPT%" (
    echo [ERRORE] Non trovo lo script: "%SCRIPT%".
    goto fallito_assente
)

rem I due flag di mutazione si attivano insieme: in anteprima non si applica
rem niente, e in mutazione non si aggiunge nulla di nuovo a meta' operazione.
set "MUTA=0"
set "PS_ARGS="
if "%DRYRUN%"=="1" (
    echo   Modalita' ANTEPRIMA: nessuna modifica verra' applicata.
    echo   Mostro cosa cambierebbe. Per applicare, rilancia senza --dry-run.
    echo.
    set "PS_ARGS=-DryRun"
) else (
    echo   ATTENZIONE: verranno rimossi dalla combo i modelli che falliscono
    echo   e aggiunti quelli gratuiti. Modifica il servizio, non i pacchetti.
    echo.
    rem 2>nul: con stdin non interattivo (--no-pause, scheduler, CI) choice fallisce
rem e stamperebbe "System.Management.Automation.RemoteException ... il file e'
rem vuoto": sembrerebbe un crash di PowerShell. Qui fallire significa ANNULLARE,
rem che e' la risposta giusta quando nessuno puo' dire di si'.
choice /C SN /N /M "   Applico le modifiche alla combo? [S/N] " 2>nul
    if errorlevel 2 (
        echo   Annullato: nessuna modifica.
        set "EXIT=0"
        goto fine
    )
    set "MUTA=1"
    set "PS_ARGS=-AddFreeModels -AutoReplace"
)

echo.
echo   Aggiornamento della combo di modelli...
pwsh -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" !PS_ARGS!
set "EXIT=%ERRORLEVEL%"

rem Il report non viene aperto nel browser: un aggiornamento non deve navigare
rem il desktop. report-html.ps1 stampa da solo il percorso esatto del file.
if "%MUTA%"=="1" if exist "%REPORT%" (
    echo.
    echo   Generazione report HTML...
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%REPORT%"
)

:fine
echo.
echo Codice di uscita: %EXIT%
if "%PAUSE_IT%"=="1" pause
exit /b %EXIT%

:fallito_pwsh
echo.
if "%PAUSE_IT%"=="1" pause
exit /b 9009

:fallito_assente
echo.
if "%PAUSE_IT%"=="1" pause
exit /b 2