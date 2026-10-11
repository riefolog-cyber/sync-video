#!/usr/bin/env python3
"""
Raggruppamento parole in finestre temporali fisse.

Helper condiviso tra `semantic_sync.build_semantic_blocks` (finestre corte,
4s, che scarta i silenzi) e `llm_sync.build_llm_chunks` (finestre larghe,
30s, che conserva i vuoti come "..."). Il loop di raggruppamento è identico:
solo la politica di filtro/formato del chiamante cambia.
"""

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
    windows: list[WordWindow] = []
    idx = 0
    n = len(words)
    start = 0.0
    while start < total_duration - 1e-6:
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
                "end": min(end, total_duration),
                "first_time": first_time if first_time is not None else start,
                "words": chunk_words,
                "text": " ".join(chunk_words) if chunk_words else "...",
            }
        )
        start = end
    return windows


def words_in_window(
    words: Sequence[Word],
    start: float,
    end: float,
) -> list[str]:
    """Parole pronunciate nella finestra ``[start, end)``, nell'ordine in cui sono.

    Usata per gli stralci di parlato che stanno dietro un'ancora "slide N"
    (verifica del mapping, prompt di verifica). La stessa finestra veniva
    ricalcolata con una scansione lineare di TUTTE le parole a ogni ancora:
    con 40 ancore su 20k parole sono 800k confronti, mentre la finestra copre
    solo una frazione del vocabolario. Con i timestamp già in ordine
    (garantito dalla trascrizione) due ricerche binariche bastano: la stessa
    finestra si ottiene in tempo costante invece che lineare.

    Il risultato è identico a
    ``[w["word"] for w in words if start <= w["start"] < end]``: la ricerca
    binaria individua esattamente l'intervallo che soddisfa la condizione,
    quindi ordine e contenuto non cambiano.

    Se i timestamp NON fossero crescenti la ricerca binaria darebbe un
    risultato sbagliato: si ricade allora sulla scansione lineare, corretta
    per definizione. Il controllo costa O(n) una volta sola e il caso normale
    (trascrizione ordinata) non lo subisce.
    """
    if end <= start:
        return []
    times = [float(w["start"]) for w in words]
    if not all(times[i] <= times[i + 1] for i in range(len(times) - 1)):
        return [str(w["word"]) for w in words if start <= float(w["start"]) < end]
    lo = bisect_left(times, start)
    hi = bisect_left(times, end)
    return [str(w["word"]) for w in words[lo:hi]]


def words_text_in_window(
    words: Sequence[Word],
    start: float,
    end: float,
    separator: str = " ",
) -> str:
    """Come ``words_in_window`` ma già unite in una stringa (con `.strip()`)."""
    return separator.join(words_in_window(words, start, end)).strip()
