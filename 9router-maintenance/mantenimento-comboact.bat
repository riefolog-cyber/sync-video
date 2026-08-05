@echo off
title 9Router comboact maintenance
echo.
echo === 9Router comboact maintenance ===
echo.
echo Options:
echo   - AddFreeModels : adds free providers (bzl, llm7, kgw, af, perplexity-web, morph, hunyuan)
echo   - AutoReplace   : replaces retired models with suggested replacements
echo   - MaxConsecutiveFails=3 : removes models failing 3+ runs in a row
echo.
echo Running maintenance...
echo.
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-comboact.ps1" -AddFreeModels -AutoReplace
echo.
echo === Maintenance done. Press any key to close ===
pause >nul