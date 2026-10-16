@echo off
rem ============================================================
rem  Sync Video: raccoglie la diagnostica da inviare a chi assiste
rem
rem  Sostituisce gli screenshot tagliati: versioni, referto verifica_pc,
rem  numeri dell'ultima run. NON contiene audio, slide, video, trascrizioni
rem  o .env: solo nomi di file e numeri.
rem
rem  Non installa e non modifica il progetto (scrive solo il proprio zip):
rem  si puo' lanciare anche su un PC rotto.
rem
rem  Uso:
rem    diagnostica.bat                 crea diagnostica-<data>.zip qui
rem    diagnostica.bat --no-pause      non fermarsi (automazione)
rem  Esito: 0 se lo zip e' pronto, 1 se non si riesce a scriverlo.
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Diagnostica
cd /d "%~dp0"

set "PAUSE_IT=1"
:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" (set "PAUSE_IT=0"& shift & goto parse)
goto run

:run
echo ========================================
echo    Sync Video: Diagnostica
echo    Prepara il file da inviare a chi ti assiste
echo ========================================

rem --- Scelta Python: helper condiviso (vedi _python.bat) ---
call "%~dp0_python.bat"
if errorlevel 1 (
    echo.
    echo [ERRORE] Serve Python per la diagnostica. Vedi il messaggio qui sopra.
    if "%PAUSE_IT%"=="1" pause
    exit /b 9009
)
echo Python scelto: !PY_CMD!

rem --- Solo raccolta: nessuna installazione, nessuna pipeline ---
!PY_CMD! src\diagnostica.py
set "EXIT=%ERRORLEVEL%"

echo.
if "%EXIT%"=="0" (
    echo Diagnostica pronta: invia lo zip indicato qui sopra.
) else (
    echo Diagnostica: non riesco a scrivere lo zip. Leggi la riga qui sopra.
)
if "%PAUSE_IT%"=="1" pause
endlocal & exit /b %EXIT%
