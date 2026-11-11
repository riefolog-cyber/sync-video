#!/usr/bin/env python3
"""Blocco per le run concorrenti sulla stessa cartella.

Due run sulla stessa cartella si pestano: la prima cancella le slide che la
seconda sta usando (``_clean_directory`` sulle PNG), la pulizia delle cache
cancella la trascrizione dell'altra, i frame di verifica vengono svuotati
sotto le mani dell'altra. Fin qui non c'era stato nessuno sblocco: due run
insieme producevano un video a meta' costruito con i pezzi dell'altro, senza
errori in console.

Il blocco e' un lock DEL SISTEMA OPERATIVO su un file: il SO lo tiene per il
processo e lo rilascia quando il processo muore (anche con un crash), quindi
non esiste il "file di blocco rimasto" da interpretare e non serve nessuna
pulizia manuale.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import IO, Any

from config import log

# I due modi di bloccare un file sono diversi per sistema operativo, e nessuno
# dei due esiste sull'altro: si importano con-tolerantemente e si usa quello
# che c'e'. `Any` perche' il type-check gira sulle tre piattaforme e su una
# sola dei due moduli e' disponibile.
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

# Byte bloccato: la posizione 0 del file. Il PID viene scritto DOPO, cosi' il
# blocco non si sposta quando il contenuto cambia.
_POSIZIONE_BLOCCO = 0
_NOME_FILE = "run.lock"

# Il blocco TENUTO da questo processo: due run NELLO STESSO processo non si
# pestano (le slide sono gia' state pulite, la cache e' la stessa), quindi il
# secondo avvio dentro lo stesso processo passa. Il caso che deve fermarsi e'
# un altro PROCESSO sulla stessa cartella.
_proprietario: BloccoRun | None = None


class BloccoRun:
    """Blocco esclusivo sulla cartella di lavoro."""

    def __init__(self, cache_dir: Path) -> None:
        self.path = cache_dir / _NOME_FILE
        self._file: IO[str] | None = None

    def acquired(self, attesa_secondi: float = 0.0) -> bool:
        """Prende il blocco. False se un altro processo lo ha gia'.

        Con ``attesa_secondi`` si ASPETTA che l'altra run finisca (il blocco e'
        del sistema operativo: quando l'altra run muore il blocco si libera
        da solo). Serve a chi aspetta: due run non si pestano, e la seconda
        parte quando la prima ha finito.
        """
        global _proprietario
        if _proprietario is not None:
            # Questo processo ha gia' il blocco: la run precedente (o un
            # test che avvia la pipeline) lo tiene ancora. Non e' una run
            # concorrente: si prosegue.
            return True
        deadline = time.time() + max(0.0, attesa_secondi)
        while True:
            if self._prova():
                return True
            if time.time() >= deadline:
                return False
            log.info(
                "   Un'altra run è già in corso su questa cartella: aspetto che finisca."
            )
            time.sleep(2.0)

    def _prova(self) -> bool:
        """Un tentativo di prendere il blocco (True se preso)."""
        global _proprietario
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - resta aperto col blocco
        except OSError as e:
            # Senza blocco la run parte lo stesso: meglio una run che puo'
            # pestarsi che una run che non parte.
            log.debug("   Blocco run non creato (%s): proseguo senza blocco.", e)
            return False
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                # Windows vuole un byte da bloccare: il file non puo' essere
                # vuoto, altrimenti locking fallisce.
                fh.write(" ")
                fh.flush()
            fh.seek(_POSIZIONE_BLOCCO)
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return False
        self._file = fh
        _proprietario = self
        try:
            fh.seek(_POSIZIONE_BLOCCO + 1)
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        """Rilascia il blocco (il SO lo rilascia comunque alla fine del processo)."""
        global _proprietario
        if _proprietario is self:
            _proprietario = None
        fh = self._file
        self._file = None
        if fh is None:
            return
        try:
            fh.seek(_POSIZIONE_BLOCCO)
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            fh.close()


def _blocco_attivo(cache_dir: Path) -> bool:
    """True se un altro processo tiene il blocco (per i messaggi)."""
    blocco = BloccoRun(cache_dir)
    if blocco.acquired():
        blocco.release()
        return False
    return True
