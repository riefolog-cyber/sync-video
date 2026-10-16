#!/usr/bin/env python3
"""Ricerca dei materiali nella cartella del progetto (P1: fuori da main.py).

Trova presentazione e audio anche con nomi non standard, elenca il contenuto
per i messaggi diagnostici e pulisce le directory di lavoro. Funzioni pure
(directory esplicita, nessun globale): i test le usano direttamente, main.py
le re-esporta per retro-compatibilita' (stesso schema dei moduli
pipeline_cache / pipeline_report).
"""

from __future__ import annotations

import re
from pathlib import Path

from chunks import Word

# Estensioni accettate dalla ricerca libera di presentazioni: il PDF e' nativo,
# .ppt/.pptx passano per convert_presentation_to_pdf. Stesso insieme della
# validazione (nome esatto) e di verifica_pc.prova_materiali: tre copie della
# stessa regola sarebbero tre modi di divergere, quindi chi cambia questo
# insieme aggiorna anche gli altri due punti.
PRESENTATION_SEARCH_SUFFIXES = frozenset({".pdf", ".pptx", ".ppt"})


def find_audio_file(directory: Path) -> Path | None:
    """Cerca qualsiasi file audio nella directory (glob pattern).
    Se più file audio presenti, sceglie il più recente per data di modifica."""
    extensions = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
    candidates = [f for f in directory.iterdir() if f.suffix.lower() in extensions]
    if not candidates:
        return None
    # Ordina per data di modifica (più recente prima)
    return max(candidates, key=lambda f: f.stat().st_mtime)


def find_presentation_file(directory: Path) -> Path | None:
    """Cerca una presentazione nella directory, anche con nome non standard.

    Il nome esatto ha la precedenza (presentazione.pdf, poi .pptx, poi .ppt,
    come la validazione); solo se nessuno esiste si ripiega su qualsiasi altro
    .pdf/.pptx/.ppt, il piu' recente. Serve al caso "presentazione (1).pptx"
    da doppio download o al nome personalizzato: senza, il primo avvio dice
    "manca il materiale" con il file li' accanto, e l'utente legge un falso.
    """
    try:
        files = [
            f
            for f in directory.iterdir()
            if f.is_file() and f.suffix.lower() in PRESENTATION_SEARCH_SUFFIXES
        ]
    except OSError:
        return None
    if not files:
        return None
    for nome in ("presentazione.pdf", "presentazione.pptx", "presentazione.ppt"):
        for f in files:
            if f.name.lower() == nome:
                return f
    return max(files, key=lambda f: f.stat().st_mtime)


def _parse_transcript_raw(raw_path: Path) -> list[Word] | None:
    """Legge transcript_raw.txt e ricostruisce la lista di parole Whisper.
    Formato atteso: 'parola [X.Xs]' per riga."""
    if not raw_path.exists():
        return None
    words: list[Word] = []
    pattern = re.compile(r"^(\S+)\s+\[([\d.]+)s\]")
    for line in raw_path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line.strip())
        if m:
            words.append({"word": m.group(1), "start": float(m.group(2))})
    return words if words else None


def _clean_directory(directory: Path, pattern: str = "*.png") -> int:
    """Rimuove tutti i file che corrispondono al pattern nella directory.
    Restituisce il numero di file rimossi."""
    if not directory.exists():
        return 0
    removed = 0
    for f in directory.glob(pattern):
        f.unlink()
        removed += 1
    return removed


# Quanti nomi di file mostra il messaggio di primo avvio: abbastanza per
# riconoscere la cartella, non tutti i .bat/.md del progetto.
_MATERIALI_VISTI_LIMITE = 12


def _nomi_file_visti(directory: Path, limite: int = _MATERIALI_VISTI_LIMITE) -> tuple[list[str], int]:
    """Nomi dei file nella directory (ordinati) + quanti restano fuori dal limite."""
    try:
        nomi = sorted(p.name for p in directory.iterdir() if p.is_file())
    except OSError:
        return [], 0
    return nomi[:limite], max(0, len(nomi) - len(nomi[:limite]))


def _materiali_nei_sottocartelle(directory: Path, limite: int = 4) -> list[str]:
    """File presentazione/audio nei sottocartelle immediati, come "sub/nome".

    Il caso reale e' lo ZIP con doppia cartella (sync-video-main dentro
    sync-video-main): il materiale finisce un livello sopra o sotto quello
    giusto. Solo un livello: scendere ricorsivi elencherebbe anche .cache/.
    """
    try:
        sottocartelle = sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    audio_est = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
    trovati: list[str] = []
    for sub in sottocartelle:
        if sub.name == "temp_slides":
            continue
        try:
            dentro = sorted(p for p in sub.iterdir() if p.is_file())
        except OSError:
            continue
        for p in dentro:
            if p.suffix.lower() in PRESENTATION_SEARCH_SUFFIXES or p.suffix.lower() in audio_est:
                trovati.append(f"{sub.name}/{p.name}")
                if len(trovati) >= limite:
                    return trovati
    return trovati
