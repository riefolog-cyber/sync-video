#!/usr/bin/env python3
"""Formattazione tempi e riepilogo (P2: estrazione da main.py, shim-compatibile).

``main.py`` re-esporta queste funzioni tali e quali: ``from main import X``
continua a funzionare per tutti i test esistenti.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import suppress

from config import (
    CACHE_DIR,
    DEFAULT_EMBED_BATCH,
    DEFAULT_EMBED_THREADS,
    DEFAULT_OCR_WORKERS,
    DEFAULT_VIDEO_THREADS,
    DEFAULT_WHISPER_BATCH,
    DEFAULT_WHISPER_THREADS,
    _physical_cpus,
    log,
)

# Soli encoder accelerati noti: tutto il resto ("" / "auto" / valori
# sconosciuti) NON e' una prova che la GPU sia stata usata. Prima bastava
# `encoder != "libx264"` e con --no-auto-setup restava "auto" -> "GPU" falso.
_ENCODER_GPU = frozenset({"h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"})


def format_time(seconds: float) -> str:
    """Formatta secondi in formato leggibile, con un'unica regola di arrotondamento.

    Prima il ramo sotto il minuto arrotondava (`0.5 -> "0s"` per il banker's
    rounding), quello sopra troncava: due politiche diverse per la stessa
    cifra. Ora la regola e' UNA, il troncamento, che preserva i tempi storici
    del referto (16m57s resta 16m57s).
    """
    interi = int(seconds)
    if interi < 60:
        return f"{interi}s"
    m, s = divmod(interi, 60)
    return f"{m}m{s:02d}s"


def append_timing_history(
    t_ocr: float, t_transcribe: float, t_sync: float, t_embed: float, t_video: float, t_total: float,
) -> None:
    """Persiste lo storico tempi in .cache/timing_history.jsonl (mai bloccante)."""
    import json

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ocr": round(t_ocr, 1),
            "transcribe": round(t_transcribe, 1),
            "sync": round(t_sync, 1),
            "embed": round(t_embed, 1),
            "video": round(t_video, 1),
            "total": round(t_total, 1),
        }
        with (CACHE_DIR / "timing_history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        log.debug("   Impossibile salvare lo storico tempi (ignorato).")


def slide_list_text(slides: Sequence[int], *, di: bool = False) -> str:
    """Elenco slide in italiano: [13] -> 'la slide 13', [6,12] -> 'le slide 6 e 12'."""
    numbers = [str(s) for s in slides]
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"{'della' if di else 'la'} slide {numbers[0]}"
    joined = f"{numbers[0]} e {numbers[1]}" if len(numbers) == 2 else f"{', '.join(numbers[:-1])} e {numbers[-1]}"
    return f"{'delle' if di else 'le'} slide {joined}"


def print_timing(
    t_ocr: float, t_transcribe: float, t_sync: float, t_embed: float, t_model: float,
    t_video: float, t_total: float,
    *,
    history_fn: Callable[..., None] | None = None,
    video_encoder: str | None = None,
) -> None:
    """Riepilogo tempi + peso cache + thread auto-tuning + hardware + storico.

    ``history_fn`` è il callback per lo storico: default = la funzione di
    questo modulo, ma main.py passa la propria (così i test che patchano
    ``main._append_timing_history`` vedono il mock).

    ``video_encoder``: l'encoder EFFETTIVO usato per il video, se noto. Serve a
    rendere visibile che la GPU è stata usata davvero (prima l'encoding era
    sempre su CPU). Con ``auto``/valori sconosciuti non si può affermare nulla:
    il riepilogo dice "non verificato" invece di "GPU".
    """
    log.info("\n" + "─" * 50)
    log.info(" ⏱️  RIEPILOGO TEMPI")
    log.info("─" * 50)
    log.info("   OCR / Slide   │ %s", format_time(t_ocr))
    log.info("   Trascrizione  │ %s", format_time(t_transcribe))
    log.info("   Sincronizzaz. │ %s", format_time(t_sync))
    if t_embed > 0:
        log.info("     └ Embedding │ %s", format_time(t_embed))
    if t_model > 0:
        log.info("     └ Modello   │ %s", format_time(t_model))
    if t_video > 0:
        log.info("   Encoding Video│ %s", format_time(t_video))
    log.info("   ─────────────────────────")
    log.info("   TOTALE         │ %s", format_time(t_total))
    log.info("─" * 50)
    with suppress(Exception):
        from cache_maintenance import cache_disk_usage, format_bytes

        righe = cache_disk_usage(CACHE_DIR)
        if righe:
            totale = sum(peso for _, peso in righe)
            dettaglio = ", ".join(f"{nome.rstrip('/')} {format_bytes(peso)}" for nome, peso in righe[:3])
            log.info("   💾 Cache: %s (%s)", format_bytes(totale), dettaglio)
    with suppress(Exception):
        fisici = _physical_cpus()
        sorgente = f"{fisici} fisici" if fisici else "logici"
        log.info(
            "   🧵 Thread: whisper %d, embedding %d, video %d, ocr %d "
            "(auto da %s; override WHISPER/EMBED/VIDEO_THREADS, OCR_WORKERS)",
            DEFAULT_WHISPER_THREADS,
            DEFAULT_EMBED_THREADS,
            DEFAULT_VIDEO_THREADS,
            DEFAULT_OCR_WORKERS,
            sorgente,
        )
    with suppress(Exception):
        from hardware import memory_tier, ram_total_bytes

        totale_ram = ram_total_bytes()
        righe_hw = [f"RAM {totale_ram / 1024**3:.1f} GB" if totale_ram else "RAM sconosciuta"]
        if totale_ram:
            righe_hw.append(memory_tier(totale_ram))
        righe_hw.append(f"batch whisper {DEFAULT_WHISPER_BATCH}, embedding {DEFAULT_EMBED_BATCH}")
        _enc = (video_encoder or "").strip().lower()
        if _enc in _ENCODER_GPU:
            righe_hw.append(f"video {_enc} (GPU)")
        elif _enc == "libx264":
            righe_hw.append("video libx264 (CPU)")
        else:
            righe_hw.append(f"video {_enc or 'sconosciuto'} (non verificato)")
        log.info("   🖥️  Hardware: %s", ", ".join(righe_hw))
    (history_fn or append_timing_history)(t_ocr, t_transcribe, t_sync, t_embed, t_video, t_total)
