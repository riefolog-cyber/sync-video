@echo off
rem ============================================================
rem  Sync Video: verifica che questo computer possa produrre un video
rem
rem  Pensato per chi RICEVE il programma, non per chi lo sviluppa.
rem  controlli.bat controlla il codice (ruff, mypy, test) e a un
rem  collega non dice nulla; questo risponde a una domanda sola:
rem  "su questo computer Sync Video riuscira' a fare un video?",
rem  riga per riga, con cosa fare quando la risposta e' no.
rem
rem  Non installa e non modifica nulla: si puo' lanciare anche su un
rem  PC che non ha ancora ricevuto i file.
rem
rem  Uso:
rem    verifica_pc.bat                 mostra il referto e si ferma
rem    verifica_pc.bat --no-pause      non fermarsi (automazione)
rem  Esito: 0 se il PC puo' produrre un video, 1 se manca qualcosa.
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Verifica del computer
cd /d "%~dp0"

set "PAUSE_IT=1"
:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" (set "PAUSE_IT=0"& shift & goto parse)
goto run

:run
echo ========================================
echo    Sync Video: Verifica del computer
echo    Questo PC e' in grado di fare un video?
echo ========================================

rem --- Scelta Python: helper condiviso (vedi _python.bat) ---
rem Se non c'e' nessun Python il helper prova a installarlo con winget ed
rem esce con 1 se non riesce: ha gia' spiegato cosa fare.
call "%~dp0_python.bat"
if errorlevel 1 (
    echo.
    echo [ERRORE] Serve Python per la verifica. Vedi il messaggio qui sopra.
    if "%PAUSE_IT%"=="1" pause
    exit /b 9009
)
echo Python scelto: !PY_CMD!

rem --- Non usare genera_video.bat: quello avvia l'installazione, qui si ---
rem --- vuole solo guardare. Percio' si chiama il modulo direttamente. -----
!PY_CMD! src\verifica_pc.py
set "EXIT=%ERRORLEVEL%"

echo.
if "%EXIT%"=="0" (
    echo Verifica: questo computer puo' produrre un video.
) else (
    echo Verifica: qualcosa non e' pronto. Leggi le righe marcate KO qui sopra.
)
if "%PAUSE_IT%"=="1" pause
endlocal & exit /b %EXIT%