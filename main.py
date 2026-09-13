#!/usr/bin/env python3
"""
Orchestratore principale del pipeline slide-audio.
Gestisce cache/resume, dry-run, e coordina tutte le fasi.
"""

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn, cast

from moviepy import AudioFileClip, VideoFileClip

from chunks import Word
from config import (
    AUTO_BEAM_AB_MARGIN,
    AUTO_BEAM_PINNED_RATIO,
    BASE_DIR,
    CACHE_DIR,
    DEFAULT_VIDEO_BUFFER_SEC,
    DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_THREADS,
    DEFAULT_WHISPER_BEAM_ACCURATE,
    STOPWORDS_ITA,
    atomic_write_text,
    bootstrap,
    log,
    parse_args,
)
from llm_sync import (
    LLM_REVIEW_CACHE_PREFIX,
    endpoints_for,
    is_interactive,
    llm_cache_keys_for,
    llm_ordered_timeline,
    llm_timeline_segments,
    llm_verify_anchor_mapping,
    review_diffs_seen,
)
from machine_setup import machine_setup
from ocr import PRESENTATION_SUFFIXES, convert_presentation_to_pdf, extract_slides_text_ocr
from semantic_sync import (
    SemanticOptions,
    alignment_quality_from_words,
    embed_seconds,
    free_order_segments_from_words,
    last_quality,
    make_anchor_remap_filter,
    merge_short_segments,
    model_load_seconds,
    refine_llm_segments_from_words,
    refine_llm_timeline_from_words,
    repair_segments_from_frames_from_words,
    reset_weak_signal_flag,
    semantic_timeline_from_words,
    set_embed_cache_enabled,
    verify_anchor_mapping_embedding,
    weak_signal_seen,
)
from timeline import (
    detect_flow_from_words,
    extract_slide_anchors,
    extract_slide_one_references,
    reconcile_timeline,
)
from transcription import correct_transcript_names, resolved_transcriber, transcribe_audio
from updates import run_update_check
from video import build_video, frame_consistency_check


# =====================================================================
# UTILITY: Ricerca file audio
# =====================================================================
def find_audio_file(directory: Path) -> Path | None:
    """Cerca qualsiasi file audio nella directory (glob pattern).
    Se più file audio presenti, sceglie il più recente per data di modifica."""
    extensions = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
    candidates = [f for f in directory.iterdir() if f.suffix.lower() in extensions]
    if not candidates:
        return None
    # Ordina per data di modifica (più recente prima)
    return max(candidates, key=lambda f: f.stat().st_mtime)


# =====================================================================
# UTILITY: Parse transcript_raw.txt come fallback per words_raw
# =====================================================================
def _parse_transcript_raw(raw_path: Path) -> list[Word] | None:
    """Legge transcript_raw.txt e ricostruisce la lista di parole Whisper.
    Formato atteso: 'parola [X.Xs]' per riga."""
    if not raw_path.exists():
        return None
    words: list[Word] = []
    pattern = re.compile(r"^(\S+)\s+\[([\d.]+)s\]")
    for line in raw_path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line.strip())
        if m:
            words.append({"word": m.group(1), "start": float(m.group(2))})
    return words if words else None


# =====================================================================
# PULIZIA FILE STALE
# =====================================================================
def _clean_directory(directory: Path, pattern: str = "*.png") -> int:
    """Rimuove tutti i file che corrispondono al pattern nella directory.
    Restituisce il numero di file rimossi."""
    if not directory.exists():
        return 0
    removed = 0
    for f in directory.glob(pattern):
        f.unlink()
        removed += 1
    return removed


# =====================================================================
# ERRORE FATALE: blocca il pipeline con messaggio uniforme
# =====================================================================
def _abort(message: str) -> NoReturn:
    """Logga l'errore in un blocco uniforme ed esce con codice 1."""
    log.error("\n" + "=" * 70)
    log.error(" [ESECUZIONE INTERROTTA] ")
    log.error(" %s", message)
    log.error("=" * 70 + "\n")
    sys.exit(1)


# Chiavi "housekeeping" che NON sono cache di contenuto: vanno conservate
# (updates_check = TTL del controllo PyPI, fastembed_ab = report test A/B,
# sync_report = report di sincronizzazione dell'ultima run, artefatto di
# diagnosi che analysis_sync.py e il debug manuale devono poter leggere).
_KEEP_CACHE_STEMS = frozenset({"machine_setup", "updates_check", "fastembed_ab", "sync_report"})

def _clean_orphan_cache(active_keys: set[str]) -> int:
    """Rimuove i file .json nella cache che non corrispondono ai
    file PDF/audio correnti (chiavi attive). Restituisce il numero rimossi.

    I file ``llm_*.json`` (timeline e review LLM) NON vengono MAI rimossi:
    la loro chiave è un hash del contenuto (slide + audio + chunk), quindi si
    invalidano da soli quando cambia l'input. Cancellarli a fine run farebbe
    ripagare la chiamata LLM a ogni esecuzione.

    Anche ``machine_setup.json`` (scelta del motore rilevata dall'hardware)
    NON viene rimosso: è un file di configurazione, non una cache, e va
    riusato nelle run successive senza rifare il rilevamento. Le chiavi
    housekeeping (``updates_check`` = TTL del check PyPI, ``fastembed_ab`` =
    report del test A/B) vengono conservate per lo stesso motivo: cancellarle
    farebbe ripetere il check di rete (o il test A/B) a ogni run.
    """
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for cache_file in CACHE_DIR.glob("*.json"):
        key = cache_file.stem  # nome file senza .json
        if key.startswith("llm_") or key in _KEEP_CACHE_STEMS:
            continue
        if key not in active_keys:
            cache_file.unlink()
            removed += 1
            log.debug("   🧹 Cache orfana rimossa: %s", cache_file.name)
    return removed


def _clean_stale_llm_cache(keep_stems: set[str]) -> int:
    """Rimuove i file cache LLM (llm_*.json) che la run corrente non riuserà.

    Le chiavi LLM sono hash del contenuto (slide + parlato + ancore +
    endpoint): cambiando podcast o presentazione i vecchi file non servono più.
    Conserva gli stem in ``keep_stems`` (le chiavi della run corrente e la
    timeline finale per la verifica post-run) e TUTTE le cache della revisione
    (``llm_review_*``): anche la revisione è un hash del contenuto, quindi si
    invalida da sola quando l'input cambia, mentre rimuoverla a ogni avvio
    farebbe ripagare la chiamata LLM a ogni run.
    """
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for cache_file in CACHE_DIR.glob("llm_*.json"):
        stem = cache_file.stem
        if stem in keep_stems or stem.startswith(LLM_REVIEW_CACHE_PREFIX):
            continue
        cache_file.unlink()
        removed += 1
        log.debug("   🧹 Cache LLM orfana rimossa: %s", cache_file.name)
    return removed


# =====================================================================
# STATISTICHE TEMPI
# =====================================================================
def _format_time(seconds: float) -> str:
    """Formatta secondi in formato leggibile."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _save_final_timeline(
    timeline: dict[int, float],
    total_slides: int,
    total_duration: float,
) -> None:
    """Persiste la timeline finale validata come ``llm_timeline_finale.json``.

    Gli strumenti di verifica post-run (analysis_sync.py) auto-rilevano la
    timeline più recente dalla cache cercando i file ``llm_*.json``: il flusso
    semantico (MiniLM) non salva cache LLM, quindi senza questo file verrebbe
    riciclata una timeline di una run precedente. Il prefisso ``llm_`` fa sì
    che il file sopravviva alla pulizia delle cache orfane, e viene
    sovrascritto a ogni run con gli start/end effettivamente usati per il video.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, float]] = []
    ordered = sorted(timeline)
    for s in ordered:
        end = (
            timeline[ordered[i + 1]]
            if (i := ordered.index(s)) + 1 < len(ordered)
            else total_duration
        )
        entries.append({"slide": s, "start": round(float(timeline[s]), 3), "end": round(float(end), 3)})
    atomic_write_text(
        CACHE_DIR / "llm_timeline_finale.json",
        json.dumps(entries, ensure_ascii=False),
    )
    log.info("   Timeline finale salvata in cache per la verifica (llm_timeline_finale.json).")


def _print_timing(
    t_ocr: float,
    t_transcribe: float,
    t_sync: float,
    t_embed: float,
    t_model: float,
    t_video: float,
    t_total: float,
) -> None:
    """Stampa il riepilogo dei tempi di ogni fase e lo salva nello storico.

    ``t_embed`` è il calcolo dei vettori (la voce che domina la
    sincronizzazione), ``t_model`` è il caricamento dei pesi: confonderli
    nascondeva il costo vero (la riga "Embedding" mostrava pochi secondi di
    caricamento invece dei ~30s di embedding).
    """
    log.info("\n" + "─" * 50)
    log.info(" ⏱️  RIEPILOGO TEMPI")
    log.info("─" * 50)
    log.info("   OCR / Slide   │ %s", _format_time(t_ocr))
    log.info("   Trascrizione  │ %s", _format_time(t_transcribe))
    log.info("   Sincronizzaz. │ %s", _format_time(t_sync))
    if t_embed > 0:
        log.info("     └ Embedding │ %s", _format_time(t_embed))
    if t_model > 0:
        log.info("     └ Modello   │ %s", _format_time(t_model))
    if t_video > 0:
        log.info("   Encoding Video│ %s", _format_time(t_video))
    log.info("   ─────────────────────────")
    log.info("   TOTALE         │ %s", _format_time(t_total))
    log.info("─" * 50)
    _append_timing_history(t_ocr, t_transcribe, t_sync, t_embed, t_video, t_total)


def _append_timing_history(
    t_ocr: float, t_transcribe: float, t_sync: float, t_embed: float, t_video: float, t_total: float
) -> None:
    """Persiste lo storico dei tempi per fase in ``.cache/timing_history.jsonl``.

    Serve a monitorare regressioni di velocità tra una run e l'altra (una
    riga JSON per run, con data/ora). La mancata scrittura non blocca mai
    la run (solo debug log).
    """
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


def _warn_sync_uncertainty() -> None:
    """Avviso nel riepilogo finale se l'ultima sync semantica aveva segnale debole."""
    if not weak_signal_seen():
        return
    log.warning(
        "\n   [Attenzione] Sincronizzazione a bassa fiducia: il segnale "
        "semantico era debole (slide simili / parlato che non segue "
        "l'ordine), quindi le durate delle slide sono STIMATE e non "
        "garantite 1:1. Se il podcast segue davvero l'ordine della "
        "presentazione il video è corretto; per un allineamento certo "
        "rigenera la presentazione dal podcast o fai pronunciare le "
        "ancore 'slide N' alle transizioni."
    )


def _log_plain_summary(
    durations: Sequence[float],
    slide_ids: Sequence[int],
    total_duration: float,
    *,
    verdicts: dict[int, str] | None = None,
    frame_check: dict[str, object] | None = None,
    quality: dict[str, float] | None = None,
    review_diffs: int = 0,
    repairs: Sequence[dict[str, object]] = (),
    title: str = "IL VIDEO È PRONTO — COSA C'È DENTRO",
) -> None:
    """Riepilogo finale in parole semplici (quello che l'utente vuole sapere).

    Non sostituisce i log tecnici: li traduce in domande concrete — quante
    slide e quanto durano, se il video finito mostra la slide giusta, cosa è
    stato corretto da solo, cosa conviene controllare a mano. I dubbi sono
    raccolti da TUTTI i segnali della run (verdetto di contenuto, verifica
    frame, fiducia del motore embedding, revisione LLM) invece che da uno solo.
    """
    verdicts = verdicts or {}
    log.info("\n" + "=" * 70)
    log.info(" %s", title)
    log.info("=" * 70)
    log.info(
        "   %d slide, %s di parlato (%.1fs).",
        len(durations),
        _format_time(total_duration),
        total_duration,
    )
    log.info("")
    start = 0.0
    for s, d in zip(slide_ids, durations, strict=True):
        log.info(
            "   slide %2d   da %7s   a %7s   (%s)",
            s,
            _format_time(start),
            _format_time(start + float(d)),
            _format_time(float(d)),
        )
        start += float(d)

    # --- Verifica del video finito (l'unico controllo sull'artefatto) ---
    log.info("")
    if frame_check is None:
        log.info(
            "   Controllo del video finito: non eseguito (si attiva con "
            "--verify-video, o da solo con --strict-sync)."
        )
    else:
        checked = int(cast("int", frame_check.get("checked") or 0))
        coherent = int(cast("int", frame_check.get("coherent") or 0))
        if checked and coherent == checked:
            log.info(
                "   Controllo del video finito: OK, tutte le %d slide sono "
                "comparse quando previsto.",
                checked,
            )
        else:
            log.warning(
                "   Controllo del video finito: PROBLEMA, %d slide su %d non "
                "compaiono quando previsto.",
                checked - coherent,
                checked,
            )

    # --- Correzioni automatiche (riparazione dei confini) ---
    for r in repairs:
        log.info(
            "   Correzione automatica: la slide %s entrava a %s, ora entra a %s "
            "(il video la mostrava nel momento sbagliato).",
            r.get("slide"),
            _format_time(float(cast("float", r.get("old_start")) or 0.0)),
            _format_time(float(cast("float", r.get("new_start")) or 0.0)),
        )

    # --- Dubbi da verificare a mano ---
    doubts: list[str] = []
    misaligned = sorted(s for s, v in verdicts.items() if v == "disallineata")
    uncertain = sorted(s for s, v in verdicts.items() if v == "incerto")
    if misaligned:
        slides_txt = ", ".join(str(s) for s in misaligned)
        doubts.append(
            f"il parlato delle slide {slides_txt} somiglia di più a un'altra "
            "slide: guarda dove iniziano nel video"
        )
    if uncertain:
        slides_txt = ", ".join(str(s) for s in uncertain)
        doubts.append(
            f"per le slide {slides_txt} la durata è anomala e il contenuto non "
            "conferma: controlla a mano"
        )
    if frame_check is not None:
        mismatches = cast("Sequence[dict[str, object]]", frame_check.get("mismatches") or [])
        if mismatches:
            doubted = ", ".join(str(m.get("slide")) for m in mismatches)
            doubts.append(
                f"il video finito mostra la slide sbagliata nei segmenti "
                f"{doubted} (frame in .cache/verify_frames/)"
            )
    if weak_signal_seen():
        doubts.append(
            "la somiglianza tra parlato e slide è risultata debole: le durate "
            "sono stimate, non garantite (1:1 solo con le ancore 'slide N')"
        )
    if review_diffs:
        doubts.append(
            f"la revisione automatica contesta {review_diffs} scelte di slide: "
            "dettagli in .cache/sync_report.json (review_diffs)"
        )

    log.info("")
    if not doubts:
        log.info(
            "   Dubbi da controllare a mano: nessuno, la sincronizzazione è "
            "risultata solida."
        )
    else:
        log.info("   Da controllare a mano:")
        for doubt in doubts:
            log.info("     - %s", doubt)
    log.info(
        "   Fiducia del motore: %s.",
        _quality_phrase(quality, weak_signal_seen()),
    )
    log.info("=" * 70)


def _quality_phrase(quality: dict[str, float] | None, weak: bool) -> str:
    """Traduce la misura di qualità del motore in una frase breve."""
    if not quality:
        return "non misurata"
    avg_z = float(quality.get("avg_z") or 0.0)
    min_z = float(quality.get("min_avg_z") or 0.0)
    avg_sim = float(quality.get("avg_sim") or 0.0)
    if weak:
        # Il verdetto "bassa" NON nasce dal picco (che può essere alto, es. 0.75
        # su soglia 0.45) ma dal fatto che le slide sono confondibili tra loro:
        # citare solo il picco faceva sembrare la frase in contraddizione con il
        # numero accanto. Si nomina il motivo reale.
        return (
            f"bassa (slide confondibili per il motore, picco medio {avg_z:.2f}): "
            "la scaletta è stimata dal contenuto"
        )
    return f"alta (picco medio {avg_z:.2f} su soglia {min_z:.2f}, cosine {avg_sim:.2f})"


def _frame_check_video(
    video_path: Path,
    slide_ids: Sequence[int],
    durations: Sequence[float],
    slide_files: Sequence[str],
) -> dict[str, object]:
    """Verifica cosa è DAVVERO a schermo: un frame a metà di ogni segmento.

    Estratto in una funzione perché dopo una riparazione il video viene
    rigenerato e il controllo va ripetuto: primo e secondo controllo devono
    usare esattamente lo stesso codice, altrimenti gli esiti non sarebbero
    confrontabili.
    """
    offsets = [0.0]
    for d in durations:
        offsets.append(offsets[-1] + float(d))
    frame_check = frame_consistency_check(
        video_path,
        [(int(slide_ids[i]), offsets[i], offsets[i + 1]) for i in range(len(durations))],
        slide_files,
        CACHE_DIR / "verify_frames",
    )
    if frame_check["checked"]:
        log.info(
            "   Verifica frame vs slide: %s/%s segmenti mostrano la slide "
            "attesa (frame in .cache/verify_frames/).",
            frame_check["coherent"],
            frame_check["checked"],
        )
    for m in cast("list[dict[str, object]]", frame_check["mismatches"]):
        log.warning(
            "   ⚠️ Verifica frame: a %.1fs il video mostra la slide %s ma "
            "la timeline dice %s (similarità %.3f).",
            m["time"],
            m["shown"],
            m["slide"],
            m["similarity"],
        )
    return frame_check


def _repair_durations_from_frames(
    durations: Sequence[float],
    slide_ids: Sequence[int],
    mismatches: Sequence[dict[str, object]],
    words_raw: Sequence[Word],
    slide_texts: Sequence[str],
    total_duration: float,
    options: SemanticOptions,
) -> tuple[list[float], list[dict[str, object]]] | None:
    """Riposiziona i confini segnalati dal controllo del video.

    Il mismatch dice quale slide è davvero a schermo in un istante preciso: il
    nuovo confine viene cercato dal motore embedding nella sola direzione
    indicata dall'evidenza (vedi
    ``semantic_sync.repair_segments_from_frame_mismatches``). La sequenza delle
    slide mostrate NON cambia: cambiano solo le durate, quindi il video si
    rigenera dagli stessi file immagine e l'audio resta allineato.

    Returns:
        ``(durate, spostamenti applicati)``, oppure None se non c'è nulla da
        spostare (in quel caso resta valido il video già generato).
    """
    start = 0.0
    segments: list[dict[str, float | int]] = []
    for slide, d in zip(slide_ids, durations, strict=True):
        segments.append({"slide": int(slide), "start": start, "end": start + float(d)})
        start += float(d)
    repaired, applied = repair_segments_from_frames_from_words(
        segments,
        mismatches,
        list(words_raw),
        list(slide_texts),
        total_duration,
        options,
    )
    if not applied or len(repaired) != len(segments):
        return None
    new_durations = [float(seg["end"]) - float(seg["start"]) for seg in repaired]
    if any(d <= 0.0 for d in new_durations):
        log.warning(
            "   [Riparazione] Durate non valide dopo lo spostamento: "
            "riparazione annullata (resta il video già generato)."
        )
        return None
    delta_total = sum(new_durations) - sum(float(d) for d in durations)
    if abs(delta_total) > 1.0:
        log.warning(
            "   [Riparazione] La durata totale cambierebbe di %.1fs: "
            "riparazione annullata.",
            delta_total,
        )
        return None
    return new_durations, applied


def _find_anomalous_durations(
    durations: Sequence[float],
    slide_ids: Sequence[int],
    long_ratio: float = 3.0,
    short_ratio: float = 0.25,
) -> list[tuple[int, float]]:
    """Slide con durata molto fuori dalla mediana delle altre (possibile
    errore di sincronizzazione). Return: lista di (slide, durata)."""
    if len(durations) < 3:
        return []
    ordered = sorted(durations)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    if median <= 0:
        return []
    return [
        (int(s), float(d))
        for s, d in zip(slide_ids, durations, strict=True)
        if d > long_ratio * median or d < short_ratio * median
    ]


def _slide_tokens(text: str) -> list[str]:
    """Token lessicali puliti di una slide (minuscoli, >=3 char, no stopwords)."""
    return [
        t
        for t in re.findall(r"[a-zà-ù]+", text.lower())
        if len(t) >= 3 and t not in STOPWORDS_ITA
    ]


def _speech_tokens_in_window(
    words_raw: Sequence[Word], start: float, end: float
) -> list[str]:
    """Token lessicali del parlato nell'intervallo [start, end)."""
    return [
        t
        for w in words_raw
        if start <= w["start"] < end
        for t in re.findall(r"[a-zà-ù]+", w["word"].lower())
        if len(t) >= 3 and t not in STOPWORDS_ITA
    ]


def _token_f1(a: list[str], b: list[str]) -> float:
    """F1 sull'intersezione degli insiemi di token (0 se disgiunti)."""
    if not a or not b:
        return 0.0
    a_set, b_set = set(a), set(b)
    inter = len(a_set & b_set)
    if inter == 0:
        return 0.0
    precision = inter / len(a_set)
    recall = inter / len(b_set)
    return 2 * precision * recall / (precision + recall)


def _segment_content_verdict(
    speech_tokens: list[str],
    all_slide_tokens: Sequence[list[str]],
    displayed_slide: int,
) -> str:
    """Verdetto di contenuto per un segmento di durata anomala.

    Confronta il parlato del segmento con TUTTE le slide: se la slide più
    simile lessicalmente è quella mostrata -> 'coerente' (durata anomala
    reale: il podcast si è soffermato); se vince una slide DIVERSA ->
    'disallineata' (probabile errore di sincronizzazione); se il segnale è
    troppo debole -> 'incerto' (si conserva l'avviso generico).
    """
    if not speech_tokens or not all_slide_tokens:
        return "incerto"
    scores = [_token_f1(speech_tokens, st) for st in all_slide_tokens]
    best_idx = max(range(len(scores)), key=lambda i: scores[i])
    if scores[best_idx] < 0.10:
        return "incerto"
    if best_idx + 1 == displayed_slide:
        return "coerente"
    return "disallineata"


def _validate_anomalous_segments(
    anomalous: Sequence[tuple[int, float]],
    slide_texts: Sequence[str],
    words_raw: Sequence[Word],
    durations: Sequence[float],
    slide_ids: Sequence[int],
) -> dict[int, str]:
    """Verifica di contenuto dei segmenti anomali (durata molto fuori mediana).

    Per ogni slide anomala estrae il parlato nel suo intervallo temporale e
    lo confronta con l'OCR di tutte le slide (F1 lessicale). Ritorna
    ``{slide: 'coerente' | 'disallineata' | 'incerto'}`` così l'avviso può
    distinguere un segmento realmente lungo/corto da un allineamento errato.
    """
    all_slide_tokens = [_slide_tokens(t) for t in slide_texts]
    verdicts: dict[int, str] = {}
    offsets = [0.0]
    for d in durations:
        offsets.append(offsets[-1] + d)
    for s, d in anomalous:
        try:
            idx = slide_ids.index(s)
        except ValueError:
            continue
        start = offsets[idx]
        speech = _speech_tokens_in_window(words_raw, start, start + d)
        verdicts[s] = _segment_content_verdict(speech, all_slide_tokens, s)
    return verdicts


def _should_escalate_weak_signal(
    weak_signal: bool, missing_count: int, llm_enabled: bool
) -> bool:
    """True se conviene abbandonare la timeline locale appena costruita.

    ``weak_signal`` è il verdetto del motore embedding stesso: l'audio non
    segue l'ordine delle slide, quindi l'allineamento appena calcolato è
    inaffidabile. Prima questo veniva solo loggato e il video veniva generato
    comunque; con l'LLM disponibile conviene passargli il posizionamento delle
    slide senza ancora, perché legge il contenuto dei chunk. Se tutte le slide
    hanno un'ancora (``missing_count == 0``) il segnale debole non cambia nulla:
    i timestamp sono già dichiarati dal parlato.
    """
    return weak_signal and missing_count > 0 and llm_enabled


def _build_sync_report(
    durations: Sequence[float],
    slide_ids: Sequence[int],
    total_duration: float,
    verdicts: dict[int, str] | None = None,
    notes: dict[str, object] | None = None,
    review_diffs: Sequence[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Tabella verificabile dei segmenti effettivamente mostrati nel video.

    Una voce per slide con inizio, fine, durata e (solo per i segmenti
    anomali) il verdetto di contenuto. È l'artefatto che rende ispezionabile a
    posteriori cosa è finito a schermo e DOVE la sincronizzazione è sospetta,
    senza rieseguire il pipeline (usato da ``analysis_sync.py`` e dal debug
    manuale).

    ``notes`` aggiunge le scelte fatte dalla run (es. escalation per segnale
    debole, esito della verifica frame) e ``review_diffs`` le discrepanze del
    secondo passaggio LLM: entrambi sono segnali che prima finivano solo nei
    log e che invece devono restare sull'artefatto.
    """
    verdicts = verdicts or {}
    start = 0.0
    segments: list[dict[str, object]] = []
    for s, d in zip(slide_ids, durations, strict=True):
        segment: dict[str, object] = {
            "slide": int(s),
            "start": round(start, 3),
            "end": round(start + float(d), 3),
            "duration": round(float(d), 3),
        }
        verdict = verdicts.get(int(s))
        if verdict is not None:
            segment["verdict"] = verdict
        segments.append(segment)
        start += float(d)
    report: dict[str, object] = {
        "audio_duration": round(float(total_duration), 3),
        "segments": segments,
    }
    if notes:
        report.update(notes)
    if review_diffs:
        report["review_diffs"] = [dict(d) for d in review_diffs]
    return report


def _save_sync_report(report: dict[str, object]) -> None:
    """Scrive ``sync_report.json`` in cache (l'errore di scrittura non blocca)."""
    try:
        atomic_write_text(
            CACHE_DIR / "sync_report.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
        log.debug("   Report di sincronizzazione salvato in cache (sync_report.json).")
    except OSError:
        log.debug("   Impossibile salvare il report di sincronizzazione (ignorato).")


# =====================================================================
# CACHE SYSTEM
# =====================================================================
def _file_hash(path: Path) -> str:
    """MD5 hash del contenuto di un file (streaming: non carica il file in memoria)."""
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(block)
    return md5.hexdigest()


def _transcript_cache_key(audio_hash: str, args: Any, beam: int | None = None) -> str:
    """Chiave di cache della trascrizione.

    Deve descrivere TUTTE le scelte che cambiano il testo prodotto: il motore
    RISOLTO ('auto' non dice nulla: può essere OpenVINO o faster-whisper),
    modello, e i parametri del motore effettivamente usato (device, compute
    type, beam, batch per faster-whisper; device per OpenVINO).

    Senza questo, cambiare `--whisper-beam`/`--whisper-batch`, il compute type
    o il device riusava in silenzio la trascrizione prodotta con altre
    impostazioni: testo e timestamp diversi da quelli attesi, senza alcun
    avviso (la cache è per chiave, non valida il contenuto).

    Args:
        beam: beam da usare nella chiave, se diverso da quello della run. Serve
            alla decodifica accurata, che deve avere una chiave SUA (altrimenti
            la seconda trascrizione sovrascriverebbe la prima).
    """
    engine = resolved_transcriber(args.transcriber, Path(args.openvino_model_dir))
    if engine == "openvino":
        engine_params = [args.openvino_device]
    else:
        engine_params = [
            args.whisper_device,
            args.whisper_compute_type,
            f"beam{args.whisper_beam if beam is None else beam}",
            f"batch{args.whisper_batch}",
        ]
    parts = ["transcript", audio_hash[:12], args.lang, args.whisper_model, engine, *engine_params]
    return "_".join(str(p) for p in parts)


def _beam_auto_enabled(args: Any) -> bool:
    """True se la pipeline può scegliere da sola il beam.

    Solo con la decodifica veloce (beam <= 1) e con la scelta automatica
    attiva: se l'utente chiede beam 2-5 ha già scelto l'accuratezza, e non c'è
    nulla da rifare.
    """
    return bool(getattr(args, "auto_beam", True)) and int(args.whisper_beam) <= 1


def _needs_accurate_beam(pinned_slides: int, total_slides: int) -> bool:
    """True quando è il CONTENUTO a decidere la timeline, non le ancore.

    Con la timeline fissata dalle ancore 'slide N' il testo serve solo a
    rifinire confini già decisi, quindi la decodifica greedy è sufficiente.
    Quando le slide vincolate sono poche (o nessuna: flusso libero), i confini
    sono stimati dal testo: lì vale la decodifica di massima accuratezza.

    Args:
        pinned_slides: slide con un'ancora esplicita (la slide 1 non ha ancora)
        total_slides: slide totali
    """
    if total_slides <= 1:
        return False
    pinned_ratio = pinned_slides / (total_slides - 1)
    return pinned_ratio < AUTO_BEAM_PINNED_RATIO


def _beam_ab_cache_key(slides_key: str, audio_hash: str, args: Any) -> str:
    """Chiave della misura di confronto fra le due trascrizioni.

    Contiene tutto ciò che può cambiare la MISURA: le slide (testi OCR),
    l'audio, il modello di embedding e i parametri dell'allineamento. NON
    contiene ``AUTO_BEAM_AB_MARGIN``: la misura è un dato, la decisione si
    riapplica a ogni run, quindi cambiare soglia non invalida nulla.
    """
    model_tag = re.sub(r"[^A-Za-z0-9]+", "-", args.semantic_model.rsplit("/", 1)[-1]).strip("-")
    return "_".join(
        [
            "beamab",
            slides_key,
            audio_hash[:12],
            model_tag,
            f"w{args.semantic_window}",
            f"d{args.semantic_min_duration}",
            f"t{args.semantic_temperature}",
        ]
    )


def _use_accurate_transcript(ab: dict[str, object], accurate_added_anchors: bool) -> tuple[bool, str]:
    """Decide quale delle due trascrizioni usare, in base alla misura.

    Regola (in ordine):

    1. l'accurata ha trovato ancore che la veloce non aveva -> resta l'accurata:
       le ancore sono riferimenti espliciti, più affidabili di un proxy di
       somiglianza;
    2. confronto non calcolabile -> resta l'accurata (scelta prudente);
    3. l'accurata vince di almeno ``AUTO_BEAM_AB_MARGIN`` -> resta l'accurata;
    4. altrimenti si usa la VELOCE: se il testo migliore ce l'ha lei, pagare
       (e usare) la decodifica accurata non ha senso.

    Returns:
        ``(usa_accurata, motivo)``
    """
    if accurate_added_anchors:
        return True, "ha trovato ancore che la decodifica veloce non aveva"
    if "error" in ab:
        return True, "confronto non calcolabile, tengo la scelta prudente"
    delta = float(cast(float, ab.get("delta_avg_z", 0.0)) or 0.0)
    if delta >= AUTO_BEAM_AB_MARGIN:
        return True, f"l'accurata ha il segnale migliore (Δ {delta:+.3f})"
    return False, f"la veloce ha il segnale migliore (Δ {delta:+.3f})"


def _compare_transcript_alignment(
    args: Any,
    slide_texts: list[str],
    total_slides: int,
    total_duration: float,
    greedy_words: list[Word],
    accurate_words: list[Word],
    cache_key: str = "",
) -> dict[str, object]:
    """Misura la qualità di allineamento delle due trascrizioni dell'audio.

    Le due trascrizioni sono misurate con lo STESSO metro e SENZA ancore:
    è esattamente il caso in cui il testo decide la timeline, quindi la
    differenza misura la trascrizione e non i vincoli. ``concordance`` e
    ``confusability`` restano nel risultato perché su un deck confondibile
    (slide quasi-duplicate) il confronto è rumore e va riconosciuto come tale.

    La misura costa due embedding completi (~25s per trascrizione su un podcast
    di 17 minuti): viene quindi messa in cache e riusata, perché la decisione va
    riapplicata identica a ogni run.
    """
    if cache_key and not args.no_cache:
        cached = _load_cache(cache_key)
        ab_cached = cached.get("ab") if cached else None
        if isinstance(ab_cached, dict):
            log.info("   [Beam] Misura di confronto recuperata dalla cache (nessun re-embedding).")
            note = dict(ab_cached)
            note["from_cache"] = True
            return note
    options = SemanticOptions(
        model_name=args.semantic_model,
        cache_dir=args.semantic_cache_dir,
        window_seconds=args.semantic_window,
        min_slide_duration=args.semantic_min_duration,
        min_avg_similarity=args.semantic_min_sim,
        min_avg_z=args.semantic_min_z,
        temperature=args.semantic_temperature,
    )
    t0 = time.time()
    q_greedy = alignment_quality_from_words(
        slide_texts, greedy_words, total_slides, total_duration, options=options
    )
    q_accurate = alignment_quality_from_words(
        slide_texts, accurate_words, total_slides, total_duration, options=options
    )
    elapsed = time.time() - t0
    if q_greedy is None or q_accurate is None:
        log.info(
            "   [Beam] Confronto non calcolabile (segnale insufficiente o modello "
            "embedding non disponibile): tengo la trascrizione accurata."
        )
        return {"error": "qualità non calcolabile", "from_cache": False}

    delta = float(q_accurate["avg_z"]) - float(q_greedy["avg_z"])
    if delta > 0:
        winner, verdict = "accurate", "avrebbe vinto la decodifica ACCURATA"
    elif delta < 0:
        winner, verdict = "greedy", "avrebbe vinto la decodifica VELOCE"
    else:
        winner, verdict = "pari", "le due decodifiche sono equivalenti"
    log.info(
        "   [Beam] Confronto sulle stesse slide, senza ancore: veloce %.3f, accurata "
        "%.3f (Δ %+.3f) -> %s (%.0fs).",
        float(q_greedy["avg_z"]),
        float(q_accurate["avg_z"]),
        delta,
        verdict,
        elapsed,
    )
    note = {
        "greedy": {k: round(v, 4) for k, v in q_greedy.items()},
        "accurate": {k: round(v, 4) for k, v in q_accurate.items()},
        "delta_avg_z": round(delta, 4),
        "would_have_won": winner,
        "seconds": round(elapsed, 1),
        "from_cache": False,
    }
    if cache_key and not args.no_cache:
        _save_cache(cache_key, {"ab": note})
    return note


def _transcribe_with_accurate_beam(
    audio_path: Path,
    args: Any,
    cache_key_accurate: str,
) -> tuple[str, list[Word], dict[str, object]]:
    """Rifà la trascrizione con il beam di massima accuratezza (o la riprende).

    Restituisce (testo, parole raw, nota per il report). Il testo della
    decodifica veloce resta in cache sotto la sua chiave: la scelta automatica
    non cancella il lavoro già fatto, e la run successiva ritrova entrambe le
    trascrizioni senza rifare nulla.
    """
    beam = DEFAULT_WHISPER_BEAM_ACCURATE
    cached = None if args.no_cache else _load_cache(cache_key_accurate)
    if cached and "transcript" in cached:
        log.info("   [Beam] Trascrizione accurata (beam %d) recuperata dalla cache.", beam)
        return cast(str, cached["transcript"]), cast("list[Word]", cached.get("words_raw") or []), {
            "accurate_beam": beam,
            "accurate_from_cache": True,
        }

    t0 = time.time()
    transcript, words = transcribe_audio(
        audio_path,
        language=args.lang,
        model_size=args.whisper_model,
        transcriber=args.transcriber,
        openvino_model_dir=Path(args.openvino_model_dir),
        openvino_device=args.openvino_device,
        whisper_device=args.whisper_device,
        whisper_compute_type=args.whisper_compute_type,
        whisper_beam=beam,
        whisper_batch=args.whisper_batch,
    )
    seconds = time.time() - t0
    if not args.no_cache:
        _save_cache(cache_key_accurate, {"transcript": transcript, "words_raw": words})
    log.info("   [Beam] Trascrizione accurata (beam %d) completata in %.0fs.", beam, seconds)
    return transcript, words, {"accurate_beam": beam, "accurate_seconds": round(seconds, 1)}


def _cache_path(key: str) -> Path:
    """Percorso del file di cache per una data chiave."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def _load_cache(key: str) -> dict | None:
    """Carica dati dalla cache, o None se non presente."""
    path = _cache_path(key)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as e:
            log.debug("   Cache corrotta (%s), ignoro: %s", key, e)
    return None


def _save_cache(key: str, data: dict) -> None:
    """Salva dati nella cache."""
    atomic_write_text(_cache_path(key), json.dumps(data, ensure_ascii=False, indent=2))
    log.debug("   Cache salvata: %s", key)


# =====================================================================
# AUTO-DETECTION FLUSSO
# =====================================================================
def _detect_flow(transcript: str, words: list[Word] | None = None) -> str:
    """
    Auto-rileva il flusso di sincronizzazione dal contenuto della trascrizione.

    Preferisce l'analisi word-level (``detect_flow_from_words``), robusta ai
    numeri in parole ("slide tre" → slide-audio). Se le parole raw non sono
    disponibili, ripiega sulla regex della trascrizione compatta.

    Restituisce:
        "slide-audio" se trova numeri di slide espliciti ("slide 2", "slide tre"...)
        "audio-slide" se trova "blocco successivo" senza numeri di slide
        "free" (riordino libero) come fallback: senza segnali espliciti il
            podcast non segue i vincoli del prompt NotebookLM, quindi le slide
            seguono il contenuto in qualsiasi ordine (e possono ripetersi)
    """
    if words:
        flow = detect_flow_from_words(words)
        if flow:
            return flow
    has_slide_numbers = bool(re.search(r"slide\s*\d+", transcript, re.IGNORECASE))
    has_blocco = bool(re.search(r"blocco\s+succe", transcript, re.IGNORECASE))

    if has_slide_numbers:
        return "slide-audio"
    elif has_blocco:
        return "audio-slide"
    else:
        return "free"


# =====================================================================
# MAIN ORCHESTRATOR
# =====================================================================
def main(argv: list | None = None) -> None:
    # Bootstrap esplicito: verifica dipendenze prima di tutto
    bootstrap()
    args = parse_args(argv)

    # --- Rilevamento hardware automatico al primo avvio ---
    # Sceglie il motore migliore per il PC (NVIDIA->CUDA, iGPU Intel->OpenVINO,
    # altrimenti CPU) e installa/scarica ciò che serve. Idempotente.
    if not args.no_auto_setup:
        machine_setup(args, force=args.force_setup)

    # --- Controllo aggiornamenti pacchetti ---
    # Segnala gli aggiornamenti; di default chiede S/N per installare i
    # non-pinnati. --no-update = solo notifica.
    if not args.no_update_check:
        run_update_check(ask_to_update=not args.no_update)

    # --- Download modello OpenVINO (una tantum) e uscita ---
    if args.openvino_download:
        from transcription import download_openvino_model

        download_openvino_model(Path(args.openvino_model_dir))
        log.info("   Modello OpenVINO pronto. Puoi ora lanciare la pipeline.")
        return

    # --- Validazione input ---
    pdf_path = args.pdf_path
    if not pdf_path.exists():
        # Il file indicato non esiste: prova il .ppt/.pptx con lo stesso nome
        ppt_tries = [Path(str(pdf_path).rsplit(".", 1)[0] + ext) for ext in (".pptx", ".ppt")]
        found = next((p for p in ppt_tries if p.exists()), None)
        if found:
            pdf_path = found
        else:
            log.error(
                "[ERRORE] File presentazione non trovato: %s\n"
                "   Cercati anche: %s (conversione automatica PPT/PPTX -> PDF).\n"
                "   Specifica con: --pdf presentazione.pdf",
                pdf_path,
                "', '".join(str(p) for p in ppt_tries),
            )
            sys.exit(1)

    # Conversione PPT/PPTX -> PDF (temporaneo) per il resto della pipeline
    if pdf_path.suffix.lower() in PRESENTATION_SUFFIXES:
        try:
            converted_dir = CACHE_DIR / "ppt_pdf"
            pdf_path = convert_presentation_to_pdf(pdf_path, converted_dir)
        except RuntimeError as e:
            log.error("[ERRORE] %s", e)
            sys.exit(1)

    audio_path: Path | None
    if args.audio:
        audio_path = Path(args.audio)
        if not audio_path.is_absolute():
            audio_path = BASE_DIR / audio_path
    else:
        audio_path = find_audio_file(BASE_DIR)

    if not audio_path or not audio_path.exists():
        log.error(
            "\n[ERRORE CRITICO] Manca il file audio.\n"
            "   Cercato: qualsiasi file .mp3/.m4a/.wav/.aac/.ogg/.flac nella cartella\n"
            "   Oppure specifica con: --audio percorso/file.mp3\n"
            "   Esempio: python main.py --audio registrazione.m4a"
        )
        sys.exit(1)

    # --- Hash per cache ---
    pdf_hash = _file_hash(pdf_path)
    audio_hash = _file_hash(audio_path)
    # ``--no-cache`` ignora anche i vettori embedding (content-addressed):
    # senza questo il flag mentirebbe, perché quella cache non viene toccata
    # dalla pulizia orfana (che guarda solo i .json).
    set_embed_cache_enabled(not args.no_cache)

    cache_key_slides = f"slides_{pdf_hash[:12]}_{args.dpi}_{args.lang}"
    # Il modello E il motore fanno parte della chiave: cambiando motore o
    # --whisper-model non deve riusarsi la cache di un altro, che produce
    # timestamp/token diversi.
    cache_key_transcript = _transcript_cache_key(audio_hash, args)
    # Entrambe le trascrizioni possibili dell'audio corrente (veloce e accurata)
    # sono cache ATTIVE: la scelta automatica del beam può produrle tutte e due,
    # e cancellare la seconda a fine run farebbe ripagare la trascrizione
    # accurata a ogni esecuzione (la decisione si prende solo dopo aver letto il
    # testo, quindi all'avvio non si sa quale servirà).
    cache_key_transcript_accurate = _transcript_cache_key(
        audio_hash, args, beam=DEFAULT_WHISPER_BEAM_ACCURATE
    )
    # Anche la misura di confronto fra le due trascrizioni è una cache attiva:
    # rifarla a ogni run costerebbe due embedding completi per una decisione che
    # non cambia.
    cache_key_beam_ab = _beam_ab_cache_key(cache_key_slides, audio_hash, args)
    active_cache_keys: set[str] = {
        cache_key_slides,
        cache_key_transcript,
        cache_key_transcript_accurate,
        cache_key_beam_ab,
    }

    # --- Pulizia cache ALL'AVVIO ---
    # A ogni nuova run rimuove subito le cache di slide/trascrizione di
    # PDF/audio precedenti (le chiavi sono hash del contenuto: con input nuovi
    # le vecchie cache non servono). Le cache LLM vengono ripulite più avanti,
    # quando i contenuti correnti sono noti.
    _startup_cleaned = _clean_orphan_cache(active_cache_keys)
    if _startup_cleaned:
        log.info("   🧹 Rimosse %d cache orfane di run precedenti (slide/trascrizione).", _startup_cleaned)

    # --- Timing ---
    t_total_start = time.time()
    t_ocr = t_transcribe = t_sync = t_video = 0.0
    # Secondi del confronto fra le due trascrizioni (misura, non decisione):
    # è lavoro semantico, quindi viene attribuito alla sincronizzazione, così la
    # tabella dei tempi continua a sommare al totale.
    beam_ab_seconds = 0.0

    # Strutture accumulate
    slide_files: list[str] | None = None
    slide_texts: list[str] | None = None
    transcript: str | None = None
    words_raw: list[Word] | None = None

    # --- Fase 1: Slide + OCR (con cache) ---
    t_phase_start = time.time()
    if not args.no_cache:
        cached = _load_cache(cache_key_slides)
        if (
            cached
            and "slide_files" in cached
            and "slide_texts" in cached
            and all(Path(f).exists() for f in cached["slide_files"])
        ):
            # Cache valida: i file immagine esistono ancora
            slide_files = cached["slide_files"]
            slide_texts = cached["slide_texts"]
            log.info("1. [CACHE] Slide OCR recuperate dalla cache (%d slide).", len(slide_files))

    if slide_files is None or slide_texts is None:
        # Pulizia slide vecchie prima di rigenerare (solo a cache miss o --no-cache)
        cleaned = _clean_directory(args.slides_dir, "slide_*.png")
        if cleaned:
            log.info("   🧹 Pulite %d slide vecchie da %s.", cleaned, args.slides_dir.name)

        slide_files, slide_texts = extract_slides_text_ocr(
            pdf_path,
            args.slides_dir,
            lang=args.lang,
            dpi=args.dpi,
            workers=args.ocr_workers,
        )
        if not args.no_cache:
            _save_cache(
                cache_key_slides,
                {
                    "slide_files": slide_files,
                    "slide_texts": slide_texts,
                },
            )

    total_slides = len(slide_files)
    if total_slides == 0:
        log.error("[ERRORE] Nessuna slide trovata nel PDF.")
        sys.exit(1)
    # Copia della lista COMPLETA 1..N: più avanti `slide_files` viene riallineata
    # alla sequenza reale dei segmenti (flusso libero, --skip-slides). La verifica
    # frame vs slide deve confrontare col file della slide per NUMERO, quindi
    # deve usare questa lista intatta.
    all_slide_files = list(slide_files)
    t_ocr = time.time() - t_phase_start

    # --- Durata audio ---
    audio_clip: AudioFileClip | None = None
    try:
        audio_clip = AudioFileClip(str(audio_path))
        total_duration = audio_clip.duration
        log.info("   Durata audio: %.1f secondi", total_duration)

        # --- Fase 2: Trascrizione (con cache) ---
        t_phase_start = time.time()

        if not args.no_cache:
            cached = _load_cache(cache_key_transcript)
            if cached and "transcript" in cached:
                transcript = cached["transcript"]
                # Fix A: recupera anche le parole raw dalla cache se presenti
                if "words_raw" in cached:
                    words_raw = cached["words_raw"]
                log.info("2. [CACHE] Trascrizione recuperata dalla cache.")

        if transcript is None:
            transcript, words_raw = transcribe_audio(
                audio_path,
                language=args.lang,
                model_size=args.whisper_model,
                transcriber=args.transcriber,
                openvino_model_dir=Path(args.openvino_model_dir),
                openvino_device=args.openvino_device,
                whisper_device=args.whisper_device,
                whisper_compute_type=args.whisper_compute_type,
                whisper_beam=args.whisper_beam,
                whisper_batch=args.whisper_batch,
            )
            if not args.no_cache:
                # Fix A: salva anche le parole raw per estrazione deterministica
                _save_cache(
                    cache_key_transcript,
                    {
                        "transcript": transcript,
                        "words_raw": words_raw,
                    },
                )

        t_transcribe = time.time() - t_phase_start

        # --- Recupero parole raw (serve a detection flusso + deterministica) ---
        # Se words_raw non è in cache, prova a leggerlo da transcript_raw.txt
        if not words_raw:
            raw_txt = audio_path.parent / "transcript_raw.txt"
            if raw_txt.exists() and raw_txt.stat().st_mtime >= audio_path.stat().st_mtime:
                log.debug("   Leggo parole raw da %s", raw_txt.name)
                words_raw = _parse_transcript_raw(raw_txt)
            elif raw_txt.exists():
                log.warning(
                    "   [Avviso] Ignoro transcript_raw.txt obsoleto (più vecchio dell'audio): "
                    "appartiene a un podcast precedente, non lo uso come fallback."
                )

        # --- Correzione nomi propri (Whisper li storpi sistematicamente: "sigmond
        # freud", "thomas mur", "mark chiuse"...). I nomi corretti sono indizi
        # cruciali per il matching slide<->parlato di LLM e MiniLM. ---
        if words_raw:
            words_raw = correct_transcript_names(words_raw)

        # Scelte fatte dalla run che finiscono in sync_report.json (es.
        # escalation per segnale debole, beam automatico): prima esistevano solo
        # nei log. Va inizializzata prima della biforcazione flusso
        # libero/ordinato, perché il report è costruito più avanti per entrambi i
        # flussi, e prima della scelta del beam, che la annota qui dentro.
        sync_notes: dict[str, object] = {}

        # --- Auto-detection flusso (dopo trascrizione, prima della sincronizzazione) ---
        flow: str
        if args.flow is not None:
            flow = args.flow
            log.info("   Flusso specificato manualmente: %s", flow)
        else:
            flow = _detect_flow(transcript, words_raw)
            log.info("   Flusso auto-rilevato: %s (usa --flow per sovrascrivere)", flow)
            if flow == "free":
                log.warning(
                    "\n   [Avviso] Nessun riferimento 'slide N' né 'blocco successivo' "
                    "rilevato nella trascrizione: flusso libero (le slide seguono il "
                    "contenuto, senza ordine fisso).\n"
                    "   - Flusso podcast -> slide (podcast generato per primo, prompt "
                    "'senza riferimenti alle slide'): comportamento ATTESO, nessuna "
                    "azione necessaria.\n"
                    "   - Flusso slide -> podcast: se il podcast doveva annunciare le "
                    "slide (es. 'passiamo alla slide 2'), le ancore mancano: "
                    "rigenera l'audio.\n"
                    "   Per forzare comunque un allineamento ordinato senza LLM: "
                    "--flow slide-audio --llm off (meno preciso senza ancore)."
                )
                if not args.no_free_ordered_fallback:
                    # Fallback automatico: su podcast senza ancore la selezione
                    # libera via LLM è lenta (~16 min) e quella con soli
                    # embeddings spezzetta le slide (micro-segmenti, ordine
                    # caotico). L'allineamento ordinato con soli embeddings
                    # produce durate bilanciate in ~1 min senza LLM. Disattivabile
                    # con --no-free-ordered-fallback.
                    log.warning(
                        "\n   [Fallback] Flusso libero senza ancore: passo automaticamente "
                        "all'allineamento ordinato slide-audio con soli embeddings "
                        "(verificato: durate bilanciate, ~1 min).\n"
                        "   Disattiva con --no-free-ordered-fallback o forza "
                        "--flow free --llm auto per la selezione libera via LLM."
                    )
                    flow = "slide-audio"
                    args.llm = "off"

        # --- Ancore deterministiche disponibili ---
        # Calcolate una volta: servono all'avviso qui sotto e alla scelta
        # automatica del beam. Nel flusso libero non vincolano l'allineamento
        # (le slide seguono il contenuto), quindi non si cercano.
        flow_anchors: dict[int, float] = (
            extract_slide_anchors(words_raw, total_slides, flow) if flow != "free" and words_raw else {}
        )

        # --- Check preventivo ancore: avviso PRIMA della sincronizzazione se il
        # podcast ha annunciato poche slide (probabile deriva del prompt
        # NotebookLM). Con poche ancore la timeline sarà stimata (9Router o
        # fallback MiniLM) e le slide non annunciate avranno durate brevi o
        # micro-segmenti: conviene rigenerare l'audio. ---
        if flow != "free" and words_raw:
            early_anchors = flow_anchors
            early_missing = [s for s in range(2, total_slides + 1) if s not in early_anchors]
            if early_anchors and early_missing:
                log.warning(
                    "\n   [Avviso] Solo %d slide su %d annunciate esplicitamente "
                    "(mancanti: %s).\n"
                    "   Le slide non annunciate saranno posizionate per contenuto: "
                    "risultato stimato, con durate possibilmente brevi.\n"
                    "   Flusso slide -> podcast: il podcast doveva annunciare "
                    "tutte le slide; se le manca, conviene rigenerare l'audio "
                    "PRIMA di procedere.",
                    len(early_anchors),
                    total_slides - 1,
                    ", ".join(str(s) for s in early_missing),
                )
                if is_interactive() and not args.no_confirm:
                    try:
                        input(
                            "   Premi Invio per continuare con la sincronizzazione stimata, "
                            "oppure Ctrl+C per fermarti: "
                        )
                    except (EOFError, KeyboardInterrupt):
                        _abort("Interrotto dall'utente prima della sincronizzazione.")

        # --- Beam automatico: veloce quando le ancore vincolano, accurato
        # quando è il contenuto a decidere -------------------------------------
        # La decisione non si può prendere PRIMA di trascrivere (le ancore
        # esistono solo dentro il testo), quindi si corregge a posteriori: se le
        # ancore fissano la timeline il testo rifinisce confini già decisi e la
        # decodifica veloce non costa precisione; se ne fissano poche (o nessuna:
        # flusso libero) i confini sono STIMATI dal testo, e lì vale la decodifica
        # di massima accuratezza. Il costo si paga una volta per audio: la cache
        # è per chiave, quindi la run successiva ritrova entrambe le trascrizioni.
        if _beam_auto_enabled(args) and _needs_accurate_beam(len(flow_anchors), total_slides):
            # Istantanea della decodifica veloce: serve al confronto misurabile e
            # a poter tornare indietro senza rifare nulla (è già in cache).
            greedy_transcript = transcript
            greedy_words = list(words_raw or [])
            greedy_anchors = dict(flow_anchors)
            log.info(
                "\n   [Beam] %d slide su %d hanno un'ancora esplicita: la timeline è "
                "decisa dal contenuto, quindi serve la decodifica accurata (beam %d). "
                "Con le ancore complete basta la decodifica veloce.",
                len(flow_anchors),
                max(total_slides - 1, 0),
                DEFAULT_WHISPER_BEAM_ACCURATE,
            )
            transcript, accurate_words, beam_note = _transcribe_with_accurate_beam(
                audio_path, args, cache_key_transcript_accurate
            )
            t_transcribe += float(cast("float", beam_note.pop("accurate_seconds", 0.0)) or 0.0)
            if accurate_words:
                words_raw = correct_transcript_names(accurate_words)
            # Le ancore vanno ricercate nel testo nuovo: una trascrizione più
            # accurata può riconoscere annunci che la decodifica veloce aveva
            # perso, e allora la timeline torna vincolata (gratis).
            rechecked = (
                extract_slide_anchors(words_raw, total_slides, flow) if flow != "free" and words_raw else {}
            )
            if rechecked and not flow_anchors:
                log.info(
                    "   [Beam] La trascrizione accurata ha trovato %d ancore che la "
                    "decodifica veloce aveva perso: la timeline torna vincolata.",
                    len(rechecked),
                )
            flow_anchors = rechecked or flow_anchors
            beam_note["pinned_slides"] = len(flow_anchors)
            beam_note["total_slides"] = total_slides
            beam_note["chosen"] = "accurate"
            # Confronto misurabile fra le due trascrizioni. Richiede l'embedding
            # di entrambe (~25s l'una), quindi si fa solo qui, dove la seconda
            # trascrizione esiste già e il caso è quello in cui il testo decide
            # tutto; la misura viene messa in cache e riusata.
            if greedy_words and words_raw:
                ab_note = _compare_transcript_alignment(
                    args,
                    slide_texts,
                    total_slides,
                    total_duration,
                    greedy_words,
                    words_raw,
                    cache_key_beam_ab,
                )
                beam_note["ab"] = ab_note
                if not ab_note.get("from_cache"):
                    beam_ab_seconds = float(cast(float, ab_note.get("seconds", 0.0)) or 0.0)
                # Le ancore sono riferimenti espliciti: se la trascrizione
                # accurata ne ha trovate di nuove, resta quella.
                use_accurate, reason = _use_accurate_transcript(
                    ab_note, len(flow_anchors) > len(greedy_anchors)
                )
                beam_note["reason"] = reason
                if not use_accurate:
                    # La misura ha deciso: si torna alla trascrizione veloce
                    # (già in cache, nessuna trascrizione rifatta).
                    transcript = greedy_transcript
                    words_raw = greedy_words
                    flow_anchors = greedy_anchors
                    beam_note["chosen"] = "greedy"
                log.info(
                    "   [Beam] Uso la trascrizione %s: %s.",
                    "ACCURATA" if use_accurate else "VELOCE",
                    reason,
                )
            sync_notes["beam"] = beam_note

        # --- Fase 3: Sincronizzazione semantica (unico motore) ---
        t_phase_start = time.time()

        if not words_raw:
            _abort("Trascrizione non disponibile: impossibile sincronizzare.")

        # --- Flusso libero (riordino): le slide seguono il contenuto del
        # podcast e possono apparire in qualsiasi ordine o ripetersi ---
        slide_ids: list[int]
        if flow == "free":
            log.info("3. Selezione libera: le slide seguono il contenuto del podcast, senza vincolo di ordine.")

            # Motore LLM (opzionale): supera il tetto di precisione del MiniLM
            # su presentazioni tematicamente omogenee. Unico provider: 9Router
            # online (cascata interna di 3 modelli), poi fallback automatico
            # al MiniLM locale.
            segments = None
            llm_used = False
            if args.llm != "off":
                endpoints = endpoints_for(args.llm)
                if args.llm_model:
                    for ep in endpoints:
                        ep["model"] = args.llm_model
                log.info("   Selezione slide con LLM (--llm %s)...", args.llm)
                try:
                    segments = llm_timeline_segments(
                        slide_texts,
                        words_raw,
                        total_slides,
                        total_duration,
                        chunk_seconds=args.llm_chunk,
                        endpoints=endpoints,
                        review=args.llm_review,
                        wait_timeout=args.llm_wait_timeout,
                        strict=True,
                    )
                except RuntimeError as e:
                    # Flusso libero: il MiniLM da solo non basta (tetto ~50%)
                    # ed è lento su audio lunghi. Senza terminale interattivo
                    # per scegliere, è meglio fermarsi con un errore chiaro.
                    _abort(str(e))
                if segments is not None:
                    log.info("   Timeline generata dall'LLM.")
                    # I segmenti provengono dall'LLM: su di essi va applicata la
                    # post-elaborazione (confini a parola + anti-flicker).
                    llm_used = True

            if not segments:
                log.info("   Sincronizzazione semantica locale (MiniLM)...")
                local_segments = free_order_segments_from_words(
                    slide_texts,
                    words_raw,
                    total_slides,
                    total_duration,
                    options=SemanticOptions(
                        model_name=args.semantic_model,
                        cache_dir=args.semantic_cache_dir,
                        window_seconds=args.semantic_window,
                        min_segment_seconds=max(8.0, 2 * args.semantic_min_duration),
                        min_avg_similarity=args.semantic_min_sim,
                        min_avg_z=args.semantic_min_z,
                    ),
                )
                # I segmenti MiniLM sono "Segment" (TypedDict): convertiti in
                # dict generici per restare omogenei ai segmenti LLM.
                if local_segments is not None:
                    segments = [dict(s) for s in local_segments]
            if not segments:
                _abort("Selezione libera fallita: nessun segmento affidabile generabile da slide + trascrizione.")

            # Post-elaborazione dei SOLI segmenti LLM (flusso libero):
            #   1) confini a livello di parola (l'LLM lavora su chunk da 30s e
            #      non può esprimere confini più fini: i cambi slide cadono a
            #      metà discorso);
            #   2) merge anti-flicker dei segmenti residui corti (es. ultimo
            #      chunk parziale da pochi secondi).
            # Il MiniLM del flusso libero ha già il suo anti-flicker e il flusso
            # ordinato ha ancore esatte da non toccare.
            if llm_used:
                # Nota: la cache llm_*.json conserva la timeline GREZZA dell'LLM
                # (a chunk); il raffinamento qui sotto è deterministico e viene
                # riapplicato a ogni run sopra il risultato cachato.
                log.info("   Post-elaborazione timeline LLM: raffinamento confini a livello di parola...")
                segments = refine_llm_segments_from_words(
                    segments,
                    words_raw,
                    slide_texts,
                    options=SemanticOptions(
                        model_name=args.semantic_model,
                        cache_dir=args.semantic_cache_dir,
                    ),
                    window_seconds=min(args.llm_chunk, 30.0),
                )
                n_segments_before = len(segments)
                segments = merge_short_segments(segments)
                if len(segments) != n_segments_before:
                    log.info(
                        "   Anti-flicker LLM: %d segmento/i corto/i assorbito/i.",
                        n_segments_before - len(segments),
                    )

            t_sync = time.time() - t_phase_start

            slide_ids = [int(seg["slide"]) for seg in segments]
            # La stessa slide può comparire più volte: costruisci la sequenza
            # reale di file e durate per l'assemblaggio video.
            slide_files = [slide_files[s - 1] for s in slide_ids]
            durations = [float(seg["end"]) - float(seg["start"]) for seg in segments]
            for i, seg in enumerate(segments):
                log.info(
                    "   -> Slide %2d: da %.1fs a %.1fs (durata: %.1fs)",
                    slide_ids[i],
                    float(seg["start"]),
                    float(seg["end"]),
                    durations[i],
                )

            missing = [s for s in range(1, total_slides + 1) if s not in set(slide_ids)]
            if missing:
                log.warning(
                    "   [Avviso] Slide mai mostrate dal riordino (%s): il loro "
                    "contenuto non è presente nella narrazione audio.",
                    ", ".join(f"slide {s}" for s in missing),
                )
        else:
            # Ancore deterministiche "slide N" dalla trascrizione: riferimenti
            # espliciti ad alta precisione che vincolano l'allineamento semantico.
            # Sono già state calcolate sopra (`flow_anchors`) e ricalcolate se la
            # scelta automatica del beam ha rifatto la trascrizione: riusarle
            # evita una seconda scansione identica (e il suo log duplicato).
            semantic_anchors = flow_anchors
            # Riferimento parlato alla "slide 1": la slide 1 reale è sempre 0.0,
            # ma la numerazione dello speaker può essere sfasata (dice "slide 1"
            # mostrando la slide 2 del PDF). Viene passato SOLO alla verifica LLM
            # del mapping, mai usato come ancora vincolante.
            slide_one_refs = extract_slide_one_references(words_raw, total_slides)
            if semantic_anchors:
                log.info(
                    "3. [Ancore] %d ancore 'slide N' coerenti usate per vincolare l'allineamento semantico.",
                    len(semantic_anchors),
                )
            else:
                log.info("3. Nessun riferimento 'slide N': sincronizzazione solo per contenuto.")
            if slide_one_refs:
                log.info(
                    "   [Ancore] Riferimento parlato alla 'slide 1' a %.1fs: "
                    "passato alla verifica del mapping (numerazione sfasata).",
                    next(iter(slide_one_refs.values())),
                )

            # --- Verifica mapping ancore: numero parlato -> slide reale del PDF ---
            # Il podcast potrebbe NON seguire le regole del prompt NotebookLM: la
            # numerazione parlata può essere sfasata rispetto al PDF (es. lo speaker
            # dice "quarta diapositiva" ma mostra la slide 5). L'euristica
            # deterministica (embeddings locali) corregge subito gli offset
            # sistematici; l'LLM legge invece il contenuto del parlato dopo ogni
            # riferimento "slide N" e corregge il numero di slide, mantenendo i
            # TEMPI esatti. Fallback: ancore originali.
            # Gira SOLO se serve davvero (slide senza ancora, come il flusso ibrido):
            # con ancore complete l'LLM non aggiunge nulla e 9Router non va toccato.
            verify_anchors = {**semantic_anchors, **slide_one_refs}
            if verify_anchors and (
                len(semantic_anchors) < total_slides - 1 or slide_one_refs
            ):
                # 1) Euristica DETERMINISTICA (embeddings locali, offline):
                #    se la numerazione parlata è sistematicamente sfasata (es.
                #    copertina esclusa: "slide 1" -> slide 2 del PDF) la corregge
                #    senza chiamare 9Router. Sempre attiva (anche con --llm off).
                _anchor_report: dict[str, bool] = {}
                verified = verify_anchor_mapping_embedding(
                    slide_texts,
                    words_raw,
                    verify_anchors,
                    total_slides,
                    window_seconds=40.0,
                    options=SemanticOptions(
                        model_name=args.semantic_model,
                        cache_dir=args.semantic_cache_dir,
                    ),
                    report=_anchor_report,
                )
                if verified is not None:
                    # La slide 1 reale è sempre 0.0: un eventuale mapping a slide 1
                    # (es. ripasso della prima slide a metà narrazione) non è un
                    # confine di transizione e non deve vincolare la timeline.
                    verified = {s: t for s, t in verified.items() if s != 1}
                    log.info(
                        "   Mapping ancore corretto dall'euristica deterministica: %d ancore.",
                        len(verified),
                    )
                    semantic_anchors = verified
                elif args.llm != "off":
                    # Salto la verifica LLM SOLO quando il mapping è coerente: una
                    # sola slide senza ancora (caso più comune: 13/14 annunciate) e
                    # nessun offset sospetto dall'euristica deterministica. Se
                    # l'euristica ha visto almeno un'ancora il cui contenuto NON
                    # conferma il numero parlato ma non può correggere in modo
                    # affidabile (una sola slide sfasata, drift a intermittenza),
                    # la verifica LLM parte comunque: è proprio il caso in cui la
                    # scorciatoia nasconderebbe un disallineamento (es. una slide
                    # mai nominata a metà deck che sfasa tutte le successive).
                    # La verifica resta inoltre per i casi già coperti: recap della
                    # slide 1 o ≥2 slide senza ancora.
                    anchor_mapping_suspicious = _anchor_report.get("suspicious", False)
                    if (
                        slide_one_refs
                        or (total_slides - 1 - len(semantic_anchors) >= 2)
                        or anchor_mapping_suspicious
                    ):
                        # 2) Fallback LLM: la numerazione non ha offset sistematico
                        #    rilevabile, lascio decidere all'LLM (lettura del contenuto).
                        endpoints = endpoints_for(args.llm)
                        if args.llm_model:
                            for ep in endpoints:
                                ep["model"] = args.llm_model
                        log.info("   Verifica mapping ancore con LLM (--llm %s)...", args.llm)
                        # Validatore dei rimappi LLM: un rimappo che contraddice il
                        # contenuto del parlato (embeddings locali) viene scartato,
                        # perché le ancore esplicite sono vincoli ad alta precisione
                        # e un rimappo errato rompe la timeline (es. slide 4/5).
                        remap_filter = make_anchor_remap_filter(
                            slide_texts,
                            words_raw,
                            total_slides,
                            window_seconds=40.0,
                            options=SemanticOptions(
                                model_name=args.semantic_model,
                                cache_dir=args.semantic_cache_dir,
                            ),
                        )
                        try:
                            verified = llm_verify_anchor_mapping(
                                slide_texts,
                                words_raw,
                                verify_anchors,
                                total_slides,
                                endpoints=endpoints,
                                wait_timeout=args.llm_wait_timeout,
                                strict=True,
                                remap_filter=remap_filter,
                            )
                        except RuntimeError as e:
                            # 9Router necessario ma non avviabile/non online: niente
                            # fallback silenzioso, il processo si arresta con l'avviso.
                            _abort(str(e))
                        if verified is not None:
                            # La slide 1 reale è sempre 0.0: un eventuale mapping a slide 1
                            # (es. ripasso della prima slide a metà narrazione) non è un
                            # confine di transizione e non deve vincolare la timeline.
                            verified = {s: t for s, t in verified.items() if s != 1}
                            log.info(
                                "   Mapping ancore corretto dall'LLM: %d ancore verificate.",
                                len(verified),
                            )
                            semantic_anchors = verified
                    else:
                        log.info(
                            "   [Ancore] Una sola slide senza ancora e mapping coerente "
                            "(nessun offset sospetto): salto la verifica LLM del mapping "
                            "(le ancore restano quelle deterministiche) e risparmio ~1 min."
                        )

            # Log diagnostico condiviso (stato finale ancore, post-verifica):
            # le slide senza ancora esplicita sono quelle che il flusso ibrido
            # posizionerà con l'LLM (o che il MiniLM allinea per contenuto).
            _missing_anchors = sorted(
                s for s in range(2, total_slides + 1) if s not in semantic_anchors
            )
            if _missing_anchors:
                log.info(
                    "   [Ancore] Slide senza ancora esplicita dopo la verifica (%d): %s.",
                    len(_missing_anchors),
                    ", ".join(str(s) for s in _missing_anchors),
                )

            # --- Pulizia cache LLM orfane ---
            # Con podcast/presentazione nuovi le chiavi contenuto-specifiche
            # cambiano: le cache LLM di run precedenti non servono più. Si
            # conservano SOLO quelle che questa run può riusare (stessi
            # contenuti, calcolate con gli stessi endpoint) e la timeline finale.
            if args.llm != "off":
                _llm_endpoints = endpoints_for(args.llm)
                if args.llm_model:
                    for ep in _llm_endpoints:
                        ep["model"] = args.llm_model
                _llm_keep = {"llm_timeline_finale"}
                _llm_keep.update(
                    llm_cache_keys_for(
                        slide_texts,
                        words_raw,
                        total_slides,
                        args.llm_chunk,
                        _llm_endpoints,
                        [verify_anchors, semantic_anchors],
                    )
                )
                _llm_cleaned = _clean_stale_llm_cache(_llm_keep)
                if _llm_cleaned:
                    log.info("   🧹 Rimosse %d cache LLM orfane (contenuti cambiati).", _llm_cleaned)

            # --- Flusso IBRIDO (ordinato + LLM, con fallback locale) ---
            # Le ancore deterministiche sono vincoli ESATTI e inviolabili. Se
            # restano slide senza ancora esplicita (mai nominate o narrate fuori
            # posizione), serve posizionarle dove il loro contenuto è discusso.
            # Con POCHE slide mancanti basta il raffinamento locale (embeddings)
            # -- percorso A, veloce e senza 9Router --; solo oltre
            # `--llm-local-threshold` si usa l'LLM cloud, e in tal caso
            # 9Router viene AVVIATO automaticamente se spento (wait_for_router)
            # e la pipeline riprende da sola appena è online. Fallback MiniLM.
            timeline: dict[int, float] | None = None
            llm_hybrid_attempted = False
            if args.llm != "off" and semantic_anchors and len(semantic_anchors) < total_slides - 1:
                missing_count = (total_slides - 1) - len(semantic_anchors)
                use_local = missing_count <= args.llm_local_threshold
                if use_local:
                    # PERCORSO A: il raffinamento locale basta per poche slide
                    # senza ancora. Nessuna chiamata LLM, nessun 9Router, nessuna
                    # attesa di rete: la sincronizzazione passa da ~minuti a
                    # secondi per queste slide.
                    log.info(
                        "   Flusso ibrido: %d slide senza ancora (<= soglia %d): uso il "
                        "motore embedding locale (semantic + refine) al posto di 9Router.",
                        missing_count,
                        args.llm_local_threshold,
                    )
                    # Il flag di segnale debole è globale: azzerato qui per
                    # misurarlo SOLO su questa chiamata (altrimenti una
                    # rilevazione precedente della stessa run lo falserebbe).
                    reset_weak_signal_flag()
                    timeline = semantic_timeline_from_words(
                        slide_texts,
                        words_raw,
                        total_slides,
                        total_duration,
                        options=SemanticOptions(
                            model_name=args.semantic_model,
                            cache_dir=args.semantic_cache_dir,
                            window_seconds=args.semantic_window,
                            min_slide_duration=args.semantic_min_duration,
                            min_avg_similarity=args.semantic_min_sim,
                            min_avg_z=args.semantic_min_z,
                            temperature=args.semantic_temperature,
                        ),
                        anchors=semantic_anchors,
                    )
                    if timeline is not None and _should_escalate_weak_signal(
                        weak_signal_seen(), missing_count, args.llm != "off"
                    ):
                        # Il motore embedding stesso dichiara il segnale
                        # inaffidabile (l'audio non segue l'ordine delle slide):
                        # prima l'avviso finiva solo nel log e il video veniva
                        # generato comunque. Qui si passa al percorso LLM, che
                        # legge il contenuto dei chunk.
                        log.warning(
                            "\n   [Fallback] Motore embedding locale con segnale DEBOLE: "
                            "passo all'LLM per posizionare le %d slide senza ancora, "
                            "invece di generare un video potenzialmente disallineato.",
                            missing_count,
                        )
                        sync_notes["engine"] = "llm_escalated_weak_signal"
                        timeline = None
                        use_local = False
                    elif timeline is not None:
                        # Raffinamento a livello di parola SOLO delle slide senza
                        # ancora (stesso refine usato dopo l'LLM: deterministico,
                        # zero chiamate di rete). Le ancore esatte restano ai loro
                        # timestamp pronunciati.
                        log.info(
                            "   Post-elaborazione locale: raffinamento confini a "
                            "livello di parola (solo slide senza ancora)..."
                        )
                        timeline = refine_llm_timeline_from_words(
                            timeline,
                            semantic_anchors,
                            words_raw,
                            slide_texts,
                            total_duration,
                            options=SemanticOptions(
                                model_name=args.semantic_model,
                                cache_dir=args.semantic_cache_dir,
                            ),
                            window_seconds=min(args.llm_chunk, 30.0),
                        )
                if not use_local:
                    # Percorso B: molte slide senza ancora (oppure percorso A
                    # abbandonato per segnale debole). Serve l'LLM per capire dove
                    # viene discusso il contenuto: 9Router parte in automatico se
                    # spento (wait_for_router in llm_ordered_timeline).
                    llm_hybrid_attempted = True
                    endpoints = endpoints_for(args.llm)
                    if args.llm_model:
                        for ep in endpoints:
                            ep["model"] = args.llm_model
                    log.info(
                        "   Flusso ibrido: ancore esatte + LLM per le %d slide senza ancora (--llm %s)...",
                        missing_count,
                        args.llm,
                    )
                    try:
                        timeline = llm_ordered_timeline(
                            slide_texts,
                            words_raw,
                            total_slides,
                            total_duration,
                            anchors=semantic_anchors,
                            chunk_seconds=args.llm_chunk,
                            endpoints=endpoints,
                            wait_timeout=args.llm_wait_timeout,
                            strict=True,
                        )
                    except RuntimeError as e:
                        # 9Router necessario ma non avviabile/non online: niente
                        # fallback silenzioso, il processo si arresta con l'avviso.
                        _abort(str(e))
                    if timeline is not None:
                        log.info("   Timeline ibrida generata dall'LLM (ancore esatte preservate).")
                        # Post-elaborazione dei SOLI confini LLM del flusso ordinato:
                        # l'LLM lavora su chunk da `llm_chunk` secondi, quindi i
                        # confini delle slide SENZA ancora esplicita possono cadere a
                        # metà parola o nel mezzo di un discorso ancora dedicato alla
                        # slide precedente. Il refine sposta SOLO quei confini al
                        # punto di parola in cui la similarità locale si inverte; le
                        # ancore esatte restano intoccate (stesso modello embedding
                        # in cache, zero chiamate LLM). Il MiniLM del fallback non ha
                        # bisogno del refine: i suoi confini sono già allineati alle
                        # parole (first_time dei blocchi da `semantic_window`s).
                        # Nota: la cache llm_*.json conserva la timeline GREZZA
                        # dell'LLM; il raffinamento è deterministico e viene
                        # riapplicato a ogni run sopra il risultato cachato.
                        log.info(
                            "   Post-elaborazione timeline LLM: raffinamento confini a livello di parola "
                            "(solo slide senza ancora)..."
                        )
                        timeline = refine_llm_timeline_from_words(
                            timeline,
                            semantic_anchors,
                            words_raw,
                            slide_texts,
                            total_duration,
                            options=SemanticOptions(
                                model_name=args.semantic_model,
                                cache_dir=args.semantic_cache_dir,
                            ),
                            window_seconds=min(args.llm_chunk, 30.0),
                        )

            if timeline is None:
                if llm_hybrid_attempted:
                    log.warning(
                        "\n   [Avviso] L'LLM non ha prodotto una timeline coerente con le "
                        "ancore (posizioni in conflitto o risposta non interpretabile).\n"
                        "   Ripiego sul motore locale (embeddings): qualità inferiore, "
                        "possibili micro-segmenti sulle slide senza ancora.\n"
                        "   Il problema nasce dal podcast: poche ancore 'slide N' "
                        "annunciate. Rigenera l'audio se possibile.\n"
                    )
                log.info("   Sincronizzazione semantica (embeddings offline)...")
                timeline = semantic_timeline_from_words(
                    slide_texts,
                    words_raw,
                    total_slides,
                    total_duration,
                    options=SemanticOptions(
                        model_name=args.semantic_model,
                        cache_dir=args.semantic_cache_dir,
                        window_seconds=args.semantic_window,
                        min_slide_duration=args.semantic_min_duration,
                        min_avg_similarity=args.semantic_min_sim,
                        min_avg_z=args.semantic_min_z,
                        temperature=args.semantic_temperature,
                    ),
                    anchors=semantic_anchors,
                )

            if timeline is None:
                _abort("Sincronizzazione semantica fallita: nessuna timeline generabile da slide + trascrizione.")

            t_sync = time.time() - t_phase_start

            # --- Riconciliazione (precisione assoluta: interrompe se non valida) ---
            try:
                durations = reconcile_timeline(
                    timeline,
                    total_slides,
                    total_duration,
                )
            except ValueError as e:
                _abort(f"{e} Sincronizzazione impossibile senza distribuzioni inventate.")
            slide_ids = list(range(1, total_slides + 1))

            # --- Persistenza timeline finale (per gli strumenti di verifica) ---
            # Il flusso semantico (MiniLM) NON salva una cache llm_*.json: senza
            # questo file, analysis_sync.py riciclerebbe una timeline LLM vecchia
            # di una run precedente, generando falsi mismatch. Il file usa il
            # prefisso llm_ per sopravvivere alla pulizia delle cache orfane e
            # viene sovrascritto a ogni run con gli start/end validati.
            try:
                _save_final_timeline(timeline, total_slides, total_duration)
            except OSError:
                log.debug("   Impossibile salvare la timeline finale in cache (ignorato).")

        # --- Avviso: slide quasi non coperte dalla narrazione ---
        thin = [slide_ids[i] for i, d in enumerate(durations) if d < 2 * args.semantic_min_duration]
        if thin:
            # Il consiglio dell'ancora esplicita vale SOLO nel flusso
            # slide -> podcast: nel flusso podcast -> slide le ancore 'slide N'
            # sono escluse dal prompt, quindi l'unico rimedio è ampliare l'audio.
            if flow != "free":
                advice = (
                    "amplia l'audio su quei temi oppure fai pronunciare "
                    "un'ancora esplicita 'slide N' al momento della transizione"
                )
            else:
                advice = (
                    "amplia l'audio su quei temi (nel flusso podcast -> slide "
                    "le ancore 'slide N' sono escluse dal prompt)"
                )
            log.warning(
                "\n   [Avviso] Slide con durata minima (%s): il loro contenuto "
                "sembra poco presente nella narrazione audio.\n"
                "   Per migliorare: %s.",
                ", ".join(f"slide {s}" for s in thin),
                advice,
            )

        # --- Avviso: durate slide molto squilibrate (possibile sync errato) ---
        # Prima di allarmare, verifica il CONTENUTO dei segmenti anomali
        # (parlato del segmento vs OCR delle slide): una durata lunga/corta
        # con parlato coerente è reale (il podcast si è soffermato), non un
        # errore di sincronizzazione. Solo i segmenti disallineati o incerti
        # meritano l'avviso.
        anomalous = _find_anomalous_durations(durations, slide_ids)
        verdicts = (
            _validate_anomalous_segments(
                anomalous, slide_texts, words_raw, durations, slide_ids
            )
            if anomalous
            else {}
        )
        review_diffs = review_diffs_seen()
        # Misura di qualità del motore embedding (picco medio normalizzato +
        # cosine grezza) e verdetto di fiducia: nel report, così una run può
        # essere riesaminata a posteriori senza rifare l'embedding.
        quality = last_quality()
        if quality:
            sync_notes["quality"] = quality
            sync_notes["weak_signal"] = weak_signal_seen()
        # Il report dei segmenti va salvato SEMPRE, anche senza anomalie: è
        # l'artefatto che rende verificabile a posteriori cosa è stato mostrato,
        # con quale verdetto di contenuto, quale motore è stato scelto e cosa ha
        # segnalato la revisione LLM.
        sync_report = _build_sync_report(
            durations,
            slide_ids,
            total_duration,
            verdicts,
            notes=sync_notes,
            review_diffs=review_diffs,
        )
        _save_sync_report(sync_report)
        if anomalous:
            coherent = sorted(s for s, v in verdicts.items() if v == "coerente")
            misaligned = [
                (s, d) for s, d in anomalous if verdicts.get(s) == "disallineata"
            ]
            uncertain = [
                (s, d) for s, d in anomalous if verdicts.get(s) in (None, "incerto")
            ]
            if coherent:
                log.info(
                    "\n   [Verifica] Durate anomale ma contenuto COERENTE col "
                    "parlato (segmenti realmente lunghi/corti, non errori di "
                    "sync): %s.",
                    ", ".join(f"slide {s}" for s in coherent),
                )
            if misaligned:
                log.warning(
                    "\n   [Avviso] Durate slide molto squilibrate E parlato del "
                    "segmento più simile a un'altra slide (probabile allineamento "
                    "errato): %s.\n"
                    "   Verifica la timeline o rigenera la presentazione dal "
                    "podcast.",
                    ", ".join(f"slide {s} = {d:.0f}s" for s, d in misaligned),
                )
                # Il verdetto non deve restare un avviso ignorato: con
                # --strict-sync si interrompe PRIMA di generare un video con la
                # slide sbagliata (era il difetto della run dell'11/09: il
                # pipeline sapeva che la slide 3 era disallineata e produceva
                # comunque il video).
                if args.strict_sync:
                    detail = ", ".join(f"slide {s} ({d:.0f}s)" for s, d in misaligned)
                    _abort(
                        f"Sincronizzazione sospetta: {detail} ha il parlato più "
                        "simile a un'altra slide (probabile allineamento errato). "
                        "Il video NON è stato generato (--strict-sync attivo): "
                        "rivedi la timeline, oppure rimuovi --strict-sync per "
                        "generarlo comunque. Dettagli in .cache/sync_report.json."
                    )
            if uncertain:
                log.warning(
                    "\n   [Avviso] Durate slide molto squilibrate rispetto alla "
                    "mediana (possibile sincronizzazione imprecisa): %s.\n"
                    "   Una slide che dura molto più o molto meno delle altre può "
                    "indicare un allineamento errato: verifica la timeline.",
                    ", ".join(f"slide {s} = {d:.0f}s" for s, d in uncertain),
                )

        # --- Revisione LLM (--llm-review): le discrepanze non vanno perse ---
        # Il secondo passaggio LLM è "advisory" (non modifica la timeline) ma è
        # già pagato: prima finiva solo nei log. Ora sta nel report e, con
        # --strict-sync, blocca la generazione di un video sospetto.
        if review_diffs:
            log.warning(
                "\n   [Verifica] La revisione LLM (--llm-review) ha segnalato %d "
                "chunk con una slide diversa da quella proposta (dettagli in "
                ".cache/sync_report.json).",
                len(review_diffs),
            )
            if args.strict_sync:
                details = ", ".join(
                    f"chunk {d.get('chunk')} -> slide {d.get('slide')}"
                    for d in review_diffs[:5]
                )
                _abort(
                    f"La revisione LLM contesta la mappa chunk->slide ({details}). "
                    "Il video NON è stato generato (--strict-sync attivo): "
                    "controlla .cache/sync_report.json e la timeline, oppure "
                    "rimuovi --strict-sync (o --llm-review) per procedere."
                )

        # --- Filtro slide da non mostrare (--skip-slides) ---
        # Le slide saltate dal podcast (mai annunciate né discusse) restano
        # nel PDF (le ancore 'slide N' del parlato non si spostano), ma nei
        # loro segmenti il video mostra la slide precedente valida: l'audio
        # resta sincronizzato e il video mostra solo le slide coperte.
        # Nel flusso libero le slide non coperte non vengono mostrate
        # comunque, quindi il filtro è solo per i flussi ordinati.
        if flow != "free" and args.skip_slides:
            skip_set = {
                int(x) for x in args.skip_slides.split(",") if x.strip().isdigit()
            }
            skip_set &= set(range(1, total_slides + 1))
            if skip_set:
                replaced: dict[int, int] = {}
                for i, s in enumerate(slide_ids):
                    if s not in skip_set:
                        continue
                    # Slide precedente valida (non saltata): l'ultima mostrata
                    # prima di questa. Se non esiste (slide 1 saltata), la
                    # successiva valida.
                    prev = next((j for j in range(s - 1, 0, -1) if j not in skip_set), None)
                    if prev is None:
                        prev = next(
                            (j for j in range(s + 1, total_slides + 1) if j not in skip_set),
                            s,
                        )
                    slide_ids[i] = prev
                    replaced[s] = prev
                if replaced:
                    # Riallinea i file immagine alla nuova sequenza (nel flusso
                    # ordinato slide_files è ancora la lista completa 1..N).
                    slide_files = [slide_files[s - 1] for s in slide_ids]
                    log.info(
                        "   [Skip] Slide non mostrate: %s (segmenti mostrano la slide precedente valida).",
                        ", ".join(f"{s}->{p}" for s, p in sorted(replaced.items())),
                    )

        # --- Anteprima timeline (--preview) ---
        if args.preview:
            log.info("\n" + "=" * 70)
            log.info(" [ANTEPRIMA TIMELINE]")
            log.info("=" * 70)
            for i, dur in enumerate(durations):
                start = sum(durations[:i])
                end = start + dur
                bar_len = 40
                filled = int(bar_len * dur / total_duration)
                bar = "█" * filled + "░" * (bar_len - filled)
                log.info("   Slide %2d: %6.1fs ─ %6.1fs (%6.1fs) %s", slide_ids[i], start, end, dur, bar)
            log.info("=" * 70)
            log.info("   Durata totale: %.1fs", total_duration)
            log.info("   Usa --dry-run per testare senza video, o rimuovi --preview per generare.")
            return

        # --- Dry-run: fermati qui ---
        if args.dry_run:
            t_total = time.time() - t_total_start
            _print_timing(
                t_ocr,
                t_transcribe,
                t_sync + beam_ab_seconds,
                embed_seconds(),
                model_load_seconds(),
                0.0,
                t_total,
            )
            _warn_sync_uncertainty()
            _log_plain_summary(
                durations,
                slide_ids,
                total_duration,
                verdicts=verdicts,
                quality=quality,
                review_diffs=len(review_diffs),
                title="TIMELINE PRONTA — COSA CONTERRÀ IL VIDEO",
            )
            log.info("\n" + "=" * 70)
            log.info(" [DRY-RUN] Timeline generata con successo.")
            log.info(" Il video NON è stato creato (--dry-run attivo).")
            log.info("=" * 70)
            return

        # --- Fase 4: Assemblaggio video ---
        t_phase_start = time.time()
        build_video(
            slide_files,
            durations,
            audio_path,
            args.output_video,
            fps=DEFAULT_VIDEO_FPS,
            threads=DEFAULT_VIDEO_THREADS,
            transition_duration=args.transitions,
            engine=args.engine,
        )
        t_video = time.time() - t_phase_start

        # --- Verifica durata output (anti-troncamento) ---
        try:
            with VideoFileClip(str(args.output_video)) as check_clip:
                out_dur = check_clip.duration
            expected = total_duration + DEFAULT_VIDEO_BUFFER_SEC
            if abs(out_dur - expected) > 1.0:
                log.warning(
                    "   ⚠️ Durata video (%.1fs) diversa da audio+buffer (%.1fs): possibili troncamenti.",
                    out_dur,
                    expected,
                )
            else:
                log.info(
                    "   ✅ Verifica durata video OK: %.1fs (audio %.1fs + buffer %.1fs).",
                    out_dur,
                    total_duration,
                    DEFAULT_VIDEO_BUFFER_SEC,
                )
        except (OSError, ValueError, RuntimeError) as e:
            log.warning("   Impossibile verificare il video generato: %s", e)

        # --- Verifica frame vs slide (--verify-video, implicita con --strict-sync) ---
        # È l'unico controllo sull'ARTEFATTO: la timeline può essere internamente
        # coerente e il video comunque sbagliato (filtro slide, riallineamenti,
        # immagine non registrata). Ogni segmento viene confrontato con la slide
        # che la timeline dichiara, non con una stima.
        #
        # Se il controllo trova un confine sbagliato la pipeline PROVA a
        # ripararlo da sola (--no-auto-repair per disattivare) e rigenera il
        # video: un controllo che scopre il problema ma non fa nulla lascia
        # l'utente con un video sbagliato in mano.
        repairs: list[dict[str, object]] = []
        if args.verify_video:
            frame_check = _frame_check_video(
                args.output_video, slide_ids, durations, all_slide_files
            )
            mismatches = cast("list[dict[str, object]]", frame_check["mismatches"])
            if mismatches and args.auto_repair:
                repaired = _repair_durations_from_frames(
                    durations,
                    slide_ids,
                    mismatches,
                    words_raw,
                    slide_texts,
                    total_duration,
                    SemanticOptions(
                        model_name=args.semantic_model,
                        cache_dir=args.semantic_cache_dir,
                        min_slide_duration=args.semantic_min_duration,
                    ),
                )
                if repaired is not None:
                    durations, repairs = repaired
                    log.info(
                        "\n   [Riparazione] Rigenero il video con i confini corretti "
                        "(nuovi inizi: %s)...",
                        ", ".join(
                            f"slide {r['slide']} -> {float(cast('float', r['new_start'])):.1f}s"
                            for r in repairs
                        ),
                    )
                    build_video(
                        slide_files,
                        durations,
                        audio_path,
                        args.output_video,
                        fps=DEFAULT_VIDEO_FPS,
                        threads=DEFAULT_VIDEO_THREADS,
                        transition_duration=args.transitions,
                        engine=args.engine,
                    )
                    t_video = time.time() - t_phase_start
                    # I frame vecchi descrivono il video precedente: la cartella
                    # citata nei log e nel riepilogo deve contenere solo quelli
                    # dell'artefatto attuale.
                    for stale in (CACHE_DIR / "verify_frames").glob("seg*.png"):
                        with suppress(OSError):
                            stale.unlink()
                    # L'artefatto di verifica (letto da analysis_sync.py) deve
                    # descrivere il video NUOVO, non la timeline pre-riparazione.
                    # Con slide ripetute (flusso libero) la mappa slide->start
                    # non è rappresentabile: lì resta la timeline originale.
                    if len(set(slide_ids)) == len(slide_ids):
                        repaired_timeline: dict[int, float] = {}
                        t_cursor = 0.0
                        for slide, d in zip(slide_ids, durations, strict=True):
                            repaired_timeline[int(slide)] = t_cursor
                            t_cursor += float(d)
                        _save_final_timeline(repaired_timeline, total_slides, total_duration)
                    frame_check = _frame_check_video(
                        args.output_video, slide_ids, durations, all_slide_files
                    )
                    mismatches = cast("list[dict[str, object]]", frame_check["mismatches"])
            sync_report["frame_check"] = frame_check
            if repairs:
                sync_report["repairs"] = repairs
            _save_sync_report(sync_report)
            if mismatches and args.strict_sync:
                _abort(
                    f"Verifica frame fallita: {len(mismatches)} segmenti mostrano "
                    "una slide diversa da quella prevista dalla timeline. Il video "
                    "È stato generato ma non è affidabile: controlla "
                    ".cache/sync_report.json e i frame in .cache/verify_frames/."
                )

        # --- Riepilogo finale ---
        t_total = time.time() - t_total_start
        _print_timing(
            t_ocr,
            t_transcribe,
            t_sync + beam_ab_seconds,
            embed_seconds(),
            model_load_seconds(),
            t_video,
            t_total,
        )
        _warn_sync_uncertainty()

        # Pulizia cache orfana
        cleaned = _clean_orphan_cache(active_cache_keys)
        if cleaned:
            log.info("🧹 Puliti %d file cache orfani.", cleaned)

        # Riepilogo in parole semplici: è l'ULTIMA cosa che l'utente legge,
        # così le informazioni pratiche (cosa c'è nel video, cosa dubitare) non
        # vanno cercate dentro i log tecnici.
        _log_plain_summary(
            durations,
            slide_ids,
            total_duration,
            verdicts=verdicts,
            frame_check=cast(
                "dict[str, object] | None", sync_report.get("frame_check")
            ),
            quality=quality,
            review_diffs=len(review_diffs),
            repairs=repairs,
        )

    finally:
        # Cleanup garantito dell'audio_clip
        if audio_clip is not None:
            audio_clip.close()
            log.debug("   audio_clip cleanup eseguito.")


# =====================================================================
# USCITA PULITA (protezione anti-zombie)
# =====================================================================
def _force_clean_exit() -> None:
    """Forza la terminazione del processo se thread residui ne bloccano l'uscita.

    Osservato in produzione: una run è rimasta appesa dopo "[COMPLETATO]",
    bruciando CPU per decine di minuti. Le librerie usate lungo la pipeline
    (moviepy, onnxruntime, client HTTP) possono lasciare thread non-daemon
    vivi: Python attende TUTTI i thread non-daemon prima di terminare, quindi
    il processo resta appeso anche se main() è già ritornato. Qui logghiamo i
    colpevoli (per la diagnosi) e, solo in quel caso, forziamo l'uscita dopo
    il flush dei log. Se non ci sono thread residui, l'uscita normale segue
    il suo corso (atexit inclusi).
    """
    lingering = [
        t.name
        for t in threading.enumerate()
        if t is not threading.current_thread() and not t.daemon and t.is_alive()
    ]
    if not lingering:
        return
    log.info(
        "   Thread residui a fine run (%s): forzo l'uscita pulita.",
        ", ".join(lingering),
    )
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
    _force_clean_exit()
