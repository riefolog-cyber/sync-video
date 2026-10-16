@echo off
rem ============================================================
rem  Sync Video: finestra grafica (prototipo)
rem
rem  Stesso programma di genera_video.bat, con pulsanti invece
rem  della riga di comando: scegli i file, premi Crea il video,
rem  guarda il diario mentre lavora.
rem
rem  Uso:
rem    gui.bat                 apre la finestra
rem    gui.bat --no-pause      non fermarsi alla chiusura (automazione)
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Finestra
cd /d "%~dp0"

set "PAUSE_IT=1"
:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" (set "PAUSE_IT=0"& shift & goto parse)
goto run

:run
rem --- Scelta Python: helper condiviso (vedi _python.bat) ---
call "%~dp0_python.bat"
if errorlevel 1 (
    echo.
    echo [ERRORE] Serve Python per la finestra. Vedi il messaggio qui sopra.
    if "%PAUSE_IT%"=="1" pause
    exit /b 9009
)
echo Python scelto: !PY_CMD!

rem --- La finestra gira nello stesso interprete scelto qui sopra ---
!PY_CMD! src\gui.py
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" goto uscita_ok
echo.
echo [USCITA ANOMALA] La finestra e' terminata con codice %EXIT_CODE%.
:uscita_ok

echo.
echo ========================================
if "%PAUSE_IT%"=="1" pause
endlocal
