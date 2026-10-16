#!/usr/bin/env python3
"""Test di `diagnostica.py`: il bundle da inviare, senza contenuti privati.

Il vincolo che conta: nomi di file e numeri SI', contenuti (audio, slide,
video, transcript, .env) MAI. Un regresso qui spedirebbe la voce dell'utente
a un estraneo.
"""

from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import diagnostica


def _bundle_senza_verifica(radice: Path) -> Path:
    """scrivi_bundle senza eseguire davvero le 11 prove (rete compresa)."""
    with mock.patch.object(diagnostica, "_righe_verifica", return_value=["riga finta"]):
        return diagnostica.scrivi_bundle(radice)


class _RadiceFinta:
    """Cartella progetto simulata: materiali veri, .env con finto segreto."""

    def __init__(self, base: Path) -> None:
        self.base = base
        (base / "podcast.m4a").write_bytes(b"finto-audio")
        (base / "presentazione.pdf").write_bytes(b"finta-presentazione")
        (base / "video_finale.mp4").write_bytes(b"finto-video")
        (base / ".env").write_text("TOKEN=finto-segreto\n", encoding="utf-8")
        (base / "transcript_raw.txt").write_text("ciao [1.0s]\n", encoding="utf-8")
        (base / "genera_video.bat").write_text("finto", encoding="utf-8")

    def nomi_nello_zip(self, zip_path: Path) -> tuple[list[str], str]:
        with zipfile.ZipFile(zip_path) as archivio:
            nomi = archivio.namelist()
            testo = "\n".join(
                archivio.read(n).decode("utf-8", errors="replace")
                for n in nomi
                if n.endswith((".txt", ".json"))
            )
        return nomi, testo


class TestBundleSicuro(unittest.TestCase):
    def test_niente_contenuti_ne_segreti(self) -> None:
        with TemporaryDirectory() as tmp:
            radice = Path(tmp)
            _RadiceFinta(radice)
            percorso = _bundle_senza_verifica(radice)
            _, testo = _RadiceFinta(radice).nomi_nello_zip(percorso)
            for vietato in ("finto-audio", "finta-presentazione", "finto-video", "finto-segreto"):
                with self.subTest(vietato=vietato):
                    self.assertNotIn(vietato, testo)
            self.assertTrue(percorso.is_file())

    def test_nomi_presenti_numeri_presenti(self) -> None:
        """I nomi servono (dov'e' il file?), i contenuti no."""
        with TemporaryDirectory() as tmp:
            radice = Path(tmp)
            _RadiceFinta(radice)
            percorso = _bundle_senza_verifica(radice)
            nomi, testo = _RadiceFinta(radice).nomi_nello_zip(percorso)
            self.assertTrue(any(n.endswith("verifica.txt") for n in nomi))
            self.assertTrue(any(n.endswith("versioni.json") for n in nomi))
            self.assertTrue(any(n.endswith("run.json") for n in nomi))
            self.assertIn("podcast.m4a", testo)


class TestBundleRobusto(unittest.TestCase):
    def test_cartella_vuota_produce_comunque_zip(self) -> None:
        with TemporaryDirectory() as tmp:
            percorso = _bundle_senza_verifica(Path(tmp))
            self.assertTrue(percorso.is_file())

    def test_versione_sempre_presente(self) -> None:
        with TemporaryDirectory() as tmp:
            radice = Path(tmp)
            percorso = _bundle_senza_verifica(radice)
            with zipfile.ZipFile(percorso) as archivio:
                nomi = archivio.namelist()
                dati = json.loads(
                    archivio.read(next(n for n in nomi if n.endswith("versioni.json")))
                )
            self.assertIn("python", dati)
            self.assertIn("app", dati)

    def test_main_ritorna_zero_e_pulisce(self) -> None:
        with TemporaryDirectory() as tmp:
            radice = Path(tmp)
            with mock.patch.object(diagnostica, "project_root", return_value=radice):
                self.assertEqual(diagnostica.main(), 0)
            zips = list(radice.glob("diagnostica-*.zip"))
            self.assertEqual(len(zips), 1)
            # Solo lo zip resta: la cartella di lavoro viene rimossa.
            self.assertEqual(
                [p.name for p in radice.iterdir() if p.is_dir()], []
            )


if __name__ == "__main__":
    unittest.main()
