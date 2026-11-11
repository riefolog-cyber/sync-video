#!/usr/bin/env python3
"""Test della prova sul materiale reale (il confronto con il riferimento).

Lo script `scripts/prova_materiale_reale.py` lancia la pipeline sul materiale
vero e confronta la timeline con un file golden. Qui si verifica solo il
CONFRONTO: che dica "identica" quando lo e', e che nomini la differenza
quando un confine si e' spostato.

Non si lancia la pipeline (ci vuole il materiale): il confronto e' una
funzione pura.

Esegui con: python -m unittest test_prova_materiale -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Il modulo e' in scripts/ (non e' un pacchetto: si esegue come script), quindi
# la sua cartella va aggiunta al sys.path per poterlo importare da qui.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import prova_materiale_reale as prova


class TestConfrontoConIlRiferimento(unittest.TestCase):
    """Il confronto deve dire la verita' su due cose: uguale o diversa."""

    def _timeline(self, *inizi: float) -> list[dict[str, object]]:
        segmenti: list[dict[str, object]] = []
        for i, inizio in enumerate(inizi):
            segmenti.append(
                {"slide": i + 1, "start": inizio, "end": inizio + 60.0}
            )
        return segmenti

    def test_timeline_uguale_non_problema(self) -> None:
        segmenti = self._timeline(0.0, 60.0, 120.0)
        self.assertEqual(prova._differenze(segmenti, segmenti), [])

    def test_un_confine_spostato_e_segnalato(self) -> None:
        prima = self._timeline(0.0, 60.0, 120.0)
        adesso = self._timeline(0.0, 62.5, 120.0)
        problemi = prova._differenze(adesso, prima)
        self.assertEqual(len(problemi), 1)
        self.assertIn("slide 2", problemi[0])
        self.assertIn("+2.50", problemi[0])

    def test_scarto_minimo_non_e_un_problema(self) -> None:
        # Tolleranza: 20 millisecondi non sono uno spostamento di confine.
        prima = self._timeline(0.0, 60.0)
        adesso = self._timeline(0.0, 60.02)
        self.assertEqual(prova._differenze(adesso, prima), [])

    def test_numero_di_segmenti_diverso_e_segnalato(self) -> None:
        problemi = prova._differenze(self._timeline(0.0, 60.0), self._timeline(0.0))
        self.assertEqual(len(problemi), 1)
        self.assertIn("segmenti", problemi[0])

    def test_slide_diverse_sono_segnalate(self) -> None:
        prima: list[dict[str, object]] = [{"slide": 1, "start": 0.0, "end": 60.0}]
        adesso: list[dict[str, object]] = [{"slide": 2, "start": 0.0, "end": 60.0}]
        problemi = prova._differenze(adesso, prima)
        self.assertEqual(len(problemi), 1)
        self.assertIn("slide", problemi[0])


if __name__ == "__main__":
    unittest.main()
