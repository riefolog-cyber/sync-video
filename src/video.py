#!/usr/bin/env python3
"""
FASE 4 — Assemblaggio video con MoviePy.
Include riconciliazione timeline e supporto per transizioni.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast

import numpy as np
from moviepy import (
    AudioFileClip,
    ImageClip,
    concatenate_videoclips,
)
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from config import (
    DEFAULT_VIDEO_BUFFER_SEC,
    DEFAULT_VIDEO_ENGINE,
    DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_RES,
    DEFAULT_VIDEO_THREADS,
    log,
)
from hardware import encoder_args
from video_riuso import chiave_video, segna_video, video_riutilizzabile


# =====================================================================
# ASSEMBLAGGIO VIDEO
# =====================================================================
def _resize_for_video(image_path: str, max_width: int, max_height: int) -> np.ndarray:
    """Ridimensiona un'immagine mantenendo le proporzioni, entro i limiti dati.
    Restituisce un array numpy RGB pronto per MoviePy ImageClip."""
    with Image.open(image_path) as img:
        orig_w, orig_h = img.size
        # Thumbnail ridimensiona solo se necessario
        if orig_w > max_width or orig_h > max_height:
            img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            log.debug("   Resize %s: %dx%d -> %dx%d", Path(image_path).name, orig_w, orig_h, *img.size)

        arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    # libx264 richiede dimensioni pari: arrotonda per difetto a numeri pari
    if w % 2 or h % 2:
        arr = arr[: h - h % 2, : w - w % 2]
        log.debug("   Dimensioni rese pari: %dx%d", arr.shape[1], arr.shape[0])
    return arr


# =====================================================================
# MOTORE FFMPEG — concat demuxer (encoding diretto, senza MoviePy)
# =====================================================================
def _fitted_size_from_size(size: tuple[int, int]) -> tuple[int, int]:
    """Dimensione adattata entro DEFAULT_VIDEO_RES (no upscale), pari a libx264."""
    w, h = size
    max_w, max_h = DEFAULT_VIDEO_RES
    if w > max_w or h > max_h:
        scale = min(max_w / w, max_h / h)
        w, h = int(w * scale), int(h * scale)
    return w - w % 2, h - h % 2


def _fitted_size(img: Image.Image) -> tuple[int, int]:
    """Dimensione dell'immagine adattata entro DEFAULT_VIDEO_RES (no upscale),
    arrotondata per difetto a numeri pari (requisito libx264)."""
    return _fitted_size_from_size(img.size)


def _open_slide_retry(slide_path: str, attempts: int = 4, delay: float = 0.75) -> Image.Image:
    """Apre una slide PNG con retry e decodifica forzata."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return Image.open(slide_path).convert("RGB")
        except (UnidentifiedImageError, OSError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    raise RuntimeError(
        f"Lettura slide non riuscita dopo {attempts} prove: {slide_path} ({last})."
    ) from last


def _slide_header_size(slide_path: str, attempts: int = 4, delay: float = 0.75) -> tuple[int, int]:
    """Dimensione della slide leggendo il solo header, senza decodificare.

    ``Image.open`` è lazy: ``.size`` non tocca i pixel. Serve al primo
    passaggio di ``_prepare_slides_for_concat``, che deve solo sapere quanto
    sarà grande il canvas. Ripete i tentativi come ``_open_slide_retry`` perché
    un file appena scritto può essere ancora bloccato su Windows.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            with Image.open(slide_path) as img:
                w, h = img.size
                return int(w), int(h)
        except (UnidentifiedImageError, OSError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    raise RuntimeError(
        f"Lettura slide non riuscita dopo {attempts} prove: {slide_path} ({last})."
    ) from last


def _canvas_size(fitted: Sequence[tuple[int, int]]) -> tuple[int, int]:
    """Canvas del concat: quanto la slide adattata più larga e più alta."""
    return max(w for w, _ in fitted), max(h for _, h in fitted)


def _letterbox(img: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """Centra `img` su un canvas nero di `canvas_w` x `canvas_h`.

    Unica definizione del letterbox dell'encoder, riusata dalla verifica
    frame-vs-slide: se i due lati applicassero trasformi diversi, ogni slide
    non allineata al canvas (4:3 in un deck 16:9, una pagina verticale, una
    slide con tabella larga) verrebbe confrontata contro un'immagine con barre
    nere in una posizione e l'altra senza, e la similarita' crollava sotto
    soglia: il segmento finiva fra i mismatches e, con la riparazione
    automatica attiva, il confine veniva spostato su rumore.
    """
    w, h = img.size
    if w > canvas_w or h > canvas_h:
        scale = min(canvas_w / w, canvas_h / h)
        w, h = int(w * scale), int(h * scale)
    w, h = w - w % 2, h - h % 2
    resized = img.resize((w, h), Image.Resampling.LANCZOS) if (w, h) != img.size else img
    canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
    canvas.paste(resized, ((canvas_w - w) // 2, (canvas_h - h) // 2))
    return canvas


def _prepare_slides_for_concat(slide_files: Sequence[str], workdir: Path) -> list[Path]:
    """Prepara i PNG delle slide per il concat demuxer di ffmpeg.

    Il demuxer richiede che tutti i segmenti abbiano LA STESSA risoluzione:
    ogni slide viene adattata (senza upscale) e poi centrata con letterbox
    nero su un canvas unico, grande quanto la slide adattata più grande.
    Restituisce i percorsi dei PNG pronti, nello stesso ordine degli input.

    Due passate: la prima legge solo gli header per calcolare il canvas, la
    seconda apre, adatta e salva UNA slide alla volta. Tenere tutte le immagini
    decodificate insieme costava ~27 MB l'una a 300 dpi (oltre 4 GB su un deck
    da 150 slide) e quel picco non serviva a niente: il PNG di destinazione è
    già su disco.
    """
    fitted = [_fitted_size_from_size(_slide_header_size(slide_path)) for slide_path in slide_files]
    canvas_w, canvas_h = _canvas_size(fitted)
    workdir.mkdir(parents=True, exist_ok=True)

    prepared: list[Path] = []
    for i, (slide_path, (w, h)) in enumerate(zip(slide_files, fitted, strict=True)):
        img = _open_slide_retry(slide_path)
        try:
            if (w, h) != img.size:
                log.debug("   Resize %s: %dx%d -> %dx%d", Path(str(slide_path)).name, *img.size, w, h)
            out_png = workdir / f"seg_{i:04d}.png"
            _letterbox(img, canvas_w, canvas_h).save(out_png, format="PNG")
            prepared.append(out_png)
        finally:
            img.close()
    return prepared


def _letterbox_references(
    slide_files: Sequence[str], workdir: Path, *, unreadable_out: list[int] | None = None
) -> list[Path]:
    """Ricostruisce le slide come l'encoder le mette nel video.

    Serve al confronto frame-vs-slide: il frame estratto dal video ha il
    letterbox, il PNG originale no, quindi vanno normalizzati allo stesso modo
    prima di misurare la similarita'.

    Una slide non leggibile NON annulla piu' tutta la verifica: prende un
    reference nero (vettore nullo -> similarita' 0.0) e il suo indice (0-based)
    finisce in ``unreadable_out``, cosi' il chiamante verifica le altre e segnala
    quante sono state saltate. Prima bastava un file rotto per dire "checked=0"
    e dichiarare il video controllato senza aver guardato niente.

    Due passate come ``_prepare_slides_for_concat``: la prima legge solo gli
    header per il canvas, la seconda apre/adatta/salva UNA slide alla volta.
    Tenere tutte le immagini decodificate insieme costava ~27 MB l'una a 300
    dpi (oltre 4 GB su un deck da 150 pagine) per dei PNG che finiscono subito
    su disco.
    """
    if unreadable_out is not None:
        unreadable_out.clear()
    # L'header mancante non puo' contribuire al canvas: lo si esclude dal
    # calcolo, altrimenti un valore inventato cambierebbe il letterbox delle
    # slide buone e il confronto con l'encoder (che ha usato il canvas vero)
    # sbaglierebbe.
    fitted: list[tuple[int, int] | None] = []
    for sf in slide_files:
        try:
            fitted.append(_fitted_size_from_size(_slide_header_size(str(sf))))
        except RuntimeError:
            fitted.append(None)
    validi = [f for f in fitted if f is not None]
    canvas_w, canvas_h = _canvas_size(validi) if validi else DEFAULT_VIDEO_RES
    workdir.mkdir(parents=True, exist_ok=True)
    refs: list[Path] = []
    for i, sf in enumerate(slide_files):
        out_png = workdir / f"ref_{i:04d}.png"
        if fitted[i] is None:
            if unreadable_out is not None:
                unreadable_out.append(i)
            Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0)).save(out_png, format="PNG")
            refs.append(out_png)
            continue
        try:
            img = _open_slide_retry(str(sf))
        except RuntimeError:
            if unreadable_out is not None:
                unreadable_out.append(i)
            Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0)).save(out_png, format="PNG")
            refs.append(out_png)
            continue
        try:
            _letterbox(img, canvas_w, canvas_h).save(out_png, format="PNG")
            refs.append(out_png)
        finally:
            img.close()
    return refs


def _concat_quote(path: Path) -> str:
    """Formatta un percorso per il file ffconcat (slash + escape apici)."""
    return "'" + str(path.resolve()).replace("\\", "/").replace("'", "'\\''") + "'"


def _write_concat_file(
    entries: Sequence[tuple[Path, float]], list_path: Path, fps: int = 5
) -> None:
    """Scrive il file ffconcat: una coppia file+duration per segmento.

    Quirk del demuxer: l'ultimo file deve essere ripetuto senza duration,
    altrimenti l'ultimo segmento viene ignorato da alcuni build di ffmpeg.

    Durate nulle o negative non fanno fallire l'encoding: una slide incastrata
    fra due ancore puo' avere durata zero, e il percorso MoviePy la tollera.
    Qui si clampa a un frame, con un avviso: meglio un fotogramma in piu' che
    un video intero non prodotto per un segmento patologico.
    """
    if not entries:
        raise ValueError("Nessun segmento da scrivere nel file ffconcat.")
    minimo = 1.0 / fps if fps > 0 else 0.001
    lines = ["ffconcat version 1.0"]
    for path, duration in entries:
        if duration <= 0:
            log.warning(
                "   [Video] Durata non positiva (%.3fs) per %s: uso un frame (%.3fs).",
                duration,
                path.name,
                minimo,
            )
        lines.append(f"file {_concat_quote(path)}")
        lines.append(f"duration {max(float(duration), minimo):.6f}")
    lines.append(f"file {_concat_quote(entries[-1][0])}")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Senza output per questo tempo ffmpeg è considerato bloccato (driver, device
# su share, encoder accelerato che non parte): il watchdog lo uccide e il
# chiamante ripiega su libx264. Con `-progress pipe:1` le righe arrivano ogni
# ~0.5s, quindi 120s di silenzio sono un blocco vero, non lentezza.
_FFMPEG_STALL_SECONDS = 120.0

# Righe key=value emesse da `-progress pipe:1`: non sono errori, e se finiscono
# nella coda del messaggio finale nascondono l'errore vero.
_FFMPEG_PROGRESS_PREFIXES = (
    "frame=",
    "fps=",
    "bitrate=",
    "total_size=",
    "out_time=",
    "out_time_ms=",
    "out_time_us=",
    "dup_frames=",
    "drop_frames=",
    "speed=",
    "progress=",
)


def _run_ffmpeg(cmd: list[str], total_duration: float) -> None:
    """Esegue ffmpeg, loggando il progresso (`-progress pipe:1`).

    stdout e stderr sono uniti: il progresso arriva come righe key=value,
    gli eventuali errori compaiono in coda. Solleva RuntimeError se exit != 0.

    Un encoder accelerato che si blocca non produce più righe: senza watchdog
    la pipeline resterebbe ferma per sempre davanti all'ultimo "Encoding: 40%".
    Dopo ``_FFMPEG_STALL_SECONDS`` senza output il processo viene ucciso e
    RuntimeError fa scattare il ripiego su libx264.
    """
    log.debug("   Comando: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    output_lines: list[str] = []
    last_pct = -10
    last_output = time.monotonic()
    done = threading.Event()

    def _watchdog() -> None:
        nonlocal last_output
        while not done.wait(5.0):
            if time.monotonic() - last_output > _FFMPEG_STALL_SECONDS:
                log.warning(
                    "   ffmpeg non produce output da %.0fs: lo interrompo.",
                    _FFMPEG_STALL_SECONDS,
                )
                with suppress(OSError):
                    proc.kill()
                return

    watcher = threading.Thread(target=_watchdog, name="ffmpeg-watchdog", daemon=True)
    watcher.start()
    try:
        for line in proc.stdout:
            last_output = time.monotonic()
            line = line.strip()
            if line.startswith("out_time="):
                try:
                    hh, mm, ss = line.split("=", 1)[1].split(":")
                    seconds = int(hh) * 3600 + int(mm) * 60 + float(ss)
                except ValueError:
                    continue
                pct = int(seconds * 100 / total_duration) if total_duration > 0 else 0
                if pct >= last_pct + 10 and 0 <= pct < 100:
                    last_pct = pct
                    log.info("   Encoding: %d%%", pct)
            elif line.startswith("progress=end"):
                log.info("   Encoding: 100%")
            elif line and not line.startswith(_FFMPEG_PROGRESS_PREFIXES) and not line.startswith("stream_"):
                output_lines.append(line)
    finally:
        proc.stdout.close()
        try:
            # Il watchdog va fermato DOPO wait(): fermandolo prima, un'attesa
            # senza tetto poteva pendere per sempre (es. KeyboardInterrupt
            # durante il loop con ffmpeg ancora vivo su un device lento).
            returncode = proc.wait(timeout=_FFMPEG_STALL_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                proc.kill()
            returncode = proc.wait()
        finally:
            done.set()
            watcher.join(timeout=1.0)
    if returncode != 0:
        tail = "\n".join(output_lines[-20:])
        raise RuntimeError(f"ffmpeg è fallito (exit code {returncode}). Ultimo output:\n{tail}")


def _audio_si_puo_copiare(audio_path: Path) -> bool:
    """True se la sorgente e' un contenitore in cui la copia ha senso.

    La decisione e' per ESTENSIONE (non si apre il file): mp3/wav si
    ricodificano in AAC, un MP4/M4A si copia. La copia puo' comunque fallire
    se il codec audio non e' ammesso in MP4: in quel caso si ritenta con AAC
    (vedi ``_tentativi_encoding``).
    """
    return audio_path.suffix.lower() in (".m4a", ".mp4", ".aac", ".m4b")


def _audio_copy_args(audio_path: Path, *, copy: bool | None = None) -> list[str]:
    """Come mettere l'audio nel video: copia o ricodifica.

    Se la sorgente e' gia' un contenitore MP4/AAC, la copia e' bit-identica:
    si evita una ricodifica lossy che peggiorava l'audio e costava tempo
    (misurato: ~10-25s su un podcast di 20 minuti). Con altri formati si
    ricodifica in AAC, come prima.

    ``copy`` esplicito vince sull'estensione: serve ai ritentativi (dopo una
    copia fallita si ricodifica anche se il file e' un MP4).
    """
    copia = _audio_si_puo_copiare(audio_path) if copy is None else copy
    if copia:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "160k"]


def _percorso_backup(output_path: Path) -> Path:
    """Dove sta la copia del video della run precedente."""
    return output_path.with_name(f"{output_path.stem} (precedente){output_path.suffix}")


def _salva_video_precedente(output_path: Path) -> None:
    """Conserva il video gia' presente prima di sovrascriverlo.

    Il riuso evita di rifare un video identico, ma una run che cambia
    qualcosa riscrive il file: se il risultato nuovo e' peggiore (un'ancora
    sbagliata, un deck diverso), quello buono sparirebbe. Una copia accanto
    costa quanto il file e non tocca il flusso.
    """
    if not output_path.exists():
        return
    copia = _percorso_backup(output_path)
    with suppress(OSError):
        shutil.copy2(output_path, copia)
        log.info("   Video precedente conservato in: %s", copia.name)


def _ripristina_video_precedente(output_path: Path) -> None:
    """Riporta il video buono accanto all'output se l'encoding e' fallito.

    L'encoding scrive su un temporaneo, quindi in condizioni normali
    ``output_path`` non e' mai toccato: qui si interviene solo se il file
    finale e' sparito o e' rimasto vuoto (interruzione, antivirus, disco
    pieno), per non rimontare un backup stale di una run molto piu' vecchia.
    """
    backup = _percorso_backup(output_path)
    try:
        if not backup.exists():
            return
        if output_path.exists() and output_path.stat().st_size > 0:
            return
        shutil.copy2(backup, output_path)
    except OSError:
        return
    log.info("   Video precedente ripristinato dopo encoding fallito: %s", backup.name)


def _tentativi_encoding(
    encoder: str, accelerato: bool, copia_audio: bool
) -> list[tuple[str, bool]]:
    """Le combinazioni (encoder, audio copiato) da provare, in ordine.

    Due reti di sicurezza indipendenti: un encoder accelerato puo' esistere
    in ffmpeg ma non funzionare (driver vecchio, device non supportato), e un
    audio copiato puo' non essere ammesso in un MP4.

    L'ordine conta: si cambia PRIMA l'audio e solo dopo l'encoder. Riprovare la
    copia con libx264 quando l'errore e' chiaramente dell'audio (codec non
    ammesso in MP4) costava un encoding intero inutile prima di arrivare ad AAC.
    Con ``accelerato=False`` i tentativi con libx264 sono duplicati e il dedup
    li elimina, lasciando al piu' (encoder, copia) e (encoder, AAC).
    """
    tentativi: list[tuple[str, bool]] = [
        (encoder, copia_audio),
        (encoder, False),
        ("libx264", copia_audio),
        ("libx264", False),
    ]
    unici: list[tuple[str, bool]] = []
    for tentativo in tentativi:
        if tentativo not in unici:
            unici.append(tentativo)
    return unici


def _build_video_ffmpeg(
    slide_files: list[str],
    durations: list[float],
    audio_path: str | Path,
    output_path: Path,
    fps: int,
    threads: int,
    encoder: str = "libx264",
    encoder_out: list[str] | None = None,
) -> None:
    """Assembla il video con il concat demuxer di ffmpeg.

    Un solo processo di encoding: le slide sono PNG statici (letterbox su
    canvas comune) e l'audio è mappato dal file sorgente. Tipicamente molto
    più veloce del percorso MoviePy a parità di output.

    `encoder`: encoder H.264 scelto dal rilevamento hardware
    (``hardware.choose_video_encoder``). Se quello accelerato fallisce a
    runtime (driver, device non supportato, filtro mancante) si ritenta
    con un'altra combinazione: un video lento è sempre meglio di nessun video.

    `encoder_out`: parametro out con l'encoder EFFETTIVO di ogni tentativo;
    l'ultimo valore e' quello che ha prodotto il file. Serve al riepilogo per
    non dire "GPU" quando il ripiego ha usato libx264.

    ffmpeg scrive su un temporaneo UNICO nella STESSA cartella e il file buono
    sostituisce quello vecchio solo a successo: un encoding interrotto non
    lascia piu' un MP4 corrotto col nome finale. Il nome e' unico perche' due
    run sulla stessa cartella non devono rinominare il temporaneo dell'altra,
    e un file lasciato da un hard kill non puo' essere scambiato per fresco.
    """
    audio = Path(audio_path)
    if not audio.exists():
        raise FileNotFoundError(f"File audio non trovato: {audio}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if encoder_out is not None:
        encoder_out.clear()
    total_duration = sum(durations)
    accelerato = encoder != "libx264"
    log.info(
        "   Motore: ffmpeg concat demuxer (%d slide, %.1fs totali, fps=%d, %s).",
        len(slide_files),
        total_duration,
        fps,
        encoder,
    )

    with tempfile.TemporaryDirectory(prefix="s2v_render_") as tmp:
        workdir = Path(tmp)
        pngs = _prepare_slides_for_concat(slide_files, workdir)
        entries = list(zip(pngs, durations, strict=True))
        concat_path = workdir / "concat.txt"
        _write_concat_file(entries, concat_path, fps=fps)
        # Il temporaneo va creato DOPO la preparazione delle slide: se una PNG
        # e' corrotta e `_prepare_slides_for_concat` solleva, non resta un file
        # vuoto nella cartella dell'utente. Nome unico: due run non si pestano.
        fd_tmp, nome_tmp = tempfile.mkstemp(
            prefix=output_path.stem + ".", suffix=".tmp.mp4", dir=output_path.parent
        )
        os.close(fd_tmp)
        tmp_out = Path(nome_tmp)

        def _comando(enc: str, copia_audio: bool) -> list[str]:
            return [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-progress", "pipe:1",
                "-f", "concat", "-safe", "0", "-i", str(concat_path),
                "-i", str(audio),
                "-map", "0:v:0", "-map", "1:a:0",
                *encoder_args(enc),
                # NOTA: si usa il filtro fps (non -r): con il concat demuxer
                # l'opzione di output -r produce overshoot di secondi sull'ultima
                # immagine, il filtro fps resta entro un frame di tolleranza.
                "-vf", f"fps={fps}", "-pix_fmt", "yuv420p",
                *_audio_copy_args(audio, copy=copia_audio),
                # Con un encoder accelerato i thread CPU non servono: il
                # lavoro e' sulla GPU e forzarli aggiunge solo contention.
                *([] if enc != "libx264" else ["-threads", str(threads)]),
                "-movflags", "+faststart",
                # Cap deterministico sulla durata: il demuxer concat estende
                # l'ultimo segmento con quirk di metadati (durata sovrastimata);
                # conosciamo la durata esatta (somma durate + buffer) e la imponiamo.
                "-t", f"{total_duration:.6f}",
                str(tmp_out),
            ]

        copia_audio = _audio_si_puo_copiare(audio)
        errore: RuntimeError | None = None
        for enc, copia in _tentativi_encoding(encoder, accelerato, copia_audio):
            # Ogni tentativo riparte da un temporaneo pulito: un file parziale
            # della combinazione precedente non puo' passare per riuscito.
            with suppress(OSError):
                tmp_out.unlink()
            if encoder_out is not None:
                encoder_out.append(enc)
            try:
                _run_ffmpeg(_comando(enc, copia), total_duration)
                if not tmp_out.exists() or tmp_out.stat().st_size == 0:
                    raise RuntimeError("ffmpeg e' uscito senza scrivere il video.")
                os.replace(tmp_out, output_path)
                return
            except (RuntimeError, OSError) as e:
                # L'ultimo errore e' quello che esce: contiene il log di ffmpeg
                # della combinazione piu' "sicura" provata.
                errore = e if isinstance(e, RuntimeError) else RuntimeError(str(e))
                with suppress(OSError):
                    tmp_out.unlink()
                log.warning(
                    "   ⚠️  Encoding %s (audio %s) fallito: provo la combinazione successiva.",
                    enc,
                    "copiato" if copia else "AAC",
                )
            except BaseException:
                # Ctrl+C/SystemExit: il temporaneo parziale resterebbe accanto
                # al video e verrebbe scambiato per un output finito.
                with suppress(OSError):
                    tmp_out.unlink()
                _ripristina_video_precedente(output_path)
                raise
        with suppress(OSError):
            tmp_out.unlink()
        _ripristina_video_precedente(output_path)
        assert errore is not None
        raise errore


def build_video(
    slide_files: list[str],
    durations: list[float],
    audio_source: AudioFileClip | str | Path,
    output_path: Path,
    fps: int = DEFAULT_VIDEO_FPS,
    threads: int = DEFAULT_VIDEO_THREADS,
    transition_duration: float = 0.0,
    engine: str = DEFAULT_VIDEO_ENGINE,
    encoder: str = "libx264",
    reuse: bool = True,
    encoder_out: list[str] | None = None,
    reused_out: list[bool] | None = None,
) -> None:
    """
    Assembla il video finale:
    - concatena le slide con le durate calcolate
    - aggiunge l'audio
    - opzionalmente applica transizioni (crossfade, solo motore moviepy)

    Args:
        slide_files: percorsi immagini slide
        durations: durata in secondi per ogni slide
        audio_source: percorso del file audio (preferito: il motore ffmpeg lo
            legge direttamente) oppure clip MoviePy AudioFileClip già aperta
        output_path: percorso file video output
        fps: frame per second
        threads: thread per encoding (solo con encoder software: con un encoder
            accelerato il lavoro è sulla GPU e i thread CPU servono a poco)
        transition_duration: durata dissolvenza in secondi (0 = nessuna);
            > 0 forza il motore moviepy
        engine: 'ffmpeg' (default, veloce) o 'moviepy' (legacy)
        encoder: encoder H.264 scelto dal rilevamento hardware; se quello
            accelerato fallisce si ripiega automaticamente su libx264. "auto"
            viene normalizzato a libx264: senza rilevamento (--no-auto-setup)
            non si puo' sapere se la GPU c'e', e trattarlo come GPU ometteva
            -threads e cambiava la chiave di riuso a ogni run.
        reuse: riusa il video gia' renderizzato se slide, tempi, audio e
            parametri sono gli stessi (71s risparmiati a ogni rilancio senza
            modifiche). False per non riusarlo (--no-cache).
        encoder_out: parametro out: riceve l'encoder EFFETTIVO usato (ultimo
            tentativo riuscito), per il riepilogo "GPU/CPU" veritiero.
    """
    log.info("\n4. Generazione rapida del flusso video definitivo...")

    # "auto" e' risolto dal chiamante (main) quando puo' interrogare l'hardware.
    # Qui, da solo, non si puo' provare una GPU a caso: si resta sul software.
    encoder = "libx264" if (encoder or "").strip().lower() == "auto" else encoder
    if encoder_out is not None:
        encoder_out[:] = [encoder]

    # Validazione: slide_files e durations devono avere la stessa lunghezza
    if not slide_files or not durations:
        raise ValueError("Nessuna slide/durata: impossibile assemblare il video.")
    if len(slide_files) != len(durations):
        raise ValueError(
            f"slide_files ({len(slide_files)}) != durations ({len(durations)}). Impossibile assemblare il video."
        )

    # I tempi come l'utente li ha dati, PRIMA dell'estensione per il buffer:
    # l'impronta del video deve descrivere gli input, non quello che ne deriva
    # (l'estensione e' deterministica, quindi non cambia la decisione).
    _durate_input = [float(d) for d in durations]

    # Buffer: estendi l'ultima slide per proteggere l'audio finale.
    # Fatto PRIMA della list comprehension per evitare clip orfani e
    # IndexError di MoviePy su clip già concatenati.
    #
    # Le transizioni (crossfade) accorciano il video di
    # transition_duration * (n_slide - 1) secondi: se non compensati,
    # il video risulta più corto dell'audio troncando gli ultimi secondi
    # di parlato. Aggiungiamo la compensazione all'ultima slide.
    transition_compensation = 0.0
    if transition_duration > 0 and len(slide_files) > 1:
        transition_compensation = transition_duration * (len(slide_files) - 1)
        log.debug(
            "   Compensazione transizioni: +%.1fs (%.1fs x %d slide).",
            transition_compensation,
            transition_duration,
            len(slide_files) - 1,
        )

    total_extra = DEFAULT_VIDEO_BUFFER_SEC + transition_compensation
    durations = list(durations)  # copia difensiva: non modificare la lista originale
    durations[-1] += total_extra
    log.debug(
        "   Ultima slide estesa di +%.1fs (buffer %.1fs + compensazione transizioni %.1fs).",
        total_extra,
        DEFAULT_VIDEO_BUFFER_SEC,
        transition_compensation,
    )

    # --- Riuso del video gia' renderizzato ---
    # Stessi slide, stessi tempi, stesso audio, stessi parametri: il video
    # verrebbe identico byte per byte. Rilanciare la pipeline per guardare
    # qualcosa non doveva costare 71s di encoding.
    if isinstance(audio_source, (str, Path)):
        audio_path: str | Path | None = audio_source
    else:
        # AudioFileClip: usa il file sottostante se disponibile (evita doppie aperture)
        audio_path = getattr(audio_source, "filename", None)
    _engine_norm = (engine or "").strip().lower()
    _chiave = chiave_video(
        list(slide_files),
        _durate_input,
        audio_path,
        fps,
        transition_duration,
        _engine_norm,
        encoder,
        threads,
    )
    if reuse and video_riutilizzabile(output_path, _chiave, encoder_out=encoder_out):
        if reused_out is not None:
            reused_out[:] = [True]
        log.info(
            "   Video riusato: slide, tempi, audio e parametri sono gli stessi "
            "della run precedente (nessuna ricodifica)."
        )
        log.info("\n[COMPLETATO] File sincronizzato salvato in: %s", output_path)
        return

    if reused_out is not None:
        reused_out[:] = [False]
    # Da qui in poi il file viene riscritto: si conserva il precedente.
    _salva_video_precedente(output_path)

    use_moviepy = _engine_norm == "moviepy" or transition_duration > 0 or audio_path is None
    if use_moviepy:
        if _engine_norm == "ffmpeg" and transition_duration > 0:
            log.info(
                "   Transizioni richieste (%.2fs): non supportate dal motore ffmpeg, uso MoviePy.",
                transition_duration,
            )
        elif audio_path is None:
            log.info("   Nessun percorso audio disponibile: uso MoviePy sulla clip esistente.")
        # MoviePy scrive sempre con libx264/aac, a prescindere dall'encoder
        # richiesto: il riepilogo deve dire quello che e' successo davvero.
        if encoder_out is not None:
            encoder_out[:] = ["libx264"]
        _build_video_moviepy(slide_files, durations, audio_source, output_path, fps, threads, transition_duration)
    else:
        assert audio_path is not None  # garantito dal ramo use_moviepy
        _build_video_ffmpeg(
            slide_files, durations, audio_path, output_path, fps, threads,
            encoder=encoder, encoder_out=encoder_out,
        )

    log.info("\n[COMPLETATO] File sincronizzato salvato in: %s", output_path)
    # L'encoder EFFETTIVO e' nell'out-param (puo' essere diverso da quello
    # richiesto se la GPU ha fallito): va salvato per il riuso, altrimenti la
    # run successiva direbbe "GPU" leggendo solo la richiesta.
    # `_build_video_ffmpeg` ACCENDA ogni tentativo: con un ripiego GPU->CPU
    # l'encoder buono e' l'ULTIMO, non il primo (main.py usa [-1] per il
    # riepilogo). Prendere [0] rimetteva "GPU" nella marca di riuso.
    encoder_effettivo = encoder_out[-1] if encoder_out else encoder
    segna_video(output_path, _chiave, encoder=encoder_effettivo)


def _build_video_moviepy(
    slide_files: list[str],
    durations: list[float],
    audio_source: AudioFileClip | str | Path,
    output_path: Path,
    fps: int,
    threads: int,
    transition_duration: float,
) -> None:
    """Assembla il video con MoviePy (percorso legacy).

    Accetta un AudioFileClip già aperto (di proprietà del chiamante, NON
    viene chiuso) oppure un percorso audio (aperto e chiuso qui).
    """
    clips = [
        ImageClip(_resize_for_video(slide_path, *DEFAULT_VIDEO_RES)).with_duration(durations[i])
        for i, slide_path in enumerate(slide_files)
    ]

    if isinstance(audio_source, AudioFileClip):
        # Clip di proprietà del chiamante: NON va chiusa qui
        audio_clip = audio_source
        own_audio = False
    else:
        audio_clip = AudioFileClip(str(audio_source))
        own_audio = True

    # Stesso schema del percorso ffmpeg: si scrive su un temporaneo unico e
    # si sostituisce solo a encoding finito. Prima MoviePy scriveva DIRETTAMENTE
    # sul file finale: un write interrotto (disco pieno, codec, Ctrl+C)
    # lasciava un MP4 troncato al nome canonico, e il backup del video buono
    # non veniva mai ripristinato (solo il ramo ffmpeg lo fa).
    fd_tmp, nome_tmp = tempfile.mkstemp(
        prefix=output_path.stem + ".", suffix=".tmp.mp4", dir=output_path.parent
    )
    os.close(fd_tmp)
    tmp_out = Path(nome_tmp)
    video_clip = None
    try:
        if transition_duration > 0 and len(clips) > 1:
            video_clip = concatenate_videoclips(clips, method="compose", padding=-transition_duration)
            log.info("   Transizioni applicate (%.1fs crossfade).", transition_duration)
        else:
            video_clip = concatenate_videoclips(clips, method="chain")

        video_clip = video_clip.with_audio(audio_clip)

        log.info("   Encoding video... (fps=%d, threads=%d)", fps, threads)
        video_clip.write_videofile(
            str(tmp_out),
            fps=fps,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            threads=threads,
        )
        if not tmp_out.exists() or tmp_out.stat().st_size == 0:
            raise RuntimeError("MoviePy e' uscito senza scrivere il video.")
        os.replace(tmp_out, output_path)
    except BaseException:
        with suppress(OSError):
            tmp_out.unlink()
        _ripristina_video_precedente(output_path)
        raise
    finally:
        # Rilascio risorse MoviePy. L'AudioFileClip è chiuso SOLO se aperto qui.
        for c in clips:
            c.close()
        if video_clip is not None:
            video_clip.close()
        if own_audio:
            audio_clip.close()


# =====================================================================
# VERIFICA FRAME vs SLIDE (post-render)
# =====================================================================
# Lato del ridimensionamento per il confronto frame vs slide: 64x64 in
# grayscale basta a discriminare slide diverse (anche con testo) senza costo
# apprezzabile.
IMAGE_CHECK_SIZE = (64, 64)


def _gray_vector(path: Path) -> np.ndarray:
    """Vettore grayscale ridotto e centrato di un'immagine (per il confronto)."""
    with Image.open(path) as img:
        arr = np.asarray(img.convert("L").resize(IMAGE_CHECK_SIZE), dtype=np.float32)
    flat: np.ndarray = arr.ravel()
    centered: np.ndarray = flat - flat.mean()
    return centered


def image_similarity(a: Path, b: Path) -> float:
    """Similarità coseno tra due immagini (0.0 se una non è leggibile).

    NON CHIAMARE DA `main.py`: qui non c'e' nessuna chiamata di questa
    funzione, e non e' un errore. E' l'implementazione di RIFERIMENTO:
    il percorso di produzione usa `_reference_matrix` + il prodotto
    matriciale (stesso coseno, ma senza riaprire la slide per ogni
    frame: 79s -> 1.4s misurati).

    Resta perche' `test_video.py` confronta il confronto vettoriale con
    questa riga per riga: e' l'unica garanzia che l'ottimizzazione non
    abbia cambiato il risultato. Cancellarla toglierebbe quella garanzia
    senza accorgersene, perche' anche i test continuerebbero a passare
    (con un confronto in meno). Non rimuoverla.
    """
    try:
        va, vb = _gray_vector(a), _gray_vector(b)
    except (UnidentifiedImageError, OSError):
        return 0.0
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(va @ vb / (na * nb))


def _reference_matrix(paths: Sequence[Path]) -> np.ndarray:
    """Matrice (N, 64*64) dei vettori di riferimento GIÀ NORMALIZZATI.

    Il confronto frame-vs-slide è un coseno: ogni reference serve UNA volta
    sola per costruire questa matrice, poi ogni frame si confronta con tutti i
    reference con un solo prodotto matriciale.

    Prima il confronto rifaceva `_gray_vector` (apertura PNG + conversione
    grayscale + resize) per ogni (frame, slide): con 60 segmenti e 60 slide
    erano 3600 aperture di immagine invece di 60, misurate in 79s contro 1.4s.
    Il costo era quadratico proprio nella funzione che certifica l'artefatto.

    Un reference illeggibile o a vettore nullo (immagine uniforme, senza
    struttura) diventa una riga di zeri: il cosene vale 0.0 esattamente come
    prima, quindi un difetto di lettura non viene più mascherato da uno 0.0
    "casuale" né trasformato in un falso abbinamento.
    """
    dim = IMAGE_CHECK_SIZE[0] * IMAGE_CHECK_SIZE[1]
    out = np.zeros((len(paths), dim), dtype=np.float32)
    for i, p in enumerate(paths):
        try:
            v = _gray_vector(p)
        except (UnidentifiedImageError, OSError):
            continue  # riga di zeri -> similarità 0.0
        n = float(np.linalg.norm(v))
        if n == 0.0:
            continue  # riga di zeri -> similarità 0.0
        out[i] = v / n
    return out


def _similarities(frame: Path, ref_matrix: np.ndarray) -> np.ndarray:
    """Coseno di un frame contro tutti i reference già normalizzati."""
    try:
        v = _gray_vector(frame)
    except (UnidentifiedImageError, OSError):
        return np.zeros(ref_matrix.shape[0], dtype=np.float32)
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return np.zeros(ref_matrix.shape[0], dtype=np.float32)
    return cast(np.ndarray, ref_matrix @ (v / n))


def _frame_tmp(out: Path) -> Path:
    """Il temporaneo usato da ffmpeg prima di rinominare il frame buono.

    Il nome finisce pero' con l'estensione originale: ffmpeg sceglie il
    formato d'uscita dall'estensione, e un `.tmp` finale non riconosciuto
    fallirebbe prima ancora di scrivere il fotogramma.
    """
    return out.with_name(out.stem + ".tmp" + out.suffix)


def _pulisci_frame(*paths: Path) -> None:
    """Rimuove frame parziali (e i loro temporanei) in OGNI ramo di errore."""
    for path in paths:
        with suppress(OSError):
            path.unlink()


def _extract_frame(video_path: Path, t: float, out: Path) -> bool:
    """Estrae un frame al tempo ``t``. True se il file è stato scritto.

    Un fallimento (ffmpeg assente, tempo oltre la durata) non interrompe la
    pipeline: il segmento viene semplicemente saltato dalla verifica. Il codice
    di uscita di ffmpeg va controllato: senza, un file parziale scritto prima di
    un errore passava il test "esiste ed è non vuoto" e veniva confrontato come
    se fosse il frame giusto.

    ffmpeg scrive su un temporaneo e solo a successo il frame prende il nome
    finale: timeout e OSError, oltre al returncode != 0, non lasciano PNG
    troncati che ``frame_consistency_check`` accetterebbe come veri.
    """
    tmp = _frame_tmp(out)
    _pulisci_frame(out, tmp)  # niente resti di tentativi/ run precedenti
    try:
        res = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{t:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(tmp),
            ],
            check=False,
            capture_output=True,
            # Timeout: senza, un ffmpeg bloccato (percorso di rete, container
            # corrotto) tiene la pipeline ferma per sempre e in silenzio, senza
            # sapere che sta aspettando li'. Un singolo frame di un video già
            # renderizzato ci mette meno di un secondo: 60s e' un margine
            # enorme, e oltre e' un'anomalia.
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        _pulisci_frame(out, tmp)
        log.warning("   [Verifica] Estrazione frame a %.1fs: timeout dopo 60s.", t)
        return False
    except OSError as e:  # ffmpeg non installato
        _pulisci_frame(out, tmp)
        log.debug("   [Verifica] ffmpeg non eseguibile: %s", e)
        return False
    if res.returncode != 0:
        _pulisci_frame(out, tmp)
        log.debug(
            "   [Verifica] Estrazione frame a %.1fs fallita (ffmpeg %d): %s",
            t,
            res.returncode,
            (res.stderr or b"").decode("utf-8", "replace").strip()[:200],
        )
        return False
    try:
        if not tmp.exists() or tmp.stat().st_size == 0:
            _pulisci_frame(out, tmp)
            return False
        os.replace(tmp, out)
    except OSError:
        _pulisci_frame(out, tmp)
        return False
    return True


def _extract_frames_batch(
    video_path: Path, times: Sequence[float], out_files: Sequence[Path]
) -> bool:
    """Estrae tutti i frame richiesti con UNA sola invocazione di ffmpeg.

    Un ffmpeg per segmento sono decine di processi e decine di aperture del
    container: su un video da 15 minuti con 80 segmenti, secondi. Qui si
    aprono N input dello stesso video, ognuno con la sua seek, e si esce con
    N file: la decodifica e' la stessa, il processo e' uno solo, e i frame
    risultano identici bit per bit (verificato sul video di prova).

    False se qualcosa non torna (frame mancanti, errore di ffmpeg): il
    chiamante ripiega sul percorso uno per uno, che resta la reference. In
    caso di fallimento cancella TUTTE le uscite, anche quelle scritte prima
    dell'errore: un batch parziale non deve convincere il chiamante che i
    frame ci siano (e fargli saltare il ripiego uno per uno).

    Come ``_extract_frame``, ffmpeg scrive su temporanei e rinomina solo a
    successo: timeout e OSError non lasciano PNG troncati.
    """
    if not times or len(times) != len(out_files):
        return False
    tmp_files = [_frame_tmp(out) for out in out_files]
    tutti = [*out_files, *tmp_files]
    for path in tutti:
        with suppress(OSError):
            path.unlink()
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for t in times:
        cmd += ["-ss", f"{float(t):.3f}", "-i", str(video_path)]
    for i, tmp in enumerate(tmp_files):
        cmd += ["-map", f"{i}:v:0", "-frames:v", "1", "-q:v", "2", str(tmp)]
    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            # La decodifica avviene N volte (un input per seek): il margine
            # scala con il numero di segmenti, ma resta un tetto.
            timeout=max(60, 20 * len(times)),
        )
    except (OSError, subprocess.TimeoutExpired):
        _pulisci_frame(*tutti)
        return False
    if res.returncode != 0:
        _pulisci_frame(*tutti)
        log.debug(
            "   [Verifica] Estrazione in blocco fallita (ffmpeg %d): %s",
            res.returncode,
            (res.stderr or b"").decode("utf-8", "replace").strip()[:200],
        )
        return False
    if not all(p.exists() and p.stat().st_size > 0 for p in tmp_files):
        _pulisci_frame(*tutti)
        return False
    for tmp, out in zip(tmp_files, out_files, strict=True):
        try:
            os.replace(tmp, out)
        except OSError:
            _pulisci_frame(*tutti)
            return False
    return True


def frame_consistency_check(
    video_path: str | Path,
    segments: Sequence[tuple[int, float, float]],
    slide_files: Sequence[str],
    frames_dir: Path,
    min_similarity: float = 0.85,
) -> dict[str, object]:
    """Verifica cosa è DAVVERO a schermo: un frame a metà di ogni segmento.

    È l'unico controllo che certifica l'ARTEFATTO: la timeline può essere
    internamente coerente e il video comunque sbagliato (filtri, riallineamenti
    successivi, slide non registrata). Per ogni segmento estrae un frame dal
    video renderizzato e lo confronta con TUTTE le slide attese: se la più
    simile non è quella dichiarata dalla timeline (o la similarità è sotto
    ``min_similarity``), il segmento finisce nei ``mismatches``.

    Args:
        video_path: video renderizzato da verificare.
        segments: sequenza di ``(slide, start, end)`` effettivamente usati.
        slide_files: percorsi delle slide (indice 0 = slide 1).
        frames_dir: dove salvare i frame estratti (per la diagnosi manuale).
            Viene svuotata prima di scrivere e riceve SOLO i fotogrammi dei
            segmenti sbagliati: sono gli unici da guardare (i corretti non
            servono a nessuno). La cartella deve contenere i frame
            dell'artefatto appena verificato, non quelli delle run precedenti
            (in produzione era cresciuta fino a 191 file / 297 MB).
        min_similarity: soglia sotto cui il match non è considerato affidabile.

    Returns:
        ``{"checked": n, "coherent": k, "mismatches": [...],
        "unreadable": [...], "skipped": [...], "incomplete": bool}``.
        ``unreadable`` sono le slide (1-based) di cui non si e' potuto
        ricostruire il reference, ``skipped`` gli indici dei segmenti non
        verificati per quel motivo: con ``incomplete=True`` il controllo
        sull'artefatto non e' completo e --strict-sync deve segnalarlo.
    """
    video = Path(video_path)
    frames_dir.mkdir(parents=True, exist_ok=True)
    # La pulizia sta QUI, dove i frame vengono scritti, e non nel chiamante: la
    # verifica può girare più volte per run (prima e dopo una riparazione) e
    # ogni giro descrive un video diverso. Prima esisteva solo nel ramo di
    # riparazione, quindi una run senza riparazioni non cancellava mai nulla:
    # ~18 MB a esecuzione, accumulati per sempre.
    for stale in frames_dir.glob("seg*.png"):
        with suppress(OSError):
            stale.unlink()
    checked = 0
    coherent = 0
    mismatches: list[dict[str, object]] = []
    skipped: list[int] = []
    # I confronti vanno fatti contro le slide COSI' COME SONO NEL VIDEO, cioe'
    # con il letterbox dell'encoder. Confrontare il frame (che ha le barre
    # nere) con il PNG originale (che non le ha) produceva similarita' negative
    # (-0.55 misurato su una slide 4:3 in un deck 16:9) contro una soglia di
    # 0.85: ogni segmento non allineato al canvas finiva fra i mismatches, e con
    # la riparazione automatica attiva il confine veniva spostato su rumore,
    # peggio che non verificare. Le reference si ricostruiscono in una cartella
    # temporanea: frames_dir deve contenere solo i frame dell'artefatto.
    with tempfile.TemporaryDirectory(prefix="syncvideo_ref_") as refdir, tempfile.TemporaryDirectory(
        prefix="syncvideo_fr_"
    ) as framedir:
        unreadable_idx: list[int] = []
        slide_paths = _letterbox_references(slide_files, Path(refdir), unreadable_out=unreadable_idx)
        unreadable = [i + 1 for i in unreadable_idx]
        if unreadable:
            log.warning(
                "   [Verifica] Slide non leggibili (%s): verifico le altre e salto i loro segmenti.",
                ", ".join(str(s) for s in unreadable),
            )
        # I reference si embeddano UNA volta: da qui in poi ogni frame si
        # confronta con la matrice (prodotto matriciale), senza più riaprire
        # le PNG delle slide a ogni segmento.
        ref_matrix = _reference_matrix(slide_paths)
        # I frame di tutti i segmenti con UNA invocazione di ffmpeg: un ffmpeg
        # per segmento sono decine di processi e decine di aperture del
        # container (su un video da 15 minuti: secondi). Il percorso uno per
        # uno resta come ripiego (ed e' la reference dei test).
        #
        # I frame vivono in una cartella TEMPORANEA: nella cartella dell'utente
        # finisce solo il fotogramma di un segmento SBAGLIATO, che e' l'unico
        # da guardare (33 MB di PNG a ogni verifica risparmiati).
        candidati: list[tuple[int, int, float, Path]] = []
        for i, (slide, start, end) in enumerate(segments):
            if not 1 <= int(slide) <= len(slide_paths):
                # Segmento non verificabile: va contato, altrimenti `checked`
                # puo' sembrare completo e --strict-sync non scatterebbe,
                # proprio mentre un segmento non e' mai stato guardato.
                skipped.append(i)
                log.debug("   [Verifica] Segmento %d: slide %d fuori dal deck, non verificato.", i, int(slide))
                continue
            if int(slide) in unreadable:
                log.debug("   [Verifica] Segmento %d: slide %d non leggibile, saltato.", i, int(slide))
                skipped.append(i)
                continue
            t = (float(start) + float(end)) / 2
            candidati.append(
                (
                    i,
                    int(slide),
                    t,
                    Path(framedir) / f"seg{i:02d}_t{t:07.1f}_slide{int(slide):02d}.png",
                )
            )
        if not _extract_frames_batch(
            video,
            [t for _, _, t, _ in candidati],
            [f for _, _, _, f in candidati],
        ):
            log.debug("   [Verifica] Estrazione in blocco non riuscita: i frame uno per uno.")
            for _, _, t, frame in candidati:
                _extract_frame(video, t, frame)

        for i, slide, t, frame in candidati:
            if not frame.exists() or frame.stat().st_size == 0:
                log.debug("   [Verifica] Frame non estratto a %.1fs (segmento %d).", t, i)
                skipped.append(i)
                continue
            checked += 1
            sims = _similarities(frame, ref_matrix)
            shown = int(np.argmax(sims)) + 1 if sims.size else 0
            best = float(max(sims)) if sims.size else 0.0
            idx_dichiarata = int(slide) - 1
            sim_dichiarata = float(sims[idx_dichiarata]) if 0 <= idx_dichiarata < sims.size else 0.0
            # Due slide pixel-identiche hanno la stessa similarita': np.argmax
            # restituisce sempre l'indice piu' basso, e la slide
            # dichiarata (identica a una precedente) sembrerebbe sbagliata. E'
            # coerente se supera la soglia E eguaglia il migliore (tolleranza
            # 1e-3 per l'arrotondamento float32).
            if sim_dichiarata >= min_similarity and sim_dichiarata >= best - 1e-3:
                coherent += 1
            else:
                mismatches.append(
                    {
                        "slide": int(slide),
                        "shown": shown,
                        "similarity": round(best, 3),
                        "time": round(t, 1),
                    }
                )
                # Il fotogramma del segmento SBAGLIATO si conserva nella
                # cartella dell'utente: e' la prova da guardare. Quelli
                # corretti non servono a nessuno (33 MB di PNG a ogni
                # verifica risparmiati).
                with suppress(OSError):
                    shutil.copy2(frame, frames_dir / frame.name)
    # Una slide non leggibile che NESSUN segmento dichiara non e' un buco
    # nella verifica: il video e' stato controllato per intero, e dire
    # "N segmenti non verificati" con N=0 faceva abortire --strict-sync per
    # niente. `unreadable` resta nel report come diagnostica, ma l'incomplete
    # e' solo cio' che e' stato davvero saltato.
    unreadable_usate = [
        s for s in unreadable if any(int(seg[0]) == s for seg in segments)
    ]
    return {
        "checked": checked,
        "coherent": coherent,
        "mismatches": mismatches,
        "unreadable": unreadable,
        "skipped": skipped,
        "incomplete": bool(unreadable_usate or skipped),
    }


# =====================================================================
# ANTEPRIMA PER CONFINI DA CONTROLLARE
# =====================================================================
def _compose_sheet(
    frames: Sequence[tuple[str, Path]],
    out_path: Path,
    *,
    tile_width: int = 720,
    caption_height: int = 34,
) -> Path | None:
    """Affianca i fotogrammi in un unico PNG, ognuno con la sua didascalia.

    Separata da ``boundary_review_sheet`` perché è la parte che può sbagliare in
    silenzio (didascalie scambiate, un fotogramma su due mancante) e non ha
    bisogno di ffmpeg: si prova con immagini già pronte.

    Returns:
        Il percorso del foglio, oppure None se nessun fotogramma è leggibile:
        un'anteprima mancante non deve mai interrompere la run.
    """
    tiles: list[tuple[str, Image.Image]] = []
    for caption, path in frames:
        try:
            with Image.open(path) as img:
                tile = img.convert("RGB")
                height = max(1, round(tile.height * tile_width / tile.width))
                tiles.append((caption, tile.resize((tile_width, height))))
        except (UnidentifiedImageError, OSError) as e:
            log.debug("   [Anteprima] Fotogramma non leggibile (%s): %s", path, e)
    if not tiles:
        return None
    height = max(tile.height for _, tile in tiles) + caption_height
    sheet = Image.new("RGB", (tile_width * len(tiles), height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=20)
    except (TypeError, ValueError):  # Pillow troppo vecchia per load_default(size=)
        font = ImageFont.load_default()
    for i, (caption, tile) in enumerate(tiles):
        x = i * tile_width
        sheet.paste(tile, (x, 0))
        draw.text((x + 10, tile.height + 8), caption, fill=(0, 0, 0), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def boundary_review_sheet(
    video_path: str | Path,
    moments: Sequence[tuple[str, float]],
    out_path: Path,
    *,
    tile_width: int = 720,
) -> Path | None:
    """Anteprima di un confine dubbio: un fotogramma per momento, affiancati.

    Il riepilogo scrive "nel parlato la slide 10 viene annunciata a 16m57s ma il
    video la mostra da 17m38s": con questo foglio i due istanti sono già a
    schermo, etichettati, e la verifica a mano costa due secondi invece di
    cercare il punto in un video da 13 minuti. Il fotogramma all'annuncio è
    quello che risponde alla domanda (mentre il parlato annunciava la slide, a
    schermo c'era già?), il secondo mostra dove la timeline la fa comparire.

    Un fallimento non interrompe nulla: se il video non c'è (dry-run, output
    spostato) o ffmpeg non risponde, il dubbio resta scritto senza anteprima.
    """
    video = Path(video_path)
    if not moments or not video.exists():
        return None
    with tempfile.TemporaryDirectory(prefix="syncvideo_preview_") as tmp:
        # Tutti i fotogrammi del foglio con UNA invocazione di ffmpeg: due per
        # dubbio, e un dubbio per confine, erano decine di processi.
        tempi = [float(t) for _, t in moments]
        uscite = [Path(tmp) / f"frame_{i}.png" for i in range(len(moments))]
        if not _extract_frames_batch(video, tempi, uscite):
            for i, t in enumerate(tempi):
                _extract_frame(video, t, uscite[i])
        frames: list[tuple[str, Path]] = [
            (caption, uscita)
            for (caption, _), uscita in zip(moments, uscite, strict=True)
            if uscita.exists() and uscita.stat().st_size > 0
        ]
        if not frames:
            log.debug("   [Anteprima] Nessun fotogramma estratto da %s.", video)
            return None
        return _compose_sheet(frames, out_path, tile_width=tile_width)
