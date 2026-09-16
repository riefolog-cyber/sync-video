@echo off
rem Seleziona il Python da usare e lo espone in PY_CMD.
rem
rem 1. Se esiste la cartella .venv del progetto (creata con crea_venv.bat) si
rem    usa QUEL Python: e' l'ambiente dedicato del progetto, e nessun altro
rem    programma installato sul PC puo' cambiarne i pacchetti. Per tornare al
rem    Python di sistema basta impostare SYNC_VIDEO_NO_VENV=1.
rem 2. Altrimenti preferisce 3.11 (richiesta su Windows ARM/Snapdragon dove i
rem    wheel nativi mancano e i pacchetti del progetto sono installati su 3.11
rem    x64 emulato), poi 3.12, 3.13, infine il launcher di default.
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

if "%SYNC_VIDEO_NO_VENV%"=="1" goto senza_venv

rem Le virgolette fanno parte del valore di PY_CMD: il percorso del progetto
rem puo' contenere spazi, e i chiamanti usano !PY_CMD! senza aggiungerne.
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD="%~dp0.venv\Scripts\python.exe""
    exit /b 0
)

:senza_venv
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
