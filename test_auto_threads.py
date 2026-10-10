#!/usr/bin/env python3
"""Test dell'auto-tuning thread (config.auto_thread_budget).

Esegui con: python -m unittest test_auto_threads -v
"""

import os
import unittest
from unittest.mock import patch

from config import auto_thread_budget


class TestAutoThreadBudget(unittest.TestCase):
    def test_override_env_ha_precedenza(self) -> None:
        with patch.dict(os.environ, {"WHISPER_THREADS": "3", "EMBED_THREADS": "5"}):
            self.assertEqual(auto_thread_budget("whisper"), 3)
            self.assertEqual(auto_thread_budget("embed"), 5)

    def test_env_non_valido_ignorato(self) -> None:
        with patch.dict(os.environ, {"WHISPER_THREADS": "zero"}):
            self.assertGreaterEqual(auto_thread_budget("whisper"), 1)
        with patch.dict(os.environ, {"VIDEO_THREADS": "0"}):
            self.assertGreaterEqual(auto_thread_budget("video"), 1)

    def test_tetti_rispettati(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for nome in ("WHISPER_THREADS", "EMBED_THREADS", "VIDEO_THREADS", "OCR_WORKERS"):
                os.environ.pop(nome, None)
            with patch("config._physical_cpus", return_value=32), patch("config._logical_cpus", return_value=64):
                self.assertLessEqual(auto_thread_budget("embed"), 12)
                self.assertLessEqual(auto_thread_budget("video"), 12)
                self.assertLessEqual(auto_thread_budget("ocr"), 6)
                self.assertGreaterEqual(auto_thread_budget("whisper"), 1)

    def test_snapdragon_8_core(self) -> None:
        # Snapdragon X: 8 fisici -> tutti i default restano su 8 o sotto.
        with patch.dict(os.environ, {}, clear=False):
            for nome in ("WHISPER_THREADS", "EMBED_THREADS", "VIDEO_THREADS", "OCR_WORKERS"):
                os.environ.pop(nome, None)
            with patch("config._physical_cpus", return_value=8), patch("config._logical_cpus", return_value=8):
                self.assertEqual(auto_thread_budget("whisper"), 8)
                self.assertEqual(auto_thread_budget("embed"), 8)
                self.assertEqual(auto_thread_budget("video"), 8)

    def test_desktop_14_fisici(self) -> None:
        # i7-12700H: 14 fisici / 20 logici -> whisper 14, embed/video al tetto 12.
        with patch.dict(os.environ, {}, clear=False):
            for nome in ("WHISPER_THREADS", "EMBED_THREADS", "VIDEO_THREADS", "OCR_WORKERS"):
                os.environ.pop(nome, None)
            with patch("config._physical_cpus", return_value=14), patch("config._logical_cpus", return_value=20):
                self.assertEqual(auto_thread_budget("whisper"), 14)
                self.assertEqual(auto_thread_budget("embed"), 12)
                self.assertEqual(auto_thread_budget("video"), 12)


if __name__ == "__main__":
    unittest.main()
