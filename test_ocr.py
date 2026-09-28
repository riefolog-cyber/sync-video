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

    def _mock_converter(self, content=b"pdf nuovo", returncode=0):
        """Converter finto che SCRIVE davvero il PDF, come fa soffice/x2t.

        Necessario da quando il bootstrap rimuove il PDF stantio prima di
        convertire: senza questo, i test passavano solo perché la vecchia
        copia del file soddisfaceva il controllo di esistenza — cioe'
        esattamente il cache poisoning che la rimozione evita.
        """

        def _run(*a, **k):
            self.pdf.write_bytes(content)
            return mock.Mock(returncode=returncode, stderr="", stdout="")

        return mock.patch("ocr.subprocess.run", side_effect=_run)

    def test_reconvert_when_source_changed(self):
        self.pdf.write_bytes(b"pdf stantio")
        marker = self.out_dir / "presentazione.src_md5"
        marker.write_text("hash_obsoleto", encoding="ascii")
        with mock.patch("ocr._find_soffice", return_value="soffice"), self._mock_converter():
            result = convert_presentation_to_pdf(self.ppt, self.out_dir)
        self.assertEqual(result, self.pdf)
        # dopo la riconversione, il marker è aggiornato all'hash corrente
        from ocr import _file_md5

        self.assertEqual(marker.read_text(encoding="ascii").strip(), _file_md5(self.ppt))
        # e il PDF è quello NUOVO, non quello lasciato dalla run precedente
        self.assertEqual(self.pdf.read_bytes(), b"pdf nuovo")

    def test_no_marker_forces_reconvert(self):
        self.pdf.write_bytes(b"pdf senza marker")
        with mock.patch("ocr._find_soffice", return_value="soffice"), self._mock_converter():
            result = convert_presentation_to_pdf(self.ppt, self.out_dir)
        self.assertEqual(result, self.pdf)
        self.assertTrue((self.out_dir / "presentazione.src_md5").exists())

    def test_failed_conversion_does_not_reuse_stale_pdf(self):
        """Il converter fallisce e il PDF di prima NON deve essere riusato.

        Prima della rimozione preventiva, la condizione di successo
        (`pdf_path.exists()`) era soddisfatta dal file della run precedente:
        il PDF stantio veniva dichiarato "convertito" e il marker aggiornato
        all'hash del sorgente nuovo, quindi ogni run successiva riusava quel
        file per sempre (slide vecchie nel video, cache OCR riusata perche'
        l'hash del PDF non cambiava). Ora il fallback fallisce e si esce con
        errore, invece di produrre un video con le slide precedenti.
        """
        self.pdf.write_bytes(b"pdf stantio")
        marker = self.out_dir / "presentazione.src_md5"
        marker.write_text("hash_obsoleto", encoding="ascii")

        def _failing_run(*a, **k):
            # Fallisce e non scrive nulla: il file stantio c'è ancora.
            return mock.Mock(returncode=1, stderr="convertitore rotto", stdout="")

        with (
            mock.patch("ocr._find_soffice", return_value="soffice"),
            mock.patch("ocr._find_onlyoffice", return_value=None),
            mock.patch("ocr.subprocess.run", side_effect=_failing_run),
            mock.patch("ocr._pptx_fallback_to_pdf", return_value=False),self.assertRaises(RuntimeError)
        ):
            convert_presentation_to_pdf(self.ppt, self.out_dir)
        # La prova che la cache non si avvelena: il PDF stantio è stato
        # rimosso, quindi non esiste più nessun file che la run successiva
        # potrebbe riusare come se fosse fresco.
        self.assertFalse(self.pdf.exists(), "il PDF stantio non deve sopravvivere a una conversione fallita")
        marker = self.out_dir / "presentazione.src_md5"
        self.assertFalse(marker.exists(), "il marker non deve essere stato scritto")


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
