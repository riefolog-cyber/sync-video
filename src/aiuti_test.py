#!/usr/bin/env python3
"""Precondizioni dell'ambiente per i test che usano strumenti di sistema.

Tesseract non e' sempre nel PATH: su Windows il progetto lo cerca in cartelle
note (`config._TESSERACT_CANDIDATE`), ed e' cosi' che funziona sul PC di chi
lo usa. Un test che guarda solo il PATH si salta su una macchina dove il
programma funziona benissimo: qui si usa la STESSA ricerca.
"""

from __future__ import annotations

from pathlib import Path


def eseguibile_tesseract() -> str | None:
    """Il percorso di tesseract come lo trova il programma (None se manca)."""
    import shutil

    nel_path = shutil.which("tesseract")
    if nel_path:
        return nel_path
    from config import _TESSERACT_CANDIDATE

    for candidato in _TESSERACT_CANDIDATE:
        if Path(candidato).exists():
            return candidato
    return None


def prepara_tesseract() -> str | None:
    """Come sopra, ma lo comunica anche a pytesseract (uso in-process)."""
    percorso = eseguibile_tesseract()
    if percorso:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = percorso
    return percorso
