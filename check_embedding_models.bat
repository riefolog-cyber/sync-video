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

python check_embedding_models.py

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
