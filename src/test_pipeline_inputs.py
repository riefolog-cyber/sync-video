#!/usr/bin/env python3
"""Regression della lettura del transcript_raw usato come fallback.

Esegui con: python -m unittest test_pipeline_inputs -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pipeline_inputs import _parse_transcript_raw


class TestParseTranscriptRaw(unittest.TestCase):
    def test_token_con_spazi_non_viene_buttato(self) -> None:
        # Whisper puo' emettere "New York [1.0s]" come un solo token: la regex
        # con \S+ buttava via l'intera parola, e il fallback per il flow non
        # vedeva piu' l'annuncio.
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "transcript_raw.txt"
            raw.write_text(
                "ciao [0.5s]\nNew York [1.0s]\npassiamo [2.0s]\n",
                encoding="utf-8",
            )
            parole = _parse_transcript_raw(raw)
        self.assertIsNotNone(parole)
        self.assertEqual([p["word"] for p in parole or []], ["ciao", "New York", "passiamo"])
        self.assertEqual([p["start"] for p in parole or []], [0.5, 1.0, 2.0])


    def test_tempo_malformato_viene_saltato_le_altre_parole_restano(self) -> None:
        # La regex accetta solo cifre e punti: `[1.2.3s]` passa ma `float()`
        # solleva ValueError. Un transcript_raw troncato non deve far fallire
        # il fallback Whisper: si butta la riga cattiva e si leggono le altre.
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "transcript_raw.txt"
            raw.write_text(
                "ciao [0.5s]\nNew York [1.2.3s]\npassiamo [2.0s]\n",
                encoding="utf-8",
            )
            parole = _parse_transcript_raw(raw)
        self.assertIsNotNone(parole)
        self.assertEqual([p["word"] for p in parole or []], ["ciao", "passiamo"])
        self.assertEqual([p["start"] for p in parole or []], [0.5, 2.0])

    def test_file_illeggibile_restituisce_none(self) -> None:
        # Una cartella con il nome del transcript: exists() e' True ma la
        # lettura solleva IsADirectoryError. Il fallback deve arrendersi
        # senza traceback (corsa con antivirus/OneDrive o file scritto a meta').
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "transcript_raw.txt"
            raw.mkdir()
            self.assertIsNone(_parse_transcript_raw(raw))


if __name__ == "__main__":
    unittest.main()
