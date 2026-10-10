@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: ambiente dedicato (.venv)
cd /d "%~dp0"

rem Crea la cartella .venv del progetto: un Python separato, con i pacchetti
rem del progetto, che nessun altro programma installato sul PC puo' cambiare.
rem Da quando .venv esiste, _python.bat (e quindi tutti i .bat) la usa da sola:
rem non serve cambiare niente a mano.
rem
rem Uso:  crea_venv.bat [--ricrea] [--no-pause]
rem   --ricrea   cancella la .venv esistente e la ricrea da zero

set "PAUSE_IT=1"
set "RICREA=0"

:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" set "PAUSE_IT=0"& shift & goto parse
if /i "%~1"=="--ricrea" set "RICREA=1"& shift & goto parse
echo Argomento non riconosciuto: %1
set "EXIT=2"
goto fine

:run
echo ========================================
echo    Sync Video: ambiente dedicato (.venv)
echo ========================================
echo.
echo Un Python separato per questo progetto: quello che si installa qui non
echo puo' essere cambiato dagli altri programmi del PC (e viceversa).
echo La prima volta scarica i pacchetti: qualche minuto.
echo I modelli gia' scaricati (cartella .cache e cache di HuggingFace) si
echo riusano: non vengono riscaricati.
echo.

rem --- Python di sistema, NON quello della venv che stiamo creando ---
rem _python.bat prova anche a INSTALLARE Python se non c'e' (winget) e
rem esce con 1 se non e' riuscito. In quel caso ha gia' spiegato cosa fare.
set "SYNC_VIDEO_NO_VENV=1"
call "%~dp0_python.bat"
if errorlevel 1 (
    set "EXIT=9009"
    goto fine
)
!PY_CMD! --version >NUL 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRORE] Python non trovato nel PATH.
    echo Installa Python oppure abilita "Add Python to PATH".
    set "EXIT=9009"
    goto fine
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" if "%RICREA%"=="1" (
    echo Rimozione della .venv esistente...
    rmdir /s /q "%~dp0.venv"
    if exist "%~dp0.venv" (
        echo [ERRORE] Non riesco a rimuovere .venv: chiudi le finestre o gli
        echo editor che la stanno usando, poi riprova.
        set "EXIT=1"
        goto fine
    )
)

if not exist "%VENV_PY%" (
    echo Python di base: !PY_CMD!
    !PY_CMD! -m venv "%~dp0.venv"
    if not exist "%VENV_PY%" (
        echo [ERRORE] Creazione della venv fallita.
        set "EXIT=1"
        goto fine
    )
) else (
    echo La .venv esiste gia': verifico e completo i pacchetti.
    echo Per rifarla da zero: crea_venv.bat --ricrea
)

echo Aggiornamento di pip...
"%VENV_PY%" -m pip install --upgrade pip
if %ERRORLEVEL% NEQ 0 echo [AVVISO] Aggiornamento di pip fallito: proseguo.

echo Installazione dei pacchetti del progetto...
"%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
if %ERRORLEVEL% NEQ 0 (
    echo [ERRORE] Installazione dei pacchetti fallita: vedi il messaggio sopra.
    set "EXIT=1"
    goto fine
)

echo Verifica degli import...
"%VENV_PY%" -c "import fastembed, faster_whisper, moviepy, pymupdf, pytesseract"
if %ERRORLEVEL% NEQ 0 (
    echo [ERRORE] Qualche pacchetto non si importa: lancia aggiornamenti.bat.
    set "EXIT=1"
    goto fine
)

echo.
echo ========================================
echo    Pronto: da ora i .bat usano .venv
echo ========================================
set "EXIT=0"

:fine
echo.
echo Codice di uscita: %EXIT%
echo.
if "%PAUSE_IT%"=="1" pause
exit /b %EXIT%
