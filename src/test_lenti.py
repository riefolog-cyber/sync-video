#!/usr/bin/env python3
"""Test lenti: girano solo con SYNC_VIDEO_RUN_SLOW=1 (job nightly CI).

Non metto qui i test che scaricano il modello da 2 GB: durerebbero minuti e
in CI costerebbero soldi senza aggiungere copertura, perche' la parte che
interessa (il download) e' gia' coperta dai test veloci con mock.

Qui vanno i test che sono lenti ma GRANDI IN PICCOLO: verificano il
comportamento reale di una dipendenza o del filesystem, non la logica (quella
la coprono i test veloci). Sono i test che falliscono quando una libreria
cambia comportamento fra una versione e l'altra.

Aggiungere un test lento:
    from test_slow import slow

    class TestRete:
        @slow("motivo leggibile")
        def test_qualcosa(self): ...

Non scriverli dentro test_*.py con lo stesso prefisso se fanno rete: vanno
in un file dedicato cosi si possono saltare in blocco.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_slow import slow


class TestPymupdfReale(unittest.TestCase):
    """PyMuPDF senza mock: la sua API cambia fra versioni, i mock no.

    Il fallback PPTX->PDF e il rendering delle slide dipendono da questo
    modulo. I test veloci lo mockano, quindi un cambiamento incompatibile
    (un metodo rinominato, un tipo restituito diverso) arrival qui inosservato.
    """

    @slow("scrive e rilegge un PDF vero")
    def test_crea_e_rilegge_un_pdf(self) -> None:
        import pymupdf

        doc = pymupdf.open()
        self.addCleanup(doc.close)
        pagina = doc.new_page(width=320, height=200)
        pagina.insert_text((40, 100), "SYNC VIDEO")
        pdf = doc.tobytes()
        self.assertGreater(len(pdf), 0)

        riletto = pymupdf.open(stream=pdf, filetype="pdf")
        self.addCleanup(riletto.close)
        self.assertEqual(riletto.page_count, 1)
        testo = riletto[0].get_text()
        self.assertIn("SYNC VIDEO", testo)

    @slow("verifica il formato del pixmap")
    def test_il_pixmap_espone_lato_alla_risoluzione(self) -> None:
        """Il rendering delle slide usa dpi=144: se il fattore cambiasse,
        le slide nel video cambierebbero risoluzione senza che nessun test
        veloce se ne accorga."""
        import pymupdf

        doc = pymupdf.open()
        self.addCleanup(doc.close)
        doc.new_page(width=720, height=405)  # 16:9, come il video
        pix = doc[0].get_pixmap(dpi=144)
        # 720 pt = 10 pollici, 144 dpi -> 1440 px
        self.assertEqual(pix.width, 1440)
        self.assertEqual(pix.height, 810)


class TestFilesystemReale(unittest.TestCase):
    """La cache scrive in cartelle annidate e confronta MD5: si prova su disco."""

    @slow("verifica il riuso del PDF per contenuto")
    def test_md5_e_riuso_su_file_reale(self) -> None:
        from ocr import _file_md5

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sorgente.pptx"
            p.write_bytes(b"contenuto identico")
            copia = Path(d) / "copia.pptx"
            copia.write_bytes(b"contenuto identico")
            diverso = Path(d) / "diverso.pptx"
            diverso.write_bytes(b"contenuto diverso")

            # Stessi byte, anche con mtime diverso -> stesso MD5.
            self.assertEqual(_file_md5(p), _file_md5(copia))
            # Byte diversi -> MD5 diverso: e' questo che invalida la cache.
            self.assertNotEqual(_file_md5(p), _file_md5(diverso))

    @slow("verifica che un PDF con lo stesso nome ma sorgente diverso venga rifatto")
    def test_conversione_usa_la_cache_solo_se_il_sorgente_e_invariato(self) -> None:
        """Il pericolo reale non e' riconvertire ogni volta (solo sprecato), e'
        riusare un PDF stantio: il video mostrerebbe slide vecchie mentre il
        log dice 'convertito'. Qui si verifica che il PDF cambi quando
        cambia il sorgente.
        """
        import tempfile

        from ocr import convert_presentation_to_pdf

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "ppt_pdf"
            out.mkdir()
            ppt = Path(d) / "presentazione.pptx"
            pdf = out / "presentazione.pdf"

            ppt.write_bytes(b"prima versione")
            pdf.write_bytes(b"PDF VECCHIO")
            marker = out / "presentazione.src_md5"
            marker.write_text("hash non corrispondente", encoding="ascii")

            def _run(*_a: object, **_k: object) -> mock.Mock:
                pdf.write_bytes(b"PDF NUOVO")
                return mock.Mock(returncode=0, stderr="", stdout="")

            with mock.patch.multiple(
                "ocr",
                _find_soffice=mock.Mock(return_value="soffice"),
                _find_genoffice=mock.Mock(return_value=None),
                _find_onlyoffice=mock.Mock(return_value=None),
                _find_powerpoint=mock.Mock(return_value=False),
            ), mock.patch("ocr.subprocess.run", side_effect=_run):
                convert_presentation_to_pdf(ppt, out)

            # Il punto: non deve essere rimasto il PDF vecchio.
            self.assertEqual(pdf.read_bytes(), b"PDF NUOVO")


if __name__ == "__main__":
    unittest.main()
