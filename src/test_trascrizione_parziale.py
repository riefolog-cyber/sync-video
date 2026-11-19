#!/usr/bin/env python3
"""Il salvataggio a pezzi della trascrizione.

Whisper non si mette in pausa: se la run muore a meta', le parole gia'
prodotte devono restare su disco. Qui si verifica che il salvataggio avvenga
ogni N segmenti, che il file sia leggibile, e che a trascrizione finita non
resti un parziale in giro.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import transcription
from transcription import (
    _CHECKPOINT_OGNI_SEGMENTI,
    _collect_words,
    _salva_parziale,
    percorso_parziale,
)


class _Segmento:
    """Il minimo che `_collect_words` legge da un segmento di faster-whisper."""

    def __init__(self, testo: str, inizio: float) -> None:
        self.text = testo
        self.start = inizio
        self.words: list[object] = []


class TestSalvataggioAPezzi(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.audio = self.base / "podcast.m4a"
        self.audio.write_bytes(b"audio fittizio")
        # Il percorso del parziale si costruisce da config.CACHE_DIR: si punta
        # a una cartella temporanea, come fa main.py nel test end-to-end.
        self._patch = mock.patch("config.CACHE_DIR", self.base / ".cache")
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_il_percorso_dipende_dall_audio(self) -> None:
        primo = percorso_parziale(self.audio)
        altro = self.base / "altro.m4a"
        altro.write_bytes(b"audio diverso")
        self.assertNotEqual(primo, percorso_parziale(altro))

    def test_il_file_salvato_e_leggibile(self) -> None:
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.5}])
        dati = json.loads(percorso_parziale(self.audio).read_text(encoding="utf-8"))
        self.assertEqual(dati["parole"], 1)
        self.assertEqual(dati["words"][0]["word"], "ciao")

    def test_si_salva_ogni_n_segmenti(self) -> None:
        segmenti = [
            _Segmento(f"parola {i}", float(i)) for i in range(_CHECKPOINT_OGNI_SEGMENTI * 2)
        ]
        with mock.patch.object(transcription, "_salva_parziale") as salva:
            parole = _collect_words(iter(segmenti), self.audio)
        # Due parole per segmento (il testo si divide in token).
        self.assertEqual(len(parole), _CHECKPOINT_OGNI_SEGMENTI * 2 * 2)
        self.assertEqual(salva.call_count, 2, "un salvataggio ogni N segmenti")

    def test_a_trascrizione_finita_il_parziale_sparisce(self) -> None:
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.0}])
        self.assertTrue(percorso_parziale(self.audio).exists())
        _collect_words(iter([_Segmento("ciao", 0.0)]), self.audio)
        self.assertFalse(percorso_parziale(self.audio).exists())

    def test_senza_audio_nessun_file(self) -> None:
        _collect_words(iter([_Segmento("ciao", 0.0)]))
        self.assertFalse((self.base / ".cache" / "parziali").exists())


if __name__ == "__main__":
    unittest.main()
