#!/usr/bin/env python3
"""
FASE 2 — Trascrizione audio con Vosk + compressione semantica.
Supporta anche faster-whisper come alternativa.
"""

import json
import urllib.request
import zipfile
from pathlib import Path

from pydub import AudioSegment
from vosk import KaldiRecognizer, Model

from chunks import Word
from config import (
    DEFAULT_AUDIO_SAMPLE_RATE,
    DEFAULT_MIN_WORD_LENGTH,
    DEFAULT_TRANSCRIPT_WINDOW,
    DEFAULT_VOSK_CHUNK_BYTES,
    TRANSITION_WORDS_ITA,
    get_stopwords,
    log,
    tqdm,
)

# ---------------------------------------------------------------------------
# faster-whisper opzionale
# ---------------------------------------------------------------------------
try:
    from faster_whisper import WhisperModel
    _HAS_FASTER_WHISPER = True
except ImportError:
    _HAS_FASTER_WHISPER = False


# =====================================================================
# DOWNLOAD MODELLO VOSK (con verifica)
# =====================================================================
def _download_vosk_model(model_name: str, model_path: Path) -> None:
    """
    Scarica ed estrae il modello Vosk se non presente.
    Verifica che l'estrazione abbia prodotto i file attesi.
    """
    log.info("   Download del modello linguistico locale (%s)...", model_name)
    url = f"https://alphacephei.com/vosk/models/{model_name}.zip"
    zip_path = model_path.parent / f"{model_name}.zip"

    try:
        urllib.request.urlretrieve(url, zip_path)
        log.debug("   Download completato: %s", zip_path)
    except Exception as e:
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Download modello Vosk fallito da {url}: {e}"
        ) from e

    # Verifica che lo zip sia valido
    if not zipfile.is_zipfile(str(zip_path)):
        zip_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Il file scaricato non è uno zip valido: {zip_path}"
        )

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(model_path.parent)
        log.debug("   Estrazione completata in: %s", model_path.parent)
    except Exception as e:
        raise RuntimeError(f"Estrazione modello Vosk fallita: {e}") from e
    finally:
        zip_path.unlink(missing_ok=True)

    # Verifica che i file chiave esistano
    required = ["am/final.mdl", "conf/model.conf", "conf/mfcc.conf"]
    missing = [f for f in required if not (model_path / f).exists()]
    if missing:
        raise RuntimeError(
            f"Modello Vosk incompleto — file mancanti: {', '.join(missing)}"
        )

    log.info("   Modello Vosk pronto in: %s", model_path)


# =====================================================================
# CONVERSIONE AUDIO -> WAV
# =====================================================================
def convert_audio_to_wav(
    audio_path: Path,
    output_wav: Path,
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
) -> None:
    """Converte l'audio in WAV mono al sample rate richiesto da Vosk."""
    audio_ext = audio_path.suffix.lower().lstrip(".")
    # Sconosciuti -> None così pydub auto-rileva il formato via ffprobe
    fmt = audio_ext if audio_ext in ("m4a", "mp3", "wav") else None

    log.debug("   Conversione audio: %s -> %s (fmt=%s, rate=%d Hz)",
              audio_path.name, output_wav.name, fmt or "auto", sample_rate)

    try:
        sound = AudioSegment.from_file(str(audio_path), format=fmt)
    except Exception as e:
        raise RuntimeError(
            f"Impossibile leggere il file audio '{audio_path}': {e}"
        ) from e

    if len(sound) < 1000:  # meno di 1 secondo
        raise RuntimeError(
            f"Audio troppo corto ({len(sound)} ms). Verifica il file: {audio_path}"
        )

    sound = sound.set_frame_rate(sample_rate).set_channels(1)
    sound.export(str(output_wav), format="wav")
    log.debug("   WAV creato: %s (%.1f sec)", output_wav, len(sound) / 1000)


# =====================================================================
# TRASCRIZIONE CON VOSK
# =====================================================================
def _transcribe_wav(
    wav_path: Path,
    model: Model,
    sample_rate: int,
    chunk_bytes: int,
) -> list[Word]:
    """Esegue il riconoscimento Vosk e restituisce la lista di parole con timestamp."""
    rec = KaldiRecognizer(model, sample_rate)
    rec.SetWords(True)
    all_words: list[Word] = []

    file_size = wav_path.stat().st_size
    with open(wav_path, "rb") as f:
        pbar = tqdm(total=file_size, desc="Trascrizione Vosk", unit="B",
                      unit_scale=True)
        while True:
            data = f.read(chunk_bytes)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                res = json.loads(rec.Result())
                if "result" in res:
                    all_words.extend(res["result"])
            pbar.update(len(data))
        pbar.close()

    # Ultimo segmento
    res_final = json.loads(rec.FinalResult())
    if "result" in res_final:
        all_words.extend(res_final["result"])

    return all_words


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
        if (not is_transition
                and (word_text in stopwords
                     or (len(word_text) < min_word_len and not word_text.isdigit()))):
            continue

        if time_word - current_chunk_start > window:
            if current_chunk:
                # Fix B: timestamp con precisione al decimo (era .0f = intero)
                blocks.append(
                    f"[{current_chunk_start:.1f}s]: {' '.join(current_chunk)}"
                )
            current_chunk = [word_text]
            current_chunk_start = time_word
        else:
            current_chunk.append(word_text)

    # Ultimo blocco
    if current_chunk:
        blocks.append(
            f"[{current_chunk_start:.1f}s]: {' '.join(current_chunk)}"
        )

    return "\n".join(blocks)


def _write_raw_transcript(words: list[Word], audio_path: Path) -> Path:
    """Salva la trascrizione RAW ('parola [X.Xs]' per riga) per debug, accanto
    all'audio. Condivisa da Vosk e Whisper (stesso formato consumato da
    ``main._parse_transcript_raw`` come fallback per le parole raw)."""
    raw_path = audio_path.parent / "transcript_raw.txt"
    raw_lines = [f"{w['word']} [{w['start']:.1f}s]" for w in words]
    raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
    log.debug("   Trascrizione RAW salvata in: %s", raw_path)
    return raw_path


# =====================================================================
# FUNZIONE PRINCIPALE
# =====================================================================
def generate_compact_transcript(
    audio_path: Path,
    vosk_model_name: str,
    vosk_model_dir: Path,
    lang: str = "ita",
    temp_wav: Path = Path("temp_vosk_audio.wav"),
    sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE,
    chunk_bytes: int = DEFAULT_VOSK_CHUNK_BYTES,
) -> tuple[str, list[Word]]:
    """
    Pipeline completa di trascrizione:
      1. Conversione audio in WAV
      2. Riconoscimento Vosk
      3. Compressione semantica (con preservazione parole di transizione)

    Returns:
        (trascrizione compressa pronta per il prompt LLM,
         lista parole raw Vosk con timestamp precisi)
    """
    log.info("2. Trascrizione ad alta precisione con compressione semantica attiva...")

    # --- Step 1: Conversione audio ---
    convert_audio_to_wav(audio_path, temp_wav, sample_rate)

    # --- Step 2: Carica/modello Vosk ---
    if not vosk_model_dir.exists():
        _download_vosk_model(vosk_model_name, vosk_model_dir)

    model = Model(str(vosk_model_dir))
    log.debug("   Modello Vosk caricato.")

    # --- Step 3: Riconoscimento ---
    words = _transcribe_wav(temp_wav, model, sample_rate, chunk_bytes)
    log.info("   Parole riconosciute: %d", len(words))

    # --- Step 3b: Salva trascrizione RAW per debug ---
    _write_raw_transcript(words, audio_path)

    # --- Step 4: Compressione ---
    stopwords = get_stopwords(lang)
    transcript = _compress_words(words, stopwords)

    # --- Cleanup ---
    temp_wav.unlink(missing_ok=True)

    n_blocks = transcript.count("\n") + 1 if transcript else 0
    log.info("   Blocchi semantici generati: %d", n_blocks)
    return transcript, words


# =====================================================================
# TRASCRIZIONE CON FASTER-WHISPER
# =====================================================================
def transcribe_with_whisper(
    audio_path: Path,
    model_size: str = "medium",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str = "ita",
    beam_size: int = 5,
    vad_filter: bool = True,
    vad_parameters: dict | None = None,
) -> tuple[str, list[Word]]:
    """
    Trascrizione audio con faster-whisper.

    Note:
        `language` usa i codici Tesseract/OCR (es. "ita", "eng"), come il
        resto della pipeline; viene mappato su ISO 639-1 per Whisper.

    Returns:
        (trascrizione compressa, lista parole raw con timestamp)
    """
    if not _HAS_FASTER_WHISPER:
        raise RuntimeError(
            "faster-whisper non è installato. Installa con: pip install faster-whisper"
        )

    log.info("2. Trascrizione con faster-whisper (%s, %s)...", model_size, device)

    # Carica modello
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    log.debug("   Modello Whisper caricato.")

    # Parametri VAD
    vad_params = vad_parameters or {
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 200,
    }

    # Mappa codici lingua Tesseract -> ISO 639-1 per Whisper
    _LANG_MAP = {
        "ita": "it", "eng": "en", "deu": "de", "fra": "fr", "spa": "es",
        "por": "pt", "rus": "ru", "chi_sim": "zh", "jpn": "ja", "kor": "ko",
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
            if hasattr(seg, 'words') and seg.words:
                for w in seg.words:
                    all_words.append({
                        "word": w.word.strip(),
                        "start": w.start,
                    })
            else:
                # Fallback: usa l'intera frase come "parola" con timestamp inizio
                for token in seg.text.strip().split():
                    all_words.append({
                        "word": token,
                        "start": seg.start,
                    })

    log.info("   Parole riconosciute: %d", len(all_words))

    # Genera trascrizione compressa (stesso formato di Vosk)
    _write_raw_transcript(all_words, audio_path)

    # Compressione semantica
    stopwords = get_stopwords(language)
    transcript = _compress_words(all_words, stopwords)

    n_blocks = transcript.count("\n") + 1 if transcript else 0
    log.info("   Blocchi semantici generati: %d", n_blocks)
    return transcript, all_words

