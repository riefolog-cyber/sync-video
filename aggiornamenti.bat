@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Aggiornamento pacchetti
cd /d "%~dp0"

echo ========================================
echo    Sync Video: Aggiornamento pacchetti
echo    Verifica versioni PyPI e installa
echo ========================================
echo.

python aggiornamenti.py

set "EXIT=%ERRORLEVEL%"
echo.
echo ========================================
echo Codice di uscita: %EXIT%
echo ========================================
echo.

pause
endlocal
exit /b %EXIT%