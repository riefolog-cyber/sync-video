#!/usr/bin/env python3
"""Cache JSON della pipeline (P2: estrazione da main.py, shim-compatibile).

Primitive di cache content-addressed: hash file, lettura/scrittura JSON,
pulizia cache orfane. ``main.py`` le re-esporta tali e quali, quindi
``from main import X`` (forma usata dai test) continua a funzionare.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

from config import CACHE_DIR, atomic_write_text, log

# Chiavi "housekeeping" che NON sono cache di contenuto: vanno conservate
# (updates_check = TTL PyPI, fastembed_ab = report A/B, sync_report =
# report ultima run, timeline_finale = timeline validata dell'ultima run
# letta da analysis_sync.py, machine_setup = fatti hardware, hardware = profilo
# hardware + encoder ffmpeg di questa macchina, openvino_attempt = tentativo
# GPU incompiuto: cancellarlo alla pulizia di avvio disattiverebbe per sempre
# il ripiego automatico su CPU dopo un crash nativo).
KEEP_CACHE_STEMS = frozenset(
    {
        "machine_setup",
        "updates_check",
        "fastembed_ab",
        "sync_report",
        "timeline_finale",
        "hardware",
        "openvino_attempt",
    }
)


def clean_orphan_cache(active_keys: set[str], cache_dir: Path | None = None) -> int:
    """Rimuove i .json che non matchano le chiavi attive. Mai housekeeping.

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
        if key in KEEP_CACHE_STEMS:
            continue
        if key not in active_keys:
            try:
                cache_file.unlink()
            except OSError as e:
                # La pulizia è housekeeping: un file bloccato (antivirus,
                # editor) non deve abortire l'avvio.
                log.debug("   🧹 Cache orfana non rimossa (%s): %s", cache_file.name, e)
                continue
            removed += 1
            log.debug("   🧹 Cache orfana rimossa: %s", cache_file.name)
    return removed


def save_final_timeline(
    timeline: dict[int, float], total_duration: float, cache_dir: Path | None = None
) -> None:
    """Persiste la timeline finale come timeline_finale.json (per analysis_sync.py).

    Se la scrittura fallisce il file precedente viene RIMOSSO: la scrittura
    atomica lo lascerebbe intatto, e ``analysis_sync.py`` lo leggerebbe come la
    timeline di QUESTA run, riportando falsi mismatch su un video che non ha
    usato quei confini. Meglio nessun file (la verifica si ferma e lo dice) che
    un file sbagliato.
    """
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    cd.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, float]] = []
    ordered = sorted(timeline)
    for i, s in enumerate(ordered):
        end = timeline[ordered[i + 1]] if i + 1 < len(ordered) else total_duration
        entries.append({"slide": s, "start": round(float(timeline[s]), 3), "end": round(float(end), 3)})
    path = cd / "timeline_finale.json"
    try:
        atomic_write_text(path, json.dumps(entries, ensure_ascii=False))
    except OSError:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    log.info("   Timeline finale salvata in cache per la verifica (timeline_finale.json).")


def save_sync_report(report: dict[str, object], cache_dir: Path | None = None) -> None:
    """Scrive sync_report.json in cache (errori ignorati)."""
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    try:
        atomic_write_text(cd / "sync_report.json", json.dumps(report, ensure_ascii=False, indent=2))
        log.debug("   Report di sincronizzazione salvato in cache (sync_report.json).")
    except OSError:
        log.debug("   Impossibile salvare il report di sincronizzazione (ignorato).")


def load_sync_report(cache_dir: Path | None = None) -> dict[str, object] | None:
    """Legge ``sync_report.json`` dalla cache (None se assente o illeggibile).

    È l'artefatto che descrive cosa è finito a schermo: rileggerlo deve costare
    un file, non una pipeline (vedi la revisione dei confini in main.py).
    """
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    path = cd / "sync_report.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def latest_transcript_words(cache_dir: Path | None = None, preferred_key: str | None = None) -> list[dict]:
    """Parole della trascrizione più recente in cache (vuota se non c'è).

    Serve alla revisione dei report scritti prima del controllo incrociato: lì
    ``mentions_off`` non esiste e va ricalcolato dalle parole. Il nome del file
    contiene la chiave della cache (modello, lingua, beam): la più recente è
    quella coerente col report che le sta accanto.

    ``preferred_key`` (se presente) ha la precedenza: è la chiave che la run
    ha effettivamente usato, quindi le sue parole sono quelle giuste anche se
    un'altra trascrizione è più recente. Si prova prima la variante con le zone
    ricucite (``_gap``), poi la base.
    """
    cd = cache_dir if cache_dir is not None else CACHE_DIR
    if preferred_key:
        for suffix in ("_gap", ""):
            path = cd / f"{preferred_key}{suffix}.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.debug("   Trascrizione in cache illeggibile (%s): %s", path, e)
                continue
            words = data.get("words_raw") if isinstance(data, dict) else None
            if isinstance(words, list) and words:
                return [w for w in words if isinstance(w, dict)]
    for path in sorted(
        cd.glob("transcript_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.debug("   Trascrizione in cache illeggibile (%s): %s", path, e)
            continue
        words = data.get("words_raw") if isinstance(data, dict) else None
        if isinstance(words, list) and words:
            return [w for w in words if isinstance(w, dict)]
    return []


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
    """Salva dati nella cache (errore di scrittura ignorato: è un'ottimizzazione).

    La cache non deve mai uccidere una run che ha già il risultato in mano:
    disco pieno, ACL o share di rete in sola lettura costano un ricalcolo alla
    prossima esecuzione, non la run corrente.
    """
    try:
        atomic_write_text(cache_path(key, cache_dir), json.dumps(data, ensure_ascii=False, indent=2))
        log.debug("   Cache salvata: %s", key)
    except OSError as e:
        log.debug("   Cache non salvata (%s): %s", key, e)
