#!/usr/bin/env python3
"""Il percorso di LibreOffice per-utente deve essere cercato.

Il buco che copre: LibreOffice installato SENZA diritti di amministratore
va in %LOCALAPPDATA%\\Programs\\LibreOffice, non in Program Files. La
lista dei candidati conteneva solo Program Files, e `shutil.which` non
basta perche' l'installer non mette soffice nel PATH. Il risultato era che
un collega con LibreOffice installato senza privilegi riceveva "nessun
converter trovato" mentre il programma era a due passi da lui.

I test usano file VERI in una cartella temporanea invece di simulare
Path.exists: patchare i metodi di pathlib globalmente e' fragile e
fragilizza anche il codice sotto test.

Esegui con: python -m unittest test_soffice_per_utente -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ocr


class TestPercorsoPerUtente(unittest.TestCase):
    """Il percorso deve essere costruito, non scritto a mano."""

    def test_e_costruito_su_localappada(self) -> None:
        """Deve seguire LOCALAPPDATA: cambia da utente a utente."""
        testo = str(ocr._SOFFICE_PER_UTENTE)
        self.assertIn("LibreOffice", testo)
        self.assertIn("soffice.exe", testo)
        self.assertIn("Programs", testo)

    def test_segue_la_variabile_dambiente(self) -> None:
        """Il percorso dipende da LOCALAPPDATA, che cambia da utente a utente."""
        locale = r"C:\Users\Collega\AppData\Local"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": locale}):
            atteso = Path(locale) / "Programs" / "LibreOffice" / "program" / "soffice.exe"
            ricalcolato = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "LibreOffice" / "program" / "soffice.exe"
        self.assertEqual(atteso, ricalcolato)
        self.assertEqual(str(ricalcolato), locale + r"\Programs\LibreOffice\program\soffice.exe")

    def _finto_soffice(self, radice: Path) -> Path:
        """Crea un eseguibile finto dove lo metterebbe un install per-utente."""
        p = radice / "Programs" / "LibreOffice" / "program" / "soffice.exe"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"MZ finto")
        return p

    def test_find_soffice_trova_quello_per_utente(self) -> None:
        """Il caso reale: non in PATH, non in Program Files, ma installato."""
        with tempfile.TemporaryDirectory() as d:
            radice = Path(d)
            atteso = self._finto_soffice(radice)
            with (
                mock.patch.object(ocr, "_SOFFICE_PER_UTENTE", atteso),
                mock.patch.object(ocr, "_SOFFICE_CANDIDATES", [str(radice / "non" / "esiste.exe")]),
                mock.patch.object(ocr.shutil, "which", return_value=None),
            ):
                self.assertEqual(ocr._find_soffice(), str(atteso))

    def test_program_files_ha_la_precedenza_sul_per_utente(self) -> None:
        """Se ci sono entrambi vince quello per-macchina: e' il caso piu' comune."""
        with tempfile.TemporaryDirectory() as d:
            radice = Path(d)
            macchina = radice / "Program Files" / "LibreOffice" / "program" / "soffice.exe"
            macchina.parent.mkdir(parents=True, exist_ok=True)
            macchina.write_bytes(b"MZ macchina")
            utente = self._finto_soffice(radice)
            with (
                mock.patch.object(ocr, "_SOFFICE_PER_UTENTE", utente),
                mock.patch.object(ocr, "_SOFFICE_CANDIDATES", [str(macchina)]),
                mock.patch.object(ocr.shutil, "which", return_value=None),
            ):
                self.assertEqual(ocr._find_soffice(), str(macchina))

    def test_nessun_soffice_resta_nessuno(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            radice = Path(d)
            with (
                mock.patch.object(ocr, "_SOFFICE_PER_UTENTE", radice / "non" / "esiste.exe"),
                mock.patch.object(ocr, "_SOFFICE_CANDIDATES", []),
                mock.patch.object(ocr.shutil, "which", return_value=None),
            ):
                self.assertIsNone(ocr._find_soffice())


if __name__ == "__main__":
    unittest.main()
