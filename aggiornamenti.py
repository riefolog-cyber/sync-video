#!/usr/bin/env python3
"""
Controllo e aggiornamento dei pacchetti (script standalone).

Esegue il bootstrap delle dipendenze e poi il controllo aggiornamenti con
richiesta S/N per installare i pacchetti NON pinnati (e i pinnati testabili
A/B, solo se equivalenti). I salti di major version vengono solo segnalati,
mai installati automaticamente.

Uso:
  python aggiornamenti.py
oppure (doppio click): aggiornamenti.bat
"""

from config import bootstrap, log
from updates import run_update_check


def main() -> None:
    bootstrap()
    log.info("=" * 40)
    run_update_check(ask_to_update=True)


if __name__ == "__main__":
    main()
