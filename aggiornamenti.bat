@echo off
rem ============================================================
rem  Sync Video: aggiornamento PACCHETTI python
rem
rem  Fa solo quello che il nome promette: controlla le versioni su PyPI e
rem  chiede S/N per installare quelle non pinnate.
rem
rem  La manutenzione di 9Router NON e' qui dentro: e' una cosa separata
rem  (aggiornamenti_9router.bat) perche' modifica quali modelli il servizio
rem  espone, e non c'entra con l'aggiornamento delle dipendenze.
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Aggiornamento pacchetti
cd /d "%~dp0"

echo ========================================
echo    Sync Video: Aggiornamento pacchetti
echo    Verifica versioni PyPI e installa
echo ========================================
echo.

rem --- Scelta Python: helper condiviso (preferisce 3.11, vedi _python.bat) ---
call "%~dp0_python.bat"
!PY_CMD! --version >NUL 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERRORE] Python non e' stato trovato nel PATH.
    echo Installa Python oppure abilita "Add Python to PATH".
    set "EXIT=9009"
    goto fine
)
echo Python scelto: !PY_CMD!
!PY_CMD! aggiornamenti.py
set "EXIT=%ERRORLEVEL%"

:fine
echo.
echo ========================================
echo Codice di uscita: %EXIT%
echo ========================================
echo.
echo Per la manutenzione di 9Router esiste "aggiornamenti_9router.bat".
echo.
pause
exit /b %EXIT%