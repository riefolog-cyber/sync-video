@echo off
rem ============================================================
rem  Sync Video: aggiornamento PACCHETTI python
rem
rem  Fa solo quello che il nome promette: controlla le versioni su PyPI e
rem  chiede S/N per installare quelle non pinnate.
rem
rem  La manutenzione di 9Router NON e' qui dentro: e' una cosa separata
rem  (aggiornamenti_9router.bat) perche' modifica quali modelli il servizio
rem  espone, e non c'entra con l'aggiornamento delle dipendenze.
rem
rem  Uso:
rem    aggiornamenti.bat                        chiede S/N prima di installare
rem    aggiornamenti.bat --no-update            notifica e basta
rem    aggiornamenti.bat --no-update-check      non controlla PyPI
rem    aggiornamenti.bat --no-pause             non fermarsi (automazione)
rem  Gli interruttori sono gli stessi di main.py: niente da ricordare due volte.
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Aggiornamento pacchetti
cd /d "%~dp0"

set "PAUSE_IT=1"
set "PA_FLAGS="

rem --- Interruttori (stessi nomi di genera_video.bat) ---
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--no-pause" (set "PAUSE_IT=0"& shift & goto parse)
if /i "%~1"=="--no-update" (set "PA_FLAGS=!PA_FLAGS! --no-update"& shift & goto parse)
if /i "%~1"=="--no-update-check" (set "PA_FLAGS=!PA_FLAGS! --no-update-check"& shift & goto parse)
echo Opzione sconosciuta: "%~1"
echo Usa --no-pause, --no-update o --no-update-check.
set "EXIT=2"
goto fine
:parsed

echo ========================================
echo    Sync Video: Aggiornamento pacchetti
echo    Verifica versioni PyPI e installa
echo ========================================
echo.

rem --- Scelta Python: helper condivioso (preferisce 3.11, vedi _python.bat) ---
rem Se non c'e' nessun Python il helper prova a installarlo con winget ed
rem esce con 1 se non riesce: ha gia' spiegato cosa fare.
call "%~dp0_python.bat"
if errorlevel 1 (
    set "EXIT=9009"
    goto fine
)
!PY_CMD! --version >NUL 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRORE] Python non e' stato trovato nel PATH.
    echo Installa Python oppure abilita "Add Python to PATH".
    set "EXIT=9009"
    goto fine
)
echo Python scelto: !PY_CMD!
!PY_CMD! aggiornamenti.py!PA_FLAGS!
set "EXIT=%ERRORLEVEL%"

:fine
echo.
echo Codice di uscita: %EXIT%
echo Per la manutenzione di 9Router esiste "aggiornamenti_9router.bat".
if "%PAUSE_IT%"=="1" pause
exit /b %EXIT%