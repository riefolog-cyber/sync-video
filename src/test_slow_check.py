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

    def test_il_decoratore_segue_run_slow_in_entrambi_gli_stati(self) -> None:
        """Verifica il comportamento, non la formula con cui RUN_SLOW e' nato.

        Confrontare RUN_SLOW con la stessa espressione usata per definirlo e'
        tautologico: passa anche se il decoratore ignora il flag. Qui si
        forzano i due stati sul modulo (il wrapper legge il globale a ogni
        chiamata) e si ripristina il valore prima di uscire, senza reload e
        senza toccare l'ambiente del processo.
        """
        import test_slow

        @test_slow.slow("motivo di prova")
        def prova(self: object) -> str:
            return "eseguito"

        originale = test_slow.RUN_SLOW
        try:
            test_slow.RUN_SLOW = True
            self.assertEqual(prova(self), "eseguito")
            test_slow.RUN_SLOW = False
            with self.assertRaises(unittest.SkipTest):
                prova(self)
        finally:
            test_slow.RUN_SLOW = originale


if __name__ == "__main__":
    unittest.main()
