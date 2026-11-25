#!/usr/bin/env python3
"""Regressioni della revisione batch 22.

Quarta revisione incrociata: segnali di crash GPU, prefetch, storico tempi,
dubbi di `--review` e qualita' dei test stessi.

Esegui con: python -m unittest test_review_batch22 -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
import pipeline_review
import riepilogo
import transcription

ROOT = Path(__file__).resolve().parent.parent


class _Args:
    def __init__(self) -> None:
        self.semantic_model = "modello-principale"
        self.semantic_cache_dir = "cache"
        self.whisper_model = "small"
        self.openvino_model_dir = "cache/ir"
        self.transcriber = "whisper"
        self.dpi = 300
        self.lang = "ita"
        self.fallback_senza_grafica = False
        self.audio = None
        self.no_cache = False


class TestSegnaleCrashGpu(unittest.TestCase):
    def _audio(self, tmp: Path, nome: str) -> Path:
        audio = tmp / nome
        audio.write_bytes(b"audio")
        return audio

    def test_clear_attempt_non_cancella_segnale_di_altro_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = self._audio(base, "a.m4a")
            b = self._audio(base, "b.m4a")
            with mock.patch.object(transcription, "CACHE_DIR", base):
                transcription._write_attempt(a)
                marker = transcription._openvino_attempt_path()
                self.assertTrue(marker.exists())
                with mock.patch.object(
                    transcription, "transcribe_with_whisper", return_value=("t", [])
                ):
                    transcription.transcribe_audio(b, transcriber="whisper")
                self.assertTrue(marker.exists())
                self.assertTrue(transcription._same_audio(transcription._read_attempt(), a))

    def test_ripiego_da_segnale_crash_cancella_il_segnale_a_fine_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            audio = self._audio(base, "podcast.m4a")
            with (
                mock.patch.object(transcription, "CACHE_DIR", base),
                mock.patch.object(transcription, "resolved_transcriber", return_value="openvino"),
                mock.patch.object(transcription, "openvino_model_complete", return_value=True),
                mock.patch.object(
                    transcription, "download_openvino_model", return_value=Path("cache/ir")
                ),
                mock.patch.object(
                    transcription,
                    "decide_transcriber",
                    return_value=("whisper", "crash marker"),
                ),
                mock.patch.object(
                    transcription, "transcribe_with_whisper", return_value=("t", [])
                ),
            ):
                transcription._write_attempt(audio)
                transcription.transcribe_audio(audio, transcriber="auto")
                self.assertFalse(transcription._openvino_attempt_path().exists())

    def test_transcriber_openvino_esplicito_scrive_il_segnale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            audio = self._audio(base, "podcast.m4a")
            visto_durante: list[bool] = []

            def openvino(*_a: object, **_k: object) -> tuple[str, list]:
                visto_durante.append(transcription._openvino_attempt_path().exists())
                return "t", []

            with (
                mock.patch.object(transcription, "CACHE_DIR", base),
                mock.patch.object(transcription, "resolved_transcriber", return_value="openvino"),
                mock.patch.object(transcription, "openvino_model_complete", return_value=True),
                mock.patch.object(transcription, "transcribe_with_openvino", side_effect=openvino),
            ):
                transcription.transcribe_audio(
                    audio, transcriber="openvino", openvino_model_dir=base / "ir"
                )
            self.assertEqual(visto_durante, [True])

    def test_download_openvino_rispetta_model_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ir"
            dest.mkdir()
            (dest / "config.json").write_text("{}", encoding="utf-8")
            (dest / "modello.xml").write_bytes(b"x")
            (dest / "modello.bin").write_bytes(b"y")
            (dest / "openvino_model_id.json").write_text(
                json.dumps({"model_id": "OpenVINO/whisper-tiny-fp16-ov"}), encoding="utf-8"
            )
            with (
                mock.patch.object(transcription, "openvino_model_complete", return_value=True),
                mock.patch("huggingface_hub.snapshot_download") as scarica,
            ):
                transcription.download_openvino_model(dest, "OpenVINO/whisper-large-v3-ov")
            self.assertEqual(scarica.call_count, 1)
            self.assertEqual(scarica.call_args.args[0], "OpenVINO/whisper-large-v3-ov")


class TestPrefetchEStorico(unittest.TestCase):
    def test_fallback_embedding_non_e_fallimento(self) -> None:
        args = _Args()
        with (
            mock.patch("semantic_sync._load_embed_model", side_effect=[None, mock.Mock()]),
            mock.patch.object(main, "whisper_repo", return_value=None),
        ):
            self.assertTrue(main._prefetch_models(args))

    def test_tempo_verifica_nello_storico(self) -> None:
        import pipeline_report

        with tempfile.TemporaryDirectory() as tmp:
            pipeline_report.append_timing_history(
                1.0, 2.0, 3.0, 0.0, 4.0, 10.0, cache_dir=Path(tmp), t_verify=12.3
            )
            righe = (Path(tmp) / "timing_history.jsonl").read_text(encoding="utf-8").splitlines()
            dato = json.loads(righe[-1])
            self.assertAlmostEqual(dato["verify"], 12.3, places=1)

    def test_preview_dichiara_video_non_creato(self) -> None:
        testo = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        # Il riepilogo di --preview passa dry_run=True anche se args.dry_run
        # e' False: il controllo statico evita una pipeline pesante nel test.
        self.assertIn("scrivi_riepilogo(sync_report, Path(args.output_video), dry_run=True)", testo)


class TestReviewVeritiero(unittest.TestCase):
    def _report(self, **anchors: object) -> dict[str, object]:
        return {
            "segments": [
                {"slide": 1, "start": 0.0, "end": 10.0, "duration": 10.0},
                {"slide": 2, "start": 10.0, "end": 20.0, "duration": 10.0},
            ],
            "anchors": anchors,
        }

    def test_trascrizione_assente_non_dice_niente_da_rivedere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "sync_report.json").write_text(
                json.dumps(self._report()), encoding="utf-8"
            )
            with (
                mock.patch("pipeline_review.latest_transcript_words", return_value=[]),
                self.assertLogs("slide2video", level="WARNING") as log,
            ):
                esito = pipeline_review._review_boundary_doubts(Path("video.mp4"), cache_dir=cache)
            testo = "\n".join(log.output)
            self.assertNotIn("niente da rivedere", testo)
            self.assertIn("NON eseguito", testo)
            self.assertEqual(esito, 3)

    def test_ricalcolo_usa_offset_effettivo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "sync_report.json").write_text(
                json.dumps(self._report(effective_offset=1, spoken_offset=0)),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "pipeline_review.latest_transcript_words",
                    return_value=[{"word": "slide", "start": 5.0}],
                ),
                mock.patch(
                    "pipeline_review.announcements_off_timeline", return_value=[]
                ) as controllo,
                self.assertLogs("slide2video", level="INFO"),
            ):
                pipeline_review._review_boundary_doubts(Path("video.mp4"), cache_dir=cache)
            self.assertEqual(controllo.call_args.kwargs["slide_offset"], 1)

    def test_slide_ripetuta_usa_ultima_occorrenza(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            report = {
                "segments": [
                    {"slide": 1, "start": 0.0},
                    {"slide": 2, "start": 10.0},
                    {"slide": 2, "start": 20.0},
                    {"slide": 3, "start": 30.0},
                ],
                # Nessuna chiave: --review deve ricalcolare i dubbi.
                "anchors": {},
            }
            (cache / "sync_report.json").write_text(json.dumps(report), encoding="utf-8")
            visti: list[dict[int, float]] = []

            def controllo(_words: object, starts: dict[int, float], *_a: object, **_k: object) -> list:
                visti.append(dict(starts))
                return []

            with (
                mock.patch(
                    "pipeline_review.latest_transcript_words",
                    return_value=[{"word": "slide", "start": 23.0}],
                ),
                mock.patch("pipeline_review.announcements_off_timeline", side_effect=controllo),
                self.assertLogs("slide2video", level="INFO"),
            ):
                pipeline_review._review_boundary_doubts(Path("video.mp4"), cache_dir=cache)
            self.assertEqual(visti[0][2], 20.0)

    def test_riepilogo_senza_video_ha_la_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            path = riepilogo.scrivi_riepilogo(
                {"segments": [{"slide": 1, "end": 10.0}]}, video, data="25/11/2026"
            )
            self.assertIsNotNone(path)
            assert path is not None
            self.assertIn("Lezione del 25/11/2026", path.read_text(encoding="utf-8"))

    def test_report_ab_scrittura_atomica(self) -> None:
        testo = (ROOT / "src" / "scripts" / "check_fastembed_upgrade.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("os.replace(tmp_report, report)", testo)


if __name__ == "__main__":
    unittest.main()
