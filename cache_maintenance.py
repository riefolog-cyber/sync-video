#!/usr/bin/env python3
"""
Gestione cache del progetto (P1: policy e pulizia sicura).

Raccoglie in un punto solo:
- misura del peso della cache (``--cache-du``),
- pulizia sicura di orfani + embedding LRU + frame di verifica
  (``--clean-cache``), senza mai toccare i modelli pesanti
  (``embedding_model/``, ``whisper_openvino_*``) né le chiavi
  housekeeping (machine_setup, updates_check, fastembed_ab, sync_report).

I modelli si riscaricano SOLO con ``--clean-cache --include-models``
(esplicito, con conferma salvo ``--yes``).
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

# Cartelle con i pesi ML: cancellarle costa GB di re-download.
MODEL_DIRS = ("embedding_model", "whisper_openvino_small", "whisper_openvino_base", "whisper_openvino_medium")
# Chiavi che non sono cache di contenuto: vanno conservate.
KEEP_STEMS = frozenset({"machine_setup", "updates_check", "fastembed_ab", "sync_report"})


def _dir_size(cartella: Path) -> int:
    """Somma i byte dei file sotto una cartella (ignora errori di lettura)."""
    totale = 0
    for f in cartella.rglob("*"):
        try:
            if f.is_file():
                totale += f.stat().st_size
        except OSError:
            continue
    return totale


def cache_disk_usage(cache_dir: Path) -> list[tuple[str, int]]:
    """Peso in byte di ogni voce di primo livello della cache, dal più pesante."""
    righe: list[tuple[str, int]] = []
    if not cache_dir.exists():
        return righe
    for voce in sorted(cache_dir.iterdir(), key=lambda p: p.name):
        with contextlib.suppress(OSError):
            if voce.is_dir() and not voce.is_symlink():
                righe.append((voce.name + "/", _dir_size(voce)))
            else:
                righe.append((voce.name, voce.stat().st_size))
    righe.sort(key=lambda r: r[1], reverse=True)
    return righe


def format_bytes(num: int) -> str:
    """12345678 -> '11.8 MB'."""
    valore = float(num)
    for unita in ("B", "KB", "MB", "GB", "TB"):
        if valore < 1024.0 or unita == "TB":
            return f"{valore:.1f} {unita}" if unita != "B" else f"{int(valore)} B"
        valore /= 1024.0
    return f"{valore:.1f} TB"


def clean_cache(
    cache_dir: Path,
    *,
    include_models: bool = False,
    clean_verify_frames: bool = True,
) -> dict[str, int]:
    """Pulizia sicura. Ritorna conteggi {'orphan_json': n, 'embed_npz': n,
    'verify_frames': n, 'model_bytes': b}.

    Non tocca mai: chiavi KEEP_STEMS, llm_*.json (hash di contenuto),
    modelli pesanti (salvo include_models=True).
    """
    import main as _main
    from semantic_sync import _prune_embed_cache  # import locale: evita cicli

    esito: dict[str, int] = {"orphan_json": 0, "embed_npz": 0, "verify_frames": 0, "model_bytes": 0}
    if not cache_dir.exists():
        return esito
    # 1. JSON orfani (slide/trascrizioni di run precedenti): senza chiavi
    # attive, rimuove solo ciò che non è housekeeping né llm_*.
    esito["orphan_json"] = _main._clean_orphan_cache(set())
    # 2. embedding content-addressed oltre il tetto LRU (40 voci).
    emb_dir = cache_dir / "embedding_cache"
    prima = len(list(emb_dir.glob("emb_*.npz"))) if emb_dir.exists() else 0
    # La cache è un'ottimizzazione: mai far fallire la pulizia per un prune.
    with contextlib.suppress(Exception):
        _prune_embed_cache()
    dopo = len(list(emb_dir.glob("emb_*.npz"))) if emb_dir.exists() else 0
    esito["embed_npz"] = max(0, prima - dopo)
    # 3. frame di verifica del video precedente (rigenerati a ogni run).
    if clean_verify_frames:
        vf = cache_dir / "verify_frames"
        if vf.exists():
            frame = [f for f in vf.glob("*.png")]
            esito["verify_frames"] = len(frame)
            for f in frame:
                with contextlib.suppress(OSError):
                    f.unlink()
    # 4. modelli pesanti SOLO su richiesta esplicita.
    if include_models:
        for nome in MODEL_DIRS:
            cartella = cache_dir / nome
            if cartella.exists():
                try:
                    peso = sum(f.stat().st_size for f in cartella.rglob("*") if f.is_file())
                except OSError:
                    peso = 0
                shutil.rmtree(cartella, ignore_errors=True)
                esito["model_bytes"] += peso
    return esito
