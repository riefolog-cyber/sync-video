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
from pathlib import Path
from typing import NoReturn

from moviepy import AudioFileClip, VideoFileClip

from chunks import Word
from config import (
    BASE_DIR,
    CACHE_DIR,
    DEFAULT_VIDEO_BUFFER_SEC,
    DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_THREADS,
    STOPWORDS_ITA,
    atomic_write_text,
    bootstrap,
    log,
    parse_args,
)
from llm_sync import (
    endpoints_for,
    is_interactive,
    llm_cache_keys_for,
    llm_ordered_timeline,
    llm_timeline_segments,
    llm_verify_anchor_mapping,
)
from machine_setup import machine_setup
from ocr import PRESENTATION_SUFFIXES, convert_presentation_to_pdf, extract_slides_text_ocr
from semantic_sync import (
    SemanticOptions,
    free_order_segments_from_words,
    make_anchor_remap_filter,
    merge_short_segments,
    model_load_seconds,
    refine_llm_segments_from_words,
    refine_llm_timeline_from_words,
    semantic_timeline_from_words,
    verify_anchor_mapping_embedding,
    weak_signal_seen,
)
from timeline import (
    detect_flow_from_words,
    extract_slide_anchors,
    extract_slide_one_references,
    reconcile_timeline,
)
from transcription import correct_transcript_names, transcribe_audio
from updates import run_update_check
from video import build_video


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
# (updates_check = TTL del controllo PyPI, fastembed_ab = report test A/B).
_KEEP_CACHE_STEMS = frozenset({"machine_setup", "updates_check", "fastembed_ab"})

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
    Conserva SOLO gli stem in ``keep_stems`` (le chiavi della run corrente e la
    timeline finale per la verifica post-run) e rimuove il resto.
    """
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for cache_file in CACHE_DIR.glob("llm_*.json"):
        if cache_file.stem in keep_stems:
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
    t_ocr: float, t_transcribe: float, t_sync: float, t_embed: float, t_video: float, t_total: float
) -> None:
    """Stampa il riepilogo dei tempi di ogni fase e lo salva nello storico."""
    log.info("\n" + "─" * 50)
    log.info(" ⏱️  RIEPILOGO TEMPI")
    log.info("─" * 50)
    log.info("   OCR / Slide   │ %s", _format_time(t_ocr))
    log.info("   Trascrizione  │ %s", _format_time(t_transcribe))
    log.info("   Sincronizzaz. │ %s", _format_time(t_sync))
    if t_embed > 0:
        log.info("     └ Embedding │ %s", _format_time(t_embed))
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
    cache_key_slides = f"slides_{pdf_hash[:12]}_{args.dpi}_{args.lang}"
    # Il modello E il motore fanno parte della chiave: cambiando motore o
    # --whisper-model non deve riusarsi la cache di un altro, che produce
    # timestamp/token diversi.
    cache_key_transcript = f"transcript_{audio_hash[:12]}_{args.lang}_{args.whisper_model}_{args.transcriber}"
    active_cache_keys: set[str] = {cache_key_slides, cache_key_transcript}

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

        # --- Check preventivo ancore: avviso PRIMA della sincronizzazione se il
        # podcast ha annunciato poche slide (probabile deriva del prompt
        # NotebookLM). Con poche ancore la timeline sarà stimata (9Router o
        # fallback MiniLM) e le slide non annunciate avranno durate brevi o
        # micro-segmenti: conviene rigenerare l'audio. ---
        if flow != "free" and words_raw:
            early_anchors = extract_slide_anchors(words_raw, total_slides, flow)
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
            semantic_anchors = extract_slide_anchors(words_raw, total_slides, flow)
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

            # --- Flusso IBRIDO (ordinato + LLM) ---
            # Le ancore deterministiche sono vincoli ESATTI e inviolabili. Se
            # restano slide senza ancora esplicita (mai nominate o narrate fuori
            # posizione), il MiniLM le allinea per similarità e può inventare
            # durate (es. contenuto slide 3 a 100s ma slide 2 mai narrata). Con
            # --llm != off un LLM posiziona SOLO quelle slide, leggendo dove il
            # loro contenuto viene discusso. Fallback automatico: MiniLM.
            timeline: dict[int, float] | None = None
            llm_hybrid_attempted = False
            if args.llm != "off" and semantic_anchors and len(semantic_anchors) < total_slides - 1:
                llm_hybrid_attempted = True
                endpoints = endpoints_for(args.llm)
                if args.llm_model:
                    for ep in endpoints:
                        ep["model"] = args.llm_model
                log.info(
                    "   Flusso ibrido: ancore esatte + LLM per le %d slide senza ancora (--llm %s)...",
                    (total_slides - 1) - len(semantic_anchors),
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
        if anomalous:
            verdicts = _validate_anomalous_segments(
                anomalous, slide_texts, words_raw, durations, slide_ids
            )
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
            if uncertain:
                log.warning(
                    "\n   [Avviso] Durate slide molto squilibrate rispetto alla "
                    "mediana (possibile sincronizzazione imprecisa): %s.\n"
                    "   Una slide che dura molto più o molto meno delle altre può "
                    "indicare un allineamento errato: verifica la timeline.",
                    ", ".join(f"slide {s} = {d:.0f}s" for s, d in uncertain),
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
            _print_timing(t_ocr, t_transcribe, t_sync, model_load_seconds(), 0.0, t_total)
            _warn_sync_uncertainty()
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

        # --- Riepilogo finale ---
        t_total = time.time() - t_total_start
        _print_timing(t_ocr, t_transcribe, t_sync, model_load_seconds(), t_video, t_total)
        _warn_sync_uncertainty()

        # Pulizia cache orfana
        cleaned = _clean_orphan_cache(active_cache_keys)
        if cleaned:
            log.info("🧹 Puliti %d file cache orfani.", cleaned)

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
