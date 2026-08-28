@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
cd /d "%~dp0"
title Sync Video: Slide -> Audio

set "PAUSE_IT=1"
set "CHECK_UPDATES=0"
set "MAIN_ARGS=--whisper-model tiny --llm auto --engine ffmpeg"
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

rem --- PATH FFmpeg: cerca in PATH, altrimenti nella cartella WinGet (ricerca dinamica, portabile) ---
where ffmpeg >NUL 2>&1
if not %ERRORLEVEL% EQU 0 (
    for /f "delims=" %%F in ('where /r "%LOCALAPPDATA%\Microsoft\WinGet\Packages" ffmpeg.exe 2^>NUL') do set "FFMPEG_FOUND=%%F"
    if defined FFMPEG_FOUND (
        set "PATH=%PATH%;!FFMPEG_FOUND:~0,-10!"
        echo FFmpeg trovato nel percorso WinGet: !FFMPEG_FOUND!
    ) else (
        echo [ERRORE] FFmpeg non trovato. Installa con: winget install Gyan.FFmpeg.Shared
        if "%PAUSE_IT%"=="1" pause
        exit /b 1
    )
)

rem --- Scelta Python: preferisce una versione 3.11-3.13 funzionante (x64 su ARM), poi il default ---
set "PY_CMD=python"
where py >NUL 2>&1
if %ERRORLEVEL% EQU 0 (
    for %%V in (3.11 3.12 3.13) do (
        py -%%V -c "import sys" >NUL 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_CMD=py -%%V"
            goto python_ok
        )
    )
    set "PY_CMD=py"
)
:python_ok
echo Python scelto: !PY_CMD!
if "%CHECK_UPDATES%"=="0" (
    !PY_CMD! main.py --no-update-check --no-confirm !MAIN_ARGS!
) else (
    !PY_CMD! main.py --no-confirm !MAIN_ARGS!
)

echo.
echo ========================================
if "%PAUSE_IT%"=="1" pause
endlocal
