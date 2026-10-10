#!/usr/bin/env python3
"""Test dell'helper slow (test_slow.py).

Esegui con: python -m unittest test_slow_check -v
"""

import unittest

from test_slow import RUN_SLOW, slow


class TestSlowHelper(unittest.TestCase):
    def test_skip_senza_env(self) -> None:
        @slow("motivo di prova")
        def prova(self: object) -> str:
            return "eseguito"

        if RUN_SLOW:
            self.assertEqual(prova(self), "eseguito")
        else:
            with self.assertRaises(unittest.SkipTest):
                prova(self)

    def test_run_slow_flag_coerente(self) -> None:
        import os

        self.assertEqual(RUN_SLOW, os.environ.get("SYNC_VIDEO_RUN_SLOW", "") == "1")


if __name__ == "__main__":
    unittest.main()
