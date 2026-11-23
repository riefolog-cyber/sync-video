#!/usr/bin/env python3
"""
Test mirati per le correzioni rivedute in config.py:
  1. pin di fastembed allineato a requirements.txt e override dalla cache;
  2. download atomico di ita.traineddata con urlopen(timeout=30);
  3. --log-file su BASE_DIR, anche sul root logger, senza handler duplicati.

Nessun test usa la rete: urllib.request.urlopen viene sempre sostituito da una fake.
Esegui con: python -B -m unittest test_review_config -v
"""

import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from test_pin_dipendenze import _riga_requirements, _versione_pin


class TestPinFastembed(unittest.TestCase):
    def test_bootstrap_usa_il_pin_di_requirements(self) -> None:
        requisito = config._REQUIRED_PACKAGES["fastembed"]
        atteso = _versione_pin(_riga_requirements("fastembed"), "requirements.txt")
        self.assertEqual(requisito, f"fastembed=={atteso}")
        self.assertEqual(config._req_name(requisito), "fastembed")

    def test_override_assente_non_cambia_nulla(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "CACHE_DIR", Path(tmp)):
            self.assertEqual(config._pin_overrides(), {})

    def test_override_presente(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "pin_overrides.json").write_text('{"fastembed": "0.6.0"}', encoding="utf-8")
            with mock.patch.object(config, "CACHE_DIR", cache):
                self.assertEqual(config._pin_overrides(), {"fastembed": "0.6.0"})

    def test_override_malformato_viene_ignorato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "pin_overrides.json").write_text("{non e' json", encoding="utf-8")
            with mock.patch.object(config, "CACHE_DIR", cache):
                self.assertEqual(config._pin_overrides(), {})


class _ReteInterrotta:
    """Restituisce qualche chunk, poi solleva OSError: download troncato."""

    def __init__(self, payload: bytes) -> None:
        self._dati = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        fine = len(self._dati) if size < 0 else min(self._offset + size, len(self._dati))
        chunk = self._dati[self._offset:fine]
        self._offset = fine
        if not chunk:
            raise OSError("rete interrotta")
        return chunk

    def __enter__(self) -> "_ReteInterrotta":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class TestDownloadTraineddata(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.dest = self.dir / "ita.traineddata"
        self.url = "https://esempio/ita.traineddata"
        # Payload plausibile secondo la guardia grossolana della produzione.
        self.payload = b"x" * (config._TRAINEDDATA_MIN_BYTES + 1)

    def test_download_riesce_e_rimuove_temporaneo(self) -> None:
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(self.payload)) as fake:
            config._download_traineddata(self.url, self.dest)
        fake.assert_called_once_with(self.url, timeout=30)
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertEqual(list(self.dir.glob("*.part")), [])

    def test_errore_di_rete_non_crea_il_finale_e_rimuove_temporaneo(self) -> None:
        with (
            mock.patch("urllib.request.urlopen", return_value=_ReteInterrotta(self.payload)),
            self.assertRaises(OSError),
        ):
            config._download_traineddata(self.url, self.dest)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dir.glob("*.part")), [])

    def test_errore_preserva_file_finale_esistente(self) -> None:
        vecchio = b"modello vecchio valido" * 5000
        self.dest.write_bytes(vecchio)

        with (
            mock.patch("urllib.request.urlopen", return_value=_ReteInterrotta(self.payload)),
            self.assertRaises(OSError),
        ):
            config._download_traineddata(self.url, self.dest)
        self.assertEqual(self.dest.read_bytes(), vecchio)
        self.assertEqual(list(self.dir.glob("*.part")), [])

    def test_file_troppo_piccolo_viene_scartato(self) -> None:
        with (
            mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"<html>404</html>")),
            self.assertRaises(OSError),
        ):
            config._download_traineddata(self.url, self.dest)
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dir.glob("*.part")), [])


class TestLogFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    @staticmethod
    def _nuovi_handler(prima: list[logging.Handler], logger: logging.Logger) -> list[logging.Handler]:
        return [h for h in list(logger.handlers) if h not in prima]

    def _prima(self) -> tuple[list[logging.Handler], list[logging.Handler]]:
        return list(config.log.handlers), list(logging.getLogger().handlers)

    def _chiudi(self, prima_log: list[logging.Handler], prima_root: list[logging.Handler]) -> None:
        for logger, prima in ((config.log, prima_log), (logging.getLogger(), prima_root)):
            for handler in self._nuovi_handler(prima, logger):
                logger.removeHandler(handler)
                handler.close()

    def test_crea_cartella_padre(self) -> None:
        prima_log, prima_root = self._prima()
        self.addCleanup(self._chiudi, prima_log, prima_root)
        log_path = self.dir / "sottocartella" / "run.log"

        config.parse_args(["--log-file", str(log_path)])

        self.assertTrue(log_path.exists())
        self.assertTrue(self._nuovi_handler(prima_log, config.log))

    def test_percorso_relativo_risolto_su_base_dir(self) -> None:
        prima_log, prima_root = self._prima()
        self.addCleanup(self._chiudi, prima_log, prima_root)

        with mock.patch.object(config, "BASE_DIR", self.dir):
            config.parse_args(["--log-file", "sotto/run.log"])

        self.assertTrue((self.dir / "sotto" / "run.log").exists())

    def test_handler_aggiunto_anche_al_root(self) -> None:
        prima_log, prima_root = self._prima()
        self.addCleanup(self._chiudi, prima_log, prima_root)
        log_path = self.dir / "root.log"

        config.parse_args(["--log-file", str(log_path)])
        self.assertTrue(self._nuovi_handler(prima_root, logging.getLogger()))

        logging.getLogger("modulo_esterno").warning("messaggio dal root")
        testo = log_path.read_text(encoding="utf-8")
        self.assertIn("messaggio dal root", testo)
        # `log` propaga al root: senza il filtro la riga verrebbe scritta due volte.
        self.assertEqual(testo.count("Log salvato anche in"), 1)

    def test_chiamate_multiple_non_duplicano_handler(self) -> None:
        prima_log, prima_root = self._prima()
        self.addCleanup(self._chiudi, prima_log, prima_root)
        log_path = self.dir / "run.log"

        config.parse_args(["--log-file", str(log_path)])
        config.parse_args(["--log-file", str(log_path)])

        self.assertEqual(len(self._nuovi_handler(prima_log, config.log)), 1)
        self.assertEqual(len(self._nuovi_handler(prima_root, logging.getLogger())), 1)
        config.log.warning("riga unica")
        self.assertEqual(log_path.read_text(encoding="utf-8").count("riga unica"), 1)

    def test_percorso_non_scrivibile_abortisce(self) -> None:
        # Una cartella al posto del file fa fallire FileHandler con
        # IsADirectoryError (sottoclasse di OSError): niente traceback grezzo.
        log_path = self.dir / "log.txt"
        log_path.mkdir()
        prima_log, prima_root = self._prima()
        self.addCleanup(self._chiudi, prima_log, prima_root)

        with self.assertRaises(SystemExit) as cm:
            config.parse_args(["--log-file", str(log_path)])

        self.assertEqual(cm.exception.code, 1)
        self.assertFalse(self._nuovi_handler(prima_log, config.log))
        self.assertFalse(self._nuovi_handler(prima_root, logging.getLogger()))


if __name__ == "__main__":
    unittest.main()
