@echo off
rem Seleziona la migliore versione Python disponibile e la espone in PY_CMD.
rem Preferisce 3.11 (richiesta su Windows ARM/Snapdragon dove i wheel nativi
rem mancano e i pacchetti del progetto sono installati su 3.11 x64 emulato),
rem poi 3.12, 3.13, infine il launcher di default.
rem
rem Prima passa cerca una versione che abbia GIA' le dipendenze del progetto
rem (fastembed): se un Python esiste ma e' "nudo" (es. py -3.13 appena
rem installato senza i pacchetti), viene saltato invece di essere scelto e
rem far fallire l'installazione automatica. Se nessuna versione ha i
rem pacchetti (primo avvio), ripiega su qualsiasi Python disponibile: il
rem bootstrap di main.py installera' tutto da solo.
rem
rem Uso:  call "%~dp0_python.bat"   poi   !PY_CMD! script.py
rem (richiede setlocal enabledelayedexpansion nel chiamante).
rem
rem NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem e fanno eseguire i commenti come comandi.

set "PY_CMD=python"
where py >NUL 2>&1
if %ERRORLEVEL% EQU 0 (
    for %%V in (3.11 3.12 3.13) do (
        py -%%V -c "import fastembed" >NUL 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_CMD=py -%%V"
            exit /b 0
        )
    )
    for %%V in (3.11 3.12 3.13) do (
        py -%%V -c "import sys" >NUL 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "PY_CMD=py -%%V"
            exit /b 0
        )
    )
    set "PY_CMD=py"
)
exit /b 0
