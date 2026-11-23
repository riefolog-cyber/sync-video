#!/usr/bin/env python3
"""Guardie sui launcher .bat (prima installazione su Windows).

Due classi di guasti silenziosi, entrambe gia' sfiorate lavorando a questi file:
- byte non ASCII: cmd.exe li esegue come comandi anche con chcp 65001
  (ogni .bat lo scrive nel proprio header: SOLO ASCII);
- goto/call verso etichette inesistenti: nessun errore, il flusso cade nella
  riga dopo e il .bat fa un'altra cosa in silenzio.

Esegui con: python -m unittest test_bat -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from hardware import project_root

RADICE = project_root()

ETICHETTA = re.compile(r"(?m)^:([A-Za-z0-9_]+)")
GOTO = re.compile(r"(?i)\bgoto\s+:?([A-Za-z0-9_]+)")
CALL_LABEL = re.compile(r"(?i)\bcall\s+:([A-Za-z0-9_]+)")
GOTO_DINAMICO = re.compile(r"(?i)\bgoto\s*[%!]")
CALL_DINAMICO = re.compile(r"(?i)\bcall\s*[%!]")
RIGA_COMMENTO = re.compile(r"(?im)^\s*(rem(\s|$)|::)")

# :eof e' di cmd.exe (fine file), non va definita.
PREDEFINITE = frozenset({"eof"})


def _bat() -> list[Path]:
    return sorted(RADICE.glob("*.bat"))


def _codice(testo: str) -> str:
    """Il .bat senza righe di commento (i nomi citati li' non contano)."""
    return "\n".join(
        riga for riga in testo.splitlines() if not RIGA_COMMENTO.match(riga)
    )


class TestBatAscii(unittest.TestCase):
    def test_solo_ascii(self) -> None:
        self.assertTrue(_bat(), "nessun .bat trovato: RADICE sbagliata?")
        for percorso in _bat():
            with self.subTest(bat=percorso.name):
                try:
                    percorso.read_bytes().decode("ascii")
                except UnicodeDecodeError as e:
                    self.fail(
                        f"{percorso.name}: byte non ASCII a offset {e.start} "
                        "(cmd.exe lo esegue come comando)"
                    )


class TestBatEtichette(unittest.TestCase):
    def test_niente_goto_call_dinamici(self) -> None:
        """I salti devono restare statici, o questo controllo non li vede."""
        for percorso in _bat():
            with self.subTest(bat=percorso.name):
                codice = _codice(percorso.read_text(encoding="utf-8", errors="replace"))
                self.assertFalse(GOTO_DINAMICO.search(codice), "goto dinamico: estendi il controllo")
                # `call "%~dp0..."` e' un percorso quotato, non una variabile:
                # la regex cerca %/! e le virgolette non la attivano.
                self.assertFalse(CALL_DINAMICO.search(codice), "call dinamica: estendi il controllo")

    def test_ogni_goto_e_call_ha_etichetta(self) -> None:
        """Le etichette cmd sono case-insensitive: si confronta in minuscolo."""
        for percorso in _bat():
            with self.subTest(bat=percorso.name):
                codice = _codice(percorso.read_text(encoding="utf-8", errors="replace"))
                definite = {e.lower() for e in ETICHETTA.findall(codice)} | PREDEFINITE
                usate = {u.lower() for u in GOTO.findall(codice)}
                usate |= {u.lower() for u in CALL_LABEL.findall(codice)}
                mancanti = sorted(usate - definite)
                self.assertEqual(mancanti, [], f"etichette mai definite: {mancanti}")


class TestInstallaCuda(unittest.TestCase):
    """Il rimedio per la GPU deve restare quello che serve davvero."""

    def test_installa_le_due_librerie_e_verifica(self) -> None:
        percorso = RADICE / "installa_cuda.bat"
        self.assertTrue(percorso.exists(), "manca installa_cuda.bat")
        testo = percorso.read_text(encoding="utf-8")
        self.assertIn("nvidia-cublas-cu12", testo)
        self.assertIn('"nvidia-cudnn-cu12==9.*"', testo)
        # CTranslate2 usa LoadLibraryA (ricerca standard): winmode=0 e' la
        # stessa ricerca, mentre il default di ctypes ignora il PATH.
        self.assertIn("winmode=0", testo)
        self.assertIn("'User'", testo)

    def test_pip_usa_l_interprete_selezionato(self) -> None:
        # Il rimedio di config.py mostra "py -m pip", ma qui py puo' puntare a
        # un interprete diverso da quello scelto (venv). Tutto deve passare da
        # PY_CMD, e una selezione vuota va fermata.
        testo = (RADICE / "installa_cuda.bat").read_text(encoding="utf-8")
        self.assertIn("!PY_CMD! -m pip install", testo)
        self.assertNotIn("py -m pip", _codice(testo))
        self.assertIn("if not defined PY_CMD goto senza_python", testo)


class TestPythonInstaller(unittest.TestCase):
    """L'installer scaricato non va mai eseguito senza verifica."""

    @staticmethod
    def _testo() -> str:
        return (RADICE / "_python.bat").read_text(encoding="utf-8")

    def test_firma_prima_dellesecuzione(self) -> None:
        testo = self._testo()
        self.assertIn("Get-AuthenticodeSignature", testo)
        self.assertIn("$f.Status -ne 'Valid'", testo)
        self.assertLess(
            testo.index("Get-AuthenticodeSignature"),
            testo.index('"%_SETUP_EXE%" /quiet'),
        )

    def test_download_con_retry_e_timeout(self) -> None:
        testo = self._testo()
        self.assertIn("--retry 3", testo)
        self.assertIn("--max-time 300", testo)

    def test_solo_installer_con_nome_esatto_e_versione_verificata(self) -> None:
        testo = self._testo()
        # Niente wildcard: su ARM prendeva il primo .exe, anche di versione
        # diversa da quella calcolata in _PY_FULL.
        self.assertNotIn("python-*-amd64.exe", testo)
        self.assertIn('python-%_PY_FULL%-amd64.exe', testo)
        self.assertIn("_PY_VER_INSTALL", testo)


class TestWingetFFmpeg(unittest.TestCase):
    def test_scope_user_allineato_a_config(self) -> None:
        # config._try_system_install usa lo scope utente (nessun admin): qui
        # non puo' installare in machine scope senza divergere dal rimedio.
        testo = (RADICE / "genera_video.bat").read_text(encoding="utf-8")
        righe = [
            r
            for r in testo.splitlines()
            if r.lstrip().startswith("winget install") and "FFmpeg" in r
        ]
        self.assertEqual(len(righe), 1, "riga winget ffmpeg non trovata")
        self.assertIn("--scope user", righe[0])
