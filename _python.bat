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
rem Se NON esiste alcun Python, prova a installarlo con winget: e' un
rem eseguibile di Windows, quindi funziona anche quando Python non c'e' (e
rem questa e' l'unica cosa che bootstrap() non puo' fare, perche' bootstrap
rem gira dentro Python). Con SYNC_VIDEO_NO_PYTHON_INSTALL=1 non installa
rem nulla e si limita ad avvisare.
rem
rem Uso:  call "%~dp0_python.bat"   poi   !PY_CMD! script.py
rem Esce con 0 se PY_CMD e' valido, con 1 se nessun Python e' disponibile.
rem
rem NON richiede 'setlocal enabledelayedexpansion': usa solo %VAR% e
rem 'if errorlevel', entrambi risolti dal parser senza espansione ritardata.
rem Il file precedente la richiedeva, e senza di essa la ricerca falliva in
rem silenzio PY_CMD vuoto: con l'installazione automatica questo avrebbe
rem fatto partire winget su un PC che aveva gia' Python, solo perche' il
rem chiamante non aveva abilitato l'espansione ritardata.
rem
rem NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem e fanno eseguire i commenti come comandi.

if "%SYNC_VIDEO_NO_VENV%"=="1" goto senza_venv

rem Le virgolette fanno parte del valore di PY_CMD: il percorso del progetto
rem puo' contenere spazi, e i chiamanti usano PY_CMD cosi' com'e'.
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD="%~dp0.venv\Scripts\python.exe""
    exit /b 0
)

:senza_venv
call :cerca_python
if not errorlevel 1 exit /b 0

rem --- Nessun Python su questa macchina ---
if "%SYNC_VIDEO_NO_PYTHON_INSTALL%"=="1" goto nessun_python
where winget >NUL 2>&1
if errorlevel 1 goto nessun_python

echo.
echo ========================================
echo    Python non trovato: lo installo
echo ========================================
echo.
echo Sto scaricando Python 3.11 (circa 25 MB) con winget.
echo Serve una volta sola, solo su questo PC.
echo Per installarlo a mano, o per saltare questo passo, vedi README.
echo.
winget install --id Python.Python.3.11 -e --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
if errorlevel 1 goto install_fallito

rem L'installer aggiunge Python al PATH dell'utente, ma questa sessione
rem cmd.exe eredita gia' il PATH vecchio: va riletto dal registro.
call :ricarica_path
call :cerca_python
if errorlevel 1 goto install_fallito
echo.
echo Python installato e pronto.
exit /b 0

:install_fallito
echo.
echo [ERRORE] Installazione di Python non riuscita (winget ha restituito un errore).

:nessun_python
echo.
echo ========================================
echo    Serve Python per continuare
echo ========================================
echo.
echo Installa Python 3.11 o superiore da https://python.org
echo e spunta "Add Python to PATH" durante l'installazione.
echo Poi rilancia questo programma.
echo.
echo Per decidere tu come installarlo: imposta
echo SYNC_VIDEO_NO_PYTHON_INSTALL=1 prima di rilanciare.
exit /b 1


rem ============================================================
rem cerca_python: imposta PY_CMD se trova un Python usabile
rem ============================================================
:cerca_python
set "PY_CMD="

rem 1) Preferisce un Python che abbia gia' fastembed installato.
call :prova_py 3.11 "import fastembed"
if not errorlevel 1 exit /b 0
call :prova_py 3.12 "import fastembed"
if not errorlevel 1 exit /b 0
call :prova_py 3.13 "import fastembed"
if not errorlevel 1 exit /b 0

rem 2) Poi qualsiasi versione installata (primo avvio: i pacchetti non ci
rem    sono ancora, li mettera' il bootstrap).
call :prova_py 3.11 "import sys"
if not errorlevel 1 exit /b 0
call :prova_py 3.12 "import sys"
if not errorlevel 1 exit /b 0
call :prova_py 3.13 "import sys"
if not errorlevel 1 exit /b 0

rem 3) Il launcher generico.
where py >NUL 2>&1
if errorlevel 1 goto senza_launcher
set "PY_CMD=py"
call :verifica
if not errorlevel 1 exit /b 0

:senza_launcher
rem 4) python nel PATH.
where python >NUL 2>&1
if errorlevel 1 goto percorso_noto
set "PY_CMD=python"
call :verifica
if not errorlevel 1 exit /b 0

:percorso_noto
rem 5) Percorso noto dell'installer. Serve SOPRATTUTTO appena dopo winget:
rem    la sessione cmd corrente ha ancora il PATH di prima dell'installazione,
rem    quindi 'where python' non vede nulla. E' il modo piu' affidabile per
rem    arrivare all'interprete appena installato.
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" (
    set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python314\python.exe""
    call :verifica
    if not errorlevel 1 exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe""
    call :verifica
    if not errorlevel 1 exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    call :verifica
    if not errorlevel 1 exit /b 0
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
    call :verifica
    if not errorlevel 1 exit /b 0
)
exit /b 1


rem prova_py <versione> <istruzione>: sceglie py -<versione> se gira.
:prova_py
py -%1 -c "%~2" >NUL 2>&1
if errorlevel 1 exit /b 1
set "PY_CMD=py -%1"
exit /b 0


rem verifica: PY_CMD deve davvero girare, non solo esistere.
:verifica
%PY_CMD% -c "import sys" >NUL 2>&1
if errorlevel 1 exit /b 1
exit /b 0


rem ============================================================
rem ricarica_path: rilegge il PATH utente e macchina dal registro
rem ============================================================
rem Serve solo dopo l'installazione di Python: il cmd.exe corrente ha
rem gia' memorizzato il PATH di quando e' partito, quindi le variabili
rem nuove dell'installer non sono visibili fino alla sessione successiva.
rem Non si puo' usare setlocal perche' il PATH modificato deve tornare al
rem chiamante.
:ricarica_path
set "_PATH_VECCHIO=%PATH%"
set "_PATH_NUOVO="
call :leggi_path "HKCU\Environment"
call :leggi_path "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
if not "%_PATH_NUOVO%"=="" set "PATH=%_PATH_NUOVO%;%_PATH_VECCHIO%"
set "_PATH_NUOVO="
set "_PATH_VECCHIO="
exit /b 0

:leggi_path
for /f "tokens=2,*" %%A in ('reg query "%~1" /v Path 2^>NUL ^| find /i "Path"') do call :aggiungi_path "%%B"
exit /b 0

:aggiungi_path
set "_PATH_NUOVO=%_PATH_NUOVO%;%~1"
exit /b 0