#!/usr/bin/env python3
"""Ricerca dei materiali nella cartella del progetto (P1: fuori da main.py).

Trova presentazione e audio anche con nomi non standard, elenca il contenuto
per i messaggi diagnostici e pulisce le directory di lavoro. Funzioni pure
(directory esplicita, nessun globale): i test le usano direttamente, main.py
le re-esporta per retro-compatibilita' (stesso schema dei moduli
pipeline_cache / pipeline_report).
"""

from __future__ import annotations

import contextlib
import math
import re
from pathlib import Path

from chunks import Word
from config import AUDIO_SEARCH_SUFFIXES, PRESENTATION_ACCEPTED_SUFFIXES, log


def _mtime_legibile(file: Path) -> float:
    """mtime, o -inf se il file e' sparito/irraggiungibile.

    Un singolo candidato non leggibile non deve far sparire TUTTI gli altri:
    il guard `OSError` intorno a `max()` faceva dire "nessun audio" con il
    podcast buono li' accanto (race con antivirus/OneDrive).
    """
    try:
        return file.stat().st_mtime
    except OSError:
        return float("-inf")


def find_audio_file(directory: Path) -> Path | None:
    """Cerca qualsiasi file audio nella directory (glob pattern).
    Se più file audio presenti, sceglie il più recente per data di modifica.
    Anche una cartella puo' finire per .m4a/.mp3: senza `is_file()` verrebbe
    scelta come audio e la pipeline aprirebbe una directory al suo posto."""
    # Il guard NON puo' essere intorno a tutto il ciclo: `Path.is_file()`
    # rilancia OSError diversi da "file sparito", quindi un solo candidato
    # bloccato faceva dire "nessun audio" anche con il podcast buono accanto.
    # Si salta il singolo candidato, non l'intera cartella.
    try:
        voci = list(directory.iterdir())
    except OSError:
        return None
    candidates: list[Path] = []
    for f in voci:
        try:
            if f.is_file() and f.suffix.lower() in AUDIO_SEARCH_SUFFIXES:
                candidates.append(f)
        except OSError:
            continue
    if not candidates:
        return None
    # Ordina per data di modifica (più recente prima)
    return max(candidates, key=_mtime_legibile)


def find_presentation_file(directory: Path) -> Path | None:
    """Cerca una presentazione nella directory, anche con nome non standard.

    Il nome esatto ha la precedenza (presentazione.pdf, poi .pptx, poi .ppt,
    come la validazione); solo se nessuno esiste si ripiega su qualsiasi altro
    .pdf/.pptx/.ppt, il piu' recente. Serve al caso "presentazione (1).pptx"
    da doppio download o al nome personalizzato: senza, il primo avvio dice
    "manca il materiale" con il file li' accanto, e l'utente legge un falso.
    """
    # Stessa regola di `find_audio_file`: un singolo file non leggibile non
    # deve far sparire la presentazione buona che sta accanto.
    try:
        voci = list(directory.iterdir())
    except OSError:
        return None
    files: list[Path] = []
    for f in voci:
        try:
            if f.is_file() and f.suffix.lower() in PRESENTATION_ACCEPTED_SUFFIXES:
                files.append(f)
        except OSError:
            continue
    if not files:
        return None
    for nome in ("presentazione.pdf", "presentazione.pptx", "presentazione.ppt"):
        for f in files:
            if f.name.lower() == nome:
                return f
    return max(files, key=_mtime_legibile)


def _parse_transcript_raw(raw_path: Path) -> list[Word] | None:
    """Legge transcript_raw.txt e ricostruisce la lista di parole Whisper.
    Formato atteso: 'parola [X.Xs]' per riga."""
    if not raw_path.exists():
        return None
    try:
        testo = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        # Il file puo' sparire fra ``exists`` e la lettura, o essere stato
        # scritto a meta' in una codifica diversa: e' un fallback, non una
        # fonte indispensabile. Meglio nessuna parola che una run interrotta.
        log.warning("   [Avviso] transcript RAW illeggibile (%s): %s", raw_path.name, e)
        return None
    words: list[Word] = []
    # Whisper puo' emettere token con spazi ("New York [1.0s]"): `\S+`
    # buttava via l'intera parola. La cattura non-greedy si ferma al tempo.
    pattern = re.compile(r"^(.+?)\s+\[([\d.]+)s\]\s*$")
    for line in testo.splitlines():
        m = pattern.match(line.strip())
        if not m:
            continue
        # La regex accetta anche "1.2.3" (solo cifre e punti): float() allora
        # solleva ValueError. Un file di appoggio troncato o corrotto non deve
        # far fallire il fallback: si salta la riga, le altre restano.
        try:
            inizio = float(m.group(2))
        except ValueError:
            continue
        if not math.isfinite(inizio):
            continue
        words.append({"word": m.group(1), "start": inizio})
    return words if words else None


def _clean_directory(directory: Path, pattern: str = "*.png") -> int:
    """Rimuove tutti i file che corrispondono al pattern nella directory.
    Restituisce il numero di file effettivamente rimossi: un file aperto da
    un altro processo (tipico su Windows) non fa fallire la pulizia, ma non
    viene nemmeno conteggiato nel messaggio finale."""
    if not directory.exists():
        return 0
    removed = 0
    for f in directory.glob(pattern):
        rimosso = False
        with contextlib.suppress(OSError):
            f.unlink()
            rimosso = True
        if rimosso:
            removed += 1
            log.debug("   🧹 Rimosso file temporaneo: %s", f)
        else:
            log.debug("   🧹 File non rimosso (aperto da un altro processo?): %s", f)
    return removed


# Quanti nomi di file mostra il messaggio di primo avvio: abbastanza per
# riconoscere la cartella, non tutti i .bat/.md del progetto.
_MATERIALI_VISTI_LIMITE = 12


def _nomi_file_visti(directory: Path, limite: int = _MATERIALI_VISTI_LIMITE) -> tuple[list[str], int]:
    """Nomi dei file nella directory (ordinati) + quanti restano fuori dal limite."""
    try:
        voci = list(directory.iterdir())
    except OSError:
        return [], 0
    # Il guard intorno a tutta la lista faceva dire "cartella vuota" quando un
    # solo file era bloccato da antivirus/OneDrive: si protegge la SINGOLA voce,
    # come in find_audio_file, e si elencano tutti gli altri.
    nomi: list[str] = []
    for p in voci:
        try:
            if p.is_file():
                nomi.append(p.name)
        except OSError:
            continue
    nomi.sort()
    return nomi[:limite], max(0, len(nomi) - len(nomi[:limite]))


def _materiali_nei_sottocartelle(directory: Path, limite: int = 4) -> list[str]:
    """File presentazione/audio nei sottocartelle immediati, come "sub/nome".

    Il caso reale e' lo ZIP con doppia cartella (sync-video-main dentro
    sync-video-main): il materiale finisce un livello sopra o sotto quello
    giusto. Solo un livello: scendere ricorsivi elencherebbe anche .cache/.
    """
    try:
        voci = list(directory.iterdir())
    except OSError:
        return []
    sottocartelle: list[Path] = []
    for p in voci:
        try:
            if p.is_dir() and not p.name.startswith("."):
                sottocartelle.append(p)
        except OSError:
            continue
    sottocartelle.sort(key=lambda p: p.name)
    audio_est = AUDIO_SEARCH_SUFFIXES
    trovati: list[str] = []
    for sub in sottocartelle:
        if sub.name == "temp_slides":
            continue
        try:
            dentro_voci = list(sub.iterdir())
        except OSError:
            continue
        dentro: list[Path] = []
        for p in dentro_voci:
            try:
                if p.is_file():
                    dentro.append(p)
            except OSError:
                continue
        dentro.sort(key=lambda p: p.name)
        for p in dentro:
            if p.suffix.lower() in PRESENTATION_ACCEPTED_SUFFIXES or p.suffix.lower() in audio_est:
                trovati.append(f"{sub.name}/{p.name}")
                if len(trovati) >= limite:
                    return trovati
    return trovati
