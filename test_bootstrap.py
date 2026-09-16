#!/usr/bin/env python3
"""
Test unitari per `config._ensure_pip_packages` (bootstrap delle dipendenze).
Non tocca pip né la rete: mocka i metadata delle versioni e l'installazione.

Copre il guasto reale del 16/09/2026: numpy 1.24.3 nell'ambiente rendeva
pandas non importabile, e l'errore compariva come un guasto di pytesseract.

Esegui con: python -m unittest test_bootstrap -v
"""

import builtins
import unittest
from unittest import mock

import config


class TestReqName(unittest.TestCase):
    def test_nome_senza_versione(self):
        self.assertEqual(config._req_name("pymupdf"), "pymupdf")

    def test_nome_con_versione_minima(self):
        self.assertEqual(config._req_name("numpy>=1.26.0"), "numpy")

    def test_nome_con_trattino(self):
        self.assertEqual(config._req_name("faster-whisper>=1.2.1"), "faster-whisper")


class TestSpecOk(unittest.TestCase):
    def test_versione_sufficiente(self):
        self.assertTrue(config._spec_ok("numpy>=1.26.0", "1.26.4"))

    def test_versione_troppo_vecchia(self):
        self.assertFalse(config._spec_ok("numpy>=1.26.0", "1.24.3"))

    def test_nessuna_versione_richiesta(self):
        self.assertTrue(config._spec_ok("pymupdf", "0.0.1"))

    def test_spec_non_parseabile_non_blocca_avvio(self):
        self.assertTrue(config._spec_ok("numpy>=>1", "1.0.0"))


class TestMissingOrOld(unittest.TestCase):
    def _con_versione(self, versione):
        return mock.patch("config._installed_version", return_value=versione)

    def test_assente(self):
        with self._con_versione(None):
            self.assertEqual(config._missing_or_old("numpy>=1.26.0"), "assente")

    def test_vecchio(self):
        with self._con_versione("1.24.3"):
            self.assertEqual(config._missing_or_old("numpy>=1.26.0"), "versione 1.24.3")

    def test_a_posto(self):
        with self._con_versione("1.26.4"):
            self.assertIsNone(config._missing_or_old("numpy>=1.26.0"))


class TestImportErrorDetail(unittest.TestCase):
    def test_include_la_causa_radice(self):
        causa = ImportError("Please upgrade numpy to >= 1.26.0 to use this pandas version")
        errore = ImportError("C extension: None not built")
        errore.__cause__ = causa
        dettaglio = config._import_error_detail(errore)
        self.assertIn("C extension", dettaglio)
        self.assertIn("causa", dettaglio)
        self.assertIn("1.26.0", dettaglio)

    def test_senza_causa(self):
        self.assertEqual(
            config._import_error_detail(ImportError("solo esterno")),
            "ImportError: solo esterno",
        )


class TestEnsurePipPackages(unittest.TestCase):
    """Comportamento di `_ensure_pip_packages` con pip e metadata mockati."""

    @staticmethod
    def _versioni(numpy_vecchio=False, assente=None):
        def side_effect(pip_name):
            if pip_name == "numpy" and numpy_vecchio:
                return "1.24.3"
            if pip_name == assente:
                return None
            # Sopra ogni soglia minima dichiarata in _REQUIRED_PACKAGES
            return "99.0.0"

        return side_effect

    def test_aggiorna_numpy_vecchio(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni(numpy_vecchio=True)),
            mock.patch("config._try_pip_install", return_value=True) as pip,
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()
        pip.assert_called_once_with("numpy>=1.26.0", upgrade=True)

    def test_installa_pacchetto_assente(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni(assente="pymupdf")),
            mock.patch("config._try_pip_install", return_value=True) as pip,
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()
        pip.assert_called_once_with("pymupdf", upgrade=True)

    def test_niente_da_fare_se_tutto_a_posto(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni()),
            mock.patch("config._try_pip_install") as pip,
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()
        pip.assert_not_called()

    def test_installazione_fallita_esce_con_errore(self):
        with (
            mock.patch("config._installed_version", side_effect=self._versioni(numpy_vecchio=True)),
            mock.patch("config._try_pip_install", return_value=False),
            mock.patch("config.log"),
            self.assertRaises(SystemExit),
        ):
            config._ensure_pip_packages()

    def test_import_rotto_esce_con_diagnosi_e_causa(self):
        import_reale = builtins.__import__

        def import_guardato(name, *args, **kwargs):
            if name == "pytesseract":
                causa = ImportError("Please upgrade numpy to >= 1.26.0 to use this pandas version")
                errore = ImportError("C extension: None not built")
                errore.__cause__ = causa
                raise errore
            return import_reale(name, *args, **kwargs)

        with (
            mock.patch("config._installed_version", side_effect=self._versioni()),
            mock.patch("config._try_pip_install") as pip,
            mock.patch("builtins.__import__", side_effect=import_guardato),
            mock.patch("config.log") as log,
            self.assertRaises(SystemExit),
        ):
            config._ensure_pip_packages()
        pip.assert_not_called()
        messaggi = " ".join(str(argomento) for chiamata in log.error.call_args_list for argomento in chiamata.args)
        self.assertIn("1.26.0", messaggi)
        self.assertIn("pip check", messaggi)
        self.assertIn("pytesseract", messaggi)

    def test_import_bloccato_dall_ambiente_non_esce(self):
        """MoviePy con la DLL di PyAV bloccata: pacchetto presente, ambiente no."""
        import_reale = builtins.__import__

        def import_guardato(name, *args, **kwargs):
            if name == "moviepy":
                raise ImportError("DLL load failed while importing av")
            return import_reale(name, *args, **kwargs)

        with (
            mock.patch("config._installed_version", side_effect=self._versioni()),
            mock.patch("config._try_pip_install") as pip,
            mock.patch("builtins.__import__", side_effect=import_guardato),
            mock.patch("config.log"),
        ):
            config._ensure_pip_packages()  # non deve uscire
        pip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
