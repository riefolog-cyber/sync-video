#!/usr/bin/env python3
"""
FASE 4 — Assemblaggio video con MoviePy.
Include riconciliazione timeline e supporto per transizioni.
"""

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


def build_video(
    slide_files: list[str],
    durations: list[float],
    audio_clip: AudioFileClip,
    output_path: Path,
    fps: int = DEFAULT_VIDEO_FPS,
    threads: int = DEFAULT_VIDEO_THREADS,
    transition_duration: float = 0.0,
) -> None:
    """
    Assembla il video finale:
    - concatena le slide con le durate calcolate
    - aggiunge l'audio
    - opzionalmente applica transizioni (crossfade)

    Args:
        slide_files: percorsi immagini slide
        durations: durata in secondi per ogni slide
        audio_clip: clip audio da associare
        output_path: percorso file video output
        fps: frame per second
        threads: thread per encoding
        transition_duration: durata dissolvenza in secondi (0 = nessuna)
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

    clips = [
        ImageClip(_resize_for_video(slide_path, *DEFAULT_VIDEO_RES)).with_duration(durations[i])
        for i, slide_path in enumerate(slide_files)
    ]

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
        log.info("\n[COMPLETATO] File sincronizzato salvato in: %s", output_path)

    finally:
        # Rilascio risorse MoviePy (NON chiudere audio_clip: è di proprietà del chiamante)
        for c in clips:
            c.close()
        if video_clip is not None:
            video_clip.close()
