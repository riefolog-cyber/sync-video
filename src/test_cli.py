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
from chunks import Word


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


class TestDipendenzeSoloSeServono(unittest.TestCase):
    """I comandi che escono subito non devono avviare pip né sys.exit(1)."""

    def test_version_e_help_non_installano(self) -> None:
        import main

        with (
            mock.patch.object(config, "installa_dipendenze_se_mancano") as installa,
            mock.patch.object(main, "_dipendenze_verificate", False),
        ):
            for argv in (["--version"], ["--help"]):
                with self.subTest(argv=argv):
                    installa.reset_mock()
                    # Sia il wrapper chiamato in cima (import) sia main()
                    # devono lasciare l'ambiente intatto.
                    self.assertFalse(main.installa_dipendenze_se_mancano(argv))
                    with (
                        mock.patch.object(main, "bootstrap") as boot,
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.suppress(SystemExit),  # --help esce con 0
                    ):
                        main.main(argv)
                    installa.assert_not_called()
                    boot.assert_not_called()

    def test_run_normale_controlla_le_dipendenze(self) -> None:
        import main

        with (
            mock.patch.object(config, "installa_dipendenze_se_mancano") as installa,
            mock.patch.object(main, "_dipendenze_verificate", False),
        ):
            self.assertIs(main.installa_dipendenze_se_mancano([]), installa.return_value)
        installa.assert_called_once_with()


class TestAvvisoMaterialePocoDenso(unittest.TestCase):
    def test_usa_la_durata_gia_nota_senza_riaprire_l_audio(self) -> None:
        import main

        with (
            mock.patch.object(main, "AudioFileClip") as clip,
            mock.patch.object(main.log, "warning") as avviso,
        ):
            main._avviso_materiale_poco_denso(10, 100.0)
        clip.assert_not_called()
        avviso.assert_called_once()
        self.assertEqual(avviso.call_args.args[1], 10)


class TestCorrezioneNomiTutteLeTrascrizioni(unittest.TestCase):
    """Veloce, zone ritrascritte e accurata ricevono lo stesso vocabolario."""

    def test_il_vocabolario_e_calcolato_una_volta_solo(self) -> None:
        import ast

        import main

        albero = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
        funzioni = {n.name: n for n in albero.body if isinstance(n, ast.FunctionDef)}

        def chiamate_in(nome: str) -> list[ast.Call]:
            return [
                n
                for n in ast.walk(funzioni[nome])
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "correct_transcript_names"
            ]

        # Due in main (veloce + accurata) e una nel recupero zone: nessuna
        # puo' piu' chiamare la correzione senza il vocabolario del deck.
        self.assertEqual(len(chiamate_in("main")), 2)
        self.assertEqual(len(chiamate_in("_recover_anchor_zones")), 1)
        for chiamata in chiamate_in("main") + chiamate_in("_recover_anchor_zones"):
            self.assertGreaterEqual(len(chiamata.args), 2, "manca il vocabolario")
            self.assertIsInstance(chiamata.args[1], ast.Name)
        # La variabile e' la stessa per veloce e accurata: l'A/B confronta
        # due trascrizioni trattate in modo identico.
        self.assertEqual(
            {
                chiamata.args[1].id
                for chiamata in chiamate_in("main")
                if isinstance(chiamata.args[1], ast.Name)
            },
            {"_vocabolario_nomi"},
        )
        calcoli = [
            n for n in ast.walk(funzioni["main"])
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "vocabolario_del_deck"
        ]
        self.assertEqual(len(calcoli), 1, "il vocabolario va calcolato una volta")

    def test_zone_ritrascritte_correggono_i_nomi_dal_deck(self) -> None:
        import main

        parole: list[Word] = [
            {"word": "ciao", "start": 5.0},
            {"word": "mondo", "start": 20.0},
        ]
        correzioni: list[str] = []

        def _fake(audio_path: Path, lo: float, hi: float, **kwargs: object) -> list[dict]:
            return [{"word": "kappo", "start": lo + 1.0}]

        with (
            mock.patch.object(main, "retranscribe_zone", side_effect=_fake),
            mock.patch.object(main, "_write_raw_transcript", return_value=None),
        ):
            esito = main._recover_anchor_zones(
                parole,
                {2: 10.0, 4: 50.0},
                4,
                300.0,
                Path("audio.m4a"),
                config.parse_args([]),
                "slide-audio",
                vocabolario_nomi={"kappa": 2},
                correzioni_nomi=correzioni,
            )
        self.assertIn("kappa", [w["word"] for w in esito.words])
        self.assertTrue(correzioni, "la correzione va annotata per il report")


class TestDipendenzeSoloSeServonoCompleta(unittest.TestCase):
    def test_comando_senza_effetti_su_pc_vuoto_installa_comunque(self) -> None:
        """--version non puo' morire con ModuleNotFoundError durante l'import.

        I moduli pesanti sono importati subito dopo la scansione: se non ci
        sono, l'installazione va fatta anche per un comando di ispezione.
        """
        import main

        with (
            mock.patch.object(config, "installa_dipendenze_se_mancano", return_value=True) as installa,
            mock.patch.object(main, "_dipendenze_verificate", False),
            mock.patch.object(main, "_moduli_gia_presenti", return_value=False),
        ):
            self.assertTrue(main.installa_dipendenze_se_mancano(["--version"]))
        installa.assert_called_once_with()

    def test_review_non_e_senza_effetti(self) -> None:
        """--review scrive report e anteprime: ha bisogno delle dipendenze."""
        import main

        self.assertNotIn("--review", main._FLAG_SENZA_EFFETTI)
        with (
            mock.patch.object(config, "installa_dipendenze_se_mancano", return_value=True) as installa,
            mock.patch.object(main, "_dipendenze_verificate", False),
        ):
            main.installa_dipendenze_se_mancano(["--review"])
        installa.assert_called_once_with()

    def test_float_infinito_rifiutato(self) -> None:
        """float('inf') passava il controllo >= 0 e arrivava al rendering."""
        with self.assertRaises(SystemExit):
            config.parse_args(["--semantic-window", "inf"])
        with self.assertRaises(SystemExit):
            config.parse_args(["--semantic-window", "nan"])


class TestParametriTrascrizioneUnicaFonte(unittest.TestCase):
    def test_chiave_e_params_usano_la_stessa_funzione(self) -> None:
        import main

        args = config.parse_args([])
        with mock.patch.object(
            main,
            "_transcript_engine_params",
            return_value=("whisper", ["cpu", "int8", "beam1", "batch8"]),
        ) as fonte:
            params = main._transcript_params("a" * 32, args)
            chiave = main._transcript_cache_key("a" * 32, args)
        self.assertEqual(fonte.call_count, 2)
        self.assertEqual(params["engine_params"], "cpu,int8,beam1,batch8")
        self.assertIn("cpu_int8_beam1_batch8", chiave)


if __name__ == "__main__":
    unittest.main()
