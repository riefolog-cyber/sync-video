#!/usr/bin/env python3
"""
Raggruppamento parole in finestre temporali fisse.

Helper condiviso tra `semantic_sync.build_semantic_blocks` (finestre corte,
4s, che scarta i silenzi) e `llm_sync.build_llm_chunks` (finestre larghe,
30s, che conserva i vuoti come "..."). Il loop di raggruppamento è identico:
solo la politica di filtro/formato del chiamante cambia.
"""

from collections.abc import Sequence
from typing import TypedDict


class WordWindow(TypedDict):
    """Finestra temporale grezza di parole (dati non filtrati)."""

    start: float
    end: float
    first_time: float
    words: list[str]
    text: str


def build_windows(
    words: Sequence[dict],
    total_duration: float,
    window_seconds: float,
) -> list[WordWindow]:
    """Raggruppa le parole in finestre temporali fisse di `window_seconds`.

    Ogni finestra conserva i dati grezzi (inizio, fine, primo timestamp reale
    di parola, parole e testo) senza applicare filtri: spetta al chiamante
    decidere cosa tenere. Restituisce una lista vuota se non ci sono parole.
    """
    if not words:
        return []
    windows: list[WordWindow] = []
    idx = 0
    n = len(words)
    start = 0.0
    while start < total_duration - 1e-6:
        end = start + window_seconds
        chunk_words: list[str] = []
        first_time: float | None = None
        while idx < n and words[idx]["start"] < end:
            if first_time is None:
                first_time = float(words[idx]["start"])
            chunk_words.append(words[idx]["word"])
            idx += 1
        windows.append({
            "start": start,
            "end": min(end, total_duration),
            "first_time": first_time if first_time is not None else start,
            "words": chunk_words,
            "text": " ".join(chunk_words) if chunk_words else "...",
        })
        start = end
    return windows
