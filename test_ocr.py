#!/usr/bin/env python3
"""
Test unitari per `ocr.convert_presentation_to_pdf` (riuso PDF per contenuto).
Non tocca LibreOffice: mocka la conversione per verificare la logica di
riuso basata su MD5 del sorgente.

Esegui con: python -m unittest test_ocr -v
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocr import _ocr_single_slide, convert_presentation_to_pdf


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


class TestLazyPytesseract(unittest.TestCase):
    """pytesseract (e dietro di lui pandas/numpy) non deve essere importato all'avvio.

    Il 16/09/2026 un numpy troppo vecchio rendeva pandas non importabile: con
    l'import di pytesseract in cima a ocr.py il guasto trascinava giù l'intero
    programma, anche le fasi che non usano affatto l'OCR. Il probe gira in un
    processo nuovo perché sys.modules non si puo' "disimportare".
    """

    _PROBE = """
import builtins
import sys

_bloccati = {"pandas", "pytesseract"}
_import_reale = builtins.__import__


def _import_guardato(name, *args, **kwargs):
    if name.split(".")[0] in _bloccati:
        raise ImportError("bloccato dal test: " + name)
    return _import_reale(name, *args, **kwargs)


builtins.__import__ = _import_guardato
import ocr

print("ESITO", "pandas" in sys.modules, "pytesseract" in sys.modules)
"""

    def test_import_di_ocr_non_tocca_pandas_ne_pytesseract(self):
        esito = subprocess.run(
            [sys.executable, "-c", self._PROBE],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent),
            timeout=120,
            check=False,
        )
        self.assertEqual(esito.returncode, 0, esito.stderr)
        self.assertIn("ESITO False False", esito.stdout)

    def test_ocr_usa_il_modulo_importato_al_momento_dell_uso(self):
        finto = mock.Mock()
        finto.image_to_string.return_value = "  testo della slide  "
        with mock.patch("ocr._tesseract", return_value=finto) as pigro:
            testo = _ocr_single_slide(Path("slide.png"), "ita")
        pigro.assert_called_once()
        finto.image_to_string.assert_called_once_with("slide.png", lang="ita")
        self.assertEqual(testo, "testo della slide")


if __name__ == "__main__":
    unittest.main()
