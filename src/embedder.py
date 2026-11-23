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
from typing import Protocol, cast, runtime_checkable

import numpy as np

from config import log


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
        out = np.asarray(self._fn(texts), dtype=np.float32)
        if out.ndim == 1:
            out = out.reshape(1, -1)
        if out.size == 0:
            return out
        # Il Protocol dichiara vettori normalizzati L2, ma `_make_embed_fn`
        # non e' l'unica fonte possibile: un backend alternativo puo' non
        # normalizzare, e il contratto resterebbe solo scritto. Qui la
        # normalizzazione e' garantita.
        if not out.flags.writeable:
            # Un backend puo' restituire un array in sola lettura (es. un
            # `frombuffer`): in quel caso la copia e' obbligatoria, negli altri
            # casi si normalizza in place senza duplicare la matrice.
            out = out.copy()
        norme = np.linalg.norm(out, axis=1, keepdims=True)
        # `np.maximum` e' la guardia sullo zero: un vettore nullo non ha
        # direzione e resta nullo, senza produrre NaN nella cache dei punteggi.
        return cast(np.ndarray, np.divide(out, np.maximum(norme, 1e-9), out=out))

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
    embedder = _FastEmbedEmbedder(model_obj, _make_embed_fn(model_obj))
    if not embedder.embed_id:
        # Senza identita' la cache content-addressed non distingue i vettori
        # di questo modello da quelli di un altro: meglio non avere un
        # embedder che riusare vettori sbagliati.
        log.warning(
            "   Modello '%s' senza embed_id: embedding non disponibile "
            "(cache dei punteggi non affidabile).",
            model_name,
        )
        return None
    return embedder
