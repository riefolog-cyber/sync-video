#!/usr/bin/env python3
"""Regressioni della revisione batch 20.

Il batch 19 aveva corretto 29 difetti; questa revisione cerca cio' che quelli
hanno lasciato indietro e le aree mai lette riga per riga (pip check, formattazione
dei file prodotti, riuso del video, conteggi della pulizia).

Esegui con: python -m unittest test_review_batch20 -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import _debug_hardware
import aggiornamenti
import cache_maintenance
import config
import pipeline_inputs
import riepilogo
import subtitles
import updates
import video_riuso


def _voce_aggiornamento(nome: str = "tqdm") -> dict[str, Any]:
    return {
        "name": nome,
        "installed": "1.0",
        "latest": "1.1",
        "pinned": False,
        "major": False,
        "note": "",
        "blocked": [],
    }


class TestAggiornamentiVeritieri(unittest.TestCase):
    def test_conflitto_del_pacchetto_aggiornato_non_e_preesistente(self) -> None:
        # pip check nomina CHI RICHIEDE: "fastembed ... requires pillow<11".
        # Guardare solo l'inizio della riga faceva passare come sano un upgrade
        # di pillow che rompeva fastembed.
        esiti = [
            mock.Mock(returncode=0),
            mock.Mock(
                returncode=1,
                stdout="fastembed 0.5.1 requires pillow<11.0.0, but you have pillow 12.3.0",
                stderr="",
            ),
        ]
        with mock.patch.object(updates.subprocess, "run", side_effect=esiti):
            self.assertFalse(updates._health_check(["pillow==12.3.0"]))

    def test_conflitto_senza_pacchetti_aggiornati_e_preesistente(self) -> None:
        esiti = [
            mock.Mock(returncode=0),
            mock.Mock(
                returncode=1,
                stdout="altro 1.0 requires foo<2, but you have foo 3.0",
                stderr="",
            ),
        ]
        with mock.patch.object(updates.subprocess, "run", side_effect=esiti):
            self.assertTrue(updates._health_check(["numpy==2.0.0"]))

    def test_installati_non_stampato_se_health_check_fallisce(self) -> None:
        esiti = [
            mock.Mock(returncode=0, stdout="Successfully installed numpy-2.0.0", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="Modulenerror"),
        ]
        with (
            mock.patch.object(updates.subprocess, "run", side_effect=esiti),
            self.assertLogs(updates.log, level="INFO") as log,
        ):
            self.assertFalse(updates._pip_upgrade(["numpy==2.0.0"]))
        self.assertFalse(any("✅ Installati" in riga for riga in log.output))

    def test_aggiornamenti_esce_uno_se_installazione_fallita(self) -> None:
        with (
            mock.patch.object(sys, "argv", ["aggiornamenti.py"]),
            mock.patch.object(aggiornamenti, "bootstrap"),
            mock.patch.object(updates, "check_updates", return_value=[_voce_aggiornamento()]),
            mock.patch.object(updates, "print_updates"),
            mock.patch.object(updates, "_ask_yes_no", return_value=True),
            mock.patch.object(updates, "_pip_upgrade", return_value=False),
        ):
            self.assertEqual(aggiornamenti.main(), 1)

    def test_bootstrap_error_elenca_solo_i_pacchetti_falliti(self) -> None:
        requisiti = {"a": "a>=1", "b": "b>=1", "c": "c>=1"}
        with (
            mock.patch.object(config, "_active_required_packages", return_value=requisiti),
            mock.patch.object(config, "_missing_or_old", return_value="assente"),
            mock.patch.object(config, "_try_pip_install", side_effect=[True, False, True]),
            mock.patch.object(config.log, "error") as errore,
            self.assertRaises(SystemExit),
        ):
            config._ensure_pip_packages()
        argomenti = errore.call_args[0]
        messaggio = str(argomenti[0]) % argomenti[1:] if len(argomenti) > 1 else str(argomenti[0])
        self.assertIn("b>=1", messaggio)
        self.assertNotIn("a>=1", messaggio)
        self.assertNotIn("c>=1", messaggio)

    def test_copia_tessdata_non_lascia_file_troncato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sorgente = Path(tmp) / "sistema"
            destinazione = Path(tmp) / "locale"
            sorgente.mkdir()
            destinazione.mkdir()
            (sorgente / "eng.traineddata").write_bytes(b"modello-intero")

            def copia_troncata(src: str | Path, dst: str | Path) -> None:
                Path(dst).write_bytes(b"parziale")
                raise OSError("disco pieno")

            with mock.patch.object(config.shutil, "copy2", side_effect=copia_troncata):
                copiati = config._mirror_system_tessdata(sorgente, destinazione)
            self.assertEqual(copiati, 0)
            self.assertFalse((destinazione / "eng.traineddata").exists())


class TestFileProdotti(unittest.TestCase):
    def test_srt_e_capitoli_con_nome_puntato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.v2.mp4"
            srt, capitoli = subtitles.scrivi_sottotitoli(
                video,
                [{"word": "ciao", "start": 0.0}],
                {1: 0.0},
                ["Slide uno"],
            )
            self.assertEqual(srt, Path(tmp) / "video.v2.srt")
            self.assertEqual(capitoli, Path(tmp) / "video.v2.capitoli.txt")
            self.assertIsNotNone(srt)
            assert srt is not None
            self.assertTrue(srt.exists())

    def test_parole_fuori_ordine_non_azzerrano_i_sottotitoli(self) -> None:
        parole = [
            {"word": "prima", "start": 5.1},
            {"word": "dopo", "start": 5.0},
        ]
        for inizio, fine, _testo in subtitles.costruisci_cue(parole):
            self.assertLessEqual(inizio, fine)
        # Ordinati per tempo: "dopo" a 5.0 viene prima di "prima" a 5.1.
        self.assertIn("dopo prima", subtitles.build_srt(parole))

    def test_valore_sporco_non_cancella_riepilogo(self) -> None:
        report: dict[str, object] = {
            "segments": [{"slide": "x", "end": 10.0, "duration": 5.0}],
            "ripresa_da": "n/d",
            "versione": "v1",
        }
        testo = riepilogo.riepilogo_markdown(report)
        self.assertIn("| Pagine | 1 |", testo)
        self.assertIn("| Trascrizione ripresa da |", testo)

    def test_find_audio_file_cartella_assente_restituisce_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pipeline_inputs.find_audio_file(Path(tmp) / "inesistente"))


class TestRiusoEPulizia(unittest.TestCase):
    def test_chiave_video_dipende_dal_formato_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m4a = Path(tmp) / "podcast.m4a"
            wav = Path(tmp) / "podcast.wav"
            m4a.write_bytes(b"stessi-byte")
            wav.write_bytes(b"stessi-byte")
            chiave_m4a = video_riuso.chiave_video([], [1.0], m4a, 5, 0.0, "ffmpeg", "libx264")
            chiave_wav = video_riuso.chiave_video([], [1.0], wav, 5, 0.0, "ffmpeg", "libx264")
            self.assertNotEqual(chiave_m4a, chiave_wav)

    def test_clean_cache_non_azzerra_frame_check_senza_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            report = {"frame_check": {"checked": 3, "coherent": 3}, "alt": "resta"}
            (base / "sync_report.json").write_text(json.dumps(report), encoding="utf-8")
            esito = cache_maintenance.clean_cache(base)
            self.assertEqual(esito["verify_frames"], 0)
            rimasto = json.loads((base / "sync_report.json").read_text(encoding="utf-8"))
            self.assertEqual(rimasto["frame_check"], report["frame_check"])

    def test_un_parziale_e_una_coppia_di_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parziali = base / "parziali"
            parziali.mkdir()
            vecchio = time.time() - 10 * 86400
            for nome in ("podz.json", "podz_resto.flac"):
                file = parziali / nome
                file.write_bytes(b"vecchio")
                os.utime(file, (vecchio, vecchio))
            esito = cache_maintenance.clean_cache(base)
            self.assertEqual(esito["parziali"], 1)
            self.assertEqual(list(parziali.iterdir()), [])

    def test_segna_video_fallito_rimuove_marca_vecchia(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache = base / "cache"
            output = base / "video.mp4"
            output.write_bytes(b"nuovo-video")
            video_riuso._percorso_marca(output, cache).parent.mkdir(parents=True, exist_ok=True)
            vecchia = video_riuso._percorso_marca(output, cache)
            vecchia.write_text(json.dumps({"chiave": "vecchia"}), encoding="utf-8")
            with (
                mock.patch.object(video_riuso, "atomic_write_text", side_effect=OSError("readonly")),
                self.assertLogs(video_riuso.log, level="WARNING"),
            ):
                video_riuso.segna_video(output, "nuova", cache_dir=cache)
            self.assertFalse(vecchia.exists())

    def test_debug_hardware_rispetta_override_video_encoder(self) -> None:
        completo = frozenset(
            {"libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"}
        )
        with (
            mock.patch.dict(os.environ, {"VIDEO_ENCODER": "libx264"}),
            mock.patch("machine_setup.ffmpeg_encoders", return_value=completo),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            ok = _debug_hardware.scenario(
                "NVIDIA desktop, 32 GB",
                ["NVIDIA GeForce RTX 4060"],
                "nvidia",
                32,
                completo,
                "win32",
            )
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
