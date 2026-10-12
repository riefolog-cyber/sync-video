@echo off
setlocal enabledelayedexpansion
chcp 65001 > NUL
cd /d "%~dp0"
title Sync Video: Slide -> Audio

set "PAUSE_IT=1"
set "CHECK_UPDATES=0"
rem --- Modello whisper: sovrascrivibile con set WHISPER_MODEL=tiny (default small, piu' preciso) ---
rem --- Velocita' trascrizione (default gia' veloci, misurati): decoding a batch
rem     (WHISPER_BATCH=8) e beam greedy (WHISPER_BEAM=1, ~2.3x piu' veloce con le
rem     ancore 'slide N' entro 0.15s). Per il testo piu' accurato possibile:
rem       set WHISPER_BEAM=5
rem     Per disattivare il decoding a batch:
rem       set WHISPER_BATCH=0
if not defined WHISPER_MODEL set "WHISPER_MODEL=small"
set "MAIN_ARGS=--whisper-model %WHISPER_MODEL% --engine ffmpeg"

rem --- Controllo del video finito: attivo di default (pochi secondi in piu').
rem Estrae un fotogramma a meta' di ogni slide e verifica che il video mostri
rem davvero quella slide: e' l'unico controllo sull'artefatto (la timeline puo'
rem essere coerente e il video sbagliato). Non blocca mai la generazione:
rem avvisa e riporta l'esito nel riepilogo. Disattivabile con:
rem   set VERIFY_VIDEO=0
if not defined VERIFY_VIDEO set "VERIFY_VIDEO=1"
if "%VERIFY_VIDEO%"=="1" set "MAIN_ARGS=!MAIN_ARGS! --verify-video"

rem --- 9Router: OFF per default ---
rem L'LLM non serve a sincronizzare. I confini li danno le frasi "slide N"
rem pronunciate dal conduttore, e sul materiale provato l'escalation all'LLM ha
rem fatto PEGGIO delle embedding: 21 dei 24 minuti dati a una sola slide, e una
rem risposta troncata al 43% dell'audio che ha fatto perdere il confine di una
rem     mzza di secondi. Resta attivabile con --llm 9router per chi vuole provarlo, ma
rem il default e' offline: nessuna dipendenza di rete, nessun 9router da
rem installare.
set "LLM_ARG=--llm off"

:parse
if "%~1"=="" goto run
if /i "%~1"=="--no-pause" set "PAUSE_IT=0"& shift & goto parse
if /i "%~1"=="--check-updates" set "CHECK_UPDATES=1"& shift & goto parse
set "MAIN_ARGS=!MAIN_ARGS! %1"
shift
goto parse

:run
echo ========================================
echo    Sync Video: Slide -^> Audio
echo    Sincronizzazione semantica (offline)
echo ========================================
echo.

echo Avvio pipeline: OCR -^> Trascrizione -^> Sincronizzazione semantica -^> Video
echo Controllo del video finito: VERIFY_VIDEO=!VERIFY_VIDEO! (0 per disattivarlo)
echo Motore LLM: OFF (--llm 9router per attivarlo)
echo ========================================
echo.

echo  Nota: il controllo aggiornamenti si fa con aggiornamenti.bat
echo  (qui disattivato per non rallentare la generazione del video).
echo ========================================
echo.

rem --- PATH FFmpeg: cerca in PATH, poi WinGet, poi winget ---
rem Prima si fermava qui con "[ERRORE] FFmpeg non trovato": su un PC nuovo
rem senza ffmpeg la preparazione automatica si fermava al primo passo, e il
rem messaggio chiedeva di installarlo a mano. Ma il codice Python sa gia'
rem farlo (config.bootstrap usa winget con lo stesso id): e' solo che questa
rem finestra gli impediva di arrivarci. Ora l'installazione la fa anche qui,
rem perche' e' l'unico punto in cui si puo' ricaricare il PATH dopo
rem l'installazione (vedi :ricarica_path_utente): winget scrive il PATH
rem dell'utente nel registro, ma questa sessione cmd.exe ha gia' il vecchio.
rem
rem Uso 'goto' e non 'if errorlevel' dentro i blocchi: dentro un blocco
rem %ERRORLEVEL% viene valutato una volta sola, al momento del parsing.
rem Stesso motivo di _python.bat.
where ffmpeg >NUL 2>&1
if not errorlevel 1 goto ffmpeg_ok

set "FFMPEG_FOUND="
for /f "delims=" %%F in ('where /r "%LOCALAPPDATA%\Microsoft\WinGet\Packages" ffmpeg.exe 2^>NUL') do set "FFMPEG_FOUND=%%F"
if defined FFMPEG_FOUND goto ffmpeg_winget

where winget >NUL 2>&1
if errorlevel 1 goto ffmpeg_fallito

echo.
echo FFmpeg non presente su questo PC: lo installo adesso (una tantum)...
winget install --id Gyan.FFmpeg.Shared -e --scope user --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
call :ricarica_path_utente
set "FFMPEG_FOUND="
for /f "delims=" %%F in ('where /r "%LOCALAPPDATA%\Microsoft\WinGet\Packages" ffmpeg.exe 2^>NUL') do set "FFMPEG_FOUND=%%F"
if defined FFMPEG_FOUND goto ffmpeg_winget
where ffmpeg >NUL 2>&1
if errorlevel 1 goto ffmpeg_fallito
goto ffmpeg_ok

:ffmpeg_winget
set "PATH=%PATH%;!FFMPEG_FOUND:~0,-10!"
echo FFmpeg trovato nel percorso WinGet: !FFMPEG_FOUND!
goto ffmpeg_ok

:ffmpeg_fallito
echo.
echo [ERRORE] FFmpeg non trovato, e l'installazione automatica non e' riuscita.
echo   Installa con: winget install Gyan.FFmpeg.Shared
if "%PAUSE_IT%"=="1" pause
exit /b 1

:ffmpeg_ok

rem --- Scelta Python: helper condiviso (preferisce 3.11, vedi _python.bat) ---
rem Se sul PC non c'e' nessun Python, il helper prova a installarlo con
rem winget; se non riesce esce con 1 e ha gia' spiegato cosa fare.
call "%~dp0_python.bat"
if errorlevel 1 (
    echo.
    if "%PAUSE_IT%"=="1" pause
    exit /b 9009
)
echo Python scelto: !PY_CMD!

rem --- Ordine degli argomenti: !LLM_ARG! PRIMA di !MAIN_ARGS! ---
rem argparse fa vincere l'ultimo flag, quindi gli argomenti dell'utente devono
rem stare DOPO. Con l'ordine inverso un esplicito "--llm off" (o "--flow", o
rem "--whisper-model") veniva sovrascritto in silenzio dal default del
rem launcher: nessun avviso, nessun errore.
if "%CHECK_UPDATES%"=="0" (
    !PY_CMD! src\main.py --no-update-check --no-confirm !LLM_ARG! !MAIN_ARGS!
) else (
    !PY_CMD! src\main.py --no-confirm !LLM_ARG! !MAIN_ARGS!
)

echo.
echo ========================================
if "%PAUSE_IT%"=="1" pause
endlocal

rem ============================================================
rem ricarica_path_utente: rilegge il PATH utente dal registro.
rem Serve solo dopo winget: questa sessione cmd.exe ha gia' in
rem memoria il PATH di quando e' partita, quindi il ffmpeg appena
rem installato non si vede con 'where' finche' non lo rileggo.
rem Stessa tecnica di _python.bat, che ha lo stesso problema
rem dopo l'installazione di Python.
rem
rem 'if defined' + 'goto' invece di espansione dentro il blocco,
rem per lo stesso motivo di sopra.
rem ============================================================
:ricarica_path_utente
set "_PATH_UTENTE="
for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>NUL ^| find /i "Path"') do set "_PATH_UTENTE=%%B"
if not defined _PATH_UTENTE exit /b 0
set "PATH=%PATH%;!_PATH_UTENTE!"
set "_PATH_UTENTE="
exit /b 0