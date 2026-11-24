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
import time
from collections.abc import Mapping
from pathlib import Path

# Cartelle con i pesi ML: cancellarle costa GB di re-download. Si scoprono
# nella cache invece di stare in un elenco fisso: con l'elenco fisso un modello
# aggiunto dopo (es. whisper_openvino_tiny) non veniva mai rimosso, nemmeno con
# --clean-cache --include-models.
EMBEDDING_MODEL_DIR = "embedding_model"
OPENVINO_MODEL_PREFIX = "whisper_openvino_"
# I parziali di una trascrizione interrotta (transcription.percorso_parziale)
# stanno in una SOTTOCARTELLA: la pulizia degli orfani guarda i .json in radice
# e non li vede, quindi senza questa regola resterebbero per sempre.
PARZIALI_DIR = "parziali"
PARZIALI_MAX_GIORNI = 7
# Chiavi che non sono cache di contenuto: vanno conservate (stesso elenco di
# pipeline_cache.KEEP_CACHE_STEMS: il test di equivalenza li tiene allineati).
KEEP_STEMS = frozenset(
    {
        "machine_setup",
        "updates_check",
        "fastembed_ab",
        "sync_report",
        "timeline_finale",
        "hardware",
        "openvino_attempt",
        "pin_overrides",
    }
)


def _model_dirs(cache_dir: Path) -> list[str]:
    """Cartelle dei modelli pesanti presenti in ``cache_dir``.

    ``embedding_model`` resta prima (ordine storico dei messaggi), poi le
    ``whisper_openvino_*`` in ordine alfabetico. La scoperta dinamica copre
    anche i modelli non ancora noti al momento della scrittura di questo modulo.
    """
    trovati: list[str] = []
    if (cache_dir / EMBEDDING_MODEL_DIR).is_dir():
        trovati.append(EMBEDDING_MODEL_DIR)
    with contextlib.suppress(OSError):
        trovati.extend(
            sorted(p.name for p in cache_dir.iterdir() if p.is_dir() and p.name.startswith(OPENVINO_MODEL_PREFIX))
        )
    return trovati


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


def descrizione_pulizia_cache(esito: Mapping[str, int]) -> str:
    """Riga di log della pulizia, COMPRESI i parziali vecchi rimossi.

    `clean_cache` restituisce da sempre `esito["parziali"]`, ma il messaggio di
    `main.py` lo ometteva: sembrava che la quarta voce non fosse stata toccata.
    Questa funzione e' il testo unico da usare nel log (una sola formulazione,
    con i conteggi forniti da `clean_cache`).
    """
    modelli = (
        f", modelli rimossi ({format_bytes(esito.get('model_bytes', 0))})"
        if esito.get("model_bytes", 0)
        else ""
    )
    return (
        f"Cache pulita: {esito.get('orphan_json', 0)} JSON orfani, "
        f"{esito.get('embed_npz', 0)} embedding oltre il tetto, "
        f"{esito.get('verify_frames', 0)} frame di verifica, "
        f"{esito.get('parziali', 0)} parziali vecchi{modelli}."
    )


def clean_cache(
    cache_dir: Path,
    *,
    include_models: bool = False,
    clean_verify_frames: bool = True,
    active_keys: set[str] | None = None,
) -> dict[str, int]:
    """Pulizia sicura. Ritorna conteggi {'orphan_json': n, 'embed_npz': n,
    'verify_frames': n, 'parziali': n, 'model_bytes': b}.

    Non tocca mai: chiavi KEEP_STEMS, modelli pesanti (salvo
    include_models=True), parziali di trascrizione recenti (la ripresa li usa).
    """
    from pipeline_cache import clean_orphan_cache as _clean_orphan_cache
    from pipeline_cache import load_sync_report as _load_sync_report
    from pipeline_cache import save_sync_report as _save_sync_report
    from semantic_sync import _prune_embed_cache  # import locale: evita cicli

    esito: dict[str, int] = {
        "orphan_json": 0,
        "embed_npz": 0,
        "verify_frames": 0,
        "parziali": 0,
        "model_bytes": 0,
    }
    if not cache_dir.exists():
        return esito
    # 1. JSON orfani (slide/trascrizioni di run precedenti): con chiavi attive
    # rimuove solo ciò che non è housekeeping NE' di un input corrente.
    # ``active_keys`` e' il punto: lanciata da sola, la pulizia non conosce i
    # materiali della cartella e senza questo elenco cancellava anche le cache
    # di trascrizione e slide di QUESTA coppia (misurato: la run successiva
    # rifaceva OCR e Whisper da zero, minuti buttati).
    esito["orphan_json"] = _clean_orphan_cache(set(active_keys or ()), cache_dir=cache_dir)
    # 2. embedding content-addressed oltre il tetto (300 MB e 30 giorni).
    # La potatura dice quante voci ha rimosso: un secondo glob qui avrebbe
    # potuto guardare una cartella diversa da quella potata (era il difetto).
    # La cache è un'ottimizzazione: mai far fallire la pulizia per un prune.
    with contextlib.suppress(Exception):
        esito["embed_npz"] = _prune_embed_cache(cache_dir)
    # 3. frame di verifica del video precedente (rigenerati a ogni run).
    if clean_verify_frames:
        vf = cache_dir / "verify_frames"
        if vf.exists():
            frame = [f for f in vf.glob("*.png")]
            esito["verify_frames"] = len(frame)
            for f in frame:
                with contextlib.suppress(OSError):
                    f.unlink()
        # Il report dell'ultima run cita quei frame nei mismatches (tempo + slide):
        # cancellati i file, il riferimento resterebbe appeso a un file inesistente.
        # Azzeriamo il campo: la verifica successiva lo riscrive con i frame nuovi.
        # Anche questa scrittura e' manutenzione: se il disco non collabora
        # non deve interrompere i passi 4 e 5 (parziali e modelli), come non
        # li interrompono le unlink protette qui sopra.
        with contextlib.suppress(OSError):
            report = _load_sync_report(cache_dir=cache_dir)
            if report and report.get("frame_check") is not None:
                report["frame_check"] = None
                _save_sync_report(report, cache_dir=cache_dir)
    # 4. parziali di trascrizioni interrotte, vecchi: dopo una settimana non
    #    servono piu' a nessuno (se l'audio non e' stato rifatto, la run e'
    #    finita comunque). I recenti RESTANO: sono il punto della ripresa.
    parziali = cache_dir / PARZIALI_DIR
    if parziali.exists():
        scadenza = time.time() - PARZIALI_MAX_GIORNI * 86400
        for f in list(parziali.glob("*")):
            with contextlib.suppress(OSError):
                if f.is_file() and f.stat().st_mtime < scadenza:
                    f.unlink()
                    esito["parziali"] += 1
    # 5. modelli pesanti SOLO su richiesta esplicita.
    if include_models:
        for nome in _model_dirs(cache_dir):
            cartella = cache_dir / nome
            if cartella.exists():
                try:
                    peso = sum(f.stat().st_size for f in cartella.rglob("*") if f.is_file())
                except OSError:
                    peso = 0
                shutil.rmtree(cartella, ignore_errors=True)
                esito["model_bytes"] += peso
    return esito
