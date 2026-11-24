#!/usr/bin/env python3
"""Riuso del video gia' renderizzato.

Rilanciare la pipeline senza cambiare nulla (materiali, tempi, parametri)
produce ogni volta lo STESSO video: 71s di encoding per un file che esiste
gia'. Qui si calcola un'impronta degli input (contenuto delle slide, tempi,
contenuto dell'audio, parametri di rendering) e si riusa il video gia'
scritto, se l'impronta coincide E il file di output non e' stato toccato.

Il riuso non puo' sbagliare: l'impronta copre tutto quello che puo' cambiare
il video. Se una slide, un tempo, l'audio, i thread o l'encoder cambiano, il
video si rifa. E se il file di output e' stato spostato o modificato
(dimensione diversa, oppure contenuto diverso a parita' di dimensione), si rifa:
il solo momento di modifica puo' cambiare per un touch di antivirus/OneDrive e
non basta piu' a invalidare il riuso.

Il "come" e' un file .json nella cache: piccolo, ispezionabile, e non lascia
niente addosso nella cartella dell'utente.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from config import CACHE_DIR, DEFAULT_VIDEO_BUFFER_SEC, DEFAULT_VIDEO_RES, atomic_write_text

_CARTELLA = "video_riuso"
# VERSIONE: se la chiave cambia senso (nuovi parametri nel video), la versione
# cambia e le chiavi vecchie non valgono piu'. v2: aggiunti i thread; v3:
# risoluzione e buffer entrano nell'impronta, il percorso delle slide ne esce.
_VERSIONE = "v3"


def _hash_file(path: Path, h: Any) -> None:
    """Aggiunge il CONTENUTO del file all'impronta."""
    try:
        with open(path, "rb") as fh:
            for blocco in iter(lambda: fh.read(1 << 20), b""):
                h.update(blocco)
    except OSError:
        h.update(b"|file-mancante|")


def _sha1_file(path: Path) -> str:
    """Hash del CONTENUTO del file di output (stream, senza leggerlo tutto in memoria)."""
    h = hashlib.sha1()
    _hash_file(path, h)
    return h.hexdigest()


def chiave_video(
    slide_files: list[str],
    durations: list[float],
    audio_path: str | Path | None,
    fps: int,
    transition_duration: float,
    engine: str,
    encoder: str,
    threads: int = 0,
    video_res: tuple[int, int] | None = None,
    video_buffer_sec: float | None = None,
) -> str:
    """Impronta di tutto quello che determina il video.

    Cambia se cambia una slide, un tempo, l'audio o un parametro di
    rendering: in quel caso il video va rifatto. ``threads`` entra
    nell'impronta perche' finisce nei flag di libx264 e un encoding con un
    altro numero di thread non e' garantito identico. Entrano anche la
    risoluzione e il buffer video (che allungano l'ultima durata), mentre il
    PERCORSO delle slide no: spostare la cartella non cambia il video.
    """
    if video_res is None:
        video_res = DEFAULT_VIDEO_RES
    if video_buffer_sec is None:
        video_buffer_sec = DEFAULT_VIDEO_BUFFER_SEC
    h = hashlib.sha1()
    h.update(_VERSIONE.encode("utf-8"))
    h.update(b"\x00")
    parametri = (
        f"{fps}|{transition_duration}|{engine}|{encoder}|{threads}|"
        f"{video_res[0]}x{video_res[1]}|{float(video_buffer_sec):.4f}"
    )
    h.update(parametri.encode())
    h.update(b"\x00")
    for i, slide in enumerate(slide_files):
        # Solo l'INDICE (l'ordine conta) e il CONTENUTO: il percorso non entra,
        # altrimenti spostare la cartella delle slide cambiava la chiave.
        h.update(f"{i}".encode())
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
    output_path: Path,
    chiave: str,
    cache_dir: Path | None = None,
    encoder_out: list[str] | None = None,
) -> bool:
    """True se il video gia' scritto e' quello che verrebbe rifatto.

    ``encoder_out`` (se dato) riceve l'encoder EFFETTIVO della run che ha
    scritto il video: riusandolo, il riepilogo non puo' dire "GPU" quando il
    file era stato codificato in CPU dopo un ripiego.
    """
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
    # Dimensione diversa: il file e' stato sostituito, si rifa senza leggerlo.
    if int(dati.get("size", -1)) != stat.st_size:
        return False
    # mtime uguale: percorso rapido, e' il caso normale (nessuna lettura del video).
    if int(dati.get("mtime_ns", -1)) == stat.st_mtime_ns:
        _ripristina_encoder(dati, encoder_out)
        return True
    # mtime diverso ma dimensione uguale: puo' essere un semplice "touch" di
    # antivirus/OneDrive oppure una modifica vera. Decide il contenuto.
    atteso = dati.get("sha1")
    if not isinstance(atteso, str):
        # Marche vecchie, scritte prima del controllo sul contenuto: senza hash
        # non si puo' distinguere un touch da una modifica, si rifa (sicuro).
        return False
    if _sha1_file(output_path) != atteso:
        return False
    # Contenuto identico: aggiorna il mtime nella marca, cosi' il prossimo
    # controllo torna al percorso rapido senza rileggere il video.
    dati["mtime_ns"] = stat.st_mtime_ns
    # Cache in sola lettura: il riuso resta valido lo stesso.
    with suppress(OSError):
        atomic_write_text(marca, json.dumps(dati, ensure_ascii=False))
    _ripristina_encoder(dati, encoder_out)
    return True


def _ripristina_encoder(dati: dict[str, object], encoder_out: list[str] | None) -> None:
    """Riporta nell'out-param l'encoder con cui il video e' stato SCRITTO."""
    if encoder_out is None:
        return
    encoder = dati.get("encoder")
    if isinstance(encoder, str) and encoder:
        encoder_out[:] = [encoder]


def segna_video(
    output_path: Path,
    chiave: str,
    cache_dir: Path | None = None,
    encoder: str | None = None,
) -> None:
    """Ricorda quale video e' stato scritto (e con quali input).

    L'hash del contenuto serve a non rifare il video quando cambia solo il
    momento di modifica (touch di antivirus/OneDrive): mtime e dimensione
    restano come controllo rapido. ``encoder`` e' quello EFFETTIVO (dopo
    l'eventuale ripiego da GPU a CPU), non quello richiesto.
    """
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
                "sha1": _sha1_file(output_path),
                "output": str(output_path),
                "encoder": encoder,
            },
            ensure_ascii=False,
        ),
    )
