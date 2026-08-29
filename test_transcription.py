#!/usr/bin/env python3
"""
Test unitari per la correzione dei nomi propri nella trascrizione
(transcription.correct_transcript_names).
Esegui con: python -m unittest test_transcription -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from transcription import correct_transcript_names, openvino_usable


def _words(items):
    """Converte [(word, start)] in lista di dict Whisper con end/conf."""
    return [{"word": w, "start": float(t), "end": float(t) + 0.3, "conf": 1.0} for w, t in items]


class TestCorrectTranscriptNames(unittest.TestCase):
    """Dizionario dei nomi propri che Whisper storpi sistematicamente."""

    def test_single_word_correction(self):
        words = _words([("sigmond", 10.0), ("freud", 10.4)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["sigmund", "freud"])

    def test_multi_word_phrase_collapses(self):
        # "thomas mur" -> "thomas more": il gruppo diventa una parola sola,
        # le successive vengono scartate e l'intervallo temporale è conservato.
        words = _words([("thomas", 5.0), ("mur", 5.4), ("invento", 6.0)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["thomas more", "invento"])
        self.assertAlmostEqual(out[0]["end"], 5.7)  # fine dell'ultima parola del gruppo

    def test_case_and_punctuation_insensitive(self):
        words = _words([("Mark", 1.0), ("chiuse!", 1.5)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["marcuse"])

    def test_phrase_split_by_other_words_not_corrected(self):
        # "kep curo" deve comparire come coppia consecutiva per essere corretto
        words = _words([("kep", 0.0), ("non", 0.5), ("curo", 1.0)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["kep", "non", "curo"])

    def test_unchanged_words_left_intact(self):
        words = _words([("ciao", 0.0), ("mondo", 1.0)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["ciao", "mondo"])

    def test_original_list_not_mutated(self):
        words = _words([("on", 0.0), ("lock", 0.5)])
        correct_transcript_names(words)
        self.assertEqual([w["word"] for w in words], ["on", "lock"])

    def test_empty_input(self):
        self.assertEqual(correct_transcript_names([]), [])


class TestOpenvinoUsable(unittest.TestCase):
    """openvino_usable: l'avviso OpenVINO va mostrato solo se percorribile."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "machine_setup.json"
        patcher = mock.patch("transcription.MACHINE_CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(self, transcriber: str) -> None:
        self.config_path.write_text(
            json.dumps({"transcriber": transcriber}), encoding="utf-8"
        )

    def _patch_runtime(self, genai_ok: bool, devices: list[str] | None = None):
        """Simula la presenza/assenza del runtime OpenVINO in sys.modules."""
        modules = {"openvino_genai": mock.MagicMock() if genai_ok else None}
        if genai_ok:
            core = mock.MagicMock(return_value=mock.MagicMock(available_devices=devices or []))
            modules["openvino"] = mock.MagicMock(Core=core)
        else:
            modules["openvino"] = None
        return mock.patch.dict(sys.modules, modules)

    def test_setup_says_openvino(self):
        self._write_config("openvino")
        self.assertTrue(openvino_usable())

    def test_setup_says_whisper(self):
        self._write_config("whisper")
        self.assertFalse(openvino_usable())

    def test_setup_whisper_wins_over_runtime(self):
        # Anche col runtime installato, la decisione di machine_setup prevale.
        self._write_config("whisper")
        with self._patch_runtime(genai_ok=True, devices=["CPU"]):
            self.assertFalse(openvino_usable())

    def test_no_setup_and_no_runtime(self):
        # Nessun machine_setup.json e openvino non installato -> avviso soppresso.
        with self._patch_runtime(genai_ok=False):
            self.assertFalse(openvino_usable())

    def test_no_setup_with_igpu(self):
        # Runtime installato con device GPU (iGPU Intel): consiglio sensato.
        with self._patch_runtime(genai_ok=True, devices=["GPU"]):
            self.assertTrue(openvino_usable())

    def test_no_setup_cpu_only(self):
        # openvino installato ma solo CPU (es. ARM/AMD): senza iGPU non c'è
        # guadagno di velocità -> avviso soppresso (caso Snapdragon).
        with self._patch_runtime(genai_ok=True, devices=["CPU"]):
            self.assertFalse(openvino_usable())

    def test_no_setup_runtime_without_devices(self):
        # Runtime installato ma nessun device disponibile -> non percorribile.
        with self._patch_runtime(genai_ok=True, devices=[]):
            self.assertFalse(openvino_usable())

    def test_corrupt_setup_falls_back_to_runtime(self):
        self.config_path.write_text("{non-json", encoding="utf-8")
        with self._patch_runtime(genai_ok=True, devices=["GPU"]):
            self.assertTrue(openvino_usable())


if __name__ == "__main__":
    unittest.main()
