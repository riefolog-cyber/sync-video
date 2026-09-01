@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
cd /d "%~dp0"
title Sync Video: Slide -> Audio

set "PAUSE_IT=1"
set "CHECK_UPDATES=0"
rem --- Modello whisper: sovrascrivibile con set WHISPER_MODEL=small (default tiny) ---
if not defined WHISPER_MODEL set "WHISPER_MODEL=tiny"
set "MAIN_ARGS=--whisper-model %WHISPER_MODEL% --engine ffmpeg"
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

rem --- 9Router: se installato usa --llm auto (posizionamento LLM), altrimenti offline (nessuna pausa) ---
set "LLM_ARG=--llm off"
where 9router >NUL 2>&1
if %ERRORLEVEL% EQU 0 set "LLM_ARG=--llm auto"

rem --- Scelta Python: helper condiviso (preferisce 3.11, vedi _python.bat) ---
call "%~dp0_python.bat"
echo Python scelto: !PY_CMD!
if "%CHECK_UPDATES%"=="0" (
    !PY_CMD! main.py --no-update-check --no-confirm !MAIN_ARGS! !LLM_ARG!
) else (
    !PY_CMD! main.py --no-confirm !MAIN_ARGS! !LLM_ARG!
)

echo.
echo ========================================
if "%PAUSE_IT%"=="1" pause
endlocal
