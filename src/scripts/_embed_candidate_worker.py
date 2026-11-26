#!/usr/bin/env python3
"""
Worker eseguito DENTRO la venv isolata del test A/B fastembed.

Riceve i testi da un file JSON, carica il modello fastembed (riusando la
cache locale del modello, quindi nessun nuovo download) e scrive gli
embedding normalizzati su file .npy. Replica esattamente la logica di
``semantic_sync._make_embed_fn`` (prefisso "passage: " per i modelli e5 +
normalizzazione L2) cosi' baseline e candidata producono vettori
confrontabili.

Uso (chiamato da check_fastembed_upgrade.py, non a mano):
  python _embed_candidate_worker.py <texts.json> <model> <cache_dir> <out.npy> [batch_size]

Il batch_size e' passato dalla baseline: il padding e' per-batch, quindi se la
candidata usasse un batch diverso i vettori non sarebbero confrontabili. Se
omesso resta 64 (vecchia firma, usata solo per compatibilita').

testi.json: {"texts": [...]} oppure {"slides": [...], "blocks": [...]}.
"""

from __future__ import annotations

import json
import sys
from typing import Any, cast

import numpy as np


def _normalized_embed(model: Any, texts: list[str], batch_size: int = 64) -> np.ndarray:
    prepared = list(texts)
    if getattr(model, "model_name", "") and "e5" in str(model.model_name).lower():
        prepared = ["passage: " + t for t in prepared]
    vecs = [np.asarray(v, dtype=np.float32) for v in model.embed(prepared, batch_size=batch_size)]
    arr: np.ndarray = np.vstack(vecs).astype(np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return cast(np.ndarray, np.divide(arr, norms).astype(np.float32))


def main() -> int:
    if len(sys.argv) not in (5, 6):
        print(
            "uso: _embed_candidate_worker.py <texts.json> <model> <cache_dir> <out.npy> [batch_size]",
            file=sys.stderr,
        )
        return 2

    texts_path, model, cache_dir, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    # Stesso batch della baseline (config.DEFAULT_EMBED_BATCH): il padding del
    # modello e' per-batch, quindi batch diversi => vettori non confrontabili.
    try:
        batch_size = int(sys.argv[5]) if len(sys.argv) == 6 else 64
    except ValueError:
        print("ERRORE: batch_size non valido", file=sys.stderr)
        return 2
    if batch_size < 1:
        print("ERRORE: batch_size deve essere positivo", file=sys.stderr)
        return 2

    with open(texts_path, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)

    # La forma documentata {"slides": ..., "blocks": ...} deve embeddare
    # ENTRAMBE: con `or` il secondo gruppo veniva scartato e il confronto A/B
    # diventava falso per costruzione.
    texts = data.get("texts") or (
        list(data.get("slides") or []) + list(data.get("blocks") or [])
    )

    from fastembed import TextEmbedding

    embed_model = TextEmbedding(model_name=model, cache_dir=cache_dir)
    emb = _normalized_embed(embed_model, texts, batch_size=batch_size)
    np.save(out, emb)
    print(f"OK {emb.shape[0]}x{emb.shape[1]} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
