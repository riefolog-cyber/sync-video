@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
cd /d "%~dp0"
title Sync Video: librerie CUDA per la GPU

echo ========================================
echo    Librerie CUDA per la trascrizione su GPU
echo ========================================
echo.
echo Installa cuBLAS 12 e cuDNN 9 nel Python usato dal programma,
echo aggiunge le loro cartelle al PATH dell'utente e verifica che
echo Windows riesca a caricarle.
echo.
echo Non serve essere amministratore. Il download e' circa 1,3 GB.
echo.

rem --- Avviso (non bloccante): su un PC senza NVIDIA non servirebbero. ---
where nvidia-smi >NUL 2>&1
if errorlevel 1 (
    echo [AVVISO] Non trovo nvidia-smi: forse questo PC non ha una GPU NVIDIA.
    echo Le librerie si possono installare lo stesso, ma non verranno usate.
    echo.
)

rem --- Stesso Python che usa genera_video.bat (venv o py -3.x). ---
call "%~dp0_python.bat"
if errorlevel 1 goto senza_python
echo Python scelto: !PY_CMD!
echo.

rem --- 1. Le due librerie che CTranslate2 carica al primo calcolo. ---
echo [1/4] Installo le librerie NVIDIA...
!PY_CMD! -m pip install --upgrade --no-input nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
if errorlevel 1 goto pip_fallito

rem --- 2. Dove pip le ha messe. ---
echo.
echo [2/4] Cerco le cartelle delle librerie...
set "CUBLAS_BIN="
set "CUDNN_BIN="
set "TMP_CUDA=%TEMP%\sync_video_cuda_%RANDOM%.txt"
!PY_CMD! -c "import nvidia.cublas, pathlib; print(pathlib.Path(nvidia.cublas.__file__).parent / 'bin')" > "%TMP_CUDA%" 2>NUL
set /p CUBLAS_BIN=<"%TMP_CUDA%"
!PY_CMD! -c "import nvidia.cudnn, pathlib; print(pathlib.Path(nvidia.cudnn.__file__).parent / 'bin')" > "%TMP_CUDA%" 2>NUL
set /p CUDNN_BIN=<"%TMP_CUDA%"
del "%TMP_CUDA%" >NUL 2>&1
if not defined CUBLAS_BIN goto cartelle_fallite
if not defined CUDNN_BIN goto cartelle_fallite
echo    !CUBLAS_BIN!
echo    !CUDNN_BIN!

rem --- 3. PATH utente, senza duplicati e senza admin. ---
rem I percorsi passano da variabili d'ambiente (non interpolati nella riga
rem PowerShell): un apostrofo nel nome utente non rompe il comando.
echo.
echo [3/4] Aggiungo le due cartelle al PATH dell'utente...
set "SYNC_CUDA_CUBLAS=!CUBLAS_BIN!"
set "SYNC_CUDA_CUDNN=!CUDNN_BIN!"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$dirs=@($env:SYNC_CUDA_CUBLAS,$env:SYNC_CUDA_CUDNN); $p=[Environment]::GetEnvironmentVariable('Path','User'); if (-not $p) { $p='' }; $parts=@($p -split ';' | Where-Object { $_ -ne '' }); foreach ($d in $dirs) { if ($d -and ($parts -notcontains $d)) { $parts += $d } }; [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')"
if errorlevel 1 goto path_fallito

rem Environment.SetEnvironmentVariable scrive nel registro ma non avvisa
rem Explorer: senza questo broadcast i nuovi processi lanciati dal desktop
rem erediterebbero ancora il PATH vecchio. setx qui tocca solo una variabile
rem inutile, NON il PATH (setx tronca i valori lunghi). La variabile viene
rem rimossa subito dopo: serve solo a far partire la notifica.
setx SYNC_VIDEO_ENV_REFRESH 1 >NUL 2>&1
reg delete "HKCU\Environment" /v SYNC_VIDEO_ENV_REFRESH /f >NUL 2>&1

rem La sessione cmd corrente ha ancora il PATH vecchio: lo estendo solo per
rem la verifica qui sotto.
set "PATH=!CUBLAS_BIN!;!CUDNN_BIN!;!PATH!"

rem --- 4. Verifica con la stessa ricerca di Windows (winmode=0). ---
echo.
echo [4/4] Verifico che Windows carichi le librerie...
!PY_CMD! -c "import ctypes; ctypes.CDLL('cublas64_12.dll', winmode=0); ctypes.CDLL('cudnn64_9.dll', winmode=0); print('librerie CUDA ok')"
if errorlevel 1 goto verifica_fallita

echo.
echo ========================================
echo    Fatto: la GPU e' pronta per la trascrizione
echo ========================================
echo.
echo CHIUDI questa finestra e rilancia genera_video.bat.
echo Se il programma era gia' aperto, chiudilo e riaprilo: solo cosi'
echo Windows vede il PATH aggiornato (se non basta, riavvia il PC).
echo.
pause
exit /b 0

:senza_python
echo.
echo [ERRORE] Non ho trovato un Python adatto: vedi il messaggio qui sopra.
echo Avvia una volta genera_video.bat (prepara il PC) e poi rilancia questo file.
goto fine_errore

:pip_fallito
echo.
echo [ERRORE] L'installazione con pip non e' riuscita.
echo Controlla la connessione e rilancia questo file.
echo Se parla di permessi, esegui il Prompt come amministratore oppure a mano:
echo   !PY_CMD! -m pip install --user --upgrade nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
goto fine_errore

:cartelle_fallite
echo.
echo [ERRORE] Le librerie risultano installate ma non trovo le loro cartelle.
echo Rilancia questo file; se il problema resta, scrivi a chi ti ha passato
echo il programma allegando questa schermata.
goto fine_errore

:path_fallito
echo.
echo [ERRORE] Non sono riuscito ad aggiornare il PATH automaticamente.
echo Aggiungi a mano queste due cartelle al PATH dell'utente:
echo   !CUBLAS_BIN!
echo   !CUDNN_BIN!
goto fine_errore

:verifica_fallita
echo.
echo [ERRORE] Le librerie sono installate ma Windows non le carica.
echo Riapri questa finestra (o riavvia il PC) e rilancia questo file:
echo il PATH appena scritto non e' visibile alla sessione corrente.
goto fine_errore

:fine_errore
echo.
pause
exit /b 1
