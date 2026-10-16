#!/usr/bin/env python3
"""Test dell'interfaccia a riga di comando.

Esegui con: python -m unittest test_cli -v

Un `%` non escapato in una stringa `help=` fa esplodere argparse al momento
di `--help` (non quando l'argomento viene usato): il bug resta quindi
invisibile finche' nessuno lancia `main.py --help`. Qui lo si blocca.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

import config


class TestHelpSiGenera(unittest.TestCase):
    def test_help_non_esplode(self) -> None:
        # argparse fa help_text % params: un '%' letterale nella help lo
        # trasforma in un errore di formato. Il sintomo e' un ValueError
        # "incomplete format" SOLO con --help.
        parser = config.build_parser()
        testo = parser.format_help()
        self.assertIn("--video-encoder", testo)
        self.assertIn("--min-anchor-coverage", testo)

    def test_il_percentuale_alla_fine_e_intatto(self) -> None:
        # Il 100% deve uscire come 100%, non come 100.
        parser = config.build_parser()
        testo = parser.format_help()
        self.assertIn("100%", testo)

    def test_help_principale_di_main(self) -> None:
        # Percorso completo: il parser vero di main, non quello ricostruito.
        import main

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            main.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)


class TestNuovoFlagVideoEncoder(unittest.TestCase):
    def test_default_e_auto(self) -> None:
        # 'auto' = misura cosa funziona davvero su questa macchina.
        self.assertEqual(config.parse_args([]).video_encoder, "auto")

    def test_valore_esplicito_accettato(self) -> None:
        for enc in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"):
            with self.subTest(enc=enc):
                self.assertEqual(config.parse_args(["--video-encoder", enc]).video_encoder, enc)

    def test_valore_inventato_rifiutato(self) -> None:
        # choices fa fallire argparse su un encoder inesistente: meglio un
        # errore immediato che un video che non esce.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            config.parse_args(["--video-encoder", "h264_inesistente"])


class TestFlagReview(unittest.TestCase):
    """`--review`: comando di sola lettura, spento di default."""

    def test_spenta_di_default(self) -> None:
        # Se partisse accesa, una run normale si fermerebbe a ristampare un
        # report invece di generare il video.
        self.assertFalse(config.parse_args([]).review)

    def test_si_accende(self) -> None:
        self.assertTrue(config.parse_args(["--review"]).review)

    def test_compare_nell_help(self) -> None:
        self.assertIn("--review", config.build_parser().format_help())


class TestFlagVersione(unittest.TestCase):
    def test_default_spento(self) -> None:
        self.assertFalse(config.parse_args([]).version)

    def test_si_accende(self) -> None:
        self.assertTrue(config.parse_args(["--version"]).version)

    def test_compare_nell_help(self) -> None:
        self.assertIn("--version", config.build_parser().format_help())

    def test_stampa_ed_esce_senza_bootstrap(self) -> None:
        """--version deve funzionare anche su un PC rotto: niente installazioni."""
        import main as m

        with (
            mock.patch.object(m, "bootstrap") as boot,
            mock.patch.object(m, "machine_setup") as setup,
            io.StringIO() as buf,
            contextlib.redirect_stdout(buf),
        ):
            m.main(["--version"])
            uscita = buf.getvalue()
        boot.assert_not_called()
        setup.assert_not_called()
        self.assertTrue(uscita.strip())


if __name__ == "__main__":
    unittest.main()
