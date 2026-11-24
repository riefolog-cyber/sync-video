#!/usr/bin/env python3
"""Un unico comando per la verifica completa del codice.

Esegue, nello stesso ordine della CI (.github/workflows/ci.yml) piu' i
controlli che il progetto fa girare a mano:

  1. ruff check .                     lint, come la CI
  2. mypy --platform win32 .          type-check su Windows
  3. mypy --platform linux .          la CI gira su ubuntu-latest: e' IL caso
  4. mypy --platform darwin .         l'altra piattaforma supportata
  5. unittest discover -p 'test_*.py' la stessa suite della CI
  6. _debug_hardware.py               encoder reali, ripiego a runtime

Perche' tre piattaforme e non un `mypy .`: in locale il type-check vede solo
il sistema di chi sviluppa. Un import condizionale passa su Windows e fa
fallire la CI su Linux (e' gia' successo: vedi docs/PIANO_OTTIMIZZAZIONI.md).
Sulla CI `mypy .` equivale a `mypy --platform linux .`, quindi questi tre
comandi coprono esattamente la CI e in piu' le piattaforme che la CI non ha.

NON e' la verifica post-run del VIDEO (quella e' analysis_sync.py, vedi
docs/verifica.md): qui si controlla il CODICE, non l'artefatto generato.

Uso:
    .\\controlli.bat                 Windows: usa il Python del progetto
    python controlli.py             ovunque

Esito unico: 0 se TUTTO passa, 1 se almeno un controllo fallisce (2 se il
filtro non trova nulla). Di un controllo fallito si stampa l'output completo;
per quelli che passano basta la riga di esito.

Opzionale: passando uno o piu' testi si eseguono solo i controlli il cui nome
li contiene (es. `python controlli.py mypy`), utile per iterare in fretta.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hardware import project_root

# I comandi qui sotto girano con cwd=RADICE e cercano file di radice
# (requirements.txt, .github/). Con `__file__.parent` diventerebbe src/ e i
# percorsi relativi punterebbero a src/src: fallirebbe senza toccare codice.
RADICE = project_root()


@dataclass(frozen=True)
class Controllo:
    """Un passo della verifica: cosa esegue, perche' esiste, come si riassume."""

    nome: str
    argomenti: list[str]
    perche: str
    # Regex della riga che riassume l'esito. Esplicita perche' l'ultima riga
    # dell'output non e' sempre il verdetto: _debug_hardware.py scrive i log su
    # stderr, che finisce DOPO il suo "RISULTATO: ..." stampato su stdout.
    riassunto: str = ""
    # Timeout del figlio, in secondi. Ogni passo ha il suo: la suite unittest
    # puo' scaricare modelli (minuti), ruff/mypy no. Senza, un figlio appeso
    # (rete, lock, subprocess figlia) blocca `controlli.bat` per sempre e non
    # si riesce nemmeno a capire quale passo si e' fermato.
    timeout: int = 300


@dataclass
class Esito:
    """Cosa e' successo a un passo: codice di uscita, durata, output."""

    controllo: Controllo
    codice: int
    secondi: float
    output: str


def _passi() -> list[Controllo]:
    """I controlli: prima quelli della CI, poi gli extra locali."""
    py = sys.executable
    return [
        Controllo(
            "ruff",
            [py, "-m", "ruff", "check", "."],
            "guardrail di stile, come la CI",
            r"All checks passed!",
        ),
        Controllo(
            "mypy win32",
            [py, "-m", "mypy", "--platform", "win32", "."],
            "type-check su Windows",
            r"Success: no issues found",
        ),
        Controllo(
            "mypy linux",
            [py, "-m", "mypy", "--platform", "linux", "."],
            "la CI gira su ubuntu-latest",
            r"Success: no issues found",
        ),
        Controllo(
            "mypy darwin",
            [py, "-m", "mypy", "--platform", "darwin", "."],
            "l'altra piattaforma supportata",
            r"Success: no issues found",
        ),
        Controllo(
            "unittest",
            [py, "-m", "unittest", "discover", "-s", "src", "-p", "test_*.py"],
            "la stessa suite della CI",
            r"^(OK|FAILED)",
            timeout=900,  # primo avvio: puo' scaricare modelli da gigabyte
        ),
        Controllo(
            "debug hardware",
            # resolve(): il modulo e' invocabile "da ovunque" (anche
            # `cd src && python controlli.py`); con __file__ relativo il
            # figlio cercava `_debug_hardware.py` nella RADICE con
            # cwd=RADICE e il passo falliva sempre.
            [py, str(Path(__file__).resolve().parent / "_debug_hardware.py")],
            "encoder reali, ripiego a runtime",
            r"^RISULTATO:",
            # `_debug_hardware.py` lancia mypy 3 volte con un tetto di 300s
            # ciascuno: un limite di 180s qui uccideva il passo prima che i
            # type-check cross-platform finissero, su una macchina lenta.
            timeout=1000,
        ),
    ]


def _riassunto(output: str, pattern: str) -> str:
    """Riga che riassume l'esito, o l'ultima riga se il verdetto non c'e'.

    Il pattern viene cercato dall'ultima riga in su: alcuni strumenti stampano
    il verdetto prima di output accessorio.
    """
    righe = [r.strip() for r in output.splitlines() if r.strip()]
    if pattern:
        for riga in reversed(righe):
            if re.search(pattern, riga):
                return riga
    return righe[-1] if righe else ""


def _testo_parziale(blocco: bytes | str | None) -> str:
    """Output parziale di un figlio interrotto, decodificato senza esplodere."""
    if blocco is None:
        return ""
    if isinstance(blocco, bytes):
        return blocco.decode("utf-8", errors="replace")
    return blocco


def _uccidi_gruppo(proc: subprocess.Popen[str]) -> None:
    """Uccide il figlio e TUTTI i suoi discendenti, non solo il processo padre.

    `subprocess.run` su timeout fa `process.kill()`: il figlio appeso muore, ma
    i suoi figli (mypy, pytest, ffmpeg lanciati a loro volta) restano vivi e
    tengono occupati CPU e file. Qui il figlio nasce in un gruppo/processo
    nuovo (`start_new_session`), quindi si puo' uccidere l'intero albero.
    """
    try:
        if os.name == "nt":
            # Su Windows non c'e' killpg: taskkill /T segue l'albero dei figli.
            esito = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            # `run` senza check NON solleva quando taskkill fallisce: senza
            # questo controllo il return verteva sempre e il ripiego su
            # `proc.kill()` non partiva mai, lasciando i nipoti vivi.
            if esito.returncode == 0:
                return
        uccidi_gruppo = getattr(os, "killpg", None)  # Windows non ce l'ha
        if uccidi_gruppo is not None:
            # Con start_new_session=True il PID del figlio e' anche il PGID.
            uccidi_gruppo(proc.pid, getattr(signal, "SIGKILL", 9))
            return
    except (OSError, subprocess.SubprocessError):
        pass
    # Ripiego: almeno il processo diretto.
    with contextlib.suppress(OSError):
        proc.kill()


def _esegui(controllo: Controllo) -> Esito:
    # PYTHONIOENCODING: i figli scrivono output con accenti ed emoji, e qui
    # viene letto e ristampato. Fissa l'UTF-8 una volta, invece di sperare
    # nell'encoding di questa console.
    ambiente = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    avvio = time.monotonic()
    with subprocess.Popen(
        controllo.argomenti,
        cwd=RADICE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ambiente,
        # Gruppo separato: uccidere l'albero del figlio non può colpire
        # controlli.py stesso né gli altri passi già terminati.
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=controllo.timeout)
        except subprocess.TimeoutExpired:
            # Senza timeout il figlio restava appeso e il comando non finiva mai:
            # qui diventa un KO con la durata e l'eventuale output parziale.
            _uccidi_gruppo(proc)
            # Dopo la kill il communicate() finale drena i pipe e raccoglie
            # l'output che il figlio aveva già scritto prima di bloccarsi.
            # Se l'albero non e' morto tutto (taskkill fallito, killpg non
            # disponibile) un nipote puo' tenere i pipe aperti: anche QUI
            # serve un tetto, altrimenti il "timeout" diventerebbe un hang.
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
                with contextlib.suppress(OSError):
                    if proc.stdout is not None:
                        proc.stdout.close()
                with contextlib.suppress(OSError):
                    if proc.stderr is not None:
                        proc.stderr.close()
            secondi = time.monotonic() - avvio
            parziale = _testo_parziale(stdout) + _testo_parziale(stderr)
            output = (
                f"TIMEOUT: '{controllo.nome}' non e' terminato entro {controllo.timeout}s "
                f"(il processo e' stato interrotto). Motivo: il figlio e' rimasto appeso "
                f"oppure ha superato il tempo previsto per questo controllo.\n"
                f"{parziale}".rstrip()
            )
            return Esito(controllo, 124, secondi, output)
    secondi = time.monotonic() - avvio
    output = (stdout or "") + (stderr or "")
    return Esito(controllo, proc.returncode, secondi, output)


def _seleziona(tutti: list[Controllo], filtri: list[str]) -> list[Controllo]:
    if not filtri:
        return tutti
    ricerche = [f.lower() for f in filtri]
    return [c for c in tutti if any(r in c.nome.lower() for r in ricerche)]


def _moduli_mancanti(necessari: set[str]) -> list[str]:
    """Gli strumenti di sviluppo richiesti dai passi scelti ma non installati.

    ruff e mypy stanno in requirements-dev.txt (non in requirements.txt): chi
    usa il programma non li ha. Senza questo controllo il passo fallirebbe con
    "No module named ruff", che sembra un guasto del progetto.
    """
    import importlib.util

    return sorted(m for m in necessari if importlib.util.find_spec(m) is None)


def main(argv: list[str] | None = None) -> int:
    tutti = _passi()
    filtri = sys.argv[1:] if argv is None else argv
    passi = _seleziona(tutti, filtri)
    if not passi:
        print("Nessun controllo corrisponde a: " + ", ".join(filtri))
        print("Disponibili: " + ", ".join(c.nome for c in tutti))
        return 2

    richiesti: set[str] = set()
    for controllo in passi:
        richiesti.update(a for a in ("ruff", "mypy") if a in controllo.argomenti)
    mancanti = _moduli_mancanti(richiesti)
    if mancanti:
        print(
            "Mancano gli strumenti di sviluppo: " + ", ".join(mancanti) + "\n"
            "Installali con:  pip install -r requirements-dev.txt"
        )
        return 2

    print("=" * 66)
    print(f"CONTROLLI DEL CODICE ({len(passi)} passi) - non e' la verifica del video")
    print("=" * 66)

    esiti: list[Esito] = []
    for numero, controllo in enumerate(passi, 1):
        print(f"\n[{numero}/{len(passi)}] {controllo.nome} - {controllo.perche}")
        esito = _esegui(controllo)
        esiti.append(esito)
        stato = "OK" if esito.codice == 0 else "KO"
        print(f"      {stato}  ({esito.secondi:.1f}s)  {_riassunto(esito.output, controllo.riassunto)}")

    falliti = [e for e in esiti if e.codice != 0]

    # Il dettaglio serve solo per cio' che e' andato storto: la riga di esito
    # basta per tutto il resto.
    for esito in falliti:
        print("\n" + "-" * 66)
        print(f"DETTAGLIO DI {esito.controllo.nome} (uscita {esito.codice})")
        print("-" * 66)
        print(esito.output.rstrip())

    print("\n" + "=" * 66)
    print("RIEPILOGO")
    print("=" * 66)
    for esito in esiti:
        stato = "OK " if esito.codice == 0 else "KO "
        print(f"  {stato} {esito.controllo.nome:<16} {esito.secondi:>6.1f}s")

    if falliti:
        nomi = ", ".join(e.controllo.nome for e in falliti)
        print(f"\nESITO: FALLITO - {len(falliti)}/{len(esiti)} controlli rossi: {nomi}")
        return 1
    print(f"\nESITO: TUTTO OK - {len(esiti)}/{len(esiti)} controlli verdi")
    return 0


if __name__ == "__main__":
    # La console puo' non essere UTF-8 (in particolare quando l'output e'
    # reindirizzato): meglio un carattere di sostituzione che un'eccezione di
    # encoding a meta' verifica.
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(errors="replace")
    sys.exit(main())
