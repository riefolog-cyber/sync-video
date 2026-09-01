#!/usr/bin/env python3
"""
FASE 2 — Trascrizione audio + compressione semantica.

Due motori disponibili:
- OpenVINO GenAI (default, se installato e il modello è presente): usa la
  iGPU/CPU via IR, con word timestamps nativi. Molto più veloce su macchine
  Intel senza GPU NVIDIA.
- faster-whisper (fallback): CTranslate2 su CPU.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, cast

from chunks import Word
from config import (
    DEFAULT_MIN_WORD_LENGTH,
    DEFAULT_OPENVINO_MODEL_DIR,
    DEFAULT_OPENVINO_MODEL_ID,
    DEFAULT_TRANSCRIPT_WINDOW,
    DEFAULT_WHISPER_BEAM,
    TRANSITION_WORDS_ITA,
    get_stopwords,
    log,
)
from machine_setup import MACHINE_CONFIG_PATH, openvino_gpu_available


# =====================================================================
# COMPRESSIONE TRASCRIZIONE
# =====================================================================
def _compress_words(
    words: list[Word],
    stopwords: frozenset,
    window: float = DEFAULT_TRANSCRIPT_WINDOW,
    min_word_len: int = DEFAULT_MIN_WORD_LENGTH,
    transition_words: frozenset = TRANSITION_WORDS_ITA,
) -> str:
    """
    Comprime le parole riconosciute:
    - rimuove stopwords (MA NON le parole di transizione!)
    - scarta parole troppo corte (MA NON le parole di transizione!)
    - raggruppa in finestre temporali di `window` secondi
    """
    blocks: list[str] = []
    current_chunk: list[str] = []
    current_chunk_start = 0.0

    for w in words:
        word_text = w["word"].lower()
        time_word = w["start"]

        # Le parole di transizione NON vengono mai filtrate
        is_transition = word_text in transition_words

        # Non filtrare i numeri ("1", "2", "3"): sono cruciali per il LLM
        if not is_transition and (
            word_text in stopwords or (len(word_text) < min_word_len and not word_text.isdigit())
        ):
            continue

        if time_word - current_chunk_start > window:
            if current_chunk:
                # Fix B: timestamp con precisione al decimo (era .0f = intero)
                blocks.append(f"[{current_chunk_start:.1f}s]: {' '.join(current_chunk)}")
            current_chunk = [word_text]
            current_chunk_start = time_word
        else:
            current_chunk.append(word_text)

    # Ultimo blocco
    if current_chunk:
        blocks.append(f"[{current_chunk_start:.1f}s]: {' '.join(current_chunk)}")

    return "\n".join(blocks)


def _write_raw_transcript(words: list[Word], audio_path: Path) -> Path:
    """Salva la trascrizione RAW ('parola [X.Xs]' per riga) per debug, accanto
    all'audio. Consumata da ``main._parse_transcript_raw`` come fallback per
    le parole raw."""
    raw_path = audio_path.parent / "transcript_raw.txt"
    raw_lines = [f"{w['word']} [{w['start']:.1f}s]" for w in words]
    raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
    log.debug("   Trascrizione RAW salvata in: %s", raw_path)
    return raw_path


# =====================================================================
# CORREZIONE NOMI PROPRI
# =====================================================================
# Nomi propri che l'ASR riconosce sistematicamente male e che sono indizi
# chiave per il matching slide<->parlato (es. "sigmond freud" invece di
# "sigmund freud"). Ogni voce è una tupla (sequenza di parole attese in
# minuscolo, termine corretto): le voci multi-parola sostituiscono l'intera
# frase, conservando l'intervallo temporale del gruppo.
_NAME_CORRECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("sigmond",), "sigmund"),           # Sigmund Freud (psicoanalisi)
    (("tommasso",), "tommaso"),          # Tommaso Moro / Campanella
    (("thomas", "mur"), "thomas more"),  # Thomas More (Utopia, 1516)
    (("on", "lock"), "locke"),           # John Locke (tolleranza)
    (("kep", "curo"), "epicuro"),        # Epicuro (atarassia)
    (("mark", "chiuse"), "marcuse"),     # Herbert Marcuse (prestazione)
)


def _norm_word(word: str) -> str:
    """Normalizza una parola per il confronto: minuscolo, senza punteggiatura."""
    return re.sub(r"[^a-zàèéìòù']", "", word.lower().strip())


def correct_transcript_names(words: list[Word]) -> list[Word]:
    """Corregge i nomi propri sistematicamente storpiato dall'ASR.

    Applica ``_NAME_CORRECTIONS`` alle parole raw con timestamp: il termine
    corretto sostituisce la prima parola della frase riconosciuta male e le
    successive vengono scartate (l'intervallo temporale complessivo del gruppo
    viene conservato: inizio della prima, fine dell'ultima). I nomi corretti
    sono indizi cruciali per il matching slide<->parlato del flusso
    LLM/semantico (es. "mark chiuse" -> "marcuse" permette di agganciare la
    slide della critica alla prestazione).

    Returns:
        Nuova lista di parole con i nomi corretti (l'originale è intatta).
    """
    if not words:
        return words
    out: list[Word] = []
    i = 0
    n = len(words)
    fixed = 0
    while i < n:
        matched = False
        for phrase, correct in _NAME_CORRECTIONS:
            if i + len(phrase) > n:
                continue
            if all(_norm_word(words[i + k]["word"]) == phrase[k] for k in range(len(phrase))):
                first = dict(words[i])
                last = dict(words[i + len(phrase) - 1])
                merged: dict[str, Any] = dict(first)
                merged["word"] = correct
                # Conserva l'intervallo temporale del gruppo (fine dell'ultima parola)
                first_start = cast(float, first.get("start", 0.0))
                merged["end"] = last.get("end", last.get("start", first_start))
                out.append(cast(Word, merged))
                i += len(phrase)
                fixed += 1
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    if fixed:
        log.info("   [Trascrizione] %d nome/i proprio/i corretto/i (dizionario).", fixed)
    return out


# =====================================================================
# TRASCRIZIONE CON OPENVINO GENAI (iGPU/CPU, più veloce su Intel)
# =====================================================================
def download_openvino_model(model_dir: Path, model_id: str = DEFAULT_OPENVINO_MODEL_ID) -> Path:
    """Scarica il modello Whisper OpenVINO IR da HuggingFace (una tantum)."""
    if model_dir.exists():
        return model_dir
    from huggingface_hub import snapshot_download

    model_dir.mkdir(parents=True, exist_ok=True)
    log.info("   ⏳ Download modello OpenVINO %s (una tantum)...", model_id)
    snapshot_download(model_id, local_dir=str(model_dir))
    log.info("   ✅ Modello OpenVINO pronto in: %s", model_dir)
    return model_dir


def _load_audio_16k(audio_path: Path) -> tuple[Any, int]:
    """Carica l'audio come float32 mono 16kHz (formato richiesto da Whisper).

    Returns:
        (campioni float32 in [-1, 1], sample rate)
    """
    from pydub import AudioSegment

    seg = AudioSegment.from_file(str(audio_path))
    seg = seg.set_frame_rate(16000).set_channels(1)
    samples = seg.get_array_of_samples()
    import numpy as np

    return (np.array(samples, dtype=np.float32) / 32768.0), 16000


def _ov_lang_token(language: str) -> str:
    """Mappa codice lingua Tesseract/OCR (es. 'ita') al token OpenVINO ('<|it|>')."""
    _MAP = {
        "ita": "it",
        "eng": "en",
        "deu": "de",
        "fra": "fr",
        "spa": "es",
        "por": "pt",
        "rus": "ru",
        "chi_sim": "zh",
        "jpn": "ja",
        "kor": "ko",
    }
    return f"<|{_MAP.get(language, language)}|>"


def transcribe_with_openvino(
    audio_path: Path,
    model_dir: Path,
    language: str = "ita",
    device: str = "GPU",
) -> tuple[str, list[Word]]:
    """
    Trascrizione audio con OpenVINO GenAI (ASRPipeline, word timestamps).

    Usa la iGPU Intel (device 'GPU') o la CPU ('CPU'). Richiede il modello
    in formato OpenVINO IR (es. scaricato da `OpenVINO/whisper-small-fp16-ov`).

    Returns:
        (trascrizione compressa, lista parole raw con timestamp)
    """
    import openvino_genai as og

    log.info("2. Trascrizione con OpenVINO GenAI (%s, device %s)...", model_dir.name, device)

    audio, _ = _load_audio_16k(audio_path)
    pipe = og.ASRPipeline(str(model_dir), device, word_timestamps=True)
    log.debug("   Pipeline OpenVINO caricata.")

    res = pipe.generate(
        audio,
        language=_ov_lang_token(language),
        task="transcribe",
        word_timestamps=True,
    )

    all_words: list[Word] = []
    if res.words:
        for w in res.words[0]:
            text = (w.text or "").strip()
            if text:
                all_words.append({"word": text, "start": float(w.start_ts)})

    log.info("   Parole riconosciute: %d", len(all_words))

    _write_raw_transcript(all_words, audio_path)

    stopwords = get_stopwords(language)
    transcript = _compress_words(all_words, stopwords)

    n_blocks = transcript.count("\n") + 1 if transcript else 0
    log.info("   Blocchi semantici generati: %d", n_blocks)
    return transcript, all_words


# =====================================================================
# TRASCRIZIONE CON FASTER-WHISPER
# =====================================================================
def openvino_usable() -> bool:
    """True se su questo PC OpenVINO è una via realmente percorribile.

    Il suggerimento "installa openvino-genai per usare la iGPU" ha senso solo
    se il rilevamento hardware (``machine_setup.json``) ha scelto OpenVINO
    (iGPU Intel presente), oppure se il runtime OpenVINO è installato ed
    espone un device GPU reale. La sola CPU non basta: senza iGPU non c'è
    alcun guadagno di velocità, quindi su macchine AMD/ARM (dove OpenVINO
    vede al più la CPU) l'avviso viene soppresso.
    """
    try:
        rec = json.loads(MACHINE_CONFIG_PATH.read_text(encoding="utf-8"))
        transcriber = rec.get("transcriber")
        if transcriber == "openvino":
            return True
        if transcriber == "whisper":
            return False
    except Exception:
        pass  # nessun machine_setup.json: si procede col probe runtime

    # Solo una iGPU Intel (device "GPU") giustifica il consiglio "usa la
    # iGPU": la CPU OpenVINO non è più veloce di faster-whisper. Il probe
    # è condiviso con machine_setup.openvino_gpu_available().
    return openvino_gpu_available()


def transcribe_audio(
    audio_path: Path,
    language: str = "ita",
    model_size: str = "small",
    transcriber: str = "auto",
    openvino_model_dir: Path | None = None,
    openvino_device: str = "GPU",
    whisper_device: str = "cpu",
    whisper_compute_type: str = "int8",
    whisper_beam: int = DEFAULT_WHISPER_BEAM,
) -> tuple[str, list[Word]]:
    """
    Dispatcher trascrizione: sceglie il motore più veloce disponibile.

    - ``auto``: OpenVINO GenAI se installato e il modello IR è presente,
      altrimenti faster-whisper.
    - ``openvino``: solo OpenVINO (errore se manca).
    - ``whisper``: solo faster-whisper.

    Returns:
        (trascrizione compressa, lista parole raw con timestamp)
    """
    if transcriber in ("auto", "openvino"):
        try:
            import openvino_genai  # noqa: F401
        except ImportError:
            if transcriber == "openvino":
                raise RuntimeError(
                    "openvino-genai non installato: impossibile usare --transcriber openvino."
                ) from None
        else:
            model_dir = openvino_model_dir
            if model_dir is None or not model_dir.exists():
                if transcriber == "openvino":
                    model_dir = download_openvino_model(
                        Path(DEFAULT_OPENVINO_MODEL_DIR) if model_dir is None else model_dir
                    )
                else:
                    log.warning(
                        "   ⚠️  Modello OpenVINO non trovato in %s, uso faster-whisper. "
                        "Scaricalo una tantum con `python main.py --openvino-download` "
                        "per usare la iGPU (~1.5x più veloce).",
                        openvino_model_dir,
                    )
            if model_dir is not None and model_dir.exists():
                return transcribe_with_openvino(
                    audio_path,
                    model_dir=model_dir,
                    language=language,
                    device=openvino_device,
                )

    return transcribe_with_whisper(
        audio_path,
        model_size=model_size,
        language=language,
        device=whisper_device,
        compute_type=whisper_compute_type,
        beam_size=whisper_beam,
    )


def transcribe_with_whisper(
    audio_path: Path,
    model_size: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str = "ita",
    beam_size: int = DEFAULT_WHISPER_BEAM,
    vad_filter: bool = True,
    vad_parameters: dict | None = None,
    openvino_available: bool | None = None,
    cpu_threads: int | None = None,
) -> tuple[str, list[Word]]:
    """
    Trascrizione audio con faster-whisper.

    Note:
        `language` usa i codici Tesseract/OCR (es. "ita", "eng"), come il
        resto della pipeline; viene mappato su ISO 639-1 per Whisper.

    Returns:
        (trascrizione compressa, lista parole raw con timestamp)
    """
    from faster_whisper import WhisperModel

    log.info("2. Trascrizione con faster-whisper (%s, %s)...", model_size, device)
    if openvino_available is None:
        openvino_available = openvino_usable()
    if openvino_available:
        log.warning(
            "   ⚠️  faster-whisper su CPU è LENTO: ~%d min per 28 min di audio. "
            "Installando openvino-genai + il modello OpenVINO (vedi README) la "
            "trascrizione usa la iGPU (~1.5x più veloce).",
            {  # stima empirica (RTF su CPU Intel)
                "tiny": 2,
                "base": 4,
                "small": 8,
                "medium": 14,
                "large": 25,
            }.get(model_size, 8),
        )

    # Carica modello. cpu_threads esplicito: il default di faster-whisper
    # sottoutilizza CPU con più core (misurato su Snapdragon X Elite: 8
    # thread ~27% più veloci di 4 su clip da 60s). Cap a 8 per non saturare.
    n_threads = cpu_threads if cpu_threads else min(os.cpu_count() or 4, 8)
    model = WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
        cpu_threads=n_threads,
    )
    log.debug("   Modello Whisper caricato.")

    # Parametri VAD
    vad_params = vad_parameters or {
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 200,
    }

    # Mappa codici lingua Tesseract -> ISO 639-1 per Whisper
    _LANG_MAP = {
        "ita": "it",
        "eng": "en",
        "deu": "de",
        "fra": "fr",
        "spa": "es",
        "por": "pt",
        "rus": "ru",
        "chi_sim": "zh",
        "jpn": "ja",
        "kor": "ko",
    }
    whisper_lang = _LANG_MAP.get(language, language)

    # Trascrizione
    segments, info = model.transcribe(
        str(audio_path),
        language=whisper_lang,
        beam_size=beam_size,
        word_timestamps=True,
        vad_filter=vad_filter,
        vad_parameters=vad_params,
    )

    log.info("   Rilevata lingua: %s (probabilità %.2f)", info.language, info.language_probability)

    # Accumula parole con timestamp
    all_words: list[Word] = []
    for seg in segments:
        if seg.text:
            # Ogni segmento ha: seg.start, seg.end, seg.text
            # Per avere word-level timestamps, usiamo la funzione interna
            # che restituisce le parole se available
            if hasattr(seg, "words") and seg.words:
                for w in seg.words:
                    all_words.append(
                        {
                            "word": w.word.strip(),
                            "start": w.start,
                        }
                    )
            else:
                # Fallback: usa l'intera frase come "parola" con timestamp inizio
                for token in seg.text.strip().split():
                    all_words.append(
                        {
                            "word": token,
                            "start": seg.start,
                        }
                    )

    log.info("   Parole riconosciute: %d", len(all_words))

    # Genera trascrizione compressa
    _write_raw_transcript(all_words, audio_path)

    # Compressione semantica
    stopwords = get_stopwords(language)
    transcript = _compress_words(all_words, stopwords)

    n_blocks = transcript.count("\n") + 1 if transcript else 0
    log.info("   Blocchi semantici generati: %d", n_blocks)
    return transcript, all_words

