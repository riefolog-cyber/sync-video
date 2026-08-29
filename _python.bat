@echo off
rem Seleziona la migliore versione Python disponibile e la espone in PY_CMD.
rem Preferisce 3.11 (richiesta su Windows ARM/Snapdragon dove i wheel nativi
rem mancano e i pacchetti del progetto sono installati su 3.11 x64 emulato),
rem poi 3.12, 3.13, infine il launcher di default.
rem
rem Uso:  call "%~dp0_python.bat"   poi   !PY_CMD! script.py
rem (richiede setlocal enabledelayedexpansion nel chiamante).

set "PY_CMD=python"
where py >NUL 2>&1
if %ERRORLEVEL% EQU 0 (
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
