#!/usr/bin/env python3
"""Riuso del video gia' renderizzato.

Rilanciare la pipeline senza cambiare nulla (materiali, tempi, parametri)
produce ogni volta lo STESSO video: 71s di encoding per un file che esiste
gia'. Qui si calcola un'impronta degli input (contenuto delle slide, tempi,
contenuto dell'audio, parametri di rendering) e si riusa il video gia'
scritto, se l'impronta coincide E il file di output non e' stato toccato.

Il riuso non puo' sbagliare: l'impronta copre tutto quello che puo' cambiare
il video. Se una slide, un tempo, l'audio o l'encoder cambiano, il video si
rifa. E se il file di output e' stato spostato o modificato (dimensione o
momento di modifica diversi), si rifa.

Il "come" e' un file .json nella cache: piccolo, ispezionabile, e non lascia
niente addosso nella cartella dell'utente.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from config import CACHE_DIR, atomic_write_text, log

_CARTELLA = "video_riuso"
# VERSIONE: se la chiave cambia senso (nuovi parametri nel video), la versione
# cambia e le chiavi vecchie non valgono piu'.
_VERSIONE = "v1"


def _hash_file(path: Path, h: hashlib._Hash) -> None:
    """Aggiunge il CONTENUTO del file all'impronta."""
    try:
        with open(path, "rb") as fh:
            for blocco in iter(lambda: fh.read(1 << 20), b""):
                h.update(blocco)
    except OSError:
        h.update(b"|file-mancante|")


def chiave_video(
    slide_files: list[str],
    durations: list[float],
    audio_path: str | Path | None,
    fps: int,
    transition_duration: float,
    engine: str,
    encoder: str,
) -> str:
    """Impronta di tutto quello che determina il video.

    Cambia se cambia una slide, un tempo, l'audio o un parametro di
    rendering: in quel caso il video va rifatto.
    """
    h = hashlib.sha1()
    h.update(_VERSIONE.encode("utf-8"))
    h.update(b"\x00")
    h.update(f"{fps}|{transition_duration}|{engine}|{encoder}".encode())
    h.update(b"\x00")
    for i, slide in enumerate(slide_files):
        h.update(f"{i}|{slide}".encode())
        h.update(b"\x00")
        _hash_file(Path(slide), h)
        h.update(b"\x00")
    for d in durations:
        h.update(f"{float(d):.4f}".encode())
        h.update(b"\x00")
    if audio_path is not None:
        _hash_file(Path(audio_path), h)
    return h.hexdigest()


def _percorso_marca(output_path: Path, cache_dir: Path | None = None) -> Path:
    base = cache_dir if cache_dir is not None else CACHE_DIR
    h = hashlib.sha1(str(output_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return base / _CARTELLA / f"{h}.json"


def video_riutilizzabile(
    output_path: Path, chiave: str, cache_dir: Path | None = None
) -> bool:
    """True se il video gia' scritto e' quello che verrebbe rifatto."""
    if not output_path.exists():
        return False
    marca = _percorso_marca(output_path, cache_dir)
    try:
        dati = json.loads(marca.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(dati, dict) or dati.get("chiave") != chiave:
        return False
    try:
        stat = output_path.stat()
    except OSError:
        return False
    # Il file di output e' stato toccato (spostato, modificato, aperto da un
    # altro programma)? Allora non e' piu' il video che abbiamo scritto: si
    # rifa.
    return int(dati.get("size", -1)) == stat.st_size and int(
        dati.get("mtime_ns", -1)
    ) == stat.st_mtime_ns


def segna_video(
    output_path: Path, chiave: str, cache_dir: Path | None = None
) -> None:
    """Ricorda quale video e' stato scritto (e con quali input)."""
    try:
        stat = output_path.stat()
    except OSError:
        return
    marca = _percorso_marca(output_path, cache_dir)
    atomic_write_text(
        marca,
        json.dumps(
            {
                "chiave": chiave,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "output": str(output_path),
            },
            ensure_ascii=False,
        ),
    )


def _log_riuso(riutilizzato: bool) -> None:
    if riutilizzato:
        log.info(
            "   Video riusato: slide, tempi, audio e parametri sono gli stessi "
            "della run precedente (nessuna ricodifica)."
        )


def _dati_marca(path: Path) -> dict[str, Any]:
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dati if isinstance(dati, dict) else {}
