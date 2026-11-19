#!/usr/bin/env python3
"""Nomi di file e cartelle come li ha un insegnante: spazi, accenti, parentesi.

Il progetto vive su Windows, dove questi nomi sono la norma: la cartella si
chiama "sync video", il prompt "PROMPT_PRESENTAZIONE (PREDEFINITO).md". Un
percorso con spazi o accenti che si rompe (quoting, encoding) si scopre solo
sul materiale vero, e intanto il video non esce.

Qui si prova il percorso che tocca per primo il file dell'utente: l'OCR di un
PDF che sta in una cartella con spazi e accenti.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiuti_test import prepara_tesseract

# Parola grande e riconoscibile: se compare nel testo OCR, la pagina e' stata
# letta davvero (un percorso non la contiene). Le cifre sono fragili (un "7"
# letto "1" cambia tutto), le lettere grandi no.
PAROLA = "PAGINA"


def _pdf_con_numero(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    pagina = doc.new_page(width=1280, height=720)
    pagina.insert_text((300, 430), PAROLA, fontsize=200)
    doc.save(str(path))
    doc.close()


class TestPercorsiWindows(unittest.TestCase):
    def test_ocr_in_una_cartella_con_spazi_e_accenti(self) -> None:
        if prepara_tesseract() is None:
            self.skipTest("tesseract non installato")
        from ocr import extract_slides_text_ocr

        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp) / "Lezione è 1 (prova)"
            cartella.mkdir()
            pdf = cartella / "presentazione è.pdf"
            _pdf_con_numero(pdf)

            primo, secondo = extract_slides_text_ocr(
                pdf, cartella / "slide renderizzate", dpi=150, workers=1
            )

            self.assertEqual(len(primo), 1, f"una pagina -> un file: {primo}")
            unito = " ".join(str(x) for x in [*primo, *secondo])
            self.assertIn(PAROLA, unito.upper(), f"testo non letto dall'OCR: {unito!r}")


if __name__ == "__main__":
    unittest.main()
