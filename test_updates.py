#!/usr/bin/env python3
"""
Test unitari per `updates` (controllo aggiornamenti pacchetti).
Non tocca la rete: mocka PyPI e la cache.

Esegui con: python -m unittest test_updates -v
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import updates


class _TempCacheMixin:
    """Reindirizza UPDATES_CACHE su un file temporaneo per i test."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp_dir.name) / "updates_check.json"
        patcher = mock.patch("updates.UPDATES_CACHE", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp_dir.cleanup)


class TestInstalledVersion(unittest.TestCase):
    def test_known_package(self):
        self.assertIsInstance(updates._installed_version("numpy"), str)

    def test_missing_package_returns_none(self):
        self.assertIsNone(updates._installed_version("pacchetto-inesistente-xyz"))


class TestPinned(unittest.TestCase):
    def test_fastembed_pinned(self):
        self.assertTrue(updates._is_pinned("fastembed"))

    def test_openvino_not_pinned(self):
        self.assertFalse(updates._is_pinned("openvino"))


class TestCheckUpdates(_TempCacheMixin, unittest.TestCase):
    def test_fresh_cache_skips_network(self):
        self.cache_path.write_text(
            json.dumps({"ts": 1e15, "outdated": [{"name": "x", "installed": "1", "latest": "2"}]}),
            encoding="utf-8",
        )
        with mock.patch("updates._latest_version_pypi") as net:
            result = updates.check_updates(ttl_hours=6)
            net.assert_not_called()
        self.assertEqual(len(result), 1)

    def test_expired_cache_queries_network(self):
        self.cache_path.write_text(json.dumps({"ts": 0, "outdated": []}), encoding="utf-8")
        with mock.patch(
            "updates._latest_version_pypi",
            side_effect=lambda p: {"numpy": "9.9.9", "tqdm": "1.0.0"}.get(p),
        ), mock.patch("updates._installed_version", side_effect=lambda p: {"numpy": "1.0.0"}.get(p)):
            result = updates.check_updates(ttl_hours=6)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "numpy")
        self.assertEqual(result[0]["latest"], "9.9.9")


class TestPrintUpdates(unittest.TestCase):
    def test_no_updates(self):
        with mock.patch("config.log.info") as info:
            updates.print_updates([])
        self.assertTrue(any("aggiornati" in str(c.args) for c in info.call_args_list))

    def test_with_updates_marks_pinned(self):
        data = [
            {"name": "fastembed", "installed": "0.5.1", "latest": "0.8.0", "pinned": True, "note": "pin"},
            {"name": "tqdm", "installed": "1.0", "latest": "1.1", "pinned": False, "note": ""},
        ]
        with mock.patch("config.log.info") as info:
            updates.print_updates(data)
        full = [" ".join(str(a) for a in c.args) for c in info.call_args_list]
        self.assertTrue(any("fastembed" in line and "pin" in line for line in full))
        self.assertTrue(any("tqdm" in line for line in full))


class TestUpgradable(unittest.TestCase):
    def test_filters_pinned(self):
        data = [
            {"name": "numpy", "pinned": False},
            {"name": "fastembed", "pinned": True},
        ]
        names = [d["name"] for d in updates._upgradable(data)]
        self.assertEqual(names, ["numpy"])

    def test_filters_major_jump(self):
        data = [
            {"name": "pillow", "pinned": False, "major": True},
            {"name": "tqdm", "pinned": False, "major": False},
        ]
        names = [d["name"] for d in updates._upgradable(data)]
        self.assertEqual(names, ["tqdm"])

    def test_no_pinned_left(self):
        self.assertEqual(updates._upgradable([{"name": "fastembed", "pinned": True}]), [])


class TestIsMajorJump(unittest.TestCase):
    def test_minor_is_not_major(self):
        self.assertFalse(updates._is_major_jump("10.4.0", "10.5.0"))

    def test_patch_is_not_major(self):
        self.assertFalse(updates._is_major_jump("1.28.0", "1.28.2"))

    def test_major_jump(self):
        self.assertTrue(updates._is_major_jump("10.4.0", "12.3.0"))

    def test_unparseable_is_cautious(self):
        self.assertTrue(updates._is_major_jump("unknown", "12.0.0"))


class TestRunUpdateCheck(_TempCacheMixin, unittest.TestCase):
    def test_no_updates_no_prompt(self):
        self.cache_path.write_text(json.dumps({"ts": 1e15, "outdated": []}), encoding="utf-8")
        with mock.patch("builtins.input") as inp, mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
            inp.assert_not_called()
            upg.assert_not_called()

    def test_prompt_yes_upgrades_non_pinned(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "numpy", "installed": "1", "latest": "2", "pinned": False, "note": ""},
                        {"name": "fastembed", "installed": "1", "latest": "2", "pinned": True, "note": "pin"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="s"), mock.patch("updates._pip_upgrade") as upg, mock.patch(
            "updates._run_pinned_ab_test", return_value=None
        ):
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_called_once_with(["numpy"])
        self.assertFalse(self.cache_path.exists())

    def test_pinned_equivalent_included_in_upgrade(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "fastembed", "installed": "1", "latest": "2", "pinned": True, "note": "pin"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="s"), mock.patch("updates._pip_upgrade") as upg, mock.patch(
            "updates._run_pinned_ab_test", return_value="EQUIVALENTE"
        ):
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_called_once_with(["fastembed"])

    def test_pinned_divergent_stays_pinned(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "fastembed", "installed": "1", "latest": "2", "pinned": True, "note": "pin"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input") as inp, mock.patch("updates._pip_upgrade") as upg, mock.patch(
            "updates._run_pinned_ab_test", return_value="DIVERGENTE"
        ):
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_not_called()
        inp.assert_not_called()

    def test_major_not_upgraded_automatically(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {
                            "name": "pillow",
                            "installed": "10.4.0",
                            "latest": "12.3.0",
                            "pinned": False,
                            "major": True,
                            "note": "",
                        },
                        {
                            "name": "tqdm",
                            "installed": "1.0",
                            "latest": "1.1",
                            "pinned": False,
                            "major": False,
                            "note": "",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="s"), mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_called_once_with(["tqdm"])

    def test_prompt_no_skips(self):
        self.cache_path.write_text(
            json.dumps(
                {
                    "ts": 1e15,
                    "outdated": [
                        {"name": "tqdm", "installed": "1.0", "latest": "1.1", "pinned": False, "note": ""},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("builtins.input", return_value="n"), mock.patch("updates._pip_upgrade") as upg:
            updates.run_update_check(ttl_hours=6, ask_to_update=True)
        upg.assert_not_called()


class TestBootstrapEnvBlocked(unittest.TestCase):
    """Bootstrap: solo gli errori di ambiente contano come "installato ma
    bloccato"; un ModuleNotFoundError (pacchetto o dipendenza davvero
    mancante) deve essere segnalato come mancante e quindi installato."""

    def test_missing_package_is_not_blocked(self):
        from config import _is_env_blocked_import

        self.assertFalse(
            _is_env_blocked_import(
                "faster_whisper", ModuleNotFoundError("No module named 'faster_whisper'")
            )
        )
        self.assertFalse(
            _is_env_blocked_import("moviepy", ModuleNotFoundError("No module named 'numpy'"))
        )

    def test_env_blocked_import_is_true(self):
        from config import _is_env_blocked_import

        # DLL di PyAV bloccata su Windows / ffmpeg di sistema mancante
        self.assertTrue(_is_env_blocked_import("faster_whisper", ImportError("DLL load failed")))
        self.assertTrue(_is_env_blocked_import("moviepy", RuntimeError("ffmpeg missing")))

    def test_other_packages_never_blocked(self):
        from config import _is_env_blocked_import

        self.assertFalse(_is_env_blocked_import("numpy", ImportError("x")))
        self.assertFalse(_is_env_blocked_import("pymupdf", RuntimeError("x")))


# Python che nessuna delle versioni di CI soddisfa: serve per i test su
# requires_python senza dipendere dalla versione in esecuzione.
_PY_IRRAGGIUNGIBILE = ">=3.99"
_PY_QUALSIASI = ">=3.9"


def _file(yanked=False, requires_python=None):
    return {"filename": "x.whl", "yanked": yanked, "requires_python": requires_python}


def _pypi(**releases):
    return {"releases": {v: files for v, files in releases.items()}}


class TestLatestCompatible(unittest.TestCase):
    """La "latest" deve essere una versione che pip installerebbe davvero."""

    def test_sceglie_la_piu_alta(self):
        data = _pypi(**{"1.0.0": [_file()], "1.5.0": [_file()], "2.0.0": [_file()]})
        self.assertEqual(updates._latest_compatible(data), "2.0.0")

    def test_salta_pre_release(self):
        # pip senza --pre non installa una beta: annunciarla sarebbe un falso
        # aggiornamento (il report direbbe "1.0.0 -> 2.0.0b1" e pip non farebbe nulla)
        data = _pypi(**{"1.0.0": [_file()], "2.0.0b1": [_file()]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_salta_release_interamente_yanked(self):
        data = _pypi(**{"1.0.0": [_file()], "2.0.0": [_file(yanked=True)]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_requires_python_dal_primo_file_utile(self):
        # files[0] è yanked e dichiara un Python più nuovo: non deve escludere
        # la release, perché esiste un file che il Python corrente lo accetta
        data = _pypi(
            **{
                "2.0.0": [
                    _file(yanked=True, requires_python=_PY_IRRAGGIUNGIBILE),
                    _file(requires_python=_PY_QUALSIASI),
                ]
            }
        )
        self.assertEqual(updates._latest_compatible(data), "2.0.0")

    def test_salta_requires_python_incompatibile(self):
        data = _pypi(**{"1.0.0": [_file()], "2.0.0": [_file(requires_python=_PY_IRRAGGIUNGIBILE)]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_salta_release_senza_file_e_versioni_illeggibili(self):
        data = _pypi(**{"1.0.0": [_file()], "2.0.0": [], "boh": [_file()]})
        self.assertEqual(updates._latest_compatible(data), "1.0.0")

    def test_nessuna_release_utilizzabile(self):
        data = _pypi(**{"1.0.0": [_file(yanked=True)]})
        self.assertIsNone(updates._latest_compatible(data))


def _urlopen_con(contenuto: bytes):
    """Finto urlopen che restituisce il JSON indicato."""
    gestore = mock.MagicMock()
    gestore.__enter__.return_value.read.return_value = contenuto
    return gestore


class TestLatestVersionPypiNonSolleva(unittest.TestCase):
    """`_latest_version_pypi` è interrogata in un thread pool: mai eccezioni."""

    def test_json_valido(self):
        data = json.dumps({"releases": {"1.0.0": [_file()]}}).encode()
        with mock.patch("updates.urllib.request.urlopen", return_value=_urlopen_con(data)):
            self.assertEqual(updates._latest_version_pypi("qualsiasi"), "1.0.0")

    def test_errore_interno_non_solleva(self):
        data = json.dumps({"releases": {"1.0.0": [_file()]}}).encode()
        with mock.patch("updates.urllib.request.urlopen", return_value=_urlopen_con(data)), mock.patch(
            "updates._latest_compatible", side_effect=RuntimeError("versione assurda")
        ), mock.patch("updates.log"):
            self.assertIsNone(updates._latest_version_pypi("qualsiasi"))

    def test_rete_assente_non_solleva(self):
        with mock.patch("updates.urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(updates._latest_version_pypi("qualsiasi"))


class TestTailLines(unittest.TestCase):
    def test_ultime_righe_non_vuote(self):
        testo = "prima\n\nseconda\n   \nterza\n"
        self.assertEqual(updates._tail_lines(testo, 2), ["seconda", "terza"])

    def test_testo_vuoto(self):
        self.assertEqual(updates._tail_lines(None), [])


class TestPipUpgrade(unittest.TestCase):
    def _risultato(self, returncode, stdout="", stderr=""):
        return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_successo_mostra_la_riga_di_pip(self):
        res = self._risultato(0, stdout="Successfully installed numpy-1.26.4")
        with mock.patch("updates.subprocess.run", return_value=res) as run, mock.patch("updates.log") as log:
            self.assertTrue(updates._pip_upgrade(["numpy"]))
        comando = run.call_args.args[0]
        self.assertEqual(comando[-2:], ["-U", "numpy"])
        messaggi = " ".join(str(argomento) for chiamata in log.info.call_args_list for argomento in chiamata.args)
        self.assertIn("Successfully installed numpy-1.26.4", messaggi)

    def test_fallimento_mostra_le_ultime_righe_di_pip(self):
        # prima si vedeva solo "non-zero exit status 1": inutile per capire se
        # è la rete, un conflitto di versioni o i permessi
        res = self._risultato(1, stderr="ERROR: ResolutionImpossible\nERROR: No matching distribution found")
        with mock.patch("updates.subprocess.run", return_value=res), mock.patch("updates.log") as log:
            self.assertFalse(updates._pip_upgrade(["numpy>=1.26.0"]))
        # il codice di uscita di pip è il primo argomento della prima riga
        self.assertEqual(log.warning.call_args_list[0].args[1], 1)
        messaggi = " ".join(str(argomento) for chiamata in log.warning.call_args_list for argomento in chiamata.args)
        self.assertIn("No matching distribution", messaggi)

    def test_fallimento_senza_stderr_usa_stdout(self):
        res = self._risultato(2, stdout="qualcosa e' andato storto")
        with mock.patch("updates.subprocess.run", return_value=res), mock.patch("updates.log") as log:
            self.assertFalse(updates._pip_upgrade(["numpy"]))
        messaggi = " ".join(str(argomento) for chiamata in log.warning.call_args_list for argomento in chiamata.args)
        self.assertIn("qualcosa e' andato storto", messaggi)

    def test_timeout(self):
        with mock.patch(
            "updates.subprocess.run", side_effect=subprocess.TimeoutExpired("pip", 900)
        ), mock.patch("updates.log"):
            self.assertFalse(updates._pip_upgrade(["numpy"]))


if __name__ == "__main__":
    unittest.main()
