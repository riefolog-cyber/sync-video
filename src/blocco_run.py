#!/usr/bin/env python3
"""Blocco per le run concorrenti sulla stessa cartella.

Due run insieme si pestano: la prima cancella le slide che la seconda sta
usando, la pulizia delle cache cancella la trascrizione dell'altra, i frame di
verifica vengono svuotati sotto le mani dell'altra. Il risultato e' un video a
meta' costruito con i pezzi dell'altro, senza errori in console.

Il blocco e' un lock DEL SISTEMA OPERATIVO su un file: il SO lo tiene per il
processo e lo rilascia quando il processo muore (anche con un crash), quindi
non resta mai un "file di blocco" da interpretare o da pulire a mano.

Tre esiti (``tenta``), perche' "occupato" e "non creabile" sono cose diverse:

- ``PRESO``: la run puo' partire;
- ``OCCUPATO``: un ALTRO processo tiene il blocco. Con ``attesa_secondi`` si
  aspetta che finisca, poi si rinuncia;
- ``NON_DISPONIBILE``: il file di blocco non si crea (cartella in sola
  lettura, ACL, disco pieno). La run parte lo stesso: meglio una run che puo'
  pestarsi che una run che non parte. Prima questo caso era indistinguibile
  da ``OCCUPATO``, quindi faceva aspettare un quarto d'ora per poi fermarsi.
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

from config import log

# I due modi di bloccare un file non esistono entrambi sullo stesso sistema:
# si importa quello che c'e'. `Any` perche' il type-check gira sulle tre
# piattaforme e su una sola dei due moduli e' disponibile.
fcntl: Any = None
msvcrt: Any = None
try:
    import fcntl as _fcntl

    fcntl = _fcntl
except ImportError:  # pragma: no cover - Windows
    pass
try:
    import msvcrt as _msvcrt

    msvcrt = _msvcrt
except ImportError:  # pragma: no cover - POSIX
    pass

# Esiti di ``tenta``.
PRESO = "preso"
OCCUPATO = "occupato"
NON_DISPONIBILE = "non_disponibile"

_NOME_FILE = "run.lock"

# Le cartelle gia' bloccate da QUESTO processo: due run nello stesso processo
# non si pestano (le slide sono gia' state pulite, la cache e' la stessa),
# quindi la seconda passa. Si ripulisce da sola in ``release``.
_proprietari: set[Path] = set()


class BloccoRun:
    """Blocco esclusivo sulla cartella di lavoro."""

    def __init__(self, cache_dir: Path) -> None:
        self.path = cache_dir / _NOME_FILE
        # Percorso normalizzato: relativo e assoluto sono la stessa cartella,
        # quindi lo stesso blocco.
        self._chiave = self.path.resolve()
        self._file: IO[str] | None = None

    def acquired(self, attesa_secondi: float = 0.0) -> bool:
        """True se la run puo' partire (anche quando il blocco non si crea)."""
        return self.tenta(attesa_secondi=attesa_secondi) != OCCUPATO

    def tenta(self, attesa_secondi: float = 0.0) -> str:
        """Prova a prendere il blocco e dice com'e' andata.

        Con ``attesa_secondi`` si aspetta che l'altra run finisca: di solito e'
        un rilancio mentre la prima lavora, non due run in competizione.
        """
        if self._chiave in _proprietari:
            return PRESO
        deadline = time.time() + max(0.0, attesa_secondi)
        avvisato = False
        while True:
            esito = self._prova()
            if esito != OCCUPATO:
                return esito
            if time.time() >= deadline:
                return OCCUPATO
            if not avvisato:
                # Un avviso solo: ripeterlo ogni 2 secondi erano centinaia di
                # righe identiche.
                log.info(
                    "   Un'altra run è già in corso su questa cartella: aspetto che finisca."
                )
                avvisato = True
            time.sleep(2.0)

    def _prova(self) -> str:
        """Un tentativo di prendere il blocco."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - resta aperto col blocco
        except OSError as e:
            log.debug("   Blocco run non creato (%s): proseguo senza blocco.", e)
            return NON_DISPONIBILE
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                # Windows non blocca un file vuoto: serve un byte. Non si
                # scrive nient'altro (nessuno legge il contenuto), quindi il
                # file non cresce a ogni run.
                fh.write(" ")
                fh.flush()
            fh.seek(0)
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return OCCUPATO
        self._file = fh
        _proprietari.add(self._chiave)
        return PRESO

    def release(self) -> None:
        """Rilascia il blocco (il SO lo rilascia comunque alla fine del processo)."""
        fh = self._file
        if fh is None:
            # Non possiede il blocco (era gia' di un altro oggetto dello stesso
            # processo): togliere la chiave libererebbe il posto a un terzo
            # tentativo che poi aspetterebbe se stesso.
            return
        self._file = None
        _proprietari.discard(self._chiave)
        try:
            fh.seek(0)
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            fh.close()

    def __del__(self) -> None:
        # main() non ha un `finally` attorno a tutta la run: se il blocco non
        # morisse con l'oggetto, un rilancio da un'altra console aspetterebbe
        # 15 minuti per un lavoro gia' finito.
        with suppress(Exception):
            self.release()
