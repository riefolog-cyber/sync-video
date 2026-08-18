@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Slide -> Audio

set "PAUSE_IT=1"
set "CHECK_UPDATES=0"
set "MAIN_ARGS="
:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" set "PAUSE_IT=0"& shift & goto parse
if /i "%~1"=="--check-updates" set "CHECK_UPDATES=1"& shift & goto parse
set "MAIN_ARGS=!MAIN_ARGS! %1"
shift
goto parse

:run
echo ========================================
echo    Sync Video: Slide -^> Audio
echo    Sincronizzazione semantica (offline)
echo ========================================
echo.

echo Avvio pipeline: OCR -^> Trascrizione -^> Sincronizzazione semantica -^> Video
echo ========================================
echo.
echo  Nota: il controllo aggiornamenti si fa con aggiornamenti.bat
echo  (qui disattivato per non rallentare la generazione del video).
echo ========================================
echo.

if "%CHECK_UPDATES%"=="0" (
    python main.py --no-update-check --no-confirm !MAIN_ARGS!
) else (
    python main.py --no-confirm !MAIN_ARGS!
)

echo.
echo ========================================
if "%PAUSE_IT%"=="1" pause
endlocal
