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
fallire la CI su Linux (e' gia' successo: vedi PIANO_OTTIMIZZAZIONI.md).
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

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RADICE = Path(__file__).resolve().parent


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
            [py, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
            "la stessa suite della CI",
            r"^(OK|FAILED)",
        ),
        Controllo(
            "debug hardware",
            [py, "_debug_hardware.py"],
            "encoder reali, ripiego a runtime",
            r"^RISULTATO:",
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


def _esegui(controllo: Controllo) -> Esito:
    # PYTHONIOENCODING: i figli scrivono output con accenti ed emoji, e qui
    # viene letto e ristampato. Fissa l'UTF-8 una volta, invece di sperare
    # nell'encoding di questa console.
    ambiente = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    avvio = time.monotonic()
    esito = subprocess.run(
        controllo.argomenti,
        cwd=RADICE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ambiente,
        check=False,
    )
    secondi = time.monotonic() - avvio
    output = (esito.stdout or "") + (esito.stderr or "")
    return Esito(controllo, esito.returncode, secondi, output)


def _seleziona(tutti: list[Controllo], filtri: list[str]) -> list[Controllo]:
    if not filtri:
        return tutti
    ricerche = [f.lower() for f in filtri]
    return [c for c in tutti if any(r in c.nome.lower() for r in ricerche)]


def main(argv: list[str] | None = None) -> int:
    tutti = _passi()
    filtri = sys.argv[1:] if argv is None else argv
    passi = _seleziona(tutti, filtri)
    if not passi:
        print("Nessun controllo corrisponde a: " + ", ".join(filtri))
        print("Disponibili: " + ", ".join(c.nome for c in tutti))
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
