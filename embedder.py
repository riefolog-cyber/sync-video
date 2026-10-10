#!/usr/bin/env python3
"""
Interfaccia del backend di embedding (P2).

Scompone il contratto che finora era implicito in ``semantic_sync``:

- caricare un modello (con fallback), poi
- produrre una funzione ``texts -> vettori normalizzati``.

Gli strumenti esterni facevano sempre ``_load_embed_model`` +
``_make_embed_fn`` a mano: qui il due passi ha un nome e un Protocol,
così un backend alternativo (hf-transformers + ONNX quantizzato,
ad esempio) si può provare senza toccare ``semantic_sync.py``.

Migrati finora: ``check_fastembed_upgrade``. Restano sul percorso
vecchio ``analysis_sync`` (uso sperimentale, non bloccante) e
``semantic_sync`` stesso, che per definizione possiede il fallback.

Il backend rimane fastembed (pinnato a 0.5.1, vedi requirements.txt): il
Protocol serve a POTERLO sostituire, non a sostituirlo oggi.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Embedder(Protocol):
    """Contratto minimo di un backend di embedding."""

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Restituisce una matrice (len(texts), dim) normalizzata L2."""
        ...

    @property
    def embed_id(self) -> str:
        """Identità della configurazione per la cache content-addressed.

        Stessi testi + stessa identità = stessi vettori: senza identità la
        cache non va usata (lo stesso vincolo che semantic_sync applica
        già alla sua embed_fn).
        """
        ...


class _FastEmbedEmbedder:
    """Backend fastembed: wrappa i due passi di semantic_sync senza duplicarli."""

    def __init__(self, model_obj: object, embed_fn: Callable[[Sequence[str]], np.ndarray]) -> None:
        self._model = model_obj
        self._fn = embed_fn

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        out = self._fn(texts)
        return np.asarray(out, dtype=np.float32)

    @property
    def embed_id(self) -> str:
        return str(getattr(self._fn, "embed_id", ""))


def load_embedder(
    model_name: str,
    cache_dir: str,
    alternate_name: str | None = None,
) -> Embedder | None:
    """Carica il modello e restituisce l'Embedder pronto, o None se impossibile.

    Mantiene il fallback del modello alternativo (mpnet) previsto da
    semantic_sync: il caricamento NON viene duplicato qui, si delega.
    """
    from semantic_sync import _load_embed_model, _make_embed_fn

    model_obj = _load_embed_model(model_name, cache_dir, alternate_name=alternate_name)
    if model_obj is None:
        return None
    return _FastEmbedEmbedder(model_obj, _make_embed_fn(model_obj))
