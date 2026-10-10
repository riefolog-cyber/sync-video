#!/usr/bin/env python3
"""Cache JSON della pipeline (P2: estrazione da main.py, shim-compatibile).

Primitive di cache content-addressed: hash file, lettura/scrittura JSON,
pulizia cache orfane. ``main.py`` le re-esporta tali e quali, quindi
``from main import X`` (forma usata dai test) continua a funzionare.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from config import CACHE_DIR, atomic_write_text, log
from llm_sync import LLM_REVIEW_CACHE_PREFIX

# Chiavi "housekeeping" che NON sono cache di contenuto: vanno conservate
# (updates_check = TTL PyPI, fastembed_ab = report A/B, sync_report =
# report ultima run, machine_setup = fatti hardware).
KEEP_CACHE_STEMS = frozenset({"machine_setup", "updates_check", "fastembed_ab", "sync_report"})


def clean_orphan_cache(active_keys: set[str], cache_dir: Path | None = None) -> int:
    """Rimuove i .json che non matchano le chiavi attive. Mai llm_* né housekeeping.

    ``cache_dir``: cartella da pulire (default: la cache del progetto). I test
    la passano esplicita indirizzando il wrapper in main, che inietta la
    propria CACHE_DIR patchabile.
    """
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    if not cd.exists():
        return 0
    removed = 0
    for cache_file in cd.glob("*.json"):
        key = cache_file.stem
        if key.startswith("llm_") or key in KEEP_CACHE_STEMS:
            continue
        if key not in active_keys:
            cache_file.unlink()
            removed += 1
            log.debug("   🧹 Cache orfana rimossa: %s", cache_file.name)
    return removed


def clean_stale_llm_cache(keep_stems: set[str], cache_dir: Path | None = None) -> int:
    """Rimuove i llm_*.json non riusati, salvo keep_stems e llm_review_*."""
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    if not cd.exists():
        return 0
    removed = 0
    for cache_file in cd.glob("llm_*.json"):
        stem = cache_file.stem
        if stem in keep_stems or stem.startswith(LLM_REVIEW_CACHE_PREFIX):
            continue
        cache_file.unlink()
        removed += 1
        log.debug("   🧹 Cache LLM orfana rimossa: %s", cache_file.name)
    return removed


def save_final_timeline(
    timeline: dict[int, float], total_duration: float, cache_dir: Path | None = None
) -> None:
    """Persiste la timeline finale come llm_timeline_finale.json (per analysis_sync.py)."""
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    cd.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, float]] = []
    ordered = sorted(timeline)
    for i, s in enumerate(ordered):
        end = timeline[ordered[i + 1]] if i + 1 < len(ordered) else total_duration
        entries.append({"slide": s, "start": round(float(timeline[s]), 3), "end": round(float(end), 3)})
    atomic_write_text(cd / "llm_timeline_finale.json", json.dumps(entries, ensure_ascii=False))
    log.info("   Timeline finale salvata in cache per la verifica (llm_timeline_finale.json).")


def save_sync_report(report: dict[str, object], cache_dir: Path | None = None) -> None:
    """Scrive sync_report.json in cache (errori ignorati)."""
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    try:
        atomic_write_text(cd / "sync_report.json", json.dumps(report, ensure_ascii=False, indent=2))
        log.debug("   Report di sincronizzazione salvato in cache (sync_report.json).")
    except OSError:
        log.debug("   Impossibile salvare il report di sincronizzazione (ignorato).")


def file_hash(path: Path) -> str:
    """MD5 streaming del contenuto di un file."""
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(block)
    return md5.hexdigest()


def cache_path(key: str, cache_dir: Path | None = None) -> Path:
    """Percorso del file di cache per una data chiave."""
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    cd.mkdir(parents=True, exist_ok=True)
    return cd / f"{key}.json"


def load_cache(key: str, cache_dir: Path | None = None) -> dict | None:
    """Carica dati dalla cache, o None se non presente/corrotto."""
    path = cache_path(key, cache_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as e:
            log.debug("   Cache corrotta (%s), ignoro: %s", key, e)
    return None


def save_cache(key: str, data: dict, cache_dir: Path | None = None) -> None:
    """Salva dati nella cache."""
    atomic_write_text(cache_path(key, cache_dir), json.dumps(data, ensure_ascii=False, indent=2))
    log.debug("   Cache salvata: %s", key)
