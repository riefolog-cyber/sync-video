@echo off
rem ============================================================
rem  Sync Video: aggiornamento PACCHETTI python
rem
rem  Fa solo quello che il nome promette: controlla le versioni su PyPI e
rem  chiede S/N per installare quelle non pinnate.
rem
rem  Uso:
rem    aggiornamenti.bat                        chiede S/N prima di installare
rem    aggiornamenti.bat --no-update            notifica e basta
rem    aggiornamenti.bat --no-update-check      non controlla PyPI
rem    aggiornamenti.bat --frozen-report        perche' i pacchetti non si aggiornano
rem                                             (referto: chi li vincola e fin dove si
rem                                             arriverebbe; non installa nulla)
rem    aggiornamenti.bat --help                 tutte le opzioni (le stampa Python)
rem    aggiornamenti.bat --no-pause             non fermarsi (automazione)
rem  Gli interruttori sono gli stessi di main.py: niente da ricordare due volte.
rem  Esito: 0 se il controllo e' andato a buon fine, altrimenti il codice di Python.
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
rem L'output di aggiornamenti.py contiene emoji e accenti: con la console in
rem cp65001 e' gia' UTF-8, ma quando l'output viene rediretto su file
rem (aggiornamenti.bat > log.txt) l'encoding di default torna cp1252 e lo
rem script moriva con UnicodeEncodeError prima di stampare il referto.
set "PYTHONIOENCODING=utf-8"
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
if /i "%~1"=="--frozen-report" (set "PA_FLAGS=!PA_FLAGS! --frozen-report"& shift & goto parse)
rem L'aiuto lo stampa Python: qui si riconosce solo la forma, cosi'
rem 'aggiornamenti.bat --help' non risponde "opzione sconosciuta" a chi
rem chiede la lista delle opzioni.
if /i "%~1"=="--help" (set "PA_FLAGS=!PA_FLAGS! --help"& shift & goto parse)
if /i "%~1"=="-h" (set "PA_FLAGS=!PA_FLAGS! --help"& shift & goto parse)
if /i "%~1"=="/?" (set "PA_FLAGS=!PA_FLAGS! --help"& shift & goto parse)
echo Opzione sconosciuta: "%~1"
echo Usa --no-pause, --no-update, --no-update-check, --frozen-report o --help.
set "EXIT=2"
goto fine
:parsed

echo ========================================
echo    Sync Video: Aggiornamento pacchetti
echo    Verifica versioni PyPI e installa
echo ========================================
echo.

rem --- Scelta Python: helper condiviso (preferisce .venv, vedi _python.bat) ---
rem Se non c'e' nessun Python il helper prova a installarlo con winget ed
rem esce con 1 se non riesce: ha gia' spiegato cosa fare. La sonda di
rem _python.bat ha GIA' eseguito PY_CMD ("import sys"), quindi qui non serve
rem un secondo avvio di Python solo per --version: era un processo in piu' a
rem ogni lancio per un controllo che il chiamante ha appena fatto.
call "%~dp0_python.bat"
if errorlevel 1 (
    set "EXIT=9009"
    goto fine
)
echo Python scelto: !PY_CMD!

rem --- Lo script deve esserci: una cartella a meta' (ZIP non scompattato
rem tutto, file cancellato) darebbe un traceback Python al posto del motivo.
if exist "%~dp0src\aggiornamenti.py" goto script_ok
echo [ERRORE] Manca src\aggiornamenti.py: la cartella del programma e' incompleta.
echo   Ricarica lo ZIP completo e rilancia.
set "EXIT=1"
goto fine
:script_ok

!PY_CMD! src\aggiornamenti.py!PA_FLAGS!
set "EXIT=%ERRORLEVEL%"

:fine
echo.
if "%EXIT%"=="0" (
    echo Aggiornamenti: controllo completato.
) else (
    echo Aggiornamenti: uscita %EXIT%. Leggi il messaggio qui sopra.
)
if "%PAUSE_IT%"=="1" pause
endlocal & exit /b %EXIT%
