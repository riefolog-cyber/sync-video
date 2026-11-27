#!/usr/bin/env python3
"""
Raggruppamento parole in finestre temporali fisse.

Helper di `semantic_sync.build_semantic_blocks` (finestre corte, 4s, che
scarta i silenzi). Il loop di raggruppamento è condiviso: cambia solo la
politica di filtro/formato del chiamante.
"""

import math
from bisect import bisect_left
from collections.abc import Sequence
from typing import TypedDict


class Word(TypedDict):
    """Parola riconosciuta con timestamp (formato Whisper/OpenVINO).

    Le trascrizioni possono includere chiavi extra (confidenza, ecc.): il
    TypedDict richiede solo ``word`` e ``start``.
    """

    word: str
    start: float


class Segment(TypedDict):
    """Segmento di timeline: una slide con inizio e fine."""

    slide: int
    start: float
    end: float


class WordWindow(TypedDict):
    """Finestra temporale grezza di parole (dati non filtrati)."""

    start: float
    end: float
    first_time: float
    words: list[str]
    text: str


def build_windows(
    words: Sequence[Word],
    total_duration: float,
    window_seconds: float,
    min_window_seconds: float = 1.0,
) -> list[WordWindow]:
    """Raggruppa le parole in finestre temporali fisse di `window_seconds`.

    Ogni finestra conserva i dati grezzi (inizio, fine, primo timestamp reale
    di parola, parole e testo) senza applicare filtri: spetta al chiamante
    decidere cosa tenere. Restituisce una lista vuota se non ci sono parole.

    ``window_seconds`` non positivi vengono alzati a ``min_window_seconds``:
    senza il clamp il ciclo non terminerebbe mai (vedi il commento sotto).
    """
    if not words:
        return []
    # Difesa contro il loop infinito: con window_seconds <= 0 `end` non avanza
    # mai, `start = end` non cambia nulla e il while non termina mai appendendo
    # finestre vuote (memoria che cresce senza limite, CPU al 100%, nessuna
    # via d'uscita). Raggiungibile da `SEMANTIC_WINDOW=0` nel .env o da
    # `--semantic-window 0`, che non validavano nulla.
    # `max(min_window_seconds, window_seconds)` e' preferibile a un errore:
    # una finestra da 1s degrada la qualita' dell'allineamento, ma produce
    # comunque una timeline, mentre l'eccezione uccide la run su un refuso.
    step = max(min_window_seconds, float(window_seconds))
    # La durata arriva dai metadati audio, quindi puo' essere `inf` (stream
    # senza fine) o `NaN` (JSON sporco): il `while` qui sotto non aveva alcun
    # tetto e con `inf` girava per sempre appendendo finestre vuote. Con una
    # durata inutilizzabile si usa l'ultimo timestamp delle parole + una
    # finestra: si perde il silenzio finale, ma la run termina e lo dice.
    try:
        durata = float(total_duration)
    except (TypeError, ValueError, OverflowError):
        durata = 0.0
    if not math.isfinite(durata) or durata <= 0:
        tempi: list[float] = []
        for w in words:
            try:
                t = float(w["start"])
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(t):
                tempi.append(t)
        durata = (max(tempi) if tempi else 0.0) + step
    windows: list[WordWindow] = []
    # `TimeIndex` tollera parole non ordinate e ripiega su scansione lineare:
    # qui il puntatore `idx` dava per scontato l'ordine, e un solo timestamp
    # fuori posto (correzione ASR/OCR) risucchiava tutte le parole seguenti
    # nella finestra sbagliata. Ordinare una volta e' equivalente nel caso
    # normale e corretto in quello patologico.
    words = sorted(words, key=lambda w: float(w["start"]))
    idx = 0
    n = len(words)
    start = 0.0
    while start < durata - 1e-6:
        end = start + step
        chunk_words: list[str] = []
        first_time: float | None = None
        while idx < n and words[idx]["start"] < end:
            if first_time is None:
                first_time = float(words[idx]["start"])
            chunk_words.append(words[idx]["word"])
            idx += 1
        windows.append(
            {
                "start": start,
                "end": min(end, durata),
                "first_time": first_time if first_time is not None else start,
                "words": chunk_words,
                "text": " ".join(chunk_words) if chunk_words else "...",
            }
        )
        start = end
    return windows


class TimeIndex:
    """I timestamp delle parole, pronti per le ricerche binarie.

    Le finestre di parlato dietro le ancore "slide N" si chiedono una volta per
    ancora. Ricostruire ogni volta la lista dei timestamp (e ricontrollare che
    sia ordinata) costava O(parole) per ancora: con 40 ancore su 20k parole
    erano 800k letture. L'indice si costruisce UNA volta e ogni finestra costa
    una ricerca binaria piu' le parole che ci stanno dentro.

    Se i timestamp NON sono crescenti la ricerca binaria darebbe un risultato
    sbagliato: ``ordered`` lo dice una volta sola e in quel caso si usa la
    scansione lineare, corretta per definizione.
    """

    __slots__ = ("ordered", "times", "words")

    def __init__(self, words: Sequence[Word]) -> None:
        # Tupla: `times` e `words` devono restare la stessa sequenza. Con la
        # lista del chiamante, una modifica dopo la costruzione li separerebbe
        # e la ricerca binaria restituirebbe parole sbagliate.
        self.words: tuple[Word, ...] = tuple(words)
        self.times = [float(w["start"]) for w in self.words]
        self.ordered = all(
            self.times[i] <= self.times[i + 1] for i in range(len(self.times) - 1)
        )

    def window(self, start: float, end: float) -> list[str]:
        """Parole in ``[start, end)``, come
        ``[w["word"] for w in words if start <= w["start"] < end]``."""
        if end <= start:
            return []
        if not self.ordered:
            return [str(w["word"]) for w in self.words if start <= float(w["start"]) < end]
        lo = bisect_left(self.times, start)
        hi = bisect_left(self.times, end)
        return [str(w["word"]) for w in self.words[lo:hi]]


def words_in_window(
    words: Sequence[Word],
    start: float,
    end: float,
    index: TimeIndex | None = None,
) -> list[str]:
    """Parole pronunciate nella finestra ``[start, end)``, nell'ordine in cui sono.

    Usata per gli stralci di parlato che stanno dietro un'ancora "slide N"
    (verifica del mapping, prompt di verifica).

    ``index`` (facoltativo) e' il ``TimeIndex`` delle STESSE parole: passarlo
    quando le finestre sono piu' di una evita di ricostruirlo ogni volta.
    Senza, l'indice si costruisce qui (comodo per una chiamata sola).

    Il risultato è identico a
    ``[w["word"] for w in words if start <= w["start"] < end]``.
    """
    indice = index if index is not None else TimeIndex(words)
    return indice.window(start, end)


def words_text_in_window(
    words: Sequence[Word],
    start: float,
    end: float,
    separator: str = " ",
    index: TimeIndex | None = None,
) -> str:
    """Come ``words_in_window`` ma già unite in una stringa (con `.strip()`).

    ``index``: vedi ``words_in_window``.
    """
    return separator.join(words_in_window(words, start, end, index)).strip()
