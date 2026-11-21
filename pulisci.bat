@echo off
rem ============================================================
rem  Sync Video: pulizia degli artefatti rigenerabili
rem
rem  Cancella solo cose che il programma ricrea da solo:
rem    - cache: .cache, .mypy_cache, .ruff_cache, __pycache__, *.pyc
rem    - temp_slides (i PNG delle slide estratti durante la run)
rem    - prodotti della run: video_finale.*, riepilogo.md,
rem      transcript_raw.txt, temp_vosk_audio.wav, *.log,
rem      diagnostica-*.zip, .analysis_frames, .analysis_refs
rem
rem  NON tocca, per scelta esplicita:
rem    - .venv     ambiente Python del progetto (si ricrea con crea_venv.bat)
rem    - tessdata  dati OCR di Tesseract (andrebbero riscaricati)
rem    - gli input podcast.m4a, presentazione.pdf e ogni altro audio/PDF
rem    - il codice e la documentazione
rem
rem  La conferma c'e' perche' cancellare i prodotti non costa zero: rifare
rem  il video richiede la trascrizione completa, che e' la fase piu' lenta.
rem  Con --forza si salta la domanda, per l'automazione.
rem
rem  Uso:
rem    pulisci.bat              mostra cosa cancella e chiede conferma
rem    pulisci.bat --forza      cancella senza chiedere
rem    pulisci.bat --no-pause   non fermarsi alla fine (automazione)
rem
rem  NOTA: mantenere questo file in SOLO ASCII. I caratteri accentati
rem  (UTF-8 multibyte) confondono il parser di cmd.exe anche con chcp 65001
rem  e fanno eseguire i commenti come comandi.
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 > NUL
title Sync Video: Pulizia
cd /d "%~dp0"

rem Le opzioni si leggono tutte insieme: nessun goto di parsing, cosi' il
rem file resta lineare anche se un domani se ne aggiunge una.
set "FORZA=0"
set "PAUSE_IT=1"
if not "%~1"=="" for %%A in (%*) do (
    if /i "%%A"=="--forza" set "FORZA=1"
    if /i "%%A"=="--no-pause" set "PAUSE_IT=0"
)

echo ========================================
echo    Sync Video: Pulizia
echo ========================================
echo.
echo Verranno cancellati, se presenti:
echo   cache .cache, .mypy_cache, .ruff_cache, __pycache__, *.pyc
echo   PNG delle slide in temp_slides
echo   video_finale.mp4 / .srt / .capitoli.txt, riepilogo.md,
echo   transcript_raw.txt, temp_vosk_audio.wav, *.log,
echo   diagnostica-*.zip, .analysis_frames, .analysis_refs
echo.
echo NON verranno toccati: .venv, tessdata, gli input.
echo.

if "%FORZA%"=="1" goto cancella
set "RISPOSTA="
set /p "RISPOSTA=Procedi? [S/n] "
if /i "%RISPOSTA%"=="n" goto annulla

:cancella
echo.
echo --- cache ---
rem rd /s /q non chiede conferma: la domanda e' gia' stata fatta sopra.
if exist ".cache" (
    rd /s /q ".cache"
    echo   rimossa .cache
)
if exist ".mypy_cache" (
    rd /s /q ".mypy_cache"
    echo   rimossa .mypy_cache
)
if exist ".ruff_cache" (
    rd /s /q ".ruff_cache"
    echo   rimossa .ruff_cache
)
if exist "src\.mypy_cache" (
    rd /s /q "src\.mypy_cache"
    echo   rimossa src\.mypy_cache
)
rem Il bytecode puo' stare in piu' punti (radice, src, src\scripts): si
rem cercano ovunque sotto src, senza mai entrare in .venv.
if exist "__pycache__" (
    rd /s /q "__pycache__"
    echo   rimossa __pycache__
)
if exist "src" for /d /r "src" %%D in (__pycache__) do @if exist "%%D" (
    rd /s /q "%%D"
    echo   rimossa %%~fD
)
del /s /q "src\*.pyc" >NUL 2>NUL

echo --- slide temporanee ---
if exist "temp_slides" (
    rd /s /q "temp_slides"
    echo   rimossa temp_slides
)

echo --- prodotti della run ---
rem Elenco chiuso, niente wildcard sui nomi: un file dell'utente che per
rem caso si chiama video_finale* e' comunque un prodotto nostro, ma gli
rem input (podcast.m4a, presentazione.pdf) non rientrano mai in questi
rem pattern e restano al loro posto.
for %%F in ("video_finale.mp4" "video_finale.srt" "video_finale.capitoli.txt" "riepilogo.md" "transcript_raw.txt" "temp_vosk_audio.wav") do (
    if exist "%%~F" (
        del /q /f "%%~F"
        echo   rimosso %%~F
    )
)
del /q /f "*.log" 2>NUL
del /q /f "diagnostica-*.zip" 2>NUL
if exist ".analysis_frames" (
    rd /s /q ".analysis_frames"
    echo   rimossa .analysis_frames
)
if exist ".analysis_refs" (
    rd /s /q ".analysis_refs"
    echo   rimossa .analysis_refs
)

echo.
echo Pulizia finita.
echo Non toccati: .venv, tessdata, podcast.m4a, presentazione.pdf.
if "%PAUSE_IT%"=="1" pause
endlocal & exit /b 0

:annulla
echo.
echo Pulizia annullata: niente e' stato cancellato.
if "%PAUSE_IT%"=="1" pause
endlocal & exit /b 0
