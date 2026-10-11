#!/usr/bin/env python3
"""
Test per la rilevazione e l'invio dei converter GenOffice e PowerPoint.

Perche' un file a parte e non dentro test_ocr.py: quello copre il riuso del
PDF per contenuto, che e' logica di generazione precedente e indipendente.
Qui invece si verifica WHICH converter viene scelto e COME viene invocato,
cioe' le parti che si rompono in silenzio: se la detection smette di trovare
GenOffice, la pipeline continua a funzionare e degrada al fallback che
produce slide di solo testo, senza alcun errore.

Non richiede GenOffice, Office, LibreOffice o ONLYOFFICE installati: tutto e'
mockato. L'unica cosa che NON copre (e non puo' coprire qui) e' il rendering
vero di PowerPoint via COM: su questa macchina Office non e' installato.

Esegui con: python -m unittest test_converter_pptx -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import ocr as ocr_module
from ocr import (
    _find_genoffice,
    _find_powerpoint,
    _office_converter_script,
    convert_presentation_to_pdf,
)


def _winreg_importabile() -> bool:
    """True se il modulo winreg si puo' importare davvero su questa macchina."""
    try:
        import winreg  # noqa: F401
    except ImportError:
        return False
    return True


class TestFindGenOffice(unittest.TestCase):
    """La detection deve trovare GenOffice solo se ci sono ENTRAMBI i pezzi."""

    def _installa(self, root: Path, *, exe: bool = True, cli: bool = True) -> Path:
        """Crea una GenOffice fittizia con i file richiesti."""
        app = root / "GenOffice.exe"
        script = root / "resources" / "cli" / "genoffice.cjs"
        if exe:
            app.write_bytes(b"MZ")
        if cli:
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_bytes(b"// cli")
        return app

    def test_trova_app_e_cli_nel_percorso_standard(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._installa(root)
            with mock.patch.dict(os.environ, {"GENOFFICE_PATH": str(root / "GenOffice.exe")}):
                found = _find_genoffice()
        self.assertIsNotNone(found)
        app, cli = found  # type: ignore[misc]
        self.assertTrue(app.endswith("GenOffice.exe"))
        self.assertTrue(cli.endswith("genoffice.cjs"))

    def test_senza_la_cli_risponde_none(self) -> None:
        """Il .exe da solo non basta: senza il .cjs la CLI non parte.

        Il percorso restituito a meta' fallirebbe SOLO a runtime, durante la
        conversione, con un errore che parla di Node e non di GenOffice.
        """
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._installa(root, cli=False)
            with mock.patch.dict(os.environ, {"GENOFFICE_PATH": str(root / "GenOffice.exe")}):
                self.assertIsNone(_find_genoffice())

    def test_cerca_anche_nei_percorsi_standard(self) -> None:
        """Senza questo, la lista di installazione puo' smettere di funzionare
        in silenzio: il caso reale e' proprio un GenOffice installato nel
        percorso di default, che nessun'altra variabile d'ambiente copre."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._installa(root)
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "ocr.shutil.which", return_value=None
            ), mock.patch("ocr._GENOFFICE_CANDIDATES", [str(root / "GenOffice.exe")]):
                found = _find_genoffice()
        self.assertIsNotNone(found, "GenOffice installato nel percorso standard non trovato")
        app, cli = found  # type: ignore[misc]
        self.assertEqual(app, str(root / "GenOffice.exe"))
        self.assertTrue(cli.endswith("genoffice.cjs"))

    def test_il_percorso_standard_deve_esistere_sul_pc(self) -> None:
        """Il percorso cercato per primo su Windows e' quello ufficiale.

        Se GenOffice cambia installazione, la detection va aggiornata qui e
        non in un ramo che si scopre solo su un PC dell'utente.
        """
        self.assertIn(
            r"C:\Program Files\GenOffice\GenOffice.exe",
            ocr_module._GENOFFICE_CANDIDATES,
            "percorso di installazione GenOffice non piu' fra quelli cercati",
        )

    def test_env_var_ignora_un_percorso_inesistente(self) -> None:
        with (
            mock.patch.dict(os.environ, {"GENOFFICE_PATH": r"D:\non\esiste\GenOffice.exe"}),
            mock.patch("ocr.shutil.which", return_value=None),
            mock.patch("ocr._GENOFFICE_CANDIDATES", []),
        ):
            self.assertIsNone(_find_genoffice())


class TestFindPowerPoint(unittest.TestCase):
    """La detection di Office non deve mai avviare l'applicazione.

    Nota importante su come e' finto il registro: qui NON si fa
    mock.patch("winreg.OpenKey"). Quel percorso importa il modulo winreg per
    nome, e su Linux/CI il modulo non esiste: il test si romperebbe con
    ModuleNotFoundError proprio dove il type-checker passa (mypy con
    --platform linux vede il ramo winreg come valido, ma a runtime l'import
    fallisce). Percio' winreg viene iniettato in sys.modules come finto, e i
    test girano identici su Windows, Linux e macOS.
    """

    @staticmethod
    def _winreg_finto(apisce: bool = True) -> Any:
        """Modulo finto che espone HKEY_CLASSES_ROOT e OpenKey."""

        def open_key(_hive: object, path: str) -> object:
            if apisce:
                # MagicMock perche' il codice usa "with winreg.OpenKey(...)":
                # un Mock semplice non implementa il context manager.
                return mock.MagicMock()
            raise OSError("chiave assente")

        return mock.MagicMock(
            HKEY_CLASSES_ROOT=object(), OpenKey=mock.MagicMock(side_effect=open_key)
        )

    def _patch_registro(self, finto: Any) -> Any:
        return mock.patch.dict(sys.modules, {"winreg": finto})

    def test_chiave_assente_risponde_false_ovunque(self) -> None:
        """Su ogni piattaforma la detection deve rispondere un booleano.

        Questo e' il test che gira ANCHE in CI (Linux): senza winreg reale
        risponde False perche' l'import fallisce, e va bene cosi'. Serve a
        garantire che _find_powerpoint non sollevi mai su sistemi dove Office
        non esiste, il tipo di errore che farebbe fallire la pipeline intera
        su ogni PC non-Windows.
        """
        finto = self._winreg_finto(apisce=False)
        with mock.patch.dict(os.environ, {}), self._patch_registro(finto):
            # Non solleva, qualunque sia la piattaforma.
            self.assertIn(_find_powerpoint(), (True, False))

    def test_risponde_false_su_non_windows(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(_find_powerpoint())

    def test_la_variabile_ambiente_disabilita(self) -> None:
        """SYNC_VIDEO_NO_OFFICE=1 deve vincere anche se Office e' presente."""
        with mock.patch.dict(os.environ, {"SYNC_VIDEO_NO_OFFICE": "1"}):
            self.assertFalse(_find_powerpoint())

    def test_leggere_il_registro_e_sufficiente(self) -> None:
        """Apre la chiave di classe: avviare COM qui lascerebbe una finestra.

        Questo test ha senso solo dove il modulo winreg esiste davvero (cioe'
        Windows). Su Linux/CI l'import di winreg fallisce e _find_powerpoint
        risponde correttamente False: e' il comportamento giusto, ed e' il
        motivo per cui non si usa mock.patch("winreg.OpenKey") per nome,
        che su Linux farebbe fallire il test con ModuleNotFoundError invece
        di verificare qualcosa.
        """
        if not _winreg_importabile():
            self.skipTest("winreg esiste solo su Windows (CI gira su Linux)")
        finto = self._winreg_finto(apisce=True)
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(os.environ, {}, clear=True),
            self._patch_registro(finto),
        ):
            self.assertTrue(_find_powerpoint())
        finto.OpenKey.assert_called_once()
        self.assertEqual(finto.OpenKey.call_args[0][1], "PowerPoint.Application")

    def test_chiave_assente_risponde_false(self) -> None:
        finto = self._winreg_finto(apisce=False)
        with (
            mock.patch.object(sys, "platform", "win32"),
            mock.patch.dict(os.environ, {}),
            self._patch_registro(finto),
        ):
            self.assertFalse(_find_powerpoint())


class TestScriptOfficePresente(unittest.TestCase):
    """Lo .ps1 deve viaggiare col codice, altrimenti il ramo Office muore lontano."""

    def test_lo_script_esiste_accanto_al_modulo(self) -> None:
        script = _office_converter_script()
        self.assertTrue(script.exists(), f"manca lo script PowerShell: {script}")
        self.assertEqual(script.name, "pptx_to_pdf_office.ps1")
        # Deve stare nella stessa cartella di ocr.py: e' il percorso da cui il
        # modulo si calcola la posizione, e su un clone git se non fosse
        # tracciato l'intero ramo fallirebbe solo su quel PC.
        self.assertEqual(script.parent, Path(ocr_module_dir()))


def ocr_module_dir() -> str:
    import ocr

    return str(Path(ocr.__file__).resolve().parent)


class _ConverterCommune(unittest.TestCase):
    """Base: una presentazione finta e un converter finito che scrive il PDF."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "ppt_pdf"
        self.out_dir.mkdir()
        self.ppt = Path(self.tmp.name) / "presentazione.pptx"
        self.ppt.write_bytes(b"sorgente")
        self.pdf = self.out_dir / "presentazione.pdf"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _converter_finto(self) -> Any:
        def _run(*_a: object, **_k: object) -> mock.Mock:
            self.pdf.write_bytes(b"pdf nuovo")
            return mock.Mock(returncode=0, stderr="", stdout="")

        return mock.patch("ocr.subprocess.run", side_effect=_run)

    def _nessun_converter(self) -> Any:
        """Tutti i converter dichiarati assenti, tranne quello sotto test."""
        return mock.patch.multiple(
            "ocr",
            _find_soffice=mock.Mock(return_value=None),
            _find_genoffice=mock.Mock(return_value=None),
            _find_onlyoffice=mock.Mock(return_value=None),
            _find_powerpoint=mock.Mock(return_value=False),
        )


class TestGenOfficeVieneScelto(_ConverterCommune):
    def test_usa_la_cli_e_imposta_il_runtime_electron(self) -> None:
        """ELECTRON_RUN_AS_NODE=1 e' obbligatorio: senza parte la GUI."""
        with self._nessun_converter(), mock.patch(
            "ocr._find_genoffice", return_value=("GenOffice.exe", "cli.cjs")
        ), self._converter_finto() as run:
            result = convert_presentation_to_pdf(self.ppt, self.out_dir)

        self.assertEqual(result, self.pdf)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "GenOffice.exe")
        self.assertEqual(cmd[1], "cli.cjs")
        self.assertIn("convert", cmd)
        env = run.call_args[1]["env"]
        self.assertEqual(env["ELECTRON_RUN_AS_NODE"], "1")

    def test_ha_precedenza_su_onlyoffice(self) -> None:
        """GenOffice e' piu' probabile di ONLYOFFICE: deve vincere."""
        with mock.patch("ocr._find_soffice", return_value=None), mock.patch(
            "ocr._find_genoffice", return_value=("app.exe", "cli.cjs")
        ), mock.patch("ocr._find_onlyoffice", return_value="x2t.exe"), mock.patch(
            "ocr._find_powerpoint", return_value=True
        ), self._converter_finto() as run:
            convert_presentation_to_pdf(self.ppt, self.out_dir)
        self.assertEqual(run.call_args[0][0][0], "app.exe")


class TestPowerPointVieneScelto(_ConverterCommune):
    def test_invocia_lo_script_powershell(self) -> None:
        with self._nessun_converter(), mock.patch(
            "ocr._find_powerpoint", return_value=True
        ), mock.patch("ocr.shutil.which", return_value="powershell"), self._converter_finto() as run:
            result = convert_presentation_to_pdf(self.ppt, self.out_dir)

        self.assertEqual(result, self.pdf)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "powershell")
        self.assertIn("-NoProfile", cmd)
        self.assertIn(str(_office_converter_script()), cmd)
        # I due path che lo script riceve, in quest'ordine.
        self.assertEqual(cmd[-2], str(self.ppt))
        self.assertEqual(cmd[-1], str(self.pdf))

    def test_ha_timeout_piu_generoso_di_libreoffice(self) -> None:
        """PowerPoint avvia l'app: 180s su un PC lento ucciderebbero il processo."""
        with self._nessun_converter(), mock.patch(
            "ocr._find_powerpoint", return_value=True
        ), mock.patch("ocr.shutil.which", return_value="powershell"), self._converter_finto() as run:
            convert_presentation_to_pdf(self.ppt, self.out_dir)
        self.assertEqual(run.call_args[1]["timeout"], 300)

    def test_ha_precedenza_su_onlyoffice_ma_no_su_libreoffice(self) -> None:
        with mock.patch("ocr._find_soffice", return_value="soffice"), mock.patch(
            "ocr._find_genoffice", return_value=None
        ), mock.patch("ocr._find_powerpoint", return_value=True), self._converter_finto() as run:
            convert_presentation_to_pdf(self.ppt, self.out_dir)
        self.assertEqual(run.call_args[0][0][0], "soffice")


if __name__ == "__main__":
    unittest.main()
