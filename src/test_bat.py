#!/usr/bin/env python3
"""Guardie sui launcher .bat (prima installazione su Windows).

Due classi di guasti silenziosi, entrambe gia' sfiorate lavorando a questi file:
- byte non ASCII: cmd.exe li esegue come comandi anche con chcp 65001
  (ogni .bat lo scrive nel proprio header: SOLO ASCII);
- goto/call verso etichette inesistenti: nessun errore, il flusso cade nella
  riga dopo e il .bat fa un'altra cosa in silenzio.

Esegui con: python -m unittest test_bat -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from hardware import project_root

RADICE = project_root()

ETICHETTA = re.compile(r"(?m)^:([A-Za-z0-9_]+)")
GOTO = re.compile(r"(?i)\bgoto\s+:?([A-Za-z0-9_]+)")
CALL_LABEL = re.compile(r"(?i)\bcall\s+:([A-Za-z0-9_]+)")
GOTO_DINAMICO = re.compile(r"(?i)\bgoto\s*[%!]")
CALL_DINAMICO = re.compile(r"(?i)\bcall\s*[%!]")
RIGA_COMMENTO = re.compile(r"(?im)^\s*(rem(\s|$)|::)")

# :eof e' di cmd.exe (fine file), non va definita.
PREDEFINITE = frozenset({"eof"})


def _bat() -> list[Path]:
    return sorted(RADICE.glob("*.bat"))


def _codice(testo: str) -> str:
    """Il .bat senza righe di commento (i nomi citati li' non contano)."""
    return "\n".join(
        riga for riga in testo.splitlines() if not RIGA_COMMENTO.match(riga)
    )


class TestBatAscii(unittest.TestCase):
    def test_solo_ascii(self) -> None:
        self.assertTrue(_bat(), "nessun .bat trovato: RADICE sbagliata?")
        for percorso in _bat():
            with self.subTest(bat=percorso.name):
                try:
                    percorso.read_bytes().decode("ascii")
                except UnicodeDecodeError as e:
                    self.fail(
                        f"{percorso.name}: byte non ASCII a offset {e.start} "
                        "(cmd.exe lo esegue come comando)"
                    )


class TestBatEtichette(unittest.TestCase):
    def test_niente_goto_call_dinamici(self) -> None:
        """I salti devono restare statici, o questo controllo non li vede."""
        for percorso in _bat():
            with self.subTest(bat=percorso.name):
                codice = _codice(percorso.read_text(encoding="utf-8", errors="replace"))
                self.assertFalse(GOTO_DINAMICO.search(codice), "goto dinamico: estendi il controllo")
                # `call "%~dp0..."` e' un percorso quotato, non una variabile:
                # la regex cerca %/! e le virgolette non la attivano.
                self.assertFalse(CALL_DINAMICO.search(codice), "call dinamica: estendi il controllo")

    def test_ogni_goto_e_call_ha_etichetta(self) -> None:
        """Le etichette cmd sono case-insensitive: si confronta in minuscolo."""
        for percorso in _bat():
            with self.subTest(bat=percorso.name):
                codice = _codice(percorso.read_text(encoding="utf-8", errors="replace"))
                definite = {e.lower() for e in ETICHETTA.findall(codice)} | PREDEFINITE
                usate = {u.lower() for u in GOTO.findall(codice)}
                usate |= {u.lower() for u in CALL_LABEL.findall(codice)}
                mancanti = sorted(usate - definite)
                self.assertEqual(mancanti, [], f"etichette mai definite: {mancanti}")
