#!/usr/bin/env python3
"""
FASE 4 — Assemblaggio video con MoviePy.
Include riconciliazione timeline e supporto per transizioni.
"""

import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from moviepy import (
    AudioFileClip,
    ImageClip,
    concatenate_videoclips,
)
from PIL import Image

from config import (
    DEFAULT_VIDEO_BUFFER_SEC,
    DEFAULT_VIDEO_ENGINE,
    DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_RES,
    DEFAULT_VIDEO_THREADS,
    log,
)


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
            img = Image.open(slide_path).convert("RGB")
            images.append(img)
            fitted.append(_fitted_size(img))
        canvas_w = max(w for w, _ in fitted)
        canvas_h = max(h for _, h in fitted)

        prepared: list[Path] = []
        for i, (img, (w, h)) in enumerate(zip(images, fitted, strict=True)):
            if (w, h) != img.size:
                log.debug("   Resize %s: %dx%d -> %dx%d", Path(str(slide_files[i])).name, *img.size, w, h)
            resized = img.resize((w, h), Image.Resampling.LANCZOS) if (w, h) != img.size else img
            canvas = Image.new("RGB", (canvas_w, canvas_h), (0, 0, 0))
            canvas.paste(resized, ((canvas_w - w) // 2, (canvas_h - h) // 2))
            out_png = workdir / f"seg_{i:04d}.png"
            canvas.save(out_png, format="PNG")
            prepared.append(out_png)
        return prepared
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
) -> None:
    """Assembla il video con il concat demuxer di ffmpeg.

    Un solo processo di encoding: le slide sono PNG statici (letterbox su
    canvas comune) e l'audio è mappato dal file sorgente. Tipicamente molto
    più veloce del percorso MoviePy a parità di output.
    """
    audio = Path(audio_path)
    if not audio.exists():
        raise FileNotFoundError(f"File audio non trovato: {audio}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_duration = sum(durations)
    log.info(
        "   Motore: ffmpeg concat demuxer (%d slide, %.1fs totali, fps=%d).",
        len(slide_files),
        total_duration,
        fps,
    )

    with tempfile.TemporaryDirectory(prefix="s2v_render_") as tmp:
        workdir = Path(tmp)
        pngs = _prepare_slides_for_concat(slide_files, workdir)
        entries = list(zip(pngs, durations, strict=True))
        concat_path = workdir / "concat.txt"
        _write_concat_file(entries, concat_path)

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-progress", "pipe:1",
            "-f", "concat", "-safe", "0", "-i", str(concat_path),
            "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            # NOTA: si usa il filtro fps (non -r): con il concat demuxer
            # l'opzione di output -r produce overshoot di secondi sull'ultima
            # immagine, il filtro fps resta entro un frame di tolleranza.
            "-vf", f"fps={fps}", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-threads", str(threads),
            "-movflags", "+faststart",
            # Cap deterministico sulla durata: il demuxer concat estende
            # l'ultimo segmento con quirk di metadati (durata sovrastimata);
            # conosciamo la durata esatta (somma durate + buffer) e la imponiamo.
            "-t", f"{total_duration:.6f}",
            str(output_path),
        ]
        _run_ffmpeg(cmd, total_duration)


def build_video(
    slide_files: list[str],
    durations: list[float],
    audio_source: AudioFileClip | str | Path,
    output_path: Path,
    fps: int = DEFAULT_VIDEO_FPS,
    threads: int = DEFAULT_VIDEO_THREADS,
    transition_duration: float = 0.0,
    engine: str = DEFAULT_VIDEO_ENGINE,
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
        threads: thread per encoding
        transition_duration: durata dissolvenza in secondi (0 = nessuna);
            > 0 forza il motore moviepy
        engine: 'ffmpeg' (default, veloce) o 'moviepy' (legacy)
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
        _build_video_ffmpeg(slide_files, durations, audio_path, output_path, fps, threads)

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
