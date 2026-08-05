@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Slide -> Audio

echo ========================================
echo    Sync Video: Slide -^> Audio
echo    Sincronizzazione semantica (offline)
echo ========================================
echo.

echo Avvio pipeline: OCR -^> Trascrizione -^> Sincronizzazione semantica -^> Video
echo ========================================
echo.

python main.py %*

echo.
echo ========================================
pause
endlocal
