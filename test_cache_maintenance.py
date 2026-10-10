#!/usr/bin/env python3
"""Test del modulo cache_maintenance (--cache-du / --clean-cache).

Esegui con: python -m unittest test_cache_maintenance -v
"""

import tempfile
import unittest
from pathlib import Path

from cache_maintenance import cache_disk_usage, clean_cache, format_bytes


class TestFormatBytes(unittest.TestCase):
    def test_unita(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(2048), "2.0 KB")
        self.assertEqual(format_bytes(3 * 1024 * 1024), "3.0 MB")
        self.assertEqual(format_bytes(2 * 1024**3), "2.0 GB")


class TestCacheDu(unittest.TestCase):
    def test_ordina_dal_piu_pesante(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "piccolo.json").write_bytes(b"x" * 100)
            pesante = base / "pesante"
            pesante.mkdir()
            (pesante / "a.bin").write_bytes(b"y" * 5000)
            righe = dict(cache_disk_usage(base))
            self.assertGreater(righe["pesante/"], righe["piccolo.json"])

    def test_cartella_mancante(self) -> None:
        self.assertEqual(cache_disk_usage(Path(tempfile.gettempdir()) / "non-esiste-xyz"), [])


class TestCleanCache(unittest.TestCase):
    def test_non_tocca_housekeeping_ne_modelli(self) -> None:
        import main as m

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = m.CACHE_DIR
            m.CACHE_DIR = base
            try:
                (base / "machine_setup.json").write_text("{}")
                (base / "sync_report.json").write_text("{}")
                (base / "slides_vecchio_300_ita.json").write_text("{}")
                modelli = base / "embedding_model"
                modelli.mkdir()
                (modelli / "pesi.bin").write_bytes(b"z" * 1000)
                esito = clean_cache(base, include_models=False)
                self.assertEqual(esito["orphan_json"], 1)
                self.assertTrue((base / "machine_setup.json").exists())
                self.assertTrue((base / "sync_report.json").exists())
                self.assertTrue(modelli.exists())
                self.assertEqual(esito["model_bytes"], 0)
            finally:
                m.CACHE_DIR = old

    def test_include_models_rimuove_pesi(self) -> None:
        import main as m

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = m.CACHE_DIR
            m.CACHE_DIR = base
            try:
                modelli = base / "embedding_model"
                modelli.mkdir()
                (modelli / "pesi.bin").write_bytes(b"z" * 1000)
                esito = clean_cache(base, include_models=True)
                self.assertFalse(modelli.exists())
                self.assertGreater(esito["model_bytes"], 0)
            finally:
                m.CACHE_DIR = old

    def test_svuota_verify_frames(self) -> None:
        import main as m

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = m.CACHE_DIR
            m.CACHE_DIR = base
            try:
                vf = base / "verify_frames"
                vf.mkdir()
                (vf / "f1.png").write_bytes(b"a" * 10)
                (vf / "f2.png").write_bytes(b"b" * 10)
                esito = clean_cache(base)
                self.assertEqual(esito["verify_frames"], 2)
                self.assertEqual(list(vf.glob("*.png")), [])
            finally:
                m.CACHE_DIR = old


if __name__ == "__main__":
    unittest.main()
