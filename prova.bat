@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Test (dry-run)
cd /d "%~dp0"

set "PAUSE_IT=1"
set "MAIN_ARGS="
:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" set "PAUSE_IT=0"& shift & goto parse
set "MAIN_ARGS=!MAIN_ARGS! %1"
shift
goto parse

:run
echo ========================================
echo    Sync Video: Test rapido (dry-run)
echo    Timeline senza generare il video
echo ========================================
echo.

rem --- Scelta Python: helper condiviso (vedi _python.bat) ---
rem Se non c'e' nessun Python il helper prova a installarlo con winget ed
rem esce con 1 se non riesce: ha gia' spiegato cosa fare.
call "%~dp0_python.bat"
if errorlevel 1 (
    echo.
    rem Anche sul fallimento --no-pause deve rispettarsi: e' il caso in cui
    rem l'automazione ha piu' bisogno di procedere senza fermarsi.
    if "!PAUSE_IT!"=="1" pause
    exit /b 9009
)
echo Python scelto: !PY_CMD!
!PY_CMD! src\main.py --dry-run --debug !MAIN_ARGS!
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo [USCITA ANOMALA] main.py e' terminato con codice %EXIT_CODE%.
)

echo.
echo ========================================
echo  Niente video generato: solo timeline.
echo  Usa genera_video.bat per il video vero.
echo ========================================
if "%PAUSE_IT%"=="1" pause
rem Propaga il codice del programma: prima endlocal lo azzerava a 0.
endlocal & exit /b %EXIT_CODE%