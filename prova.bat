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

rem --- Scelta Python: helper condiviso (preferisce 3.11, vedi _python.bat) ---
call "%~dp0_python.bat"
echo Python scelto: !PY_CMD!
!PY_CMD! main.py --dry-run --debug !MAIN_ARGS!

echo.
echo ========================================
echo  Niente video generato: solo timeline.
echo  Usa genera_video.bat per il video vero.
echo ========================================
if "%PAUSE_IT%"=="1" pause
endlocal