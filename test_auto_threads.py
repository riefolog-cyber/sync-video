#!/usr/bin/env python3
"""Test dell'auto-tuning thread (config.auto_thread_budget).

Esegui con: python -m unittest test_auto_threads -v
"""

import os
import sys
import unittest
from unittest.mock import patch

from config import _logical_cpus, _physical_cpus, auto_thread_budget

if sys.platform == "win32":
    from config import _physical_cpus_win32


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


class TestPhysicalCpuProbe(unittest.TestCase):
    """Il probe dei core fisici: se fallisce silenziosamente, whisper si
    aggancia ai fratelli SMT (20 thread su 14 core) invece che ai core
    veri. La catena di fallback non deve MAI sollevare."""

    def test_ritorna_un_numero_positivo_o_none(self) -> None:
        # Sulla macchina di test: se il probe funziona torna un intero
        # <= logici; altrimenti None. Non deve mai alzare.
        fisici = _physical_cpus()
        if fisici is not None:
            self.assertGreaterEqual(fisici, 1)
            self.assertLessEqual(fisici, _logical_cpus())

    def test_nessun_core_piu_dei_logici(self) -> None:
        # Errore classico dei probe: restituire i logici come "fisici" fa
        # sembrare SMT piu' veloce di quanto sia.
        fisici = _physical_cpus()
        if fisici is not None:
            self.assertLessEqual(fisici, _logical_cpus())

    def test_psutil_ha_precedenza_ma_non_e_necessario(self) -> None:
        # psutil non e' in requirements.txt: il probe deve funzionare
        # anche senza, altrimenti il fallback silenzioso tornerebbe a
        # logici su ogni macchina che non lo ha installato.
        with patch.dict(sys.modules, {"psutil": None}):
            fisici = _physical_cpus()
        self.assertTrue(fisici is None or fisici >= 1)

    def test_probe_windows_oltre_64_logici_rinuncia(self) -> None:
        # Oltre i 64 thread logici l'informazione e' divisa in gruppi e la
        # nostra lettura non la aggrega: deve rinunciare (None), non
        # restituire un numero sbagliato.
        if sys.platform != "win32":
            self.skipTest("solo Windows")
        with patch("os.cpu_count", return_value=128):
            self.assertIsNone(_physical_cpus_win32())


if __name__ == "__main__":
    unittest.main()
