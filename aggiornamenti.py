#!/usr/bin/env python3
"""
Controllo e aggiornamento dei pacchetti (script standalone).

Esegue il bootstrap delle dipendenze e poi il controllo aggiornamenti con
richiesta S/N per installare i pacchetti NON pinnati (e i pinnati testabili
A/B, solo se equivalenti). I salti di major version vengono solo segnalati,
mai installati automaticamente.

Uso:
  python aggiornamenti.py            chiede S/N prima di installare
  python aggiornamenti.py --no-update   notifica e basta, non installa
  python aggiornamenti.py --no-update-check   non controlla nulla su PyPI
oppure (doppio click): aggiornamenti.bat [--no-update] [--no-update-check]
"""

import argparse

from config import bootstrap, log
from updates import run_update_check


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Controllo e aggiornamento dei pacchetti del progetto."
    )
    # Gli stessi nomi di main.py: uno script che fa la stessa cosa deve avere
    # gli stessi interruttori, altrimenti si impara una cosa e si usa l'altra.
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="Notifica gli aggiornamenti senza chiedere e senza installare",
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="Non controllare gli aggiornamenti su PyPI",
    )
    args = parser.parse_args()

    bootstrap()
    log.info("=" * 40)
    # Come in main.py: --no-update-check non è un parametro di run_update_check,
    # e' proprio l'assenza della chiamata. Senza questo, l'interruttore
    # accetterebbe il flag e non cambierebbe nulla.
    if not args.no_update_check:
        run_update_check(ask_to_update=not args.no_update)
    else:
        log.info("Controllo aggiornamenti saltato (--no-update-check).")


if __name__ == "__main__":
    main()
