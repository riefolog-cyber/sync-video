#!/usr/bin/env python3
"""Regressioni della revisione batch 19 (cache, CLI, video, timeline, diagnostica).

Ogni test riproduce un caso in cui il programma diceva una cosa e ne faceva
un'altra: cache viva cancellata, video riusato definito "dry-run", frame non
estratti presentati come slide 1, batch/encoder verificati a se' stessi.

Esegui con: python -m unittest test_review_batch19 -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

import cache_maintenance
import config
import controlli
import diagnostica
import hardware
import machine_setup
import pipeline_report
import pipeline_review
import semantic_sync
import timeline
import transcription
import updates
import verifica_pc
import video
from boundary_snap import aggancia_confini


class _Args:
    """Namespace minimo per le funzioni che leggono gli argomenti."""

    def __init__(self, **valori: object) -> None:
        self.transcriber = "auto"
        self.whisper_device = "cpu"
        self.whisper_compute_type = "int8"
        self.openvino_device = "GPU"
        self.openvino_model_dir = "x"
        self.video_encoder = "auto"
        self.whisper_model = "small"
        self.lang = "ita"
        self.dpi = 300
        self.semantic_model = "intfloat/multilingual-e5-large"
        self.semantic_window = 4.0
        self.semantic_min_duration = 3.0
        self.semantic_temperature = 0.0
        for chiave, valore in valori.items():
            setattr(self, chiave, valore)


class TestCacheNonUccideLaRun(unittest.TestCase):
    def test_marker_gpu_non_scrivibile_non_blocca_la_trascrizione(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            with (
                mock.patch.object(transcription, "CACHE_DIR", Path(tmp)),
                mock.patch.object(transcription, "atomic_write_text", side_effect=OSError("disco pieno")),
            ):
                # La funzione e' best-effort: se la cache non collabora non deve
                # sollevare, altrimenti la run moriva prima di provare OpenVINO.
                transcription._write_attempt(audio)

    def test_cache_aggiornamenti_non_scrivibile_non_blocca(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(updates, "CACHE_DIR", Path(tmp)),
                mock.patch.object(updates, "UPDATES_CACHE", Path(tmp) / "updates_check.json"),
                mock.patch.object(
                    updates, "atomic_write_text", side_effect=OSError("readonly")
                ) as scrittura,
                # Con PyPI irraggiungibile `check_updates` torna prima di
                # scrivere: il test non esercitava affatto la correzione.
                mock.patch.object(updates, "_latest_version_pypi", return_value="9.9.9"),
                mock.patch.object(updates, "_installed_version", return_value="1.0.0"),
            ):
                esito = updates.check_updates()
            self.assertIsInstance(esito, list)
            self.assertGreater(scrittura.call_count, 0, "la scrittura della cache non e' stata provata")

    def test_cache_scaduta_non_riporta_versione_gia_installata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "updates_check.json"
            cache.write_text(
                json.dumps(
                    {
                        "ts": time.time() - 100 * 3600,
                        "outdated": [
                            {
                                "name": "numpy",
                                "installed": "1.0.0",
                                "latest": "9.9.9",
                                "pinned": False,
                                "major": True,
                                "note": "",
                                "blocked": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(updates, "CACHE_DIR", Path(tmp)),
                mock.patch.object(updates, "UPDATES_CACHE", cache),
                mock.patch.object(updates, "_latest_version_pypi", return_value=None),
                mock.patch.object(updates, "_installed_version", return_value="9.9.9"),
            ):
                esito = updates.check_updates()
            self.assertEqual(esito, [], "non si propone un aggiornamento gia' installato")

    def test_conteggio_pulizia_non_dichiara_byte_non_rimossi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp)
            modello = cartella / "embedding_cache"
            modello.mkdir()
            (modello / "weights.bin").write_bytes(b"x" * 1000)
            with mock.patch.object(cache_maintenance.shutil, "rmtree"):
                esito = cache_maintenance.clean_cache(cartella, include_models=True)
            self.assertEqual(esito["model_bytes"], 0)
            self.assertTrue(modello.exists())
            self.assertNotIn("modelli rimossi", cache_maintenance.descrizione_pulizia_cache(esito))


class TestFlagCli(unittest.TestCase):
    def test_device_con_refuso_esce_subito(self) -> None:
        # Un valore che non e' nemmeno lontanamente un device deve morire
        # qui (exit 2), non dopo OCR e download dei modelli.
        for flag, valore in (("--whisper-device", "cpuu"), ("--openvino-device", "gpuu")):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as esito:
                    config.build_parser().parse_args([flag, valore])
                self.assertEqual(esito.exception.code, 2)

    def test_device_case_insensitive_e_normalizzato(self) -> None:
        # 'CUDA'/gpu scritti con la case sbagliata non devono piu' arrivare
        # a CTranslate2/OpenVINO: la normalizzazione e' la stessa per CLI e .env.
        args = config.build_parser().parse_args(
            ["--whisper-device", "CUDA", "--openvino-device", "gpu"]
        )
        self.assertEqual(args.whisper_device, "cuda")
        self.assertEqual(args.openvino_device, "GPU")

    def test_handler_log_file_non_sopravvive_a_seconda_main(self) -> None:
        self.addCleanup(config._rimuovi_handler_log_file)
        with tempfile.TemporaryDirectory() as tmp:
            primo = Path(tmp) / "primo.log"
            secondo = Path(tmp) / "secondo.log"
            config.parse_args(["--log-file", str(primo)])
            config.parse_args(["--log-file", str(secondo)])
            config.log.info("RIGA_UNICA_DI_PROVA")
            for handler in config.log.handlers + config.logging.getLogger().handlers:
                if getattr(handler, "_slide2video_log_file", False):
                    handler.flush()
            testo_primo = primo.read_text(encoding="utf-8")
            testo_secondo = secondo.read_text(encoding="utf-8")
            self.assertNotIn("RIGA_UNICA_DI_PROVA", testo_primo)
            self.assertIn("RIGA_UNICA_DI_PROVA", testo_secondo)
            # Su Windows un FileHandler ancora aperto blocca la cancellazione
            # della cartella temporanea: chiudere qui, non in addCleanup.
            config._rimuovi_handler_log_file()

    def test_encoder_auto_non_dice_gpu(self) -> None:
        args = _Args()
        with (
            mock.patch.dict(os.environ, {"VIDEO_ENCODER": "auto"}),
            mock.patch.object(machine_setup, "ffmpeg_encoders", return_value=frozenset({"libx264"})),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertLogs(machine_setup.log, level="INFO") as log,
        ):
            machine_setup._apply_video_encoder(args, [])
        self.assertFalse(any("GPU accelerata" in riga for riga in log.output))
        self.assertEqual(args.video_encoder, "auto")


class TestVeritaDiagnostica(unittest.TestCase):
    def test_videotoolbox_e_usabile_su_mac(self) -> None:
        relazione = verifica_pc.Relazione()
        with (
            mock.patch.object(verifica_pc.sys, "platform", "darwin"),
            mock.patch.object(verifica_pc.shutil, "which", return_value="/usr/bin/ffmpeg"),
            mock.patch.object(
                verifica_pc.subprocess,
                "run",
                return_value=mock.Mock(stdout=" V....D h264_videotoolbox\n", returncode=0),
            ),
            mock.patch("machine_setup.detect_gpus", return_value=["Apple M2"]),
            mock.patch.object(machine_setup.sys, "platform", "darwin"),
        ):
            verifica_pc.prova_ffmpeg(relazione)
        esito = relazione.esiti[0]
        self.assertEqual(esito.stato, verifica_pc.OK)
        self.assertIn("h264_videotoolbox", esito.dettaglio)

    def test_riepilogo_video_riusato_non_e_dry_run(self) -> None:
        with self.assertLogs(pipeline_report.log, level="INFO") as log:
            pipeline_report.print_timing(
                1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 6.0,
                video_encoder=None,
                video_presente=True,
                history_fn=lambda *_a, **_k: None,
            )
        testo = "\n".join(log.output)
        self.assertNotIn("dry-run", testo)
        self.assertIn("riusato", testo)

    def test_riepilogo_dry_run_resta_dry_run(self) -> None:
        with self.assertLogs(pipeline_report.log, level="INFO") as log:
            pipeline_report.print_timing(
                1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 6.0,
                video_encoder=None,
                video_presente=False,
                history_fn=lambda *_a, **_k: None,
            )
        self.assertIn("non generato (dry-run)", "\n".join(log.output))

    def test_cache_ts_non_numerico_non_rompe_avvio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "hardware.json"
            cache.write_text(
                json.dumps(
                    {
                        "fingerprint": hardware._cache_fingerprint(),
                        "ts": "ieri",
                        "encoders": ["libx264"],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(hardware, "HARDWARE_CACHE", cache):
                self.assertIsNone(hardware._read_encoder_cache())

    def test_bundle_diagnostica_legge_cache_rediretta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            radice = Path(tmp)
            cache = radice / "altra_cache"
            cache.mkdir()
            (cache / "sync_report.json").write_text('{"foo": 1}', encoding="utf-8")
            (cache / "transcript_x.json").write_text(
                json.dumps({"words_raw": [{"word": "ciao"}]}), encoding="utf-8"
            )
            with mock.patch.object(diagnostica, "CACHE_DIR", cache):
                stato = diagnostica._stato_run(radice)
            self.assertEqual(stato["report"], {"foo": 1})
            self.assertEqual(cast_dizione(stato["trascrizione"])["file"], "transcript_x.json")

    def test_debug_hardware_richiede_mypy(self) -> None:
        with (
            mock.patch("importlib.util.find_spec", return_value=None),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            codice = controlli.main(["debug hardware"])
        self.assertEqual(codice, 2)
        self.assertIn("requirements-dev.txt", output.getvalue())


class TestVideoEReview(unittest.TestCase):
    def test_moviepy_fallito_ripristina_video_precedente(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "video_finale.mp4"
            output.write_bytes(b"video-buono")
            # Il backup DEVE esistere: senza di esso il test passava anche se
            # `_ripristina_video_precedente` non venisse mai chiamato.
            video._salva_video_precedente(output)

            def scrivi_parziale(percorso: str, **_kw: object) -> None:
                Path(percorso).write_bytes(b"parziale")
                raise RuntimeError("encoding interrotto")

            class _AudioFinto:
                def __init__(self, *_a: object) -> None:
                    pass

                def close(self) -> None:
                    pass

            clip = mock.Mock()
            clip.write_videofile.side_effect = scrivi_parziale
            clip.with_audio.return_value = clip
            with (
                mock.patch.object(video, "_resize_for_video", return_value=np.zeros((4, 4))),
                mock.patch.object(video, "ImageClip", return_value=clip),
                mock.patch.object(video, "AudioFileClip", _AudioFinto),
                mock.patch.object(video, "concatenate_videoclips", return_value=clip),
                self.assertRaises(RuntimeError),
            ):
                video._build_video_moviepy(["s.png"], [1.0], "a.wav", output, 5, 1, 0.0)
            self.assertEqual(output.read_bytes(), b"video-buono")
            self.assertEqual(list(Path(tmp).glob("*.tmp.mp4")), [])

    def test_backup_ripristina_output_sparito(self) -> None:
        """Il ripristino vero si esercita solo con il file finale mancante."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "video_finale.mp4"
            output.write_bytes(b"video-buono")
            video._salva_video_precedente(output)
            output.unlink()
            video._ripristina_video_precedente(output)
            self.assertEqual(output.read_bytes(), b"video-buono")

    def test_anteprime_stali_vengono_rimosse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp) / "review"
            cartella.mkdir()
            stala = cartella / "confine_slide99_99.png"
            stala.write_bytes(b"vecchia")

            def foglio(_video: object, _momenti: object, destinazione: Path) -> Path:
                destinazione.parent.mkdir(parents=True, exist_ok=True)
                destinazione.write_bytes(b"nuova")
                return destinazione

            with mock.patch("main.boundary_review_sheet", side_effect=foglio):
                pipeline_review._write_boundary_previews(
                    "video.mp4", [{"slide": 1, "mention": 1.0, "declared": 2.0}], cartella
                )
            self.assertFalse(stala.exists())
            self.assertTrue((cartella / "confine_slide01_00.png").exists())

    def test_slide_illeggibile_non_usata_non_rende_incompleto(self) -> None:
        def letterbox(
            _slide_files: object, _dest: object, unreadable_out: list[int] | None = None
        ) -> list[Path]:
            if unreadable_out is not None:
                unreadable_out.append(1)
            return [Path("ref.png")]

        def estrae(_video: object, _tempi: object, percorsi: list[Path]) -> bool:
            for percorso in percorsi:
                percorso.write_bytes(b"png")
            return True

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(video, "_letterbox_references", side_effect=letterbox),
            mock.patch.object(video, "_reference_matrix", return_value=np.ones((1, 8))),
            mock.patch.object(video, "_extract_frames_batch", side_effect=estrae),
            mock.patch.object(video, "_similarities", return_value=np.array([1.0])),
        ):
            esito = video.frame_consistency_check(
                "video.mp4", [(1, 0.0, 1.0)], ["s1.png", "s2.png"], Path(tmp)
            )
        self.assertEqual(esito["unreadable"], [2])
        self.assertEqual(esito["skipped"], [])
        self.assertFalse(esito["incomplete"])


class TestTimelineESemantica(unittest.TestCase):
    def test_aggancio_non_accorcia_ultima_slide(self) -> None:
        parole = [
            {"word": "a", "start": 0.0},
            {"word": "b", "start": 10.0},
            {"word": "c", "start": 20.45},
            {"word": "d", "start": 22.0},
        ]
        nuove, _mosse = aggancia_confini(
            {1: 0.0, 2: 10.0, 3: 20.0}, {}, parole, min_secondi=5.0, total_duration=25.0
        )
        self.assertGreaterEqual(25.0 - nuove[3], 5.0 - 1e-9)

    def test_mentioni_esplicite_non_vengono_duplicate(self) -> None:
        kinds: dict[int, list[tuple[float, bool]]] = {
            2: [(10.0, False)],
            3: [(20.0, False)],
        }
        risultato = timeline._risolvi_mentioni_implicite(kinds, [(15.0, False)], 5)
        # Asserire solo unicita'/ordine passava anche con un'implementazione
        # che non assegnava affatto la menzione implicita.
        self.assertEqual(
            risultato,
            {2: [(10.0, False)], 3: [(15.0, False), (20.0, False)]},
        )

    def test_riuso_parziale_porta_il_costo_dei_vettori_riusati(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(semantic_sync, "_EMBED_CACHE_DIR", Path(tmp)),
                mock.patch.object(semantic_sync, "CACHE_DIR", Path(tmp)),
                mock.patch.object(semantic_sync, "_EMBED_MEMO", {}),
            ):
                semantic_sync._salva_cache_voce("modello|b16", ["a", "b", "c"], np.ones((3, 4)), 25.0)
                costo = semantic_sync._costo_testi_riusati(
                    "modello|b16", [0, 1], ["a", "b", "c"]
                )
            self.assertGreater(costo, 0.0)


class TestScriptDiagnostici(unittest.TestCase):
    def test_baseline_assente_non_e_aggiornamento(self) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
        import check_embedding_models as cm

        dati = {
            "tracked": {
                cm.PREFERRED_MODEL: {"lastModified": "2026-04-02T00:00:00+00:00"},
                "intfloat/multilingual-e5-base": {"lastModified": "2026-04-02T00:00:00+00:00"},
            },
            "new_candidates": {},
        }
        precedente = {"tracked": {cm.PREFERRED_MODEL: {"lastModified": None}}}
        azione, _motivi = cm._recommendation(precedente, dati)  # type: ignore[arg-type]
        self.assertFalse(azione)


def cast_dizione(valore: object) -> dict[str, Any]:
    """Restringe il tipo per mypy senza mascherare errori veri."""
    assert isinstance(valore, dict)
    return {str(k): v for k, v in valore.items()}


if __name__ == "__main__":
    unittest.main()
