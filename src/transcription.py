#!/usr/bin/env python3
"""
FASE 2 — Trascrizione audio + compressione semantica.

Due motori disponibili:
- OpenVINO GenAI (default, se installato e il modello è presente): usa la
  iGPU/CPU via IR, con word timestamps nativi. Molto più veloce su macchine
  Intel senza GPU NVIDIA.
- faster-whisper (fallback): CTranslate2 su CPU.
"""

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from chunks import Word
from config import (
    CACHE_DIR,
    CUDA_LIBS_REMEDY,
    DEFAULT_MIN_WORD_LENGTH,
    DEFAULT_OPENVINO_MODEL_DIR,
    DEFAULT_OPENVINO_MODEL_ID,
    DEFAULT_TRANSCRIPT_WINDOW,
    DEFAULT_WHISPER_BATCH,
    DEFAULT_WHISPER_BEAM,
    DEFAULT_WHISPER_THREADS,
    OPENVINO_MODEL_REVISIONI,
    TRANSITION_WORDS_ITA,
    atomic_write_text,
    cuda_runtime_lib_mancante,
    get_stopwords,
    log,
    openvino_device_available,
    openvino_model_complete,
)
from hardware import ram_available_bytes

# Mappa codici lingua Tesseract/OCR -> ISO 639-1 per Whisper. Condivisa tra la
# trascrizione principale e la ritrascrizione delle zone cieche: duplicarla
# faceva si' che la recovery passasse codici a tre lettere ("eng", "deu",
# "fra") a faster-whisper, che ne vuole due.
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


def _whisper_lang(language: str) -> str:
    """Codice lingua per Whisper partendo dal codice OCR (Tesseract)."""
    return _LANG_MAP.get(language, language)


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

        # Non filtrare i numeri ("1", "2", "3"): sono cruciali per le ancore
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


def raw_transcript_path(audio_path: Path) -> Path:
    """Percorso del transcript RAW, nella cache e derivato dall'audio.

    Il nome non è più fisso accanto all'audio (``transcript_raw.txt``): due
    podcast nella stessa cartella si sovrascrivevano a vicenda e il fallback
    per mtime di ``main.py`` poteva leggere le parole di uno come se fossero
    dell'altro. La chiave è il percorso del file, non il contenuto: qui serve
    distinguere i file, non firmarli (l'hash del contenuto è già la chiave
    della trascrizione vera).

    Vive in ``CACHE_DIR`` perché è diagnostica/fallback, non un input
    dell'utente: la pulizia delle cache non lo tocca (guarda i ``.json``) e
    ``--clean-cache`` lo rimuove insieme al resto della cache.
    """
    try:
        ident = str(audio_path.resolve())
    except OSError:
        ident = str(audio_path.absolute())
    key = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:12]
    return CACHE_DIR / f"transcript_raw_{key}.txt"


def _write_raw_transcript(words: list[Word], audio_path: Path) -> Path | None:
    """Salva la trascrizione RAW ('parola [X.Xs]' per riga) nella cache, per debug.

    Consumata da ``main._parse_transcript_raw`` come fallback per le parole
    raw. Non è un input della pipeline: se la scrittura fallisce (disco pieno,
    cartella non scrivibile, share di rete) si prosegue, perché il risultato
    della trascrizione è già in memoria e perderlo per un file di diagnostica
    sarebbe assurdo.
    """
    raw_path = raw_transcript_path(audio_path)
    raw_lines = [f"{w['word']} [{w['start']:.1f}s]" for w in words]
    try:
        atomic_write_text(raw_path, "\n".join(raw_lines))
    except OSError as e:
        log.debug("   Trascrizione RAW non salvata (%s): %s", raw_path, e)
        return None
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


def _distanza_le_1(a: str, b: str) -> bool:
    """True se le due parole differiscono di UNA sola lettera.

    Solo il caso che serve: Whisper storpiando un nome proprio sbaglia
    una lettera ("sigmond" per "sigmund"), non due. Confronto fatto a mano
    invece che con Levenshtein: una parola sola, e quattro casi.
    """
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        differenze = sum(1 for x, y in zip(a, b, strict=True) if x != y)
        return differenze == 1
    # Una parola piu' corta: si prova a togliere un carattere dalla piu' lunga.
    corta, lunga = (a, b) if len(a) < len(b) else (b, a)
    return any(lunga[:i] + lunga[i + 1 :] == corta for i in range(len(lunga)))


def vocabolario_del_deck(slide_texts: Sequence[str], min_occorrenze: int = 2) -> dict[str, int]:
    """Nomi propri del deck, con quante volte compaiono.

    Sono le parole **iniziali maiuscole** dell'OCR che ricorrono almeno due
    volte: e' il segnale che sono nomi ("Freud", "Moro") e non l'inizio di una
    frase ("Il", "Per"). Una maiuscola che compare una volta sola non entra:
    il deck la puo' aver messa li' per caso.

    Il vocabolario serve a correggere la trascrizione: l'ASR storpia i nomi
    e il motore di confronto lavora sulle parole, quindi piu' nomi corretti =
    slide agganciate meglio.
    """
    conteggi: dict[str, int] = {}
    for testo in slide_texts:
        for parola in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ']+", str(testo)):
            if not parola[0].isupper():
                continue
            chiave = _norm_word(parola)
            if len(chiave) < 4:
                continue
            conteggi[chiave] = conteggi.get(chiave, 0) + 1
    return {k: v for k, v in conteggi.items() if v >= min_occorrenze}


def correct_transcript_names(
    words: list[Word],
    vocabolario: dict[str, int] | None = None,
    correzioni: list[str] | None = None,
) -> list[Word]:
    """Corregge i nomi propri sistematicamente storpiato dall'ASR.

    Due fonti, in quest'ordine:

    1. il dizionario noto (``_NAME_CORRECTIONS``): nomi che l'ASR sbaglia
       sempre, gia' corretti a mano;
    2. il **vocabolario del deck** (se fornito): i nomi propri che il deck
       contiene davvero. Una parola della trascrizione che nel deck non c'e'
       e che sta a una lettera di distanza da un nome del deck viene
       corretta: e' il caso "sigmond" -> "sigmund" ma valido per QUALSIASI
       deck, non solo per quello per cui il dizionario e' stato scritto.

    Una parola gia' presente nel deck non viene toccata (e' gia' giusta), e
    una parola a distanza 1 da DUE nomi del deck viene lasciata com'e': e'
    ambigua, e una correzione sbagliata fa scendere la similarita' invece
    di farla salire.

    Applica le correzioni alle parole raw con timestamp: il termine
    corretto sostituisce la prima parola della frase riconosciuta male e le
    successive vengono scartate (l'intervallo temporale complessivo del gruppo
    viene conservato: inizio della prima, fine dell'ultima). I nomi corretti
    sono indizi cruciali per il matching slide<->parlato del flusso
    semantico (es. "mark chiuse" -> "marcuse" permette di agganciare la
    slide della critica alla prestazione).

    Returns:
        Nuova lista di parole con i nomi corretti (l'originale è intatta).
        Se ``correzioni`` e' una lista, viene riempita con le correzioni fatte
        ("da -> a"), cosi' il chiamante puo' metterle nell'artefatto della run.
    """
    if not words:
        return words
    # Indice inverso per il vocabolario: parola trascritta -> nome del deck.
    correzioni_deck: dict[str, str] = {}
    if vocabolario:
        # Costruisce l'indice delle modifiche a 1 carattere UNA volta per
        # vocabolario invece di confrontare ogni parola del parlato con ogni
        # nome del deck (misurato: 2.6s per 20k parole x 200 nomi, e la
        # correzione gira 2-3 volte a run). La forma con wildcard e' la stessa
        # regola di `_distanza_le_1`, solo indicizzata.
        wildcard: dict[str, set[str]] = {}
        cancellazioni: dict[str, set[str]] = {}
        for nome in vocabolario:
            for i in range(len(nome)):
                wildcard.setdefault(nome[:i] + "\0" + nome[i + 1 :], set()).add(nome)
            for i in range(len(nome) + 1):
                cancellazioni.setdefault(nome[:i] + nome[i + 1 :], set()).add(nome)
        for w in words:
            parola = _norm_word(w["word"])
            if len(parola) < 4:
                continue
            # Una parola gia' nel deck e' gia' giusta: non si tocca.
            if parola in vocabolario:
                continue
            vicini: set[str] = set()
            for i in range(len(parola)):
                vicini.update(wildcard.get(parola[:i] + "\0" + parola[i + 1 :], ()))
                corta = parola[:i] + parola[i + 1 :]
                if corta in vocabolario:
                    vicini.add(corta)
            # Candidati di UNA lettera piu' lunghi: la cancellazione di una
            # loro lettera da' esattamente la parola trascritta.
            vicini.update(cancellazioni.get(parola, ()))
            if len(vicini) == 1:
                correzioni_deck.setdefault(parola, next(iter(vicini)))
    out: list[Word] = []
    i = 0
    n = len(words)
    fixed = 0
    correzioni_locali: list[str] = []
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
                correzioni_locali.append(f"{' '.join(phrase)} -> {correct}")
                matched = True
                break
        if not matched:
            parola = _norm_word(words[i]["word"])
            nuovo = correzioni_deck.get(parola)
            if nuovo:
                merged = dict(words[i])
                merged["word"] = nuovo
                out.append(cast(Word, merged))
                fixed += 1
                correzioni_locali.append(f"{parola} -> {nuovo}")
                i += 1
                matched = True
        if not matched:
            out.append(words[i])
            i += 1
    if fixed:
        log.info("   [Trascrizione] %d nome/i proprio/i corretto/i.", fixed)
        log.debug("   [Trascrizione] Correzioni: %s", "; ".join(correzioni_locali[:60]))
    if correzioni is not None:
        correzioni.extend(correzioni_locali)
    return out


# =====================================================================
# TRASCRIZIONE CON OPENVINO GENAI (iGPU/CPU, più veloce su Intel)
# =====================================================================
_OPENVINO_ID_FILE = "openvino_model_id.json"


def openvino_model_id_ok(model_dir: Path, model_id: str) -> bool:
    """True se una cartella IR completa e' proprio del modello richiesto.

    I chiamanti saltano `download_openvino_model` quando la cartella "sembra
    completa": senza questo controllo cambiare `OPENVINO_MODEL_ID` riusava il
    vecchio IR mentre il report e la chiave di cache annunciavano quello nuovo.
    Una cartella senza marcatore e' un IR storico, compatibile col default.
    """
    try:
        grezzo = json.loads((model_dir / _OPENVINO_ID_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(grezzo, dict):
        # Marcatore corrotto: non e' una prova di un ID diverso. Si tratta
        # come "non noto" (cartella storica) invece di far crashare setup,
        # prefetch e trascrizione con AttributeError.
        return True
    salvato = cast("dict[str, object]", grezzo)
    return salvato.get("model_id") == model_id


def download_openvino_model(model_dir: Path, model_id: str = DEFAULT_OPENVINO_MODEL_ID) -> Path:
    """Scarica il modello Whisper OpenVINO IR da HuggingFace (una tantum).

    Il download finisce in una cartella TEMPORANEA accanto alla destinazione e
    diventa visibile solo se il contenuto e' completo: `os.replace` rinomina
    in un secondo. Se la rete salta, la parziale viene cancellata e alla run
    successiva si riprova da zero. Prima si scaricava dentro la cartella finale:
    una snapshot interrotta lasciava un IR a meta' che tutti (qui, in
    machine_setup e in openvino_usable) consideravano buono per sempre.
    """
    if openvino_model_complete(model_dir):
        # `OPENVINO_MODEL_ID` e' una scelta dell'utente: se cambia ID tenendo
        # la stessa cartella, il vecchio IR "sembra completo" e veniva riusato
        # mentre il report annunciava il nuovo modello. Si registra QUALE ID ha
        # prodotto la cartella e si riscarica se non corrisponde.
        try:
            salvato = json.loads((model_dir / _OPENVINO_ID_FILE).read_text(encoding="utf-8"))
            marcatore_presente = True
        except (OSError, ValueError):
            salvato, marcatore_presente = {}, False
        if not isinstance(salvato, dict):
            salvato, marcatore_presente = {}, False
        # Cartella scritta prima che il marcatore esistesse: e' il modello di
        # default, non un ID diverso. Riscaricarla farebbe perdere ore di
        # download a ogni aggiornamento su installazioni gia' funzionanti.
        if not marcatore_presente or salvato.get("model_id") == model_id:
            return model_dir
        log.warning(
            "   ⚠️ Modello OpenVINO in %s non e' %s (e' %s): lo sostituisco.",
            model_dir,
            model_id,
            salvato.get("model_id") or "di origine sconosciuta",
        )
    if model_dir.exists():
        # Parziale o vuota: non la si riusa, neppure come " gia' presente".
        log.warning("   ⚠️ Modello OpenVINO incompleto in %s: lo scarico di nuovo.", model_dir)
        try:
            shutil.rmtree(model_dir)
        except OSError as e:
            raise RuntimeError(f"Impossibile rimuovere il modello OpenVINO incompleto in {model_dir}: {e}") from e
    from huggingface_hub import snapshot_download

    model_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=model_dir.name + ".tmp-", dir=model_dir.parent))
    log.info("   ⏳ Download modello OpenVINO %s (una tantum)...", model_id)
    try:
        # `revision` esplicito: senza, si scarica il "main" di oggi, e se chi
        # mantiene il modello pubblica una versione nuova il PC scarica un
        # modello diverso da quello su cui e' stata misurata la baseline, senza
        # alcun errore. Il caso silenzioso e' il peggiore.
        snapshot_download(
            model_id,
            revision=OPENVINO_MODEL_REVISIONI.get(model_id),
            local_dir=str(tmp),
        )
        if not openvino_model_complete(tmp):
            raise RuntimeError(
                f"Download OpenVINO incompleto in {tmp}: manca config.json o un file IR (.xml/.bin)."
            )
        # L'identita' si scrive DENTRO la temporanea: se os.replace fallisce
        # non resta una cartella "completa" con l'ID sbagliato.
        atomic_write_text(tmp / _OPENVINO_ID_FILE, json.dumps({"model_id": model_id}))
        os.replace(tmp, model_dir)
    except BaseException:
        # Nessuna parziale deve restare vicino alla destinazione: verra'
        # riscaricata alla prossima run invece di essere creduta valida.
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log.info("   ✅ Modello OpenVINO pronto in: %s", model_dir)
    return model_dir


def _load_audio_16k(audio_path: Path) -> tuple[Any, int]:
    """Carica l'audio come float32 mono 16kHz (formato richiesto da Whisper).

    Decodifica in streaming via ffmpeg (f32le) in blocchi da 64 KB, accumulati
    peró in un ``bytearray`` e poi convertiti con ``np.frombuffer``: il PCM a
    16 kHz resta quindi tutto in memoria fino alla fine. Il vantaggio è evitare
    il PCM ORIGINALE (pydub teneva ~2-3 GB di picco su audio lunghi, proprio
    dove ``decide_transcriber`` misura la memoria libera) e la doppia copia
    (``bytes(buf)`` + ``.copy()``) del vecchio percorso. Il risultato è identico
    (float32 in [-1, 1], 16 kHz, mono). Se ffmpeg non è disponibile si ripiega
    su pydub (che carica tutto, ma è il caso raro).

    Nota: il buffer accumulato pesa ~64 KB/s (circa 77 MB per 20 minuti, 460 MB
    per due ore). Non è il PCM sorgente, ma su audio molto lunghi resta un picco
    di memoria rilevante: lo streaming evita la copia, non l'accumulo.

    Returns:
        (campioni float32 in [-1, 1], sample rate)
    """
    import shutil
    import threading

    import numpy as np

    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg:
        cmd = [
            ffmpeg,
            "-v", "error",
            # Senza -nostdin un tasto premuto nella console (una 'q' durante
            # una trascrizione di ore) ferma ffmpeg: esce 0 e il PCM e'
            # troncato, quindi la run continua con un audio piu' corto.
            "-nostdin",
            "-i", str(audio_path),
            "-ac", "1",
            "-ar", "16000",
            "-f", "f32le",
            "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        buf = bytearray()
        stderr_bytes = b""

        def _leggi_stderr() -> None:
            """Drena stderr in parallelo: se riempie il pipe (64 KB), ffmpeg si blocca."""
            nonlocal stderr_bytes
            if proc.stderr is None:
                return
            try:
                stderr_bytes = proc.stderr.read()
            finally:
                proc.stderr.close()

        # stderr va letto MENTRE scorre stdout, non solo nel finally: un ffmpeg
        # che scrive molto su stderr (errori ripetuti) riempie il pipe e si
        # ferma, e la lettura di stdout resta appesa per sempre.
        t_stderr = threading.Thread(target=_leggi_stderr, daemon=True)
        t_stderr.start()
        returncode = 0
        lettura_ok = False
        try:
            try:
                while True:
                    block = proc.stdout.read(65536)
                    if not block:
                        break
                    buf.extend(block)
                lettura_ok = True
            finally:
                # stdout va chiuso SEMPRE: se la lettura del PCM solleva, il
                # processo e il pipe restano aperti.
                proc.stdout.close()
        finally:
            if lettura_ok:
                # EOF su stdout NON vuol dire che ffmpeg sia già uscito: tra
                # la chiusura del pipe e l'exit c'e' una finestra in cui
                # ucciderlo lo fa finire con un codice d'errore, trasformando
                # una decodifica riuscita in "Decodifica audio fallita".
                try:
                    returncode = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    returncode = proc.wait()
            else:
                proc.kill()
                returncode = proc.wait()
            # Join con tetto: un figlio che tiene stderr aperto non deve
            # bloccare la run per sempre.
            t_stderr.join(timeout=10)
            if t_stderr.is_alive():
                log.warning("   [Audio] Lettura stderr non terminata: proseguo senza il log ffmpeg.")
        if returncode != 0:
            raise RuntimeError(
                f"Decodifica audio fallita (ffmpeg exit {returncode}): "
                f"{stderr_bytes.decode('utf-8', errors='replace')[:500]}"
            )
        if not buf:
            raise RuntimeError("Decodifica audio fallita: output vuoto.")
        # Un solo buffer: `bytes(buf)` + `.copy()` facevano DUE copie complete
        # del PCM (circa 240 MB per un podcast di 20 minuti, oltre 1 GB per due
        # ore). `np.frombuffer` sul bytearray non copia e tiene vivo il buffer.
        samples = np.frombuffer(buf, dtype=np.float32)
        return samples, 16000

    # Fallback (ffmpeg assente): pydub, che carica tutto in RAM.
    from pydub import AudioSegment

    seg = AudioSegment.from_file(str(audio_path)).set_frame_rate(16000).set_channels(1)
    if seg.sample_width == 1:
        # pydub restituisce gia' il byte 8 bit CENTRATO (0x80 -> 0): 128 e' il
        # centro del file WAV, non del suo array. Sottrarre 128 (o passare per
        # set_sample_width(2), che mappa 0x80 a -32768) faceva diventare il
        # silenzio un tono a -1.0.
        samples = np.array(seg.get_array_of_samples(), dtype=np.float32)
        return (samples / 128.0), 16000
    # 24/32 bit: portare a 16 bit e dividere per 32768 da' lo stesso float32
    # in [-1,1] che restituisce la via ffmpeg.
    samples = np.array(seg.set_sample_width(2).get_array_of_samples(), dtype=np.float32)
    return (samples / 32768.0), 16000


def _ov_lang_token(language: str) -> str:
    """Token OpenVINO (es. '<|it|>') dal codice lingua Tesseract/OCR (es. 'ita').

    Usa la stessa mappa di ``_whisper_lang``: aggiungere una lingua a
    ``_LANG_MAP`` la fa funzionare anche con ``--transcriber openvino``.
    """
    return f"<|{_whisper_lang(language)}|>"


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

    Nota: `generate()` e' una chiamata BLOCCANTE che restituisce tutto in una
    volta, quindi qui non c'e' un parziale da salvare a pezzi (il testo finisce
    su disco a fine decodifica, con `_write_raw_transcript`). La ripresa a
    meta' audio esiste solo nel percorso faster-whisper.

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
def openvino_usable(openvino_model_dir: Path | str | None = None) -> bool:
    """True se su questo PC OpenVINO è una via realmente percorribile.

    Tre condizioni, tutte necessarie:

    - il runtime ``openvino_genai`` è importabile;
    - il modello IR è presente e COMPLETO (senza, ogni run lo riscaricherebbe;
      con una cartella a meta' la pipeline fallirebbe al caricamento);
    - il runtime espone un device **GPU** reale. La sola CPU non basta: senza
      iGPU non c'è guadagno di velocità, quindi su AMD/ARM (dove OpenVINO vede
      al più la CPU) l'avviso "usa OpenVINO" sarebbe rumore.

    Non legge la decisione di ``machine_setup``: quella non viene più persistita
    (vedi ``machine_setup``) e, comunque, sarebbe derivata da esattamente queste
    condizioni. Rileggere il file aggiungeva un quarto stato possibile — "una
    decisione salvata che il runtime non conferma" — e produceva un avviso
    contraddittorio: la pipeline su faster-whisper che invitava a installare
    OpenVINO dopo che il setup aveva già provato a installarlo e non era riuscito.
    """
    try:
        import openvino_genai  # noqa: F401
    except ImportError:
        return False
    model_dir = Path(openvino_model_dir) if openvino_model_dir is not None else Path(DEFAULT_OPENVINO_MODEL_DIR)
    # Non basta `exists()`: una snapshot interrotta lascia la cartella e i
    # file a meta', e li' OpenVINO non carica nulla. Stesso criterio di
    # download_openvino_model e machine_setup.
    if not openvino_model_complete(model_dir) or not openvino_model_id_ok(
        model_dir, DEFAULT_OPENVINO_MODEL_ID
    ):
        return False
    return openvino_device_available()


def resolved_transcriber(transcriber: str, openvino_model_dir: Path | str | None) -> str:
    """Nome del motore che ``transcribe_audio`` userà DAVVERO: 'openvino' o 'whisper'.

    ``auto`` significa "OpenVINO se è percorribile, altrimenti faster-whisper":
    la scelta dipende dal runtime installato e dal modello IR presente. La cache
    della trascrizione deve usare la scelta RISOLTA e non la stringa ``auto``,
    altrimenti (es. dopo `--openvino-download`) il passaggio da un motore
    all'altro riuserebbe un testo prodotto dall'altro, con timestamp diversi.

    Il download del modello (richiesto da ``openvino`` esplicito) avviene solo
    dentro ``transcribe_audio``: qui la funzione resta senza effetti.

    Accetta anche una ``str`` per il percorso: è il tipo naturale con cui
    arriva da argparse, e chiamare ``.exists()`` su una ``str`` sollevava
    ``AttributeError`` invece di rispondere semplicemente "whisper".
    """
    if transcriber == "whisper":
        return "whisper"
    try:
        import openvino_genai  # noqa: F401
    except ImportError:
        return "whisper"
    if transcriber == "openvino":
        # Motore esplicito: se il modello manca, transcribe_audio lo scarica.
        return "openvino"
    # `openvino_usable` e `download_openvino_model` usano la cartella di
    # default quando il percorso e' None (ed e' il default di `transcribe_audio`):
    # qui prima si tornava sempre a whisper, quindi un chiamante che usava i
    # valori documentati non usava OpenVINO nemmeno con il modello completo.
    cartella = Path(openvino_model_dir) if openvino_model_dir is not None else Path(DEFAULT_OPENVINO_MODEL_DIR)
    if openvino_model_complete(cartella):
        return "openvino"
    return "whisper"


# File-segnale di un tentativo OpenVINO: scritto prima di caricare la pipeline,
# cancellato a trascrizione riuscita (o a eccezione Python, che si rivede alla
# prossima run). Se il processo muore dentro il codice nativo (segfault della
# GPU, OOM-killer di Windows: nessun traceback, nessuna eccezione), il file
# resta e la run dopo sa che la GPU ha ucciso quel lavoro.
_OPENVINO_ATTEMPT_FILENAME = "openvino_attempt.json"

# Fabbisogno per caricare il modello IR sulla GPU, che condivide la RAM di
# sistema: due volte i byte del modello (copia sul device + blob compilato)
# piu' 1 GiB di margine per OS e resto del programma. Se la cartella del
# modello non si legge, 2 GiB fissi (ordine di grandezza di whisper-small).
_GPU_SPIKE_FACTOR = 2
_GPU_OS_MARGIN_BYTES = 1024**3
_GPU_MIN_NEED_BYTES = 2 * 1024**3


def _openvino_attempt_path() -> Path:
    """Percorso del file-segnale (nella cache del progetto)."""
    return CACHE_DIR / _OPENVINO_ATTEMPT_FILENAME


def _model_dir_bytes(model_dir: Path) -> int | None:
    """Byte dei file del modello IR, o None se non leggibili."""
    try:
        return sum(p.stat().st_size for p in model_dir.rglob("*") if p.is_file())
    except OSError:
        return None


def _openvino_ram_need_bytes(model_dir: Path | None) -> int:
    """RAM libera richiesta per caricare il modello sulla GPU."""
    if model_dir is not None:
        misurati = _model_dir_bytes(model_dir)
        if misurati:
            return _GPU_SPIKE_FACTOR * misurati + _GPU_OS_MARGIN_BYTES
    return _GPU_MIN_NEED_BYTES


def _read_attempts() -> list[dict[str, Any]]:
    """Tutti i segnali di tentativi GPU ancora incompiuti.

    Il file e' uno solo, ma puo' contenere piu' record: il segnale di un
    podcast non deve impedire a un SECONDO podcast di registrare il proprio
    crash. Il formato vecchio (un dict solo) resta leggibile.
    """
    try:
        dati = json.loads(_openvino_attempt_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(dati, dict):
        precedent = dati.get("tentativi")
        if isinstance(precedent, list):
            return [r for r in precedent if isinstance(r, dict)]
        return [dati]
    if isinstance(dati, list):
        return [r for r in dati if isinstance(r, dict)]
    return []


def _scrivi_attempts(records: list[dict[str, Any]]) -> None:
    with contextlib.suppress(OSError):
        atomic_write_text(
            _openvino_attempt_path(),
            json.dumps({"tentativi": records}, ensure_ascii=False),
        )


def _read_attempt() -> dict[str, Any] | None:
    """Il primo tentativo incompiuto (compatibilita' con i chiamanti storici)."""
    records = _read_attempts()
    return records[0] if records else None


def _read_attempt_for(audio_path: Path) -> dict[str, Any] | None:
    """Il segnale di QUESTO audio, se c'e'."""
    return next((r for r in _read_attempts() if _same_audio(r, audio_path)), None)


def _write_attempt(audio_path: Path) -> None:
    """Registra l'inizio di un tentativo GPU (sovrascrive il precedente).

    Best-effort come la sua cancellazione (``_clear_attempt``): se la cartella
    di cache non e' scrivibile, o il disco e' pieno, il segnale non viene
    scritto ma la trascrizione deve proseguire sul motore scelto. Prima la
    scrittura non era protetta: un OSError qui abbatteva la run PRIMA di
    chiamare OpenVINO, cioe' il modo peggiore di perdere minuti di setup.
    """
    # Si AGGIUNGE il record di questo audio senza toccare gli altri: cosi'
    # anche un secondo podcast puo' registrare il proprio crash GPU.
    with contextlib.suppress(OSError):
        info = audio_path.stat()
        records = [r for r in _read_attempts() if not _same_audio(r, audio_path)]
        records.append(
            {
                "audio": str(audio_path),
                "size": info.st_size,
                "mtime": info.st_mtime,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        _scrivi_attempts(records)


def _clear_attempt(audio_path: Path | None = None) -> None:
    """Cancella il file-segnale, ma solo se parla dell'audio corrente.

    Il segnale e' un file UNICO in cache, e `decide_transcriber` lo onora
    solo quando `_same_audio` corrisponde. Cancellarlo senza controllare
    l'identita' faceva sparire il segnale di un crash su un ALTRO audio:
    il proprietario perdeva la protezione e al prossimo run ripeteva il
    segfault. Con `audio_path=None` si mantiene il vecchio comportamento.
    """
    if audio_path is None:
        with contextlib.suppress(OSError):
            _openvino_attempt_path().unlink(missing_ok=True)
        return
    # Si rimuove SOLO il record di questo audio: un segnale di crash di un
    # altro podcast resta intatto.
    records = [r for r in _read_attempts() if not _same_audio(r, audio_path)]
    if records:
        _scrivi_attempts(records)
    else:
        with contextlib.suppress(OSError):
            _openvino_attempt_path().unlink(missing_ok=True)


def _same_audio(segnalato: object, audio_path: Path) -> bool:
    """True se il segnale si riferisce a questo stesso file audio invariato.

    Il confronto diretto regge anche i segnali vecchi o corrotti: chiavi
    mancanti o di tipo diverso non uguagliano mai dimensione/mtime veri, e un
    segnale non-dict non e' un segnale.
    """
    if not isinstance(segnalato, dict):
        return False
    try:
        info = audio_path.stat()
    except OSError:
        return False
    return (
        segnalato.get("audio") == str(audio_path)
        and segnalato.get("size") == info.st_size
        and segnalato.get("mtime") == info.st_mtime
    )


def decide_transcriber(
    transcriber: str,
    openvino_model_dir: Path | str | None,
    audio_path: Path,
) -> tuple[str, str | None]:
    """Motore effettivo + eventuale motivo del ripiego su CPU.

    Ritorna ``(motore, motivo)``: motivo None = via libera alla GPU. Il ripiego
    scatta solo con scelta ``auto`` (un ``--transcriber openvino`` esplicito non
    si discute: chi lo chiede sa cosa fa). Tre salvagenti, in quest'ordine:

    1. poca RAM libera *adesso* per caricare il modello sulla GPU;
    2. un tentativo GPU precedente sullo stesso audio mai finito (il processo
       e' morto dentro il codice nativo: nessun traceback allora, e sarebbe
       inutile riprovare identico);
    3. il device OpenVINO richiesto non e' disponibile adesso (modello IR
       presente ma iGPU disattivata: provare a usarla morirebbe con
       "device not found").

    La chiave di cache della trascrizione resta calcolata sul motore risolto da
    ``resolved_transcriber`` (main.py): in caso di ripiego il testo CPU finisce
    sotto chiave OpenVINO, ma e' sempre una trascrizione valida dello stesso
    audio e l'ultima run vince — approssimazione documentata, non corruzione.
    """
    if resolved_transcriber(transcriber, openvino_model_dir) != "openvino":
        return "whisper", None
    if transcriber == "openvino":
        return "openvino", None
    cartella = Path(openvino_model_dir) if openvino_model_dir is not None else None
    libera = ram_available_bytes()
    necessari = _openvino_ram_need_bytes(cartella)
    if libera is not None and libera < necessari:
        return (
            "whisper",
            f"RAM libera {libera / 1024**3:.1f} GB sotto il fabbisogno "
            f"({necessari / 1024**3:.1f} GB per caricare il modello sulla GPU)",
        )
    if cartella is not None and _same_audio(_read_attempt_for(audio_path), audio_path):
        return (
            "whisper",
            "l'ultimo tentativo OpenVINO su questo audio non e' finito "
            "(il processo e' morto dentro la GPU, senza traceback)",
        )
    # Runtime e modello IR bastano a `resolved_transcriber` per la chiave di
    # cache, ma NON bastano a trascrivere: il device richiesto (GPU di default)
    # deve esistere ADESSO. Senza questo controllo, un PC con il modello gia'
    # scaricato e la iGPU disattivata moriva con "device not found" invece di
    # ripiegare su faster-whisper come fa `openvino_usable`.
    if not openvino_device_available():
        return "whisper", "nessun device OpenVINO disponibile adesso"
    return "openvino", None


def _avvisa_modello_ignorato(model_size: str, model_dir: Path) -> None:
    """Dice che ``--whisper-model`` non si applica al motore OpenVINO.

    Il percorso OpenVINO usa un modello IR gia' convertito (default
    ``whisper-small-fp16-ov``): la taglia chiesta con ``--whisper-model`` non
    entra in nessun calcolo. Accettare il flag e non usarlo, senza dirlo, e'
    peggio di rifiutarlo: chi chiede ``medium`` crede di aver alzato la
    qualita' e non e' cambiato niente.
    """
    if model_size.strip().lower() == "small":
        return
    log.warning(
        "   \u26a0\ufe0f  [Trascrizione] --whisper-model %s NON si applica al motore "
        "OpenVINO: usa il modello IR fisso in %s (%s). Per cambiare modello "
        "usa --transcriber whisper (faster-whisper), oppure un altro IR con "
        "--openvino-model-dir / OPENVINO_MODEL_ID.",
        model_size,
        model_dir,
        DEFAULT_OPENVINO_MODEL_ID,
    )


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
    whisper_batch: int = DEFAULT_WHISPER_BATCH,
    whisper_device_explicit: bool = False,
    no_ripresa: bool = False,
) -> tuple[str, list[Word]]:
    """
    Dispatcher trascrizione: sceglie il motore più veloce disponibile.

    - ``auto``: OpenVINO GenAI se installato e il modello IR è presente,
      altrimenti faster-whisper.
    - ``openvino``: solo OpenVINO (errore se manca).
    - ``whisper``: solo faster-whisper.

    La decisione passa da ``resolved_transcriber`` (stessa logica usata per la
    chiave di cache in main.py): così la cache non può descrivere un motore
    diverso da quello che ha davvero prodotto il testo.

    Returns:
        (trascrizione compressa, lista parole raw con timestamp)
    """
    if resolved_transcriber(transcriber, openvino_model_dir) == "openvino":
        model_dir = openvino_model_dir
        if (
            model_dir is None
            or not openvino_model_complete(model_dir)
            or not openvino_model_id_ok(model_dir, DEFAULT_OPENVINO_MODEL_ID)
        ):
            model_dir = download_openvino_model(
                Path(DEFAULT_OPENVINO_MODEL_DIR) if model_dir is None else model_dir
            )
        if transcriber == "auto":
            motore, motivo = decide_transcriber(transcriber, model_dir, audio_path)
            if motore != "openvino":
                log.warning(
                    "\n   [Trascrizione] %s.\n"
                    "   Uso faster-whisper su CPU: piu' lento, ma arriva in fondo.\n"
                    "   Per forzare comunque la GPU: --transcriber openvino.",
                    motivo or "motivo non registrato",
                )
                esito_cpu = transcribe_with_whisper(
                    audio_path,
                    model_size=model_size,
                    language=language,
                    device=whisper_device,
                    compute_type=whisper_compute_type,
                    openvino_available=openvino_usable(openvino_model_dir),
                    beam_size=whisper_beam,
                    batch_size=whisper_batch,
                    device_explicit=whisper_device_explicit,
                    riprendi=not no_ripresa,
                )
                # Solo una CPU che e' arrivata in fondo dimostra che la
                # macchina funziona: un'eccezione qui NON deve cancellare il
                # segnale del crash GPU che ha causato il ripiego.
                _clear_attempt(audio_path)
                return esito_cpu
            _avvisa_modello_ignorato(model_size, model_dir)
            _write_attempt(audio_path)
            try:
                esito_gpu = transcribe_with_openvino(
                    audio_path,
                    model_dir=model_dir,
                    language=language,
                    device=openvino_device,
                )
            except BaseException:
                # Qualsiasi eccezione PYTHON (compreso Ctrl+C) dimostra che
                # il processo e' vivo: il segnale di "crash dentro il nativo"
                # sarebbe falso e condannerebbe la run dopo alla CPU. Un vero
                # segfault non lancia eccezioni, quindi lascia il segnale.
                _clear_attempt(audio_path)
                raise
            # Il segnale resta se il processo e' morto dentro il nativo
            # (segfault/OOM-killer): e' il caso che deve essere ricordato.
            _clear_attempt(audio_path)
            return esito_gpu
        _avvisa_modello_ignorato(model_size, model_dir)
        # `--transcriber openvino` e' il modo in cui l'utente FORZA la GPU:
        # e' proprio qui che un segfault lascerebbe il segnale. Senza scriverlo
        # anche su questo ramo, il crash restava invisibile e il prossimo auto
        # ripartiva con la GPU suicida.
        _write_attempt(audio_path)
        try:
            esito_gpu = transcribe_with_openvino(
                audio_path,
                model_dir=model_dir,
                language=language,
                device=openvino_device,
            )
        except BaseException:
            _clear_attempt(audio_path)
            raise
        _clear_attempt(audio_path)
        return esito_gpu

    if transcriber == "openvino":
        raise RuntimeError(
            "openvino-genai non installato: impossibile usare --transcriber openvino."
        ) from None
    # L'avviso ha senso solo se il runtime OpenVINO c'è ma manca il modello:
    # senza runtime sarebbe rumore (nessun download renderebbe percorribile la strada).
    openvino_runtime = resolved_transcriber("openvino", None) == "openvino"
    if (
        transcriber == "auto"
        and openvino_runtime
        and openvino_model_dir is not None
        and not openvino_model_complete(openvino_model_dir)
    ):
        log.warning(
            "   ⚠️  Modello OpenVINO non trovato o incompleto in %s, uso faster-whisper. "
            "Scaricalo una tantum con `python src/main.py --openvino-download` "
            "per usare la iGPU (~1.5x più veloce).",
            openvino_model_dir,
        )

    esito = transcribe_with_whisper(
        audio_path,
        model_size=model_size,
        language=language,
        device=whisper_device,
        compute_type=whisper_compute_type,
        # Il model dir reale, non il default: con --openvino-model-dir
        # personalizzato il probe deve guardare lo stesso percorso che verrà
        # usato davvero, altrimenti l'avviso "usa OpenVINO" valuterebbe una
        # cartella diversa da quella della pipeline.
        openvino_available=openvino_usable(openvino_model_dir),
        beam_size=whisper_beam,
        batch_size=whisper_batch,
        device_explicit=whisper_device_explicit,
        riprendi=not no_ripresa,
    )
    # Solo una trascrizione arrivata in fondo dimostra che la macchina e'
    # utilizzabile: un'eccezione lascia il segnale di crash intatto.
    _clear_attempt(audio_path)
    return esito


# --- Salvataggio a pezzi della trascrizione --------------------------------
# faster-whisper non si mette in pausa: se la run muore a meta', il testo gia'
# prodotto va salvato, altrimenti e' perduto (la decodifica, comunque,
# ripartira' da capo). Ogni N segmenti le parole fin qui finiscono in un file
# sotto `.cache/parziali/`: una sottocartella, quindi la pulizia delle cache
# (che guarda i .json in radice) non li cancella.
_CHECKPOINT_OGNI_SEGMENTI = 50


def _firma_audio(audio_path: Path) -> str:
    """Impronta del CONTENUTO INTERO: due finestre potevano nascondere una
    modifica nel mezzo e fare riattaccare parole di un altro audio.

    Nessuna memoizzazione per (percorso, dimensione, mtime): con quegli stessi
    valori un file sostituito riceveva la firma vecchia. Un MP4 da ~15 MB si
    legge in pochi millisecondi.
    """
    try:
        stat = audio_path.stat()
    except OSError:
        return audio_path.stem
    try:
        h = hashlib.sha1(usedforsecurity=False)
        with open(audio_path, "rb") as fh:
            for blocco in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(blocco)
        percorso = hashlib.sha1(
            str(audio_path.resolve()).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
        return f"{audio_path.stem}_{percorso}_{stat.st_size}_{h.hexdigest()[:16]}"
    except OSError:
        percorso = hashlib.sha1(
            str(audio_path.resolve()).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
        return f"{audio_path.stem}_{percorso}_{stat.st_size}"


def percorso_parziale(audio_path: Path, firma: str | None = None) -> Path:
    """Dove finiscono le parole gia' trascritte di QUEST'audio.

    `firma` e' opzionale: chi ha gia' letto l'intero file la passa, così non
    lo rilegge a ogni checkpoint.
    """
    from config import CACHE_DIR

    return CACHE_DIR / "parziali" / f"{firma or _firma_audio(audio_path)}.json"


def _salva_parziale(
    audio_path: Path,
    parole: list[Word],
    coperto_fino: float = 0.0,
    params: dict[str, Any] | None = None,
) -> None:
    """Scrive le parole fin qui (errore ignorato: e' una rete di sicurezza).

    ``coperto_fino`` e' il secondo fino a cui l'audio e' stato DECODIFICATO
    (la fine dell'ultimo segmento lavorato, non l'ultimo timestamp di parola:
    un segmento finisce dove il parlato gira, quindi tagliare li' non perde
    niente). Serve alla ripresa.

    ``params`` sono i parametri della decodifica. Senza, un parziale fatto
    con `small` verrebbe "ripreso" da una run con `large`: il transcript
    sarebbe mezzo modello e mezzo altro, senza alcun segnale.
    """
    from config import atomic_write_text

    firma = _firma_audio(audio_path)
    # Se esiste un parziale di QUEST'audio con una firma diversa, l'audio e'
    # stato sostituito a meta' trascrizione: le parole vecchie non vanno
    # riattaccate a quelle nuove. Si butta il parziale e si riparte da capo.
    prefixed = f"{audio_path.stem}_"
    cartella = percorso_parziale(audio_path, firma).parent
    for vecchio in cartella.glob(f"{prefixed}*.json"):
        if vecchio.stem == f"{audio_path.stem}_{firma}":
            continue
        try:
            contenuto = json.loads(vecchio.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(contenuto.get("audio") or "") == str(audio_path):
            with contextlib.suppress(OSError):
                vecchio.unlink(missing_ok=True)
            log.info(
                "   [Trascrizione] Audio sostituito durante la decodifica: "
                "butto il parziale e riparto da capo."
            )
            return
    try:
        path = percorso_parziale(audio_path, firma)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(
                {
                    "audio": str(audio_path),
                    "firma": firma,
                    "parole": len(parole),
                    "coperto_fino": round(float(coperto_fino), 3),
                    "params": params or {},
                    "words": parole,
                },
                ensure_ascii=False,
            ),
        )
    except (OSError, TypeError, ValueError) as e:
        # Rete di sicurezza: un parziale non scritto non deve mai fermare la
        # decodifica (TypeError = valore non serializzabile, es. np.float64).
        log.debug("   Trascrizione parziale non salvata (%s).", e)


def _leggi_parziale(audio_path: Path, firma: str | None = None) -> dict[str, Any] | None:
    """Il parziale di quest'audio, se c'e' ed e' leggibile."""
    try:
        dati = json.loads(percorso_parziale(audio_path, firma).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dati if isinstance(dati, dict) else None


def _taglia_audio(audio_path: Path, da: float) -> Path | None:
    """Copia la parte di audio da ``da`` in poi, per decodificare solo quella.

    ``-ss`` PRIMA di ``-i``: ffmpeg salta senza decodificare.

    Il taglio e' LOSSLESS (FLAC): il motore sente lo stesso segnale della
    sorgente, campione per campione, e l'istante di partenza e' esattamente
    ``da``. Misurato sulle tre strade possibili: ricodificando in AAC la coda
    cambiava una parola ("è" -> "e"); con ``-c copy`` il taglio cade sul
    pacchetto piu' vicino e i timestamp slittano di ~20 ms, abbastanza da
    spostare il confine (parola doppia al giunto); con FLAC il testo e i tempi
    tornano quelli della decodifica intera.

    Il file e' piu' grosso dell'originale (FLAC comprime meno di AAC) ma vive
    solo per la durata della decodifica: chi chiama lo cancella.

    None se il taglio non riesce: chi chiama ricomincia da capo.
    """
    parziale = percorso_parziale(audio_path)
    resto = parziale.with_name(f"{parziale.stem}_resto.flac")
    with contextlib.suppress(OSError):
        resto.parent.mkdir(parents=True, exist_ok=True)
    codec = ["-c:a", "flac"]
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{max(0.0, da):.3f}",
        "-i",
        str(audio_path),
        "-vn",
        *codec,
        str(resto),
    ]
    try:
        esito = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("   Ripresa: taglio audio non riuscito (%s).", e)
        return None
    if esito.returncode != 0 or not resto.exists() or resto.stat().st_size == 0:
        log.debug(
            "   Ripresa: taglio audio non riuscito (%s).",
            (esito.stderr or "").strip()[:200],
        )
        return None
    return resto


# Secondi di audio riascoltati PRIMA del punto salvato, quando si riprende.
# Servono al contesto: il motore deve sentire l'attacco della frase al confine,
# non partire a meta'. Misurato sul materiale di prova: senza sovrapposizione
# la parola dopo il giunto cambiava ("è" -> "e").
_SOVRAPPOSIZIONE_RIPRESA = 3.0

# Fino a che secondo e' stata ripresa l'ultima trascrizione (0 = da capo).
# Letta dal report: una lezione trascritta in due passate deve dirlo.
_ULTIMA_RIPRESA = 0.0


def ultima_ripresa() -> float:
    """Il secondo da cui e' ripartita l'ultima trascrizione (0 se da capo)."""
    return _ULTIMA_RIPRESA


def azzera_ripresa() -> None:
    """Azzera il segnale all'inizio di una nuova run nello stesso processo."""
    global _ULTIMA_RIPRESA
    _ULTIMA_RIPRESA = 0.0


def _prepara_ripresa(
    audio_path: Path, params: dict[str, Any] | None = None
) -> tuple[list[Word], Path, float]:
    """(parole gia' fatte, file da trascrivere, secondo di TAGLIO).

    Riprendere DAVVERO significa decodificare solo la coda: le parole salvate
    restano e i timestamp nuovi si spostano dell'offset. Se qualcosa non torna
    (parziale vecchio formato, taglio fallito) si ricomincia da capo: meglio
    una trascrizione intera che una trascrizione sbagliata.

    Il terzo valore e' il punto dove COMINCIA l'audio tagliato, che e' qualche
    secondo PRIMA del punto salvato (``_SOVRAPPOSIZIONE_RIPRESA``): serve a
    dare contesto al motore. Le parole riascoltate in quella sovrapposizione
    le scarta ``_collect_words``, perche' sono gia' nel parziale.
    """
    global _ULTIMA_RIPRESA
    # NON si azzera qui: main chiama la trascrizione piu' di una volta (la
    # seconda, "accurata", usa un beam diverso e quindi non riprende).
    # Azzerare a ogni chiamata cancellava il dato della prima, e il riepilogo
    # di una lezione ripresa diceva "una passata sola".
    firma = _firma_audio(audio_path)
    dati = _leggi_parziale(audio_path, firma)
    if dati is None:
        return [], audio_path, 0.0
    # L'impronta del nome puo' collidere fra due file con lo stesso nome in
    # cartelle diverse: il percorso salvato nel parziale e' l'unica prova che
    # le parole appartengano a QUESTO audio.
    audio_salvato = str(dati.get("audio") or "")
    if audio_salvato and audio_salvato not in (str(audio_path), str(audio_path.resolve())):
        log.info(
            "   [Trascrizione] Parziale di un altro audio (%s): lo lascio stare e "
            "decodifico tutto, per non cucire parole estranee.",
            audio_salvato,
        )
        return [], audio_path, 0.0
    # Il percorso puo' essere lo stesso con il contenuto cambiato (stesso nome,
    # stessa dimensione e mtime): la firma dell'intero file e' l'unica prova.
    firma_salvata = str(dati.get("firma") or "")
    if firma_salvata and firma_salvata != firma:
        log.info(
            "   [Trascrizione] Parziale di un'altra versione dell'audio: lo "
            "lascio stare e decodifico tutto, per non cucire parole estranee."
        )
        return [], audio_path, 0.0
    salvati = dati.get("params")
    if params is not None and (not isinstance(salvati, dict) or salvati != params):
        log.info(
            "   [Trascrizione] C'e' un parziale, ma di un'altra decodifica "
            "(modello/beam/lingua diversi): lo lascio stare e decodifico tutto. "
            "Un transcript mezzo modello e mezzo altro sarebbe peggio di uno lento."
        )
        return [], audio_path, 0.0
    grezze = dati.get("words")
    parole: list[Word] = []
    if isinstance(grezze, list):
        for w in grezze:
            if not (isinstance(w, dict) and "word" in w and "start" in w):
                continue
            # Un timestamp non numerico (parziale vecchio o corrotto) verrebbe
            # scartato solo dopo il taglio audio, e `float()` esploderebbe in
            # `_collect_words`: si butta via il parziale e si riparte da capo.
            try:
                istante = float(w["start"])
            except (TypeError, ValueError, OverflowError):
                istante = float("nan")
            if not math.isfinite(istante):
                log.info(
                    "   [Trascrizione] Parziale con timestamp non valido: "
                    "decodifico tutto da capo."
                )
                return [], audio_path, 0.0
            parole.append(cast("Word", w))
    try:
        coperto = float(dati.get("coperto_fino") or 0.0)
    except (TypeError, ValueError, OverflowError):
        coperto = 0.0
    if not math.isfinite(coperto):
        log.info(
            "   [Trascrizione] Parziale con punto di ripresa non valido: "
            "decodifico tutto da capo."
        )
        return [], audio_path, 0.0
    if not parole or coperto <= 1.0:
        if parole:
            log.info(
                "   [Trascrizione] Trovate %d parole di una run interrotta, ma senza "
                "il punto di ripresa: decodifico tutto (il testo resta in %s).",
                len(parole),
                percorso_parziale(audio_path, firma),
            )
        return [], audio_path, 0.0
    taglio = max(0.0, coperto - _SOVRAPPOSIZIONE_RIPRESA)
    resto = _taglia_audio(audio_path, taglio)
    if resto is None:
        log.warning(
            "   [Trascrizione] La run precedente si era interrotta: %d parole salvate "
            "in %s. Non riesco a tagliare l'audio per riprendere, quindi decodifico "
            "da capo (il testo resta; faster-whisper non riprende a meta' audio).",
            len(parole),
            percorso_parziale(audio_path, firma),
        )
        return [], audio_path, 0.0
    log.info(
        "   [Trascrizione] Riprendo da %.0fs: %d parole gia' trascritte, decodifico "
        "solo la coda (riascolto %.1fs per il contesto).",
        coperto,
        len(parole),
        coperto - taglio,
    )
    _ULTIMA_RIPRESA = coperto
    return parole, resto, taglio


def _collect_words(
    segments: Any,
    audio_path: Path | None = None,
    *,
    offset: float = 0.0,
    precedenti: list[Word] | None = None,
    params: dict[str, Any] | None = None,
) -> list[Word]:
    """Consuma i segmenti di faster-whisper e ne estrae le parole con timestamp.

    Con ``word_timestamps=True`` ogni segmento porta le sue parole; il ramo
    di riserva (una "parola" per token, con l'inizio del segmento) tiene in
    piedi la pipeline anche se il motore non restituisce i timestamp per parola.

    ``audio_path`` (facoltativo) attiva il salvataggio a pezzi: un crash a
    meta' non butta via il testo gia' trascritto.

    ``offset`` e' il secondo da cui parte l'audio passato al motore (ripresa
    dopo un'interruzione): i timestamp vanno spostati per restare sull'audio
    INTERO. ``precedenti`` sono le parole gia' fatte, che il parziale deve
    continuare a contenere.
    """
    all_words: list[Word] = []
    gia_fatte = list(precedenti or [])
    segmenti = 0
    coperto = offset
    # La ripresa riascolta qualche secondo gia' trascritto per avere contesto:
    # quelle parole sarebbero doppioni. Si scarta tutto cio' che non sta dopo
    # l'ultima parola salvata (le parole salvate coprono l'audio fino al punto
    # di ripresa, quindi non si perde nulla di vero).
    limite = max((float(w["start"]) for w in gia_fatte), default=None)
    # Indice delle parole salvate per testo: la coda viene riascoltata per
    # parecchi secondi e una parola puo' essere riconosciuta di nuovo con un
    # inizio di pochi centesimi successivo a quello gia' salvato (misurato:
    # 57 -> 58 parole al giunto). Il solo confronto coi timestamp non basta.
    gia_per_testo: dict[str, list[float]] = {}
    for w in gia_fatte:
        gia_per_testo.setdefault(str(w["word"]).strip().lower(), []).append(float(w["start"]))

    def _gia_detto(testo: str, inizio: float) -> bool:
        for inizio_salvato in gia_per_testo.get(testo.strip().lower(), ()):
            # Finestra stretta: un vero ripasso della stessa parola a distanza
            # di secondi e' legittimo, un doppione del giunto si scosta di
            # pochi centesimi (al massimo qualche decimo).
            if abs(inizio - inizio_salvato) <= 0.6:
                return True
        return False
    for seg in segments:
        segmenti += 1
        fine = offset + float(getattr(seg, "end", 0.0) or 0.0)
        coperto = max(coperto, fine)
        if not seg.text:
            continue
        if hasattr(seg, "words") and seg.words:
            for w in seg.words:
                inizio = float(w.start) + offset
                if limite is not None and inizio <= limite:
                    continue
                if _gia_detto(w.word, inizio):
                    continue
                if w.word.strip():
                    # float(): i timestamp di faster-whisper sono np.float64, e
                    # json.dumps non li sa scrivere (il primo checkpoint su un
                    # file lungo sarebbe fallito).
                    all_words.append({"word": w.word.strip(), "start": inizio})
        else:
            for token in seg.text.strip().split():
                inizio = float(seg.start) + offset
                if limite is not None and inizio <= limite:
                    continue
                if _gia_detto(token, inizio):
                    continue
                all_words.append({"word": token, "start": inizio})
        if audio_path is not None and segmenti % _CHECKPOINT_OGNI_SEGMENTI == 0:
            _salva_parziale(audio_path, gia_fatte + all_words, coperto, params)
    if audio_path is not None:
        # Trascrizione arrivata in fondo: il parziale non serve piu'.
        with contextlib.suppress(OSError):
            percorso_parziale(audio_path).unlink(missing_ok=True)
    return all_words


def _transcribe_with_fallback(
    model: Any,
    audio_path: Path,
    language: str,
    beam_size: int,
    vad_filter: bool,
    vad_parameters: dict,
    batch_size: int,
    checkpoint_path: Path | None = None,
    offset: float = 0.0,
    precedenti: list[Word] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[list[Word], Any]:
    """Trascrive usando il decoding a batch quando è disponibile.

    Il decoding a batch (``BatchedInferencePipeline``) usa lo STESSO modello e
    la stessa decodifica: cambia solo quanti segmenti vengono elaborati
    insieme, quindi guadagna throughput senza toccare la qualità dell'output.
    Su podcast reale (1058s, small int8): 91.5s -> 40.1s su uno slice di 240s,
    con le ancore 'slide N' che si spostano al massimo di 0.15s.

    Qualunque problema (versione di faster-whisper senza la classe, batch non
    supportato insieme ai word timestamps, memoria insufficiente) fa ripiegare
    sul percorso sequenziale: la velocità non deve mai diventare un punto di
    rottura della trascrizione.

    Returns:
        (parole con timestamp, info della trascrizione)
    """
    kwargs: dict[str, Any] = {
        "language": language,
        "beam_size": beam_size,
        "word_timestamps": True,
        "vad_filter": vad_filter,
        "vad_parameters": vad_parameters,
    }

    if batch_size and batch_size > 1:
        try:
            from faster_whisper import BatchedInferencePipeline
        except ImportError:
            log.debug("   Decoding a batch non disponibile: uso il percorso sequenziale.")
        else:
            log.info("   Decoding a batch: batch_size=%d, beam=%d.", batch_size, beam_size)
            try:
                segments, info = BatchedInferencePipeline(model=model).transcribe(
                    str(audio_path), batch_size=batch_size, **kwargs
                )
                return _collect_words(
                    segments,
                    checkpoint_path,
                    offset=offset,
                    precedenti=precedenti,
                    params=params,
                ), info
            except Exception as e:  # ottimizzazione di velocità: mai fatale
                log.warning(
                    "   ⚠️  Decoding a batch non riuscito (%s): ripiego sul percorso sequenziale.",
                    e,
                )

    segments, info = model.transcribe(str(audio_path), **kwargs)
    return _collect_words(
        segments, checkpoint_path, offset=offset, precedenti=precedenti, params=params
    ), info


def _warn_cpu_transcription(model_size: str) -> None:
    """Stima della durata della trascrizione su CPU, scalata sui core.

    I minuti base sono una stima empirica misurata su una CPU Intel con 8
    core fisici: sono quindi riferiti a quella macchina, non a questa. Il
    fattore `8 / core_di_questa_macchina` e' un'approssimazione lineare (il
    throughput non scala esattamente con i core), ma sbagliare in eccesso e'
    preferibile a restare muti.

    Il messaggio dice sempre cosa fare: un modello piu' piccolo via env, che
    e' l'unico intervento che l'utente puo' fare senza toccare il codice.
    """
    minuti_riferimento = {  # minuti per 28 min di audio, 8 core fisici Intel
        "tiny": 2,
        "base": 4,
        "small": 8,
        "medium": 14,
        "large": 25,
    }
    base = minuti_riferimento.get(model_size, 8)

    from config import _logical_cpus, _physical_cpus

    core = _physical_cpus() or _logical_cpus() or 8
    stima = max(1, round(base * 8 / max(1, core)))

    log.warning(
        "   ⚠️  Trascrizione su CPU (nessuna accelerazione GPU): ~%d min per 28 min di audio "
        "su %d core. Per andare piu' veloce: WHISPER_MODEL=tiny (o base) nell'env, "
        "oppure una macchina con GPU accelerabile.",
        stima,
        core,
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
    batch_size: int = DEFAULT_WHISPER_BATCH,
    device_explicit: bool = False,
    riprendi: bool = True,
) -> tuple[str, list[Word]]:
    """
    Trascrizione audio con faster-whisper.

    Note:
        `language` usa i codici Tesseract/OCR (es. "ita", "eng"), come il
        resto della pipeline; viene mappato su ISO 639-1 per Whisper.
        `batch_size > 1` abilita il decoding a batch (stesso modello, solo
        più throughput); 0 o 1 lo disattivano.
        `device_explicit` True se il device è arrivato da una scelta esplicita
        dell'utente: in quel caso un fallimento di CUDA non viene nascosto con
        un ripiego silenzioso su CPU.

    Returns:
        (trascrizione compressa, lista parole raw con timestamp)
    """
    from faster_whisper import WhisperModel

    _device_was_explicit = device_explicit

    # CTranslate2 carica cuBLAS/cuDNN al primo calcolo, non alla costruzione
    # del modello: se mancano, il decoding a batch fallisce e anche il
    # percorso sequenziale muore a meta' trascrizione con "Library
    # cublas64_12.dll is not found or cannot be loaded". Qui il difetto si
    # vede PRIMA di iniziare, quindi si puo' ripiegare su CPU senza perdere
    # la run (o fermarsi subito, se l'utente ha chiesto CUDA esplicitamente).
    if device == "cuda":
        mancante = cuda_runtime_lib_mancante()
        if mancante:
            if _device_was_explicit:
                log.error(
                    "   GPU NVIDIA ma manca %s: non posso usare CUDA. %s",
                    mancante,
                    CUDA_LIBS_REMEDY,
                )
                raise RuntimeError(
                    f"libreria CUDA mancante ({mancante}). {CUDA_LIBS_REMEDY}"
                )
            log.warning(
                "   ⚠️ GPU NVIDIA ma manca %s: CTranslate2 non puo' usare CUDA. "
                "%s Ripiego su faster-whisper su CPU (int8): piu' lento, ma la "
                "run prosegue.",
                mancante,
                CUDA_LIBS_REMEDY,
            )
            device = "cpu"
            compute_type = "int8"

    log.info(
        "2. Trascrizione con faster-whisper (%s, %s, %s, beam=%d, batch=%d)...",
        model_size,
        device,
        compute_type,
        beam_size,
        batch_size,
    )
    if openvino_available is None:
        openvino_available = openvino_usable()
    # Avviso "ci mette un po'" su QUALSIASI macchina che sta davvero
    # trascrivendo su CPU. La versione precedente lo mostrava solo quando
    # OpenVINO era un upgrade possibile, cioe' sulle macchine veloci: il
    # caso in cui l'utente ha davvero bisogno di saperlo restava muto.
    if device == "cpu":
        _warn_cpu_transcription(model_size)
    elif device == "cuda":
        # `openvino_available` dice solo che un upgrade e' POSSIBILE, non che
        # sia il motore in uso: su un portatile ibrido NVIDIA+iGPU stampava
        # "iGPU OpenVINO attiva" mentre la decodifica girava su CUDA.
        log.info("   Trascrizione su NVIDIA CUDA: accelerazione attiva.")

    # Carica modello. cpu_threads esplicito: il default di faster-whisper
    # sottoutilizza CPU con piu' core (misurato su Snapdragon X Elite: 8
    # thread ~27% piu' veloci di 4 su clip da 60s). Il tetto (non piu' dei
    # core FISICI: i fratelli SMT non danno throughput, solo context switch)
    # e' in config.auto_thread_budget; resta pero' esposto via
    # WHISPER_THREADS perche' il tetto nasce dalla misura su una sola CPU.
    n_threads = cpu_threads if cpu_threads else DEFAULT_WHISPER_THREADS
    # Rete di sicurezza: se l'accelerazione richiesta fallisce alla COSTRUZIONE
    # del modello, si ripiega su CPU invece di far morire la pipeline. Il
    # fallimento qui e' istantaneo (nessuna decodifica e' iniziata), quindi il
    # ripiego non costa il doppio del runtime.
    #
    # Motori di fallimento osservati: driver NVIDIA troppo vecchio per la
    # runtime CUDA ("CUDA driver version is insufficient for CUDA runtime
    # version"), CTranslate2 compilato senza supporto CUDA, GPU non visibile
    # dopo un suspend/riavvio del driver. Sono tutti indistinguibili dall'alto,
    # e tutti risolti dal ripiego: la trascrizione e' piu' lenta ma esce.
    #
    # Se invece e' l'utente ad aver chiesto CUDA esplicitamente, il fallback
    # silenzioso sarebbe una sorpresa: si avvisa e si lascia morire.
    try:
        model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=n_threads,
        )
    except (RuntimeError, OSError) as e:
        if device != "cuda":
            raise
        log.warning("   ⚠️  CUDA non utilizzabile (%s)", e)
        if _device_was_explicit:
            log.error(
                "   Hai chiesto esplicitamente --whisper-device cuda, quindi non ripiego "
                "silenziosamente: usa --whisper-device cpu (oppure lascia --transcriber auto)."
            )
            raise
        log.warning("   Ripiego su faster-whisper su CPU (int8): piu' lento, ma la run prosegue.")
        device = "cpu"
        compute_type = "int8"
        # Il controllo in cima non scatta piu': e' QUI che la macchina comincia
        # davvero a trascrivere su CPU, ed e' il momento in cui l'utente deve
        # sapere che i minuti che seguono sono quelli lenti.
        _warn_cpu_transcription(model_size)
        model = WhisperModel(model_size, device=device, compute_type=compute_type, cpu_threads=n_threads)
    log.debug("   Modello Whisper caricato (device=%s).", device)

    # Parametri VAD
    vad_params = vad_parameters or {
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 200,
    }

    # Codice OCR -> ISO 639-1 per Whisper (mappa condivisa con le zone cieche)
    whisper_lang = _whisper_lang(language)

    # Ripresa: se una run precedente si e' interrotta, decodifica solo la coda.
    # Il parziale e' dell'audio INTERO (timestamp assoluti) mentre il motore
    # lavora su un file tagliato: da qui i due aggiustamenti (offset e parole
    # gia' fatte).
    # I parametri della decodifica vanno nel parziale: sono cio' che rende
    # sicuro riprendere (un transcript fatto per meta' con `small` e per meta'
    # con `large` sarebbe sbagliato senza dirlo).
    _params = {
        "engine": "faster-whisper",
        "model": model_size,
        # Device e compute_type sono nei parametri della cache completa:
        # senza, un parziale CUDA/float16 veniva cucito con una coda CPU/int8
        # dopo un cambio di hardware, con timestamp di due decoder diversi.
        "device": device,
        "compute_type": compute_type,
        "beam": int(beam_size),
        "language": whisper_lang,
        "vad": bool(vad_filter),
        "batch": int(batch_size),
    }
    if riprendi:
        iniziali, sorgente, offset = _prepara_ripresa(audio_path, _params)
    else:
        iniziali, sorgente, offset = [], audio_path, 0.0
        # Il parziale NON resta: `_collect_words` lo rimuove a trascrizione
        # riuscita. Dire il contrario faceva credere a chi lo cercasse che
        # fosse ancora li' per un ripiego.
        log.info("   [Trascrizione] --no-ripresa: ignoro il parziale e decodifico da capo.")


    # Trascrizione (decoding a batch quando possibile, con ripiego sequenziale)
    try:
        all_words, info = _transcribe_with_fallback(
            model,
            sorgente,
            language=whisper_lang,
            beam_size=beam_size,
            vad_filter=vad_filter,
            vad_parameters=vad_params,
            batch_size=batch_size,
            checkpoint_path=audio_path,
            offset=offset,
            precedenti=iniziali,
            params=_params,
        )
    finally:
        if sorgente != audio_path:
            with contextlib.suppress(OSError):
                sorgente.unlink(missing_ok=True)

    if iniziali:
        all_words = iniziali + all_words
        log.info(
            "   Ripresa: %d parole recuperate dal parziale (primi %.0fs non rifatti).",
            len(iniziali),
            offset,
        )

    log.info("   Rilevata lingua: %s (probabilità %.2f)", info.language, info.language_probability)
    log.info("   Parole riconosciute: %d", len(all_words))

    # Genera trascrizione compressa
    _write_raw_transcript(all_words, audio_path)

    # Compressione semantica
    stopwords = get_stopwords(language)
    transcript = _compress_words(all_words, stopwords)

    n_blocks = transcript.count("\n") + 1 if transcript else 0
    log.info("   Blocchi semantici generati: %d", n_blocks)
    return transcript, all_words


# Modelli delle zone, condivisi dentro la stessa run. La ritrascrizione
# mirata gira a grappoli (una zona per volta) e ogni chiamata costruiva il
# modello da zero: ~500 MB di pesi riletti da disco e riportati in memoria per
# OGNI zona, piu' il tempo di costruzione. La chiave sono le quattro cose che
# `WhisperModel` usa per costruire la rete: due configurazioni diverse non
# possono condividere l'istanza (e non devono: sarebbe un modello che decodifica
# con i parametri di un'altra).
_ZONE_MODELS: dict[tuple[str, str, str, int], Any] = {}


def _zone_model(model_size: str, device: str, compute_type: str, cpu_threads: int) -> Any:
    """Il modello per le zone, riusato se la configurazione e' la stessa."""
    from faster_whisper import WhisperModel

    chiave = (model_size, device, compute_type, cpu_threads)
    modello = _ZONE_MODELS.get(chiave)
    if modello is None:
        modello = WhisperModel(
            model_size, device=device, compute_type=compute_type, cpu_threads=cpu_threads
        )
        _ZONE_MODELS[chiave] = modello
    else:
        log.debug("   [Gap] Modello delle zone riusato (nessun ricaricamento).")
    return modello


def reset_zone_models() -> None:
    """Dimentica i modelli delle zone. usato dai test per non ereditare l'istanza.

    NON CHIAMARE DA `main.py`: il riuso e' il punto di questa cache, e svuotarla
    a ogni zona riporterebbe il costo che esiste per togliere.
    """
    _ZONE_MODELS.clear()


def retranscribe_zone(
    audio_path: Path,
    t0: float,
    t1: float,
    model_size: str = "small",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str = "ita",
    beam_size: int = 5,
    pad_seconds: float = 5.0,
    cpu_threads: int | None = None,
) -> list[Word] | None:
    """Ritrascrive ``[t0, t1]`` col beam accurato per recuperare ancore garbate.

    Il beam veloce (greedy) mangia intere frasi nelle zone difficili ("la c'
    la c'..." invece di "Passiamo alla slide 3", run del 13/10): l'ancora non
    è storpiata, è distrutta, e nessuna mappa di varianti può recuperarla. Si
    ritrascrive SOLO la zona (secondi, non l'intero audio) con un po' di
    contesto ai bordi per il decoder; tornano le parole con timestamp assoluti
    dentro ``[t0, t1]``.

    ``cpu_threads`` esplicito, come nella decodifica principale: senza questo
    argomento faster-whisper sceglie da solo e su questa macchina il default si
    comporta come 4 thread. Misurato su una clip dentro una zona reale (beam 5,
    modello small int8): default 32.6s, 4 thread 34.2s, 8 thread 24.7s, con il
    testo IDENTICO (somiglianza 1.000, le stesse parole). Le zone sono la fase
    in cui si decodifica piu' volte, quindi era anche quella col costo nascosto
    piu' alto: 1.32-1.38x, senza toccare una parola dell'output.

    Limite noto, non risolto qui: le zone girano sempre su CPU con
    ``compute_type`` ``int8`` quando il chiamante non dice altro, quindi una run
    con ``--whisper-device cuda`` decodifica le zone su CPU. Non e' cambiato da
    questo intervento, che tocca solo thread e riuso del modello.

    Returns None se qualcosa fallisce (ffmpeg assente, modello non caricato):
    il chiamante prosegue senza recovery, mai un'interruzione.
    """
    import shutil
    import subprocess
    import tempfile

    if t1 <= t0 or not shutil.which("ffmpeg"):
        return None
    start = max(0.0, t0 - pad_seconds)
    try:
        with tempfile.TemporaryDirectory(prefix="sync_zone_") as tmp:
            # FLAC lossless, come `_taglia_audio`: qui si recupera PROPRIO
            # l'annuncio che il decoder veloce ha storpiato, e una ricodifica
            # AAC puo' cambiare una parola ("è" -> "e") spostando l'ancora.
            clip = Path(tmp) / "zona.flac"
            res = subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-nostdin",
                    "-ss", f"{start:.1f}", "-t", f"{t1 - start + pad_seconds:.1f}",
                    "-i", str(audio_path), "-c:a", "flac", str(clip),
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=180,
            )
            if res.returncode != 0 or not clip.exists():
                log.warning("   [Gap] Estrazione clip [%.1f-%.1f] fallita: proseguo senza recovery.", t0, t1)
                return None
            n_threads = cpu_threads if cpu_threads else DEFAULT_WHISPER_THREADS
            model = _zone_model(model_size, device, compute_type, n_threads)
            whisper_lang = _whisper_lang(language)
            segments, _ = model.transcribe(
                str(clip),
                language=whisper_lang,
                beam_size=beam_size,
                word_timestamps=True,
                vad_filter=True,
            )
            parole = _collect_words(segments)
    except Exception as e:  # recovery facoltativo, mai fatale
        log.warning("   [Gap] Ritrascrizione [%.1f-%.1f] fallita (%s): proseguo senza recovery.", t0, t1, e)
        return None
    out: list[Word] = [
        {"word": str(w["word"]), "start": float(w["start"]) + start}
        for w in parole
        if t0 <= float(w["start"]) + start <= t1
    ]
    return sorted(out, key=lambda w: float(w["start"]))

