#!/usr/bin/env python3
"""Test mirati sulle correzioni del core (OCR, timeline, trascrizione).

Nessuna rete, nessun ffmpeg reale e nessun converter installato: i PDF sono
generati in memoria/temp e i processi sono finti o figli di Python.

Esegui con: python -B -m unittest test_review_core1 -v
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import pymupdf as fitz

import ocr as ocr_module
import transcription as transcription_module
from timeline import enforce_min_durations
from transcription import _load_audio_16k, _whisper_lang


class TestValidazionePdf(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    @staticmethod
    def _scrivi_pdf(path: Path) -> None:
        doc = fitz.open()
        try:
            doc.new_page()
            doc.save(str(path))
        finally:
            doc.close()

    def test_pdf_minimo_valido(self) -> None:
        path = self.base / "buono.pdf"
        self._scrivi_pdf(path)
        self.assertTrue(ocr_module._pdf_valido(path))

    def test_pdf_parziale_non_vuoto_non_valido(self) -> None:
        path = self.base / "rotto.pdf"
        path.write_bytes(b"%PDF-1.4\n" + b"x" * 4096)
        self.assertGreater(path.stat().st_size, 0)
        self.assertFalse(ocr_module._pdf_valido(path))

    def test_converter_parziale_non_lascia_pdf_ne_marker(self) -> None:
        """Il PDF difettoso va rimosso e il marker NON scritto: si deve riprovare."""
        sorgente = self.base / "deck.pptx"
        sorgente.write_bytes(b"pptx finto")
        out_dir = self.base / "out"
        pdf = out_dir / (sorgente.stem + ".pdf")
        marker = out_dir / (sorgente.stem + ".src_md5")

        def _run_finto(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 4096)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            mock.patch.object(ocr_module, "_find_soffice", return_value="soffice"),
            mock.patch.object(ocr_module, "_find_genoffice", return_value=None),
            mock.patch.object(ocr_module, "_find_onlyoffice", return_value=None),
            mock.patch.object(ocr_module, "_find_powerpoint", return_value=None),
            mock.patch.object(ocr_module, "_esegui_converter", side_effect=_run_finto),self.assertRaises(RuntimeError)
        ):
            ocr_module.convert_presentation_to_pdf(sorgente, out_dir)

        self.assertFalse(pdf.exists())
        self.assertFalse(marker.exists())

    def test_pdf_invalido_non_blocca_converter_successivo(self) -> None:
        sorgente = self.base / "deck2.pptx"
        sorgente.write_bytes(b"pptx finto")
        out_dir = self.base / "out2"
        pdf = out_dir / (sorgente.stem + ".pdf")
        marker = out_dir / (sorgente.stem + ".src_md5")
        chiamate: list[str] = []

        def _run_finto(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            chiamate.append(cmd[0])
            pdf.parent.mkdir(parents=True, exist_ok=True)
            if len(chiamate) == 1:
                pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 4096)
            else:
                self._scrivi_pdf(pdf)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            mock.patch.object(ocr_module, "_find_soffice", return_value="soffice"),
            mock.patch.object(ocr_module, "_find_genoffice", return_value=None),
            mock.patch.object(ocr_module, "_find_onlyoffice", return_value="x2t"),
            mock.patch.object(ocr_module, "_find_powerpoint", return_value=None),
            mock.patch.object(ocr_module, "_esegui_converter", side_effect=_run_finto),
        ):
            risultato = ocr_module.convert_presentation_to_pdf(sorgente, out_dir)

        self.assertEqual(risultato, pdf)
        self.assertEqual(len(chiamate), 2)
        self.assertTrue(marker.exists())
        self.assertTrue(ocr_module._pdf_valido(pdf))


class TestMappaLingua(unittest.TestCase):
    def test_codici_ocr_tre_lettere(self) -> None:
        for codice, atteso in {"ita": "it", "eng": "en", "deu": "de", "fra": "fr"}.items():
            with self.subTest(codice=codice):
                self.assertEqual(_whisper_lang(codice), atteso)

    def test_codici_gia_iso_e_sconosciuti(self) -> None:
        self.assertEqual(_whisper_lang("en"), "en")
        self.assertEqual(_whisper_lang("zzz"), "zzz")


class TestEnforceMinDurations(unittest.TestCase):
    def test_spazio_insufficiente_indietro_non_applica_spostamento(self) -> None:
        # Slide 2: 6->9 (3s < 5s). Il `max(work[prev] + min, ...)` darebbe 5,
        # che lascerebbe la slide a 4s: non va applicato ne' registrato.
        work, moved = enforce_min_durations({1: 0.0, 2: 6.0, 3: 9.0}, 20.0, 5.0)
        self.assertEqual(work[2], 6.0)
        self.assertNotIn((2, 6.0, 5.0), moved)
        self.assertGreaterEqual(work[3] - work[2], 5.0 - 1e-3)

    def test_non_allungabile_resta_corta_senza_spostamenti(self) -> None:
        work, moved = enforce_min_durations(
            {1: 0.0, 2: 6.0, 3: 9.0}, 20.0, 5.0, anchors={3: 9.0}
        )
        self.assertEqual(work, {1: 0.0, 2: 6.0, 3: 9.0})
        self.assertEqual(moved, [])

    def test_spostamento_indietro_valido_rimane(self) -> None:
        work, moved = enforce_min_durations({1: 0.0, 2: 8.0, 3: 10.0}, 15.0, 5.0)
        self.assertEqual(work[2], 5.0)
        self.assertIn((2, 8.0, 5.0), moved)


class TestDecodificaAudio(unittest.TestCase):
    def test_stderr_drenato_in_parallelo(self) -> None:
        """Un figlio Python scrive 200 KB su stderr: senza thread si blocca."""
        import sys
        import threading

        popen_originale = transcription_module.subprocess.Popen
        figlio = (
            "import struct, sys;"
            "sys.stderr.buffer.write(b'E' * 200000);"
            "sys.stderr.buffer.flush();"
            "sys.stdout.buffer.write(struct.pack('<2f', 0.0, 1.0));"
            "sys.stdout.buffer.flush()"
        )

        def _popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            return popen_originale([sys.executable, "-c", figlio], **kwargs)

        esito: dict[str, Any] = {}

        def _chiama() -> None:
            try:
                esito["valore"] = _load_audio_16k(Path("audio_finto.m4a"))
            except BaseException as e:
                esito["errore"] = e

        with (
            mock.patch("shutil.which", return_value="ffmpeg"),
            mock.patch.object(transcription_module.subprocess, "Popen", side_effect=_popen),
        ):
            t = threading.Thread(target=_chiama, daemon=True)
            t.start()
            t.join(timeout=20)

        self.assertFalse(t.is_alive(), "decodifica bloccata: stderr non drenato in parallelo")
        if "errore" in esito:
            raise esito["errore"]
        samples, rate = esito["valore"]
        self.assertEqual(rate, 16000)
        self.assertEqual(len(samples), 2)


if __name__ == "__main__":
    unittest.main()
