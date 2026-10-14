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
rem 3. Su macchina ARM64 scarta gli interpreti ARM64 nativi e prende solo
rem    quelli x86-64 emulati (platform.machine == AMD64). I pacchetti del
rem    progetto non hanno wheel win_arm64 (py-rust-stemmers via fastembed per
rem    nessuna versione, pyyaml senza win_arm64 su 3.11): con il Python nativo
rem    pip compila dai sorgenti e muore senza toolchain C++/Rust. Con quello
rem    emulato ci sono le wheel win_amd64 e tutto si installa da solo,
rem    trascrizione su CPU inclusa.
rem
rem Prima passa cerca una versione che abbia GIA' le dipendenze del progetto
rem (fastembed): se un Python esiste ma e' "nudo" (es. appena installato senza
rem i pacchetti), viene saltato invece di essere scelto e far fallire
rem l'installazione automatica. Se nessuna versione ha i pacchetti (primo
rem avvio), ripiega su qualsiasi Python disponibile: il bootstrap di main.py
rem installera' tutto da solo.
rem
rem Se NON esiste alcun Python, prova a installarlo con winget: e' un
rem eseguibile di Windows, quindi funziona anche quando Python non c'e' (e
rem questa e' l'unica cosa che bootstrap() non puo' fare, perche' bootstrap
rem gira dentro Python). Con SYNC_VIDEO_NO_PYTHON_INSTALL=1 non installa
rem nulla e si limita ad avvisare.
rem
rem Uso:  call "%~dp0_python.bat"   poi   %PY_CMD% script.py
rem Esce con 0 se PY_CMD e' valido, con 1 se nessun Python e' disponibile.
rem
rem NON richiede 'setlocal enabledelayedexpansion' nel chiamante: usa solo
rem %VAR%, 'if errorlevel' e '&&', che il parser risolve senza espansione
rem ritardata. La versione precedente la richiedeva, e senza di essa la
rem ricerca falliva in silenzio con PY_CMD vuoto: con l'installazione
rem automatica questo avrebbe fatto partire winget su un PC che aveva gia'
rem Python. E' esattamente cio' che succede a chi digita 'call _python.bat'
rem da un prompt cmd, dove l'espansione ritardata non e' attiva.
rem Per lo stesso motivo la ricerca usa 'goto' e non 'call :subroutine':
rem dentro un blocco, l'errore di una subroutine non sempre torna al
rem chiamante e la selezione risultava sbagliata (sceglieva un Python che
rem non aveva i pacchetti invece del primo che li aveva).
rem
rem NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem e fanno eseguire i commenti come comandi.

if "%SYNC_VIDEO_NO_VENV%"=="1" goto senza_venv

rem Le virgolette fanno parte del valore di PY_CMD: il percorso del progetto
rem puo' contenere spazi, e i chiamanti usano %PY_CMD% cosi' com'e'.
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD="%~dp0.venv\Scripts\python.exe""
    exit /b 0
)

:senza_venv

rem ---------------------------------------------------------------------
rem Quale Python usare/installare dipende dall'ARCHITETTURA di questa
rem macchina, non dalla macchina su cui il progetto e' stato sviluppato:
rem  - ARM64 (Snapdragon e simili): 3.11, la versione su cui il progetto e'
rem    stata collaudata su Windows ARM. Li' faster-whisper e OpenVINO non
rem    sono installabili (CTranslate2 non pubblica wheel win_arm64), quindi
rem    la trascrizione va su CPU, ma gli altri pacchetti funzionano.
rem  - x86-64 (tutti i PC Intel/AMD): 3.12, che e' nella matrice della CI e
rem    non ha ragioni per essere evitata. Fissare il 3.11 perche' "e' quello
rem    che funziona su ARM" obbligherebbe anche i PC x86-64 a una versione
rem    vecchia senza motivo.
rem
rem %PROCESSOR_ARCHITEW6432% e' l'architettura REALE quando questo cmd.exe
rem gira emulato su Windows ARM: %PROCESSOR_ARCHITECTURE% direbbe AMD64,
rem cioe' l'architettura del processo, non della macchina. Senza questo
rem controllo un PC ARM si farebbe passare per x86-64 e riceverebbe il
rem pacchetto sbagliato.
rem
rem _PY_ORDINE e' anche l'ordine in cui vengono cercate le versioni gia'
rem installate, cosi' che su x86-64 venga usato il 3.12 e non un 3.11
rem lasciato sul PC per un motivo qualsiasi.
rem ---------------------------------------------------------------------
set "_ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "_ARCH=%PROCESSOR_ARCHITEW6432%"
set "_PY_VERSION=3.12"
set "_PY_ORDINE=3.12 3.11 3.13"
if /i "%_ARCH%"=="ARM64" (
    set "_PY_VERSION=3.11"
    set "_PY_ORDINE=3.11 3.12 3.13"
)
set "_PY_PKG=Python.Python.%_PY_VERSION%"
rem Versione completa per l'URL python.org (la richiede con micro inclusa):
rem tenere aggiornata insieme a _PY_VERSION qui sopra.
set "_PY_FULL=3.12.10"
if /i "%_ARCH%"=="ARM64" set "_PY_FULL=3.11.9"
rem L'architettura va detta anche a Python: ogni candidato sotto si scarta da
rem solo se e' ARM64 nativo su macchina ARM64 (punto 3 qui sopra). Nome con
rem prefisso per non pestare variabili dell'utente.
set "SYNC_VIDEO_ARCH=%_ARCH%"
rem Su ARM64 anche l'auto-installazione deve chiedere la versione x86-64:
rem il default di winget sarebbe di nuovo quella ARM64.
set "_WINGET_ARCH="
if /i "%_ARCH%"=="ARM64" set "_WINGET_ARCH=--architecture x64"

call :cerca_python
if not errorlevel 1 goto pulisci_e_esci

rem --- Nessun Python adatto su questa macchina ---
if "%SYNC_VIDEO_NO_PYTHON_INSTALL%"=="1" goto nessun_python

echo.
echo ========================================
echo    Python non trovato o non adatto: lo installo
echo ========================================
echo.
echo Architettura rilevata: %_ARCH%
echo Installo Python %_PY_VERSION% (%_PY_PKG%, circa 25 MB): provo winget,
echo se manca o fallisce scarico da python.org. Una volta sola, solo su
echo questo PC. Per farlo a mano, o per saltare questo passo, vedi README.
echo.
winget install --id %_PY_PKG% -e --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity %_WINGET_ARCH%
if errorlevel 1 goto install_diretta

goto dopo_install_python

rem ------------------------------------------------------------
rem install_diretta: winget manca o ha fallito. Si prova il download
rem diretto da python.org, poi l'installer trovato nella cartella
rem (chiavetta USB, PC offline con file portato a mano: niente download).
rem ------------------------------------------------------------
:install_diretta
set "_SETUP_EXE="
if exist "%~dp0python-%_PY_FULL%-amd64.exe" set "_SETUP_EXE=%~dp0python-%_PY_FULL%-amd64.exe"
if not "%_SETUP_EXE%"=="" goto install_esegui
for %%F in ("%~dp0python-*-amd64.exe") do set "_SETUP_EXE=%%F"
if not "%_SETUP_EXE%"=="" (
    echo Trovato installer locale, lo uso senza scaricare nulla.
    goto install_esegui
)
set "_SETUP_URL=https://www.python.org/ftp/python/%_PY_FULL%/python-%_PY_FULL%-amd64.exe"
set "_SETUP_EXE=%TEMP%\sync-video-python-setup.exe"
set "_SETUP_SCARICATO="
echo winget non disponibile o fallito: scarico Python %_PY_VERSION% da python.org...
where curl >NUL 2>&1 && curl -L --fail -o "%_SETUP_EXE%" "%_SETUP_URL%" >NUL 2>&1 && set "_SETUP_SCARICATO=1"
if "%_SETUP_SCARICATO%"=="1" goto install_esegui
where powershell >NUL 2>&1 && powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%_SETUP_URL%' -OutFile '%_SETUP_EXE%'" >NUL 2>&1 && set "_SETUP_SCARICATO=1"
if "%_SETUP_SCARICATO%"=="" goto install_fallito_scaricamento

:install_esegui
echo Installazione di Python %_PY_VERSION% in corso...
rem TargetDir esplicita nella cartella standard per-utente (e' dove guarda
rem anche il passo 5 qui sotto): niente sorprese sui percorsi.
"%_SETUP_EXE%" /quiet InstallAllUsers=0 TargetDir="%LOCALAPPDATA%\Programs\Python\Python%_PY_VERSION:.=%" PrependPath=1 >NUL 2>&1
if errorlevel 1 goto install_fallito
if not "%_SETUP_SCARICATO%"=="" del "%_SETUP_EXE%" >NUL 2>&1
goto dopo_install_python

:install_fallito_scaricamento
del "%_SETUP_EXE%" >NUL 2>&1
goto install_fallito

:dopo_install_python

rem L'installer aggiunge Python al PATH dell'utente, ma questa sessione
rem cmd.exe eredita gia' il PATH vecchio: va riletto dal registro.
call :ricarica_path
call :cerca_python
if errorlevel 1 goto install_fallito
echo.
echo Python %_PY_VERSION% installato e pronto.
goto pulisci_e_esci

:install_fallito
echo.
echo [ERRORE] Installazione di Python non riuscita (ne' winget ne' download diretto).
echo Se il PC non ha rete: da un altro PC scarica
echo   https://www.python.org/ftp/python/%_PY_FULL%/python-%_PY_FULL%-amd64.exe
echo metti il file nella cartella del programma e rilancia: lo usa senza scaricare.
if /i not "%_ARCH%"=="ARM64" goto nessun_python
echo.
echo Su questo PC ARM serve l'installer Windows a 64 bit x86-64, NON quello ARM64:
echo da https://www.python.org/downloads/ scegli "Windows installer (64-bit)".
echo Poi rilancia questo programma.

:nessun_python
echo.
echo ========================================
echo    Serve Python per continuare
echo ========================================
echo.
call :mostra_interprete_trovato
rem Finestra garantita: tenere allineata a hardware.PYTHON_MIN/MAX.
echo Installa Python 3.10 - 3.13 da https://python.org
echo e spunta "Add Python to PATH" durante l'installazione.
echo Poi rilancia questo programma.
if /i "%_ARCH%"=="ARM64" (
    echo.
    echo Su questo PC ARM installa la versione x86-64 a 64 bit,
    echo NON quella ARM64: i pacchetti non hanno versioni per ARM nativo.
)
echo.
echo Per decidere tu come installarlo: imposta
echo SYNC_VIDEO_NO_PYTHON_INSTALL=1 prima di rilanciare.
goto pulisci_e_esci_1

rem Uscita pulita: le variabili di lavoro non restano nel chiamante, che
rem potrebbe usarle per errore come se fossero sue.
:pulisci_e_esci
set "_ARCH="
set "_PY_VERSION="
set "_PY_ORDINE="
set "_PY_PKG="
set "_PY_FULL="
set "_SONDA="
set "SYNC_VIDEO_ARCH="
set "_WINGET_ARCH="
set "_SETUP_URL="
set "_SETUP_EXE="
set "_SETUP_SCARICATO="
exit /b 0

:pulisci_e_esci_1
set "_ARCH="
set "_PY_VERSION="
set "_PY_ORDINE="
set "_PY_PKG="
set "_PY_FULL="
set "_SONDA="
set "SYNC_VIDEO_ARCH="
set "_WINGET_ARCH="
set "_SETUP_URL="
set "_SETUP_EXE="
set "_SETUP_SCARICATO="
exit /b 1


rem ============================================================
rem cerca_python: imposta PY_CMD se trova un Python usabile
rem ============================================================
:cerca_python
set "PY_CMD="

rem La sonda decide se un interprete e' usabile: versione nella finestra
rem garantita E architettura giusta su ARM64. La finestra si legge da
rem hardware.py (unica fonte, stessa di bootstrap e verifica_pc): se src/
rem manca, la sonda fallisce e l'interprete si scarta.
rem Senza punti esclamativi (i chiamanti usano l'espansione ritardata) e
rem senza % (dentro un for andrebbero raddoppiati).
set "_SONDA=import os,platform,sys;sys.path.insert(0,r'%~dp0src');from hardware import PYTHON_MAX as _MAX,PYTHON_MIN as _MIN;sys.exit(0 if _MIN <= sys.version_info[:2] <= _MAX and not (os.environ.get('SYNC_VIDEO_ARCH')=='ARM64' and platform.machine()=='ARM64') else 1)"

rem 1) Preferisce un Python che abbia gia' fastembed installato: risparmia
rem    di ricaricare qualche centinaio di MB solo perche' sul PC c'era
rem    gia' una versione pronta. L'ordine e' quello giusto per questa
rem    architettura (_PY_ORDINE). '&&' e 'goto' invece di 'if errorlevel'
rem    dentro il for: usabili anche senza espansione ritardata, e il goto
rem    esce dal ciclo al primo successo.
for %%V in (%_PY_ORDINE%) do (
    py -%%V -c "import fastembed;%_SONDA%" >NUL 2>&1 && (
        set "PY_CMD=py -%%V"
        goto scelto
    )
)

rem 2) Poi qualsiasi versione installata, nello stesso ordine: al primo
rem    avvio i pacchetti non ci sono ancora, li mettera' il bootstrap.
for %%V in (%_PY_ORDINE%) do (
    py -%%V -c "%_SONDA%" >NUL 2>&1 && (
        set "PY_CMD=py -%%V"
        goto scelto
    )
)

rem 3) Il launcher generico. Anche qui vale il filtro ARM64: un 'py' che
rem    punta al solo Python ARM64 nativo va saltato come sopra.
set "PY_CMD="
where py >NUL 2>&1 && set "PY_CMD=py"
if "%PY_CMD%"=="" goto passo_python_path
%PY_CMD% -c "%_SONDA%" >NUL 2>&1 && goto scelto
set "PY_CMD="
:passo_python_path

rem 4) python nel PATH.
where python >NUL 2>&1 && set "PY_CMD=python"
if "%PY_CMD%"=="" goto passo_percorsi_noti
%PY_CMD% -c "%_SONDA%" >NUL 2>&1 && goto scelto
set "PY_CMD="
:passo_percorsi_noti

rem 5) Percorso noto dell'installer. Serve SOPRATTUTTO appena dopo winget:
rem    la sessione cmd corrente ha ancora il PATH di prima dell'installazione,
rem    quindi 'where python' non vede ancora nulla. E' il modo piu' affidabile
rem    per arrivare all'interprete appena installato. Vince la versione piu'
rem    alta come prima, ma solo se passa la sonda (i 'goto scelto' diretti
rem    dopo ogni riga la scavalcavano). Piatto con goto, senza blocchi.
set "PY_CMD="
if not exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" goto pp313
"%LOCALAPPDATA%\Programs\Python\Python314\python.exe" -c "%_SONDA%" >NUL 2>&1 && set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python314\python.exe"" && goto scelto
:pp313
if not exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" goto pp312
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -c "%_SONDA%" >NUL 2>&1 && set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"" && goto scelto
:pp312
if not exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" goto pp311
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -c "%_SONDA%" >NUL 2>&1 && set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"" && goto scelto
:pp311
if not exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" goto pp310
"%LOCALAPPDATA%\Programs\Python\Python311\python.exe" -c "%_SONDA%" >NUL 2>&1 && set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python311\python.exe"" && goto scelto
:pp310
if not exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" goto nessun_interprete
"%LOCALAPPDATA%\Programs\Python\Python310\python.exe" -c "%_SONDA%" >NUL 2>&1 && set "PY_CMD="%LOCALAPPDATA%\Programs\Python\Python310\python.exe"" && goto scelto
:nessun_interprete
exit /b 1

:scelto
rem Verifica finale: PY_CMD deve davvero girare, non solo esistere.
%PY_CMD% -c "import sys" >NUL 2>&1
if errorlevel 1 (
    set "PY_CMD="
    exit /b 1
)
exit /b 0


rem ============================================================
rem ricarica_path: rilegge il PATH utente e macchina dal registro
rem ============================================================
rem Serve solo dopo l'installazione di Python: il cmd.exe corrente ha gia'
rem memorizzato il PATH di quando e' partito, quindi le variabili nuove
rem dell'installer non sono visibili fino alla sessione successiva.
rem Non si puo' usare setlocal perche' il PATH modificato deve tornare al
rem chiamante.
:ricarica_path
set "_PATH_UTENTE="
set "_PATH_MACCHINA="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>NUL ^| find /i "Path"') do set "_PATH_UTENTE=%%B"
for /f "tokens=2,*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>NUL ^| find /i "Path"') do set "_PATH_MACCHINA=%%B"
if not "%_PATH_UTENTE%%_PATH_MACCHINA%"=="" set "PATH=%_PATH_UTENTE%;%_PATH_MACCHINA%;%PATH%"
set "_PATH_UTENTE="
set "_PATH_MACCHINA="
exit /b 0


rem ============================================================
rem mostra_interprete_trovato: dice quale Python c'e' (se c'e')
rem ============================================================
rem A chi ha gia' il 3.14 "installa Python" senza questa riga suona come una
rem presa in giro. Chiamata fuori dai blocchi: 'call' qui e' sicuro.
rem La versione si chiede al launcher generico, che e' quello che resta
rem quando le versioni giuste non ci sono.
:mostra_interprete_trovato
set "_TROVATO="
where py >NUL 2>&1 || exit /b 0
rem Nel file batch ogni % letterale va raddoppiato (%%d): con %d singolo
rem cmd mangia il carattere e a Python arriva una sintassi rotta (silenziosa
rem per via del 2^>NUL, quindi cattura vuota e riga sparita).
for /f "delims=" %%T in ('py -c "import sys;print('%%d.%%d' %% sys.version_info[:2])" 2^>NUL') do set "_TROVATO=%%T"
if "%_TROVATO%"=="" exit /b 0
echo C'e' gia' Python %_TROVATO%, che non va bene: serve 3.10 - 3.13.
echo.
set "_TROVATO="
exit /b 0