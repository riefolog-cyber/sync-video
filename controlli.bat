@echo off
rem ============================================================
rem  Sync Video: controlli del codice in un unico comando
rem
rem  Esegue, nello stesso ordine della CI piu' gli extra locali:
rem    ruff, mypy su win32/linux/darwin, la suite completa,
rem    _debug_hardware.py (encoder reali e ripiego a runtime).
rem
rem  NON e' la verifica post-run del video (quella e' analysis_sync.py,
rem  vedi docs/verifica.md): qui si controlla il codice.
rem
rem  Uso:
rem    controlli.bat                 tutti i controlli
rem    controlli.bat --no-pause      non fermarsi (automazione)
rem    controlli.bat mypy            solo i controlli col nome che contiene "mypy"
rem  Esito: 0 se tutto passa, 1 se qualcosa fallisce.
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Controlli del codice
cd /d "%~dp0"

set "PAUSE_IT=1"
set "CONTROLLI_ARGS="
:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" (set "PAUSE_IT=0"& shift & goto parse)
set "CONTROLLI_ARGS=!CONTROLLI_ARGS! %1"
shift
goto parse

:run
echo ========================================
echo    Sync Video: Controlli del codice
echo    CI + mypy 3 piattaforme + debug hardware
echo ========================================

rem --- Scelta Python: helper condiviso (vedi _python.bat) ---
rem Se non c'e' nessun Python il helper prova a installarlo con winget ed
rem esce con 1 se non riesce: ha gia' spiegato cosa fare.
call "%~dp0_python.bat"
if errorlevel 1 (
    echo.
    if "%PAUSE_IT%"=="1" pause
    exit /b 9009
)
echo Python scelto: !PY_CMD!
!PY_CMD! src\controlli.py!CONTROLLI_ARGS!
set "EXIT=%ERRORLEVEL%"

echo.
if "%EXIT%"=="0" (
    echo Controlli: TUTTO OK.
) else (
    echo Controlli: QUALCOSA E' FALLITO ^(codice %EXIT%^).
)
if "%PAUSE_IT%"=="1" pause
endlocal & exit /b %EXIT%
