#!/usr/bin/env python3
"""Test di `gui.py`: solo la logica pura, mai la finestra.

Aprire Tk nei test li renderebbe dipendenti dallo schermo (in CI non c'e'):
qui si prova costruisci_comando() e il riconoscimento fasi, che e' dove
stanno le decisioni (quali tasti premere).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import gui
from gui import _fase_da_riga, costruisci_comando


class TestCostruisciComando(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.radice = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.radice / "presentazione.pdf").write_bytes(b"finta")
        (self.radice / "podcast.m4a").write_bytes(b"finto")

    def test_default_come_genera_video(self) -> None:
        comando = costruisci_comando(
            str(self.radice / "presentazione.pdf"), str(self.radice / "podcast.m4a")
        )
        for pezzo in ("--no-update-check", "--no-confirm", "--llm", "off", "--engine", "ffmpeg"):
            self.assertIn(pezzo, comando)
        self.assertIn("--verify-video", comando)
        self.assertNotIn("--transcriber", comando)
        self.assertNotIn("tiny", comando)

    def test_solo_cpu_e_riverifica_e_bozza(self) -> None:
        comando = costruisci_comando(
            str(self.radice / "presentazione.pdf"),
            str(self.radice / "podcast.m4a"),
            solo_cpu=True,
            riverifica=True,
            bozza=True,
        )
        self.assertIn("whisper", comando)
        self.assertIn("--verify-video", comando)
        self.assertIn("tiny", comando)

    def test_file_mancante_spiega_prima(self) -> None:
        with self.assertRaisesRegex(ValueError, "presentazione non trovata"):
            costruisci_comando(
                str(self.radice / "manca.pdf"), str(self.radice / "podcast.m4a")
            )
        with self.assertRaisesRegex(ValueError, "audio non trovato"):
            costruisci_comando(
                str(self.radice / "presentazione.pdf"), str(self.radice / "manca.m4a")
            )

    def test_nomi_relativi_partono_dalla_cartella(self) -> None:
        with mock.patch.object(gui, "BASE_DIR", self.radice):
            comando = costruisci_comando("presentazione.pdf", "podcast.m4a")
        self.assertIn(str(self.radice / "presentazione.pdf"), comando)


class TestFasi(unittest.TestCase):
    def test_fasi_riconosciute(self) -> None:
        casi = {
            "1. [CACHE] Slide OCR recuperate": "slide",
            "2. Trascrizione con OpenVINO": "trascriv",
            "sincronizzazione semantica": "Allineo",
            "IL VIDEO È PRONTO": "Pronto",
            "Manca il tuo materiale": "Manca",
            "Traceback inatteso": "problema",
        }
        for riga, atteso in casi.items():
            with self.subTest(riga=riga):
                trovata = _fase_da_riga(riga)
                self.assertIsNotNone(trovata)
                self.assertIn(atteso, trovata or "")

    def test_righe_ignote_ignorate(self) -> None:
        self.assertIsNone(_fase_da_riga("INFO | tutto bene, niente da dire"))


if __name__ == "__main__":
    unittest.main()
