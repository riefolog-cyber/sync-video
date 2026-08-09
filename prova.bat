@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Test (dry-run)

echo ========================================
echo    Sync Video: Test rapido (dry-run)
echo    Timeline senza generare il video
echo ========================================
echo.

python main.py --dry-run --debug %*

echo.
echo ========================================
echo  Niente video generato: solo timeline.
echo  Usa genera_video.bat per il video vero.
echo ========================================
pause
endlocal
