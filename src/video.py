#!/usr/bin/env python3
"""
FASE 4 — Assemblaggio video con MoviePy.
Include riconciliazione timeline e supporto per transizioni.
"""

import subprocess
import tempfile
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
from PIL import Image, UnidentifiedImageError

from config import (
    DEFAULT_VIDEO_BUFFER_SEC,
    DEFAULT_VIDEO_ENGINE,
    DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_RES,
    DEFAULT_VIDEO_THREADS,
    log,
)
from hardware import encoder_args


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
def _fitted_size(img: Image.Image) -> tuple[int, int]:
    """Dimensione dell'immagine adattata entro DEFAULT_VIDEO_RES (no upscale),
    arrotondata per difetto a numeri pari (requisito libx264)."""
    w, h = img.size
    max_w, max_h = DEFAULT_VIDEO_RES
    if w > max_w or h > max_h:
        scale = min(max_w / w, max_h / h)
        w, h = int(w * scale), int(h * scale)
    return w - w % 2, h - h % 2


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
    """
    fitted: list[tuple[int, int]] = []
    images: list[Image.Image] = []
    try:
        for slide_path in slide_files:
            img = _open_slide_retry(slide_path)
            images.append(img)
            fitted.append(_fitted_size(img))
        canvas_w, canvas_h = _canvas_size(fitted)
        workdir.mkdir(parents=True, exist_ok=True)

        prepared: list[Path] = []
        for i, (img, (w, h)) in enumerate(zip(images, fitted, strict=True)):
            if (w, h) != img.size:
                log.debug("   Resize %s: %dx%d -> %dx%d", Path(str(slide_files[i])).name, *img.size, w, h)
            out_png = workdir / f"seg_{i:04d}.png"
            _letterbox(img, canvas_w, canvas_h).save(out_png, format="PNG")
            prepared.append(out_png)
        return prepared
    finally:
        for img in images:
            img.close()


def _letterbox_references(slide_files: Sequence[str], workdir: Path) -> list[Path] | None:
    """Ricostruisce le slide come l'encoder le mette nel video.

    Serve al confronto frame-vs-slide: il frame estratto dal video ha il
    letterbox, il PNG originale no, quindi vanno normalizzati allo stesso modo
    prima di misurare la similarita'. Restituisce None se una slide non e'
    leggibile, cosi' il chiamante puo' saltare la verifica invece di produrre
    un referto inventato.
    """
    try:
        images = [_open_slide_retry(str(sf)) for sf in slide_files]
    except RuntimeError:
        return None
    try:
        fitted = [_fitted_size(img) for img in images]
        canvas_w, canvas_h = _canvas_size(fitted)
        workdir.mkdir(parents=True, exist_ok=True)
        refs: list[Path] = []
        for i, img in enumerate(images):
            out_png = workdir / f"ref_{i:04d}.png"
            _letterbox(img, canvas_w, canvas_h).save(out_png, format="PNG")
            refs.append(out_png)
        return refs
    finally:
        for img in images:
            img.close()


def _concat_quote(path: Path) -> str:
    """Formatta un percorso per il file ffconcat (slash + escape apici)."""
    return "'" + str(path.resolve()).replace("\\", "/").replace("'", "'\\''") + "'"


def _write_concat_file(entries: Sequence[tuple[Path, float]], list_path: Path) -> None:
    """Scrive il file ffconcat: una coppia file+duration per segmento.

    Quirk del demuxer: l'ultimo file deve essere ripetuto senza duration,
    altrimenti l'ultimo segmento viene ignorato da alcuni build di ffmpeg.
    """
    lines = ["ffconcat version 1.0"]
    for path, duration in entries:
        if duration <= 0:
            raise ValueError(f"Durata non positiva ({duration:.3f}s) per {path.name}.")
        lines.append(f"file {_concat_quote(path)}")
        lines.append(f"duration {duration:.6f}")
    lines.append(f"file {_concat_quote(entries[-1][0])}")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_ffmpeg(cmd: list[str], total_duration: float) -> None:
    """Esegue ffmpeg, loggando il progresso (`-progress pipe:1`).

    stdout e stderr sono uniti: il progresso arriva come righe key=value,
    gli eventuali errori compaiono in coda. Solleva RuntimeError se exit != 0.
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
    try:
        for line in proc.stdout:
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
            else:
                output_lines.append(line)
    finally:
        proc.stdout.close()
        returncode = proc.wait()
    if returncode != 0:
        tail = "\n".join(output_lines[-20:])
        raise RuntimeError(f"ffmpeg è fallito (exit code {returncode}). Ultimo output:\n{tail}")


def _build_video_ffmpeg(
    slide_files: list[str],
    durations: list[float],
    audio_path: str | Path,
    output_path: Path,
    fps: int,
    threads: int,
    encoder: str = "libx264",
) -> None:
    """Assembla il video con il concat demuxer di ffmpeg.

    Un solo processo di encoding: le slide sono PNG statici (letterbox su
    canvas comune) e l'audio è mappato dal file sorgente. Tipicamente molto
    più veloce del percorso MoviePy a parità di output.

    `encoder`: encoder H.264 scelto dal rilevamento hardware
    (``hardware.choose_video_encoder``). Se quello accelerato fallisce a
    runtime (driver, device non supportato, filtro mancante) si ritenta
    una volta con libx264: un video lento è sempre meglio di nessun video.
    """
    audio = Path(audio_path)
    if not audio.exists():
        raise FileNotFoundError(f"File audio non trovato: {audio}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
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
        _write_concat_file(entries, concat_path)

        def _comando(enc: str) -> list[str]:
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
                "-c:a", "aac", "-b:a", "160k",
                # Con un encoder accelerato i thread CPU non servono: il
                # lavoro e' sulla GPU e forzarli aggiunge solo contention.
                *([] if enc != "libx264" else ["-threads", str(threads)]),
                "-movflags", "+faststart",
                # Cap deterministico sulla durata: il demuxer concat estende
                # l'ultimo segmento con quirk di metadati (durata sovrastimata);
                # conosciamo la durata esatta (somma durate + buffer) e la imponiamo.
                "-t", f"{total_duration:.6f}",
                str(output_path),
            ]

        try:
            _run_ffmpeg(_comando(encoder), total_duration)
        except RuntimeError:
            if not accelerato:
                raise
            # Rete di sicurezza: un encoder accelerato puo' essere compilato
            # in ffmpeg ma non funzionare (driver vecchio, device non
            # supportato, presenza errata). Il fallback e' sempre software e
            # funziona su ogni macchina.
            log.warning(
                "   ⚠️  Encoding %s fallito: ritento con libx264 (CPU, piu' lento).", encoder
            )
            _run_ffmpeg(_comando("libx264"), total_duration)


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
            accelerato fallisce si ripiega automaticamente su libx264
    """
    log.info("\n4. Generazione rapida del flusso video definitivo...")

    # Validazione: slide_files e durations devono avere la stessa lunghezza
    if not slide_files or not durations:
        raise ValueError("Nessuna slide/durata: impossibile assemblare il video.")
    if len(slide_files) != len(durations):
        raise ValueError(
            f"slide_files ({len(slide_files)}) != durations ({len(durations)}). Impossibile assemblare il video."
        )

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

    # --- Dispatch motore ---
    engine_normalized = (engine or "").strip().lower()
    if isinstance(audio_source, (str, Path)):
        audio_path: str | Path | None = audio_source
    else:
        # AudioFileClip: usa il file sottostante se disponibile (evita doppie aperture)
        audio_path = getattr(audio_source, "filename", None)

    use_moviepy = engine_normalized == "moviepy" or transition_duration > 0 or audio_path is None
    if use_moviepy:
        if engine_normalized == "ffmpeg" and transition_duration > 0:
            log.info(
                "   Transizioni richieste (%.2fs): non supportate dal motore ffmpeg, uso MoviePy.",
                transition_duration,
            )
        elif audio_path is None:
            log.info("   Nessun percorso audio disponibile: uso MoviePy sulla clip esistente.")
        _build_video_moviepy(slide_files, durations, audio_source, output_path, fps, threads, transition_duration)
    else:
        assert audio_path is not None  # garantito dal ramo use_moviepy
        _build_video_ffmpeg(slide_files, durations, audio_path, output_path, fps, threads, encoder=encoder)

    log.info("\n[COMPLETATO] File sincronizzato salvato in: %s", output_path)


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
            str(output_path),
            fps=fps,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            threads=threads,
        )

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
    """Similarità coseno tra due immagini (0.0 se una non è leggibile)."""
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


def _extract_frame(video_path: Path, t: float, out: Path) -> bool:
    """Estrae un frame al tempo ``t``. True se il file è stato scritto.

    Un fallimento (ffmpeg assente, tempo oltre la durata) non interrompe la
    pipeline: il segmento viene semplicemente saltato dalla verifica.
    """
    try:
        subprocess.run(
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
                str(out),
            ],
            check=False,
            # Timeout: senza, un ffmpeg bloccato (percorso di rete, container
            # corrotto) tiene la pipeline ferma per sempre e in silenzio, senza
            # sapere che sta aspettando li'. Un singolo frame di un video già
            # renderizzato ci mette meno di un secondo: 60s e' un margine
            # enorme, e oltre e' un'anomalia.
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log.warning("   [Verifica] Estrazione frame a %.1fs: timeout dopo 60s.", t)
        return False
    except OSError as e:  # ffmpeg non installato
        log.debug("   [Verifica] ffmpeg non eseguibile: %s", e)
        return False
    return out.exists() and out.stat().st_size > 0


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
            Viene svuotata prima di scrivere: la cartella deve contenere solo i
            frame dell'artefatto appena verificato, non quelli delle run
            precedenti (in produzione era cresciuta fino a 191 file / 297 MB).
        min_similarity: soglia sotto cui il match non è considerato affidabile.

    Returns:
        ``{"checked": n, "coherent": k, "mismatches": [...]}``.
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
    # I confronti vanno fatti contro le slide COSI' COME SONO NEL VIDEO, cioe'
    # con il letterbox dell'encoder. Confrontare il frame (che ha le barre
    # nere) con il PNG originale (che non le ha) produceva similarita' negative
    # (-0.55 misurato su una slide 4:3 in un deck 16:9) contro una soglia di
    # 0.85: ogni segmento non allineato al canvas finiva fra i mismatches, e con
    # la riparazione automatica attiva il confine veniva spostato su rumore,
    # peggio che non verificare. Le reference si ricostruiscono in una cartella
    # temporanea: frames_dir deve contenere solo i frame dell'artefatto.
    with tempfile.TemporaryDirectory(prefix="syncvideo_ref_") as refdir:
        slide_paths = _letterbox_references(slide_files, Path(refdir))
        if slide_paths is None:
            log.warning("   [Verifica] Slide non leggibili: confronto frame-vs-slide saltato.")
            return {"checked": 0, "coherent": 0, "mismatches": []}
        # I reference si embeddano UNA volta: da qui in poi ogni frame si
        # confronta con la matrice (prodotto matriciale), senza più riaprire
        # le PNG delle slide a ogni segmento.
        ref_matrix = _reference_matrix(slide_paths)
        for i, (slide, start, end) in enumerate(segments):
            if not 1 <= int(slide) <= len(slide_paths):
                continue
            t = (float(start) + float(end)) / 2
            frame = frames_dir / f"seg{i:02d}_t{t:07.1f}_slide{int(slide):02d}.png"
            if not _extract_frame(video, t, frame):
                log.debug("   [Verifica] Frame non estratto a %.1fs (segmento %d).", t, i)
                continue
            checked += 1
            sims = _similarities(frame, ref_matrix)
            shown = int(np.argmax(sims)) + 1
            best = float(max(sims)) if sims.size else 0.0
            if shown == int(slide) and best >= min_similarity:
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
    return {"checked": checked, "coherent": coherent, "mismatches": mismatches}
