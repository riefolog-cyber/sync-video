#!/usr/bin/env python3
"""Il percorso di LibreOffice per-utente deve essere cercato.

Il buco che copre: LibreOffice installato SENZA diritti di amministratore
va in %LOCALAPPDATA%\\Programs\\LibreOffice, non in Program Files. La
lista dei candidati di _find_soffice conteneva solo i percorsi per
macchina, e `shutil.which` non basta perche' l'installer non mette
soffice nel PATH. Il risultato era che un collega con LibreOffice
installato senza privilegi riceveva "nessun converter trovato" mentre il
programma era a due passi da lui.

ATTENZIONE, e' il bacio che ha fatto fallire la CI: i confronti sui
percorsi sono fatti sulle PARTI, non sulla stringa. Su Windows
str(Path("C:\\...") / "Programs") usa il backslash, su Linux la barra:
un test che confronta la stringa intera passa su Windows e fallisce su
ubuntu-latest. E' successo, e la CI l'ha preso.

Esegui con: python -m unittest test_soffice_per_utente -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ocr

_SEGMENTI = ("Programs", "LibreOffice", "program", "soffice.exe")


class TestPercorsoPerUtente(unittest.TestCase):
    """Il percorso deve essere costruito sull'ambiente, non scritto a mano.

    I test usano una directory NATIVA come LOCALAPPDATA, non una stringa
    Windows: Path(r"C:\\Users\\X") su Linux e' un nome RELATIVO che non si
    spezza in parti, quindi un test costruito su quello passa qui e
    fallisce su ubuntu-latest. E' gia' successo: la CI e' andata rossa due
    volte per questo, ed e' la stessa classe di errore delle altre due
    volte (import per nome di winreg, thread finto che non finiva).
    """

    def test_costruito_su_localappada(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LOCALAPPDATA": d}):
            percorso = ocr._soffice_per_utente()
        assert percorso is not None
        self.assertEqual(percorso.parts[-4:], _SEGMENTI)
        self.assertTrue(percorso.is_absolute())
        self.assertTrue(str(percorso).startswith(d))

    def test_seguono_localappada_che_cambia_utente(self) -> None:
        """Due utenti diversi devono dare due percorsi diversi."""
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": a}):
                primo = ocr._soffice_per_utente()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": b}):
                secondo = ocr._soffice_per_utente()
        assert primo is not None and secondo is not None
        self.assertNotEqual(primo, secondo)

    def test_senza_localappada_nessun_percorso(self) -> None:
        """Il caso Linux: nessun Path RELATIVO.

        Senza LOCALAPPDATA, Path("") / "Programs" / ... produce
        "Programs/LibreOffice/program/soffice.exe", risolto rispetto alla
        cartella corrente: il check potrebbe trovare per caso un file
        omonimo. Meglio nessun percorso.
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ocr._soffice_per_utente())

    def test_il_percorso_e_mai_relativo(self) -> None:
        """Se esiste, e' assoluto: e' la garanzia che conta."""
        with tempfile.TemporaryDirectory() as d:
            for ambiente in ({}, {"LOCALAPPDATA": ""}, {"LOCALAPPDATA": d}):
                with self.subTest(ambiente=bool(ambiente)), mock.patch.dict(
                    os.environ, ambiente, clear=True
                ):
                    percorso = ocr._soffice_per_utente()
                    if percorso is not None:
                        self.assertTrue(percorso.is_absolute(), f"{percorso} e' relativo")

    def _finto_soffice(self, radice: Path) -> Path:
        p = radice.joinpath(*_SEGMENTI)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"MZ finto")
        return p

    def test_find_soffice_trova_quello_per_utente(self) -> None:
        """Il caso reale: non in PATH, non in Program Files, ma installato."""
        with tempfile.TemporaryDirectory() as d:
            radice = Path(d)
            atteso = self._finto_soffice(radice)
            with (
                mock.patch.object(ocr, "_soffice_per_utente", return_value=atteso),
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
            atteso_utente = self._finto_soffice(radice)
            with (
                mock.patch.object(ocr, "_soffice_per_utente", return_value=atteso_utente),
                mock.patch.object(ocr, "_SOFFICE_CANDIDATES", [str(macchina)]),
                mock.patch.object(ocr.shutil, "which", return_value=None),
            ):
                self.assertEqual(ocr._find_soffice(), str(macchina))

    def test_nessun_soffice_resta_nessuno(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            radice = Path(d)
            with (
                mock.patch.object(ocr, "_soffice_per_utente", return_value=radice / "no.exe"),
                mock.patch.object(ocr, "_SOFFICE_CANDIDATES", []),
                mock.patch.object(ocr.shutil, "which", return_value=None),
            ):
                self.assertIsNone(ocr._find_soffice())


if __name__ == "__main__":
    unittest.main()
