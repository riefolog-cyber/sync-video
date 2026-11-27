#!/usr/bin/env python3
"""Regression del Batch 36 (review: updates, hardware, video, diagnostica).

Ogni test copre un difetto verificato: cache che certificava uno stato falso,
referto hardware che dichiarava l'encoder sbagliato, backup distrutto, bundle
contenente dati privati. Nessun test richiede rete o ffmpeg reale.

Esegui con: python -m unittest discover -s src -p 'test_review_batch36.py' -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _debug_hardware
import blocco_run
import cache_maintenance
import diagnostica
import hardware
import machine_setup
import main as main_mod
import pipeline_cache
import riepilogo
import updates
import verifica_pc
import video


class TestCacheAggiornamenti(unittest.TestCase):
    def test_cache_vuota_non_certifica_tutti_aggiornati_dopo_downgrade(self) -> None:
        cache = {
            "ts": time.time(),
            "outdated": [],
            "installed": {"numpy": "1.0"},
        }
        with (
            mock.patch.object(updates, "_read_cache", return_value=cache),
            mock.patch.object(
                updates, "_installed_version", side_effect=lambda n: "0.1" if n == "numpy" else None
            ),
            mock.patch.object(
                updates,
                "_latest_version_pypi",
                side_effect=lambda n: "9.9.9" if n == "numpy" else None,
            ),
            mock.patch.object(updates, "_installed_constraints", return_value={}),
        ):
            trovati = updates.check_updates(ttl_hours=6)
        self.assertTrue(any(v["name"] == "numpy" for v in trovati))

    def test_cache_hit_ricalcola_i_blocchi_dalle_dipendenze_fresche(self) -> None:
        voce = {
            "name": "tqdm",
            "installed": "1.0",
            "latest": "1.1",
            "pinned": False,
            "major": False,
            "note": "",
            "blocked": [],
        }
        cache = {
            "ts": time.time(),
            "outdated": [voce],
            "installed": {"tqdm": "1.0"},
        }
        with (
            mock.patch.object(updates, "_read_cache", return_value=cache),
            mock.patch.object(updates, "_installed_version", return_value="1.0"),
            mock.patch.object(updates, "_installed_constraints", return_value={}),
            mock.patch.object(updates, "_blockers", return_value=["altrove<1.1"]) as blocchi,
        ):
            trovati = updates.check_updates(ttl_hours=6)
        blocchi.assert_called()
        self.assertEqual(trovati[0]["blocked"], ["altrove<1.1"])

    def test_pin_non_scrive_override_se_requirements_non_scrivibile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "requirements.txt").write_text("fastembed==0.5.1\n", encoding="utf-8")
            cache = base / "cache"
            cache.mkdir()

            def scrivi(path: Path, _testo: str) -> None:
                if path.name == "requirements.txt":
                    raise OSError("read-only")

            with (
                mock.patch.object(updates, "BASE_DIR", base),
                mock.patch.object(updates, "CACHE_DIR", cache),
                mock.patch.object(updates, "atomic_write_text", side_effect=scrivi),
            ):
                updates._aggiorna_pin({"fastembed": "0.6.0"})
            self.assertFalse((cache / "pin_overrides.json").exists())


class TestSetupHardware(unittest.TestCase):
    def test_probe_fallito_non_viene_persistere_come_no_gpu(self) -> None:
        with (
            mock.patch.object(machine_setup.subprocess, "run", side_effect=OSError("ACL")),
            mock.patch.object(machine_setup, "_ULTIMO_PROBE_FALLITO", False),
        ):
            self.assertEqual(machine_setup._run(["falso"]), "")
            self.assertTrue(machine_setup._ULTIMO_PROBE_FALLITO)

    def test_device_openvino_esplicito_non_viene_riscritto(self) -> None:
        args = SimpleNamespace(
            transcriber="auto",
            whisper_device="cpu",
            whisper_compute_type="int8",
            openvino_device="CPU",
            openvino_device_explicit=True,
        )
        machine_setup._apply(args, {"transcriber": "openvino", "whisper_device": "cpu",
                                     "whisper_compute_type": "int8", "openvino_device": "GPU"})
        self.assertEqual(args.openvino_device, "CPU")

    def test_provision_openvino_rispetta_il_cap_di_requirements(self) -> None:
        with (
            mock.patch.dict(sys.modules, {"openvino_genai": None}),
            mock.patch.object(machine_setup, "_pip_install", return_value=False) as pip,
        ):
            machine_setup._provision_openvino(Path("ir"))
        pip.assert_called_once()
        self.assertIn("<2026", pip.call_args.args[0])


class TestProbeEncoder(unittest.TestCase):
    @staticmethod
    def _esito() -> verifica_pc.Relazione:
        return verifica_pc.Relazione()

    def test_override_video_encoder_usato_nel_dettaglio(self) -> None:
        risposta = SimpleNamespace(stdout=" V..... h264_nvenc nvenc")
        relazione = self._esito()
        with (
            mock.patch.object(verifica_pc.shutil, "which", return_value="ffmpeg"),
            mock.patch.object(verifica_pc.subprocess, "run", return_value=risposta),
            mock.patch("machine_setup.detect_gpus", return_value=["NVIDIA RTX 4060"]),
            mock.patch.dict(os.environ, {"VIDEO_ENCODER": "libx264"}),
        ):
            verifica_pc.prova_ffmpeg(relazione)
        self.assertIn("libx264", relazione.esiti[0].dettaglio)
        self.assertNotIn("encoder effettivo: h264_nvenc", relazione.esiti[0].dettaglio)

    def test_probe_encoder_non_disponibile_non_dice_ok(self) -> None:
        risposta = SimpleNamespace(stdout=" V..... h264_nvenc nvenc")
        relazione = self._esito()
        with (
            mock.patch.object(verifica_pc.shutil, "which", return_value="ffmpeg"),
            mock.patch.object(verifica_pc.subprocess, "run", return_value=risposta),
            mock.patch.dict(sys.modules, {"machine_setup": None}),
        ):
            verifica_pc.prova_ffmpeg(relazione)
        esito = relazione.esiti[0]
        self.assertEqual(esito.stato, verifica_pc.AVV)
        self.assertTrue(esito.rimedio)
        self.assertNotIn("effettivo:  ", esito.dettaglio)

    def test_cache_encoder_con_ts_non_finito_scaduta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            payload = {
                "fingerprint": hardware._cache_fingerprint(),
                "ts": float("nan"),
                "encoders": ["h264_nvenc"],
            }
            (base / "hardware.json").write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch.object(hardware, "HARDWARE_CACHE", base / "hardware.json"):
                self.assertIsNone(hardware._read_encoder_cache())

    def test_encoding_ok_solo_se_non_c_e_ripiego(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.mp4"
            out.write_bytes(b"video")
            self.assertTrue(_debug_hardware._encoding_ok(out, "h264", ["h264_qsv"], "h264_qsv"))
            self.assertFalse(
                _debug_hardware._encoding_ok(out, "h264", ["h264_qsv", "libx264"], "h264_qsv")
            )


class TestBackupVideo(unittest.TestCase):
    def test_output_vuoto_non_sovrascrive_il_backup_buono(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out = base / "video_finale.mp4"
            out.write_bytes(b"")
            backup = base / "video_finale (precedente).mp4"
            backup.write_bytes(b"BUONO")
            video._salva_video_precedente(out)
            self.assertEqual(backup.read_bytes(), b"BUONO")

    def test_clip_senza_percorso_non_riusa_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            slide = base / "slide.png"
            slide.write_bytes(b"png")
            out = base / "video.mp4"
            clip = SimpleNamespace()
            with (
                mock.patch.object(video, "video_riutilizzabile", return_value=True),
                mock.patch.object(video, "_build_video_moviepy") as moviepy,
                mock.patch.object(video, "segna_video") as segna,
            ):
                video.build_video(
                    [str(slide)], [1.0], clip, out, 5, 1, engine="ffmpeg", reuse=True
                )
            moviepy.assert_called_once()
            segna.assert_not_called()

    def test_temporaneo_vecchio_rimosso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vecchio = base / "video.abc.tmp.mp4"
            vecchio.write_bytes(b"parziale")
            vecchio_padrona = base / "video.recente.tmp.mp4"
            vecchio_padrona.write_bytes(b"fresco")
            antico = time.time() - 48 * 3600
            os.utime(vecchio, (antico, antico))
            video._pulisci_temporanei_vecchi(base / "video.mp4")
            self.assertFalse(vecchio.exists())
            self.assertTrue(vecchio_padrona.exists())

    def test_blocco_non_supportato_non_dice_occupato(self) -> None:
        lock = blocco_run.BloccoRun(Path(tempfile.gettempdir()))
        modulo = blocco_run.msvcrt if blocco_run.msvcrt is not None else blocco_run.fcntl
        metodo = "locking" if blocco_run.msvcrt is not None else "flock"
        with mock.patch.object(modulo, metodo, side_effect=OSError(50, "non supportata")):
            esito = lock.tenta(0)
        self.assertEqual(esito, blocco_run.NON_DISPONIBILE)


class TestAbortCoerente(unittest.TestCase):
    def test_abort_dopo_rendering_non_dice_video_non_generato(self) -> None:
        with (
            mock.patch.object(
                main_mod, "_audio_cambiato_durante_la_run", return_value=True
            ),
            mock.patch.object(main_mod, "_abort_senza_video") as abort,
        ):
            main_mod._verifica_audio_invariato(
                {}, Path("podcast.m4a"), "hash", Path("video.mp4"), video_generato=True
            )
        messaggio = abort.call_args.args[1]
        self.assertNotIn("NON e' stato generato", messaggio)
        self.assertIn("versione precedente", messaggio)


class TestBundlePrivacy(unittest.TestCase):
    @staticmethod
    def _bundle(radice: Path) -> Path:
        with mock.patch.object(diagnostica, "_righe_verifica", return_value=["riga"]):
            return diagnostica.scrivi_bundle(radice)

    def test_report_grezzo_non_finisce_nello_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            radice = Path(tmp)
            cache = radice / "cache"
            cache.mkdir()
            (cache / "sync_report.json").write_text(
                json.dumps(
                    {
                        "audio_duration": 120.5,
                        "correzioni_nomi": ["SEGRETO_PRIVATO"],
                        "sorgenti": {"pdf": "segreto.pdf"},
                        "anchors": {"mentions_off": [{"slide": 2, "preview": "C:/segreti/f.png"}]},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(diagnostica, "CACHE_DIR", cache):
                zip_path = self._bundle(radice)
            with zipfile.ZipFile(zip_path) as archivio:
                testo = "\n".join(
                    archivio.read(n).decode("utf-8", errors="replace")
                    for n in archivio.namelist()
                    if n.endswith(".json")
                )
        self.assertNotIn("SEGRETO_PRIVATO", testo)
        self.assertNotIn("C:/segreti/f.png", testo)
        self.assertNotIn("segreto.pdf", testo)
        self.assertIn("audio_duration", testo)

    def test_cartella_preesistente_non_finisce_nello_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            radice = Path(tmp)
            marca = "20261127-999999"
            pre = radice / f"diagnostica-{marca}"
            pre.mkdir()
            segreto = pre / "personale.txt"
            segreto.write_text("DATI_PRIVATI", encoding="utf-8")
            with mock.patch.object(diagnostica.time, "strftime", return_value=marca):
                zip_path = self._bundle(radice)
            with zipfile.ZipFile(zip_path) as archivio:
                nomi = archivio.namelist()
            self.assertFalse(any("personale.txt" in n for n in nomi))
            self.assertTrue(segreto.exists())


class TestHousekeepingEmbedding(unittest.TestCase):
    def test_stato_check_modelli_sopravvive_allapulizia(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "embedding_model_check.json"
            path.write_text('{"known_models": ["m1"]}', encoding="utf-8")
            pipeline_cache.clean_orphan_cache(set(), cache_dir=base)
            cache_maintenance.clean_cache(base)
            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"known_models": ["m1"]})


class TestRiepilogoCampiSporchi(unittest.TestCase):
    def test_campi_di_forma_sbagliata_non_cancellano_il_riepilogo(self) -> None:
        testo = riepilogo.riepilogo_markdown(
            {
                "segments": [{"slide": 1, "start": 0.0, "end": 10.0}],
                "tempi": ["ocr", "trascrizione"],
                "correzioni_nomi": {"a": 1},
            }
        )
        self.assertIn("| Pagine | 1 |", testo)
        self.assertNotIn("## Tempi della run", testo)


if __name__ == "__main__":
    unittest.main()
