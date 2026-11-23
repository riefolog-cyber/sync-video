#!/usr/bin/env python3
"""Test dell'interfaccia a riga di comando.

Esegui con: python -m unittest test_cli -v

Un `%` non escapato in una stringa `help=` fa esplodere argparse al momento
di `--help` (non quando l'argomento viene usato): il bug resta quindi
invisibile finche' nessuno lancia `main.py --help`. Qui lo si blocca.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import unittest
from pathlib import Path
from typing import ClassVar
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


class TestFlagCitatiNeiMessaggiDiConfig(unittest.TestCase):
    """Ogni --flag citato in una stringa di config deve esistere nel parser.

    Il bug che copre: il messaggio 'disco pieno' suggeriva
    --embedding-cache-dir, che non esiste (quello vero e'
    --semantic-cache-dir): copiando il comando si prendeva un errore.
    """

    # Flag di comandi esterni citati nei messaggi (git, pip, tesseract,
    # winget) e l'aiuto standard di argparse: non sono opzioni della nostra CLI.
    NON_CLI: ClassVar[frozenset[str]] = frozenset({
        "--help",
        "--upgrade",
        "--tags",
        "--always",
        "--dirty",
        "--list-langs",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--id",
    })

    def test_ogni_flag_citato_esiste_nel_parser(self) -> None:
        sorgente = Path(config.__file__).read_text(encoding="utf-8")
        citati: set[str] = set()
        for nodo in ast.walk(ast.parse(sorgente)):
            # Solo stringhe: i commenti con flag storici non sono messaggi.
            if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
                citati.update(re.findall(r"--[A-Za-z][A-Za-z0-9-]*", nodo.value))

        parser = config.build_parser()
        esistenti = {opzione for azione in parser._actions for opzione in azione.option_strings}
        sconosciuti = citati - esistenti - self.NON_CLI
        self.assertEqual(
            sconosciuti,
            set(),
            f"Flag citati in config ma inesistenti nella CLI: {sorted(sconosciuti)}",
        )


class TestMutuaEsclusioneModalita(unittest.TestCase):
    """--preview / --dry-run / --verify-video non possono coesistere."""

    def test_coppie_rifiutate(self) -> None:
        for coppia in (
            ("--preview", "--dry-run"),
            ("--preview", "--verify-video"),
            ("--dry-run", "--verify-video"),
        ):
            with self.subTest(coppia=coppia):
                with (
                    contextlib.redirect_stderr(io.StringIO()) as errore,
                    self.assertRaises(SystemExit) as ctx,
                ):
                    config.parse_args(list(coppia))
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("not allowed with argument", errore.getvalue())

    def test_singoli_accettati(self) -> None:
        for flag in ("--preview", "--dry-run", "--verify-video"):
            with self.subTest(flag=flag):
                args = config.parse_args([flag])
                self.assertTrue(getattr(args, flag[2:].replace("-", "_")))


class TestCleanCacheSenzaTty(unittest.TestCase):
    """Senza terminale la conferma non si puo' porre: serve --yes."""

    def test_senza_yes_rifiuta_con_exit_2(self) -> None:
        with (
            mock.patch.object(config.sys.stdin, "isatty", return_value=False),
            mock.patch.object(config.log, "error") as errore,
            self.assertRaises(SystemExit) as ctx,
        ):
            config.parse_args(["--clean-cache", "--include-models"])
        self.assertEqual(ctx.exception.code, 2)
        messaggio = " ".join(str(argomento) for argomento in errore.call_args.args)
        self.assertIn("--yes", messaggio)
        self.assertIn("terminale", messaggio)

    def test_con_yes_senza_tty_procede(self) -> None:
        with mock.patch.object(config.sys.stdin, "isatty", return_value=False):
            args = config.parse_args(["--clean-cache", "--include-models", "--yes"])
        self.assertTrue(args.yes)
        self.assertTrue(args.include_models)

    def test_con_tty_senza_yes_non_rifiuta(self) -> None:
        # La conferma resta interattiva (la pone main): qui non deve scattare
        # il rifiuto previsto per batch/CI/pipe.
        with mock.patch.object(config.sys.stdin, "isatty", return_value=True):
            args = config.parse_args(["--clean-cache", "--include-models"])
        self.assertFalse(args.yes)

    def test_help_yes_spiega_obbligatorieta(self) -> None:
        testo = " ".join(config.build_parser().format_help().split())
        self.assertIn("Obbligatorio senza terminale", testo)


class TestHelpAllineatoAlCodice(unittest.TestCase):
    def test_verify_video_solo_frame_sbagliati(self) -> None:
        testo = " ".join(config.build_parser().format_help().split())
        self.assertIn("solo i frame dei segmenti SBAGLIATI", testo)
        self.assertIn("se la cartella e' vuota la verifica e' andata bene", testo)

    def test_fix_slide_ricorda_dry_run(self) -> None:
        testo = " ".join(config.build_parser().format_help().split())
        self.assertIn("La run normale continua fino al rendering", testo)
        # argparse puo' spezzare `--dry-run` a cavallo di riga (`--dry- run`)
        # quando l'help e' lungo: verifichiamo l'inizio della frase, non la
        # formattazione del wrapping.
        self.assertIn("aggiungi --dry", testo)


class TestFlagUscitaAnticipata(unittest.TestCase):
    """Flag che fermano la pipeline prima del rendering, e le loro implicazioni.

    Qui si chiama `parse_args` (non `main`): e' li' che i flag vengono letti e
    che `--strict-sync` accende `--verify-video`. I percorsi che escono davvero
    stanno in `main`, ma se il parser non espone il flag giusto la run non ci
    arriva nemmeno.
    """

    # flag -> attributo del Namespace restituito da parse_args.
    FLAG_SEMPLICI: ClassVar[dict[str, str]] = {
        "--dry-run": "dry_run",
        "--preview": "preview",
        "--verify-video": "verify_video",
        "--cache-du": "cache_du",
        "--solo-riepilogo": "solo_riepilogo",
        "--clean-cache": "clean_cache",
    }

    def test_default_spenti(self) -> None:
        args = config.parse_args([])
        for nome in self.FLAG_SEMPLICI.values():
            with self.subTest(flag=nome):
                self.assertFalse(getattr(args, nome))
        self.assertFalse(args.strict_sync)
        self.assertFalse(args.fix_slide)

    def test_ogni_flag_si_accende(self) -> None:
        for flag, dest in self.FLAG_SEMPLICI.items():
            with self.subTest(flag=flag):
                self.assertTrue(getattr(config.parse_args([flag]), dest))

    def test_strict_sync_attiva_verify_video(self) -> None:
        # L'implicazione e' applicata da parse_args, non da main: se sparisce,
        # una run "stretta" non controlla piu' l'artefatto e nessun errore
        # avvisa.
        args = config.parse_args(["--strict-sync"])
        self.assertTrue(args.strict_sync)
        self.assertTrue(args.verify_video)

    def test_strict_sync_e_dry_run_coexistono(self) -> None:
        # --strict-sync accende verify_video come effetto collaterale, senza
        # passare dal gruppo mutuamente esclusivo: con --dry-run il video non
        # si genera e la verifica non parte, ma il parsing non deve errorare.
        args = config.parse_args(["--strict-sync", "--dry-run"])
        self.assertTrue(args.strict_sync)
        self.assertTrue(args.verify_video)
        self.assertTrue(args.dry_run)


class TestFlagFixSlide(unittest.TestCase):
    """`--fix-slide` e' ripetibile: ogni occorrenza aggiunge una correzione."""

    def test_default_vuoto(self) -> None:
        self.assertFalse(config.parse_args([]).fix_slide)

    def test_una_correzione(self) -> None:
        self.assertEqual(config.parse_args(["--fix-slide", "5=252.0"]).fix_slide, ["5=252.0"])

    def test_piu_correzioni(self) -> None:
        args = config.parse_args(["--fix-slide", "5=252.0", "--fix-slide", "9=431.25"])
        self.assertEqual(args.fix_slide, ["5=252.0", "9=431.25"])


class TestMutuaEsclusioneUscitaAnticipata(unittest.TestCase):
    """`--preview`, `--dry-run` e `--verify-video` non si combinano.

    In `main` il controllo su `--preview` arriva prima del dry-run e della
    verifica video: combinarli faceva vincere il primo in silenzio. Ora il
    conflitto viene dichiarato subito da argparse, con un errore che nomina i
    due flag. Il parser e' la guardia: qui si verifica che rifiuti ogni coppia
    e che lasci passare i flag da soli.
    """

    COPPIE_INCOMPATIBILI = (
        ("--preview", "--dry-run"),
        ("--preview", "--verify-video"),
        ("--dry-run", "--verify-video"),
    )

    def test_ogni_flag_da_solo_e_accettato(self) -> None:
        for flag in ("--preview", "--dry-run", "--verify-video"):
            with self.subTest(flag=flag):
                self.assertTrue(getattr(config.parse_args([flag]), flag.strip("-").replace("-", "_")))

    def test_le_coppie_vengono_rifiutate(self) -> None:
        for primo, secondo in self.COPPIE_INCOMPATIBILI:
            with self.subTest(primo=primo, secondo=secondo):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                    config.parse_args([primo, secondo])
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
