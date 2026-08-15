#!/usr/bin/env python3
"""
Test unitari per `updates` (controllo aggiornamenti pacchetti).
Non tocca la rete: mocka PyPI e la cache.

Esegui con: python -m unittest test_updates -v
"""

import json
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


if __name__ == "__main__":
    unittest.main()
