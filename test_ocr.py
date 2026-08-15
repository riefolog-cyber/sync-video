#!/usr/bin/env python3
"""
Test unitari per `ocr.convert_presentation_to_pdf` (riuso PDF per contenuto).
Non tocca LibreOffice: mocka la conversione per verificare la logica di
riuso basata su MD5 del sorgente.

Esegui con: python -m unittest test_ocr -v
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocr import convert_presentation_to_pdf


class TestConvertPresentationToPdf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "ppt_pdf"
        self.out_dir.mkdir()
        self.ppt = Path(self.tmp.name) / "presentazione.pptx"
        self.ppt.write_bytes(b"contenuto sorgente")
        self.pdf = self.out_dir / "presentazione.pdf"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reuse_when_source_unchanged(self):
        self.pdf.write_bytes(b"pdf esistente")
        marker = self.out_dir / "presentazione.src_md5"
        from ocr import _file_md5

        marker.write_text(_file_md5(self.ppt), encoding="ascii")
        with mock.patch("ocr._find_soffice", return_value="soffice"), mock.patch(
            "ocr.subprocess.run"
        ) as run:
            result = convert_presentation_to_pdf(self.ppt, self.out_dir)
            run.assert_not_called()
        self.assertEqual(result, self.pdf)

    def test_reconvert_when_source_changed(self):
        self.pdf.write_bytes(b"pdf stantio")
        marker = self.out_dir / "presentazione.src_md5"
        marker.write_text("hash_obsoleto", encoding="ascii")
        with mock.patch("ocr._find_soffice", return_value="soffice"), mock.patch(
            "ocr.subprocess.run",
            side_effect=lambda *a, **k: mock.Mock(returncode=0, stderr=""),
        ):
            result = convert_presentation_to_pdf(self.ppt, self.out_dir)
        self.assertEqual(result, self.pdf)
        # dopo la riconversione, il marker è aggiornato all'hash corrente
        from ocr import _file_md5

        self.assertEqual(marker.read_text(encoding="ascii").strip(), _file_md5(self.ppt))

    def test_no_marker_forces_reconvert(self):
        self.pdf.write_bytes(b"pdf senza marker")
        with mock.patch("ocr._find_soffice", return_value="soffice"), mock.patch(
            "ocr.subprocess.run",
            side_effect=lambda *a, **k: mock.Mock(returncode=0, stderr=""),
        ):
            result = convert_presentation_to_pdf(self.ppt, self.out_dir)
        self.assertEqual(result, self.pdf)
        self.assertTrue((self.out_dir / "presentazione.src_md5").exists())


if __name__ == "__main__":
    unittest.main()
