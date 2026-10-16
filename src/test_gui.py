#!/usr/bin/env python3
"""Test di `gui.py`: solo la logica pura, mai la finestra.

Aprire Tk nei test li renderebbe dipendenti dallo schermo (in CI non c'e'):
qui si prova costruisci_comando() e il riconoscimento fasi, che e' dove
stanno le decisioni (quali tasti premere).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import gui
from gui import _fase_da_riga, costruisci_comando


class TestCostruisciComando(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.radice = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.radice / "presentazione.pdf").write_bytes(b"finta")
        (self.radice / "podcast.m4a").write_bytes(b"finto")

    def test_default_come_genera_video(self) -> None:
        comando = costruisci_comando(
            str(self.radice / "presentazione.pdf"), str(self.radice / "podcast.m4a")
        )
        for pezzo in ("--no-update-check", "--no-confirm", "--llm", "off", "--engine", "ffmpeg"):
            self.assertIn(pezzo, comando)
        self.assertIn("--verify-video", comando)
        self.assertNotIn("--transcriber", comando)
        self.assertNotIn("tiny", comando)

    def test_motori_e_opzioni(self) -> None:
        base = (
            str(self.radice / "presentazione.pdf"),
            str(self.radice / "podcast.m4a"),
        )
        comando = costruisci_comando(*base, motore="whisper", riverifica=True, bozza=True)
        self.assertIn("whisper", comando)
        self.assertIn("--verify-video", comando)
        self.assertIn("tiny", comando)
        comando = costruisci_comando(*base, motore="openvino")
        self.assertIn("openvino", comando)
        with self.assertRaisesRegex(ValueError, "motore non valido"):
            costruisci_comando(*base, motore="warp")

    def test_file_mancante_spiega_prima(self) -> None:
        with self.assertRaisesRegex(ValueError, "presentazione non trovata"):
            costruisci_comando(
                str(self.radice / "manca.pdf"), str(self.radice / "podcast.m4a")
            )
        with self.assertRaisesRegex(ValueError, "audio non trovato"):
            costruisci_comando(
                str(self.radice / "presentazione.pdf"), str(self.radice / "manca.m4a")
            )

    def test_nomi_relativi_partono_dalla_cartella(self) -> None:
        with mock.patch.object(gui, "BASE_DIR", self.radice):
            comando = costruisci_comando("presentazione.pdf", "podcast.m4a")
        self.assertIn(str(self.radice / "presentazione.pdf"), comando)


class TestFasi(unittest.TestCase):
    def test_fasi_riconosciute(self) -> None:
        casi = {
            "1. [CACHE] Slide OCR recuperate": "slide",
            "2. Trascrizione con OpenVINO": "trascriv",
            "sincronizzazione semantica": "Allineo",
            "IL VIDEO È PRONTO": "Pronto",
            "Manca il tuo materiale": "Manca",
            "Traceback inatteso": "problema",
        }
        for riga, atteso in casi.items():
            with self.subTest(riga=riga):
                trovata = _fase_da_riga(riga)
                self.assertIsNotNone(trovata)
                self.assertIn(atteso, trovata or "")

    def test_righe_ignote_ignorate(self) -> None:
        self.assertIsNone(_fase_da_riga("INFO | tutto bene, niente da dire"))


class TestRiepilogoMateriali(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.radice = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_mancanze_dette_chiaro(self) -> None:
        from gui import riepilogo_materiali

        scheda = riepilogo_materiali("manca.pdf", "manca.m4a", self.radice)
        self.assertFalse(scheda["pronto"])
        self.assertIn("presentazione", str(scheda["riga"]))
        self.assertIn("audio", str(scheda["riga"]))

    def test_pagine_contate_se_possibile(self) -> None:
        try:
            import fitz
        except ImportError:
            self.skipTest("pymupdf assente: pagine non verificabili")
            return
        from gui import riepilogo_materiali

        pdf = self.radice / "slide.pdf"
        doc = fitz.open()
        for _ in range(3):
            doc.new_page()
        doc.save(pdf)
        doc.close()
        (self.radice / "voce.mp3").write_bytes(b"finto-audio")
        scheda = riepilogo_materiali("slide.pdf", "voce.mp3", self.radice)
        self.assertTrue(scheda["pronto"])
        self.assertEqual(scheda["pagine"], 3)
        self.assertIsNone(scheda["durata_audio"])
        self.assertIn("3 slide", str(scheda["riga"]))


class TestStatoRun(unittest.TestCase):
    def test_vuota_none(self) -> None:
        from gui import stato_ultima_run

        with TemporaryDirectory() as tmp:
            self.assertIsNone(stato_ultima_run(Path(tmp)))

    def test_numeri_letti(self) -> None:
        import json

        from gui import stato_ultima_run

        with TemporaryDirectory() as tmp:
            radice = Path(tmp)
            (radice / ".cache").mkdir()
            (radice / ".cache" / "sync_report.json").write_text(
                json.dumps(
                    {
                        "audio_duration": 120.0,
                        "segments": [{"slide": 1}, {"slide": 2}],
                        "anchors": {"anchored": 1, "transitions": 1},
                        "frame_check": {"checked": 2, "coherent": 2, "mismatches": []},
                    }
                ),
                encoding="utf-8",
            )
            (radice / "video_finale.mp4").write_bytes(b"x" * 100)
            info = stato_ultima_run(radice)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info["slide"], 2)
            self.assertTrue(info["verificato"])
            self.assertEqual(info["video_byte"], 100)

    def test_storico_ultime_cinque(self) -> None:
        import json

        from gui import storico_run

        with TemporaryDirectory() as tmp:
            radice = Path(tmp)
            (radice / ".cache").mkdir()
            with (radice / ".cache" / "timing_history.jsonl").open("w", encoding="utf-8") as f:
                f.write("riga-rotta\n")
                for i in range(7):
                    f.write(json.dumps({"ts": f"t{i}", "total": float(i)}) + "\n")
            voci = storico_run(radice)
            self.assertEqual(len(voci), 5)
            self.assertEqual(voci[0]["quando"], "t6")


class TestImpostazioni(unittest.TestCase):
    def test_roundtrip_isolato(self) -> None:
        import os
        from unittest import mock

        from gui import impostazioni_carica, impostazioni_salva

        with (
            TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"APPDATA": tmp}),
        ):
            self.assertEqual(impostazioni_carica(), {})
            impostazioni_salva({"audio": "voce.mp3", "bozza": True})
            self.assertEqual(
                impostazioni_carica(), {"audio": "voce.mp3", "bozza": True}
            )

    def test_file_rotto_non_esplode(self) -> None:
        import os
        from unittest import mock

        from gui import impostazioni_carica

        with TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"APPDATA": tmp}):
            casa = Path(tmp)
            (casa / "sync-video").mkdir()
            (casa / "sync-video" / "impostazioni.json").write_text("rotto{", encoding="utf-8")
            self.assertEqual(impostazioni_carica(), {})


class TestRigheVerifica(unittest.TestCase):
    def test_righe_come_testo(self) -> None:
        from unittest import mock

        import verifica_pc
        from gui import righe_verifica

        with (
            TemporaryDirectory() as tmp,
            mock.patch.object(
                verifica_pc, "_esegui_prove", side_effect=lambda rep, prove: rep.aggiungi(
                    verifica_pc.Esito("Prova", verifica_pc.OK, "tutto bene")
                ),
            ),
        ):
            righe = righe_verifica(Path(tmp))
        self.assertEqual(righe, [("OK", "Prova", "tutto bene", "")])


class TestManutenzione(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.radice = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.radice / "presentazione.pdf").write_bytes(b"finta")
        (self.radice / "podcast.m4a").write_bytes(b"finto")

    def test_prova_usa_i_file_scelti_e_non_genera(self) -> None:
        from gui import comando_prova

        comando = comando_prova(
            str(self.radice / "presentazione.pdf"), str(self.radice / "podcast.m4a")
        )
        self.assertIn("--dry-run", comando)
        self.assertNotIn("--verify-video", comando)
        with self.assertRaisesRegex(ValueError, "presentazione non trovata"):
            comando_prova(str(self.radice / "manca.pdf"), str(self.radice / "podcast.m4a"))

    def test_aggiornamenti_mai_installa_da_solo(self) -> None:
        """Solo notifica: la S/N resta nel .bat apposta, qui non si preme."""
        from gui import comando_aggiornamenti

        comando = comando_aggiornamenti()
        self.assertIn("--no-update", comando)

    def test_cache_sicura_mai_modelli(self) -> None:
        from gui import comando_cache_peso, comando_cache_pulisci

        self.assertIn("--cache-du", comando_cache_peso())
        pulisci = comando_cache_pulisci()
        self.assertIn("--clean-cache", pulisci)
        self.assertNotIn("--include-models", pulisci)

    def test_runner_restituisce_uscita_e_righe(self) -> None:
        import sys

        from gui import esegui_streaming

        viste: list[str] = []
        codice = esegui_streaming(
            [sys.executable, "-c", "print('riga1'); print('riga2')"],
            viste.append,
            lambda: False,
        )
        self.assertEqual(codice, 0)
        self.assertEqual("".join(viste).split(), ["riga1", "riga2"])

    def test_runner_stop_sollega_fermato(self) -> None:
        import sys

        from gui import Fermato, esegui_streaming

        chiamate = {"n": 0}

        def on_riga(riga: str) -> None:
            chiamate["n"] += 1

        with self.assertRaises(Fermato):
            esegui_streaming(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                on_riga,
                lambda: True,
            )


if __name__ == "__main__":
    unittest.main()
