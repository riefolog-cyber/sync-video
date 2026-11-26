#!/usr/bin/env python3
"""Regressioni del controllo "audio cambiato durante la run".

Il 26/11 il podcast e' stato rigenerato mentre la pipeline lavorava: la
trascrizione (e quindi la timeline) apparteneva alla versione precedente e il
video e' usato fuori sincrono. Questi test coprono i due controlli aggiunti.

Esegui con: python -m unittest test_audio_race -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import cast

import main
from chunks import Word


class TestAudioCambiato(unittest.TestCase):
    def test_file_invariato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio originale")
            hash_audio = main._file_hash(audio)
            self.assertFalse(main._audio_cambiato_durante_la_run(audio, hash_audio))

    def test_file_sostituito(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"versione vecchia")
            vecchia = main._file_hash(audio)
            # Stesso nome, stessa estensione, contenuto diverso.
            audio.write_bytes(b"versione nuova piu' lunga")
            self.assertTrue(main._audio_cambiato_durante_la_run(audio, vecchia))

    def test_file_assunto_scomparso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            self.assertTrue(main._audio_cambiato_durante_la_run(audio, "hash"))


class TestCoperturaTrascrizione(unittest.TestCase):
    @staticmethod
    def _parole(*secondi: float) -> list[Word]:
        return [cast("Word", {"word": "ciao", "start": t}) for t in secondi]

    def test_trascrizione_completa(self) -> None:
        self.assertEqual(main._coda_audio_senza_parole(self._parole(0, 60, 119.5), 120.0), 0.5)

    def test_trascrizione_troncata(self) -> None:
        # Il caso reale: testo fino a 860s, audio di 1198s.
        self.assertAlmostEqual(
            main._coda_audio_senza_parole(self._parole(0, 400, 860.0), 1198.34), 338.34, places=2
        )

    def test_trascrizione_vuota(self) -> None:
        self.assertEqual(main._coda_audio_senza_parole([], 120.0), 120.0)

    def test_parole_sporche_non_crashano(self) -> None:
        parole = [cast("Word", {"word": "x", "start": "n/d"}), cast("Word", {"word": "y"})]
        self.assertEqual(main._coda_audio_senza_parole(parole, 100.0), 100.0)


if __name__ == "__main__":
    unittest.main()
