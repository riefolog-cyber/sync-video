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


if __name__ == "__main__":
    unittest.main()
