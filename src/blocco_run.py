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
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
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
# quindi la seconda passa. Il contatore e' il numero di oggetti che credono di
# tenere il blocco: se il primo viene distrutto mentre il secondo e' ancora
# vivo, il file resta aperto e il lock del SO non si libera troppo presto.
@dataclass
class _BloccoCondiviso:
    file: IO[str]
    utenti: int = 1


_proprietari: dict[Path, _BloccoCondiviso] = {}
# Il controllo su `_proprietari` e la presa del lock del SO devono essere
# atomici fra thread dello stesso processo: senza, due thread potevano
# entrambi non trovare la chiave, creare due lock distinti e finish per
# aspettare 15 minuti uno contro l'altro.
_proprietari_lock = threading.Lock()


class BloccoRun:
    """Blocco esclusivo sulla cartella di lavoro."""

    def __init__(self, cache_dir: Path) -> None:
        self.path = cache_dir / _NOME_FILE
        # Percorso normalizzato: relativo e assoluto sono la stessa cartella,
        # quindi lo stesso blocco.
        self._chiave = self.path.resolve()
        self._file: IO[str] | None = None
        self._stato: _BloccoCondiviso | None = None

    def acquired(self, attesa_secondi: float = 0.0) -> bool:
        """True se la run puo' partire (anche quando il blocco non si crea)."""
        return self.tenta(attesa_secondi=attesa_secondi) != OCCUPATO

    def tenta(self, attesa_secondi: float = 0.0) -> str:
        """Prova a prendere il blocco e dice com'e' andata.

        Con ``attesa_secondi`` si aspetta che l'altra run finisca: di solito e'
        un rilancio mentre la prima lavora, non due run in competizione.
        """
        with _proprietari_lock:
            if self._stato is not None:
                # Questo oggetto ha gia' preso (o ereditato) il blocco: un
                # secondo `tenta`/`acquired` non deve contarlo due volte.
                return PRESO
            stato = _proprietari.get(self._chiave)
            if stato is not None:
                # Non e' un nuovo lock del SO: ci si aggrega a quello gia'
                # preso, ma il proprietario va contato, altrimenti il
                # `__del__` del primo toglierebbe la chiave e chiuderebbe
                # il file sotto il secondo.
                stato.utenti += 1
                self._stato = stato
                return PRESO
        deadline = time.time() + max(0.0, attesa_secondi)
        avvisato = False
        while True:
            with _proprietari_lock:
                # Un altro thread dello stesso processo può averlo preso
                # mentre dormivamo: non serve un lock del SO nuovo.
                stato = _proprietari.get(self._chiave)
                if stato is not None:
                    stato.utenti += 1
                    self._stato = stato
                    return PRESO
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
            # Non dormire 2s quando mancano meno di 2s alla scadenza: con
            # `attesa_secondi=0.5` l'attesa promessa era invece di 2 secondi.
            time.sleep(max(0.0, min(2.0, deadline - time.time())))

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
        except OSError as e:
            # Disco pieno/mount read-only durante la preparazione: NON e'
            # "occupato da un'altra run". Mandare OCCUPATO farebbe aspettare
            # 15 minuti e poi abortire, contro il contratto del modulo.
            fh.close()
            log.debug("   Blocco run non preparato (%s): proseguo senza blocco.", e)
            return NON_DISPONIBILE
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return OCCUPATO
        self._file = fh
        stato = _BloccoCondiviso(fh)
        self._stato = stato
        _proprietari[self._chiave] = stato
        return PRESO

    def release(self) -> None:
        """Rilascia il blocco (il SO lo rilascia comunque alla fine del processo)."""
        with _proprietari_lock:
            stato = self._stato
            if stato is None:
                # Questo oggetto non ha mai preso (o ha gia' rilasciato) il blocco.
                return
            self._stato = None
            self._file = None
            stato.utenti -= 1
            if stato.utenti > 0:
                # Un altro oggetto crede ancora di tenere il blocco: il file
                # resta aperto (e' referenziato dallo stato condiviso), quindi
                # il lock del SO resta attivo anche se questo era il
                # proprietario originale.
                return
            # Non togliere lo stato di un altro oggetto: dopo una
            # riacquisizione la chiave può puntare a un nuovo _BloccoCondiviso.
            if _proprietari.get(self._chiave) is stato:
                _proprietari.pop(self._chiave, None)
        fh = stato.file
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

    def __enter__(self) -> BloccoRun:
        """Uso con `with`: il blocco si rilascia anche se la run solleva."""
        if self.tenta() == OCCUPATO:
            raise RuntimeError(
                f"Un'altra run tiene il blocco su {self.path}: riprova quando e' finita."
            )
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:
        # main() non ha un `finally` attorno a tutta la run: se il blocco non
        # morisse con l'oggetto, un rilancio da un'altra console aspetterebbe
        # 15 minuti per un lavoro gia' finito.
        with suppress(Exception):
            self.release()
