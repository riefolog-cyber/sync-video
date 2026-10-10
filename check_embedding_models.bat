@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Check Modello Embedding (regola e5)
cd /d "%~dp0"

echo ========================================
echo    Controllo modello embedding (regola e5)
echo    Verifica aggiornamenti HuggingFace
echo ========================================
echo.

rem --- Scelta Python: helper condiviso (preferisce 3.11, vedi _python.bat) ---
rem Se non c'e' nessun Python il helper prova a installarlo con winget ed
rem esce con 1 se non riesce: ha gia' spiegato cosa fare.
call "%~dp0_python.bat"
if errorlevel 1 (
    echo.
    echo Codice di uscita: 9009
    echo 9009 = nessun Python disponibile
    exit /b 9009
)
echo Python scelto: !PY_CMD!
!PY_CMD! check_embedding_models.py
set "EXIT=%ERRORLEVEL%"
echo.
echo ========================================
echo Codice di uscita: %EXIT%
echo  0 = nessuna azione necessaria
echo  2 = azione consigliata (fai il test A/B)
echo  3 = errore di rete (ripeti il controllo)
echo ========================================
echo.
echo Report: .cache\embedding_model_check_report.md
echo.

exit /b %EXIT%
