#!/usr/bin/env python3
"""Regressioni della revisione batch 24.

Sesta revisione: capitoli, segnali GPU per audio, riparazione guidata dai
frame, stato tra main() e robustezza della CI.

Esegui con: python -m unittest test_review_batch24 -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

import numpy as np

import semantic_sync
import subtitles
import transcription

ROOT = Path(__file__).resolve().parent.parent


class TestCapitoli(unittest.TestCase):
    def test_typo_minuscolo_viene_correetto_dal_parlato(self) -> None:
        capitoli = subtitles.build_capitoli(
            {1: 0.0},
            ["cunosita'"],
            words=[
                {"word": "la", "start": 0.0},
                {"word": "curiosita", "start": 1.0},
                {"word": "e", "start": 2.0},
                {"word": "pazienza", "start": 3.0},
            ],
        )
        self.assertNotIn("cunosita", capitoli)
        self.assertIn("curiosita", capitoli)

    def test_titolo_tutto_maiuscolo_non_viene_sostituito(self) -> None:
        parlato = [
            {"word": "buongiorno", "start": 0.0},
            {"word": "a", "start": 1.0},
            {"word": "tutti", "start": 2.0},
        ]
        for titolo in ("NASA", "FAQ", "BISOGNI PRIMARI"):
            with self.subTest(titolo=titolo):
                capitoli = subtitles.build_capitoli({1: 0.0}, [titolo], words=parlato)
                self.assertIn(titolo, capitoli)
                self.assertNotIn("buongiorno", capitoli)

    def test_similitudine_typo(self) -> None:
        self.assertTrue(subtitles._simili("cunosita", "curiosita"))
        self.assertTrue(subtitles._a_una_lettera("cunosita", "cunosite"))
        self.assertFalse(subtitles._a_una_lettera("abc", "xyz"))


class TestSegnaleGpu(unittest.TestCase):
    def _audio(self, tmp: Path, nome: str) -> Path:
        p = tmp / nome
        p.write_bytes(nome.encode())
        return p

    def test_secondo_audio_registra_il_proprio_segnale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = self._audio(base, "a.m4a")
            b = self._audio(base, "b.m4a")
            with mock.patch.object(transcription, "CACHE_DIR", base):
                transcription._write_attempt(a)
                transcription._write_attempt(b)
                self.assertTrue(transcription._same_audio(transcription._read_attempt_for(b), b))
                self.assertTrue(transcription._same_audio(transcription._read_attempt(), a))
                transcription._clear_attempt(b)
                self.assertIsNone(transcription._read_attempt_for(b))
                self.assertTrue(transcription._same_audio(transcription._read_attempt(), a))

    def test_eccezione_python_sulla_gpu_cancella_il_segnale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            audio = self._audio(base, "podcast.m4a")
            with (
                mock.patch.object(transcription, "CACHE_DIR", base),
                mock.patch.object(transcription, "resolved_transcriber", return_value="openvino"),
                mock.patch.object(transcription, "openvino_model_complete", return_value=True),
                mock.patch.object(
                    transcription,
                    "transcribe_with_openvino",
                    side_effect=RuntimeError("device not found"),
                ),
                mock.patch.object(
                    transcription, "decide_transcriber", return_value=("openvino", None)
                ),
            ):
                transcription._write_attempt(audio)
                with self.assertRaises(RuntimeError):
                    transcription.transcribe_audio(
                        audio, transcriber="auto", openvino_model_dir=base / "ir"
                    )
                self.assertIsNone(transcription._read_attempt())

    def test_marcatore_id_non_dict_non_crasha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir = Path(tmp) / "ir"
            ir.mkdir()
            (ir / "openvino_model_id.json").write_text("[]", encoding="utf-8")
            self.assertIs(transcription.openvino_model_id_ok(ir, "qualsiasi"), True)
            (ir / "openvino_model_id.json").write_text(
                json.dumps({"model_id": "ALTRO"}), encoding="utf-8"
            )
            self.assertIs(transcription.openvino_model_id_ok(ir, "atteso"), False)


class TestRiparazioneEStato(unittest.TestCase):
    def test_due_occorenze_stessa_slide_restano_distinte(self) -> None:
        semantic_sync.reset_refine_diagnostics()
        try:
            semantic_sync._record_refine(2, 3, decision="A")
            semantic_sync._record_refine(5, 3, decision="B")
            # L'ultima decisione sulla stessa slide sostituisce la precedente.
            voci = semantic_sync.refine_diagnostics()
            self.assertEqual(len(voci), 1)
            self.assertEqual(voci[0]["decision"], "B")
        finally:
            semantic_sync.reset_refine_diagnostics()

    def test_finestra_riparazione_non_supera_il_frame(self) -> None:
        # Due mismatch sullo stesso confine: quello piu' vincolante (55s)
        # deve restare il limite, non il piu' debole (58s).
        segmenti = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        parole = cast("list[Any]", [
            {"word": "prima", "start": 10.0},
            {"word": "cambio", "start": 56.0},
            {"word": "dopo", "start": 70.0},
        ])
        visti: list[dict[int, tuple[float, float]]] = []

        def falso_refine(*_a: object, **kw: object) -> list[dict[str, Any]]:
            visti.append(cast("dict[int, tuple[float, float]]", kw["search_window"]))
            return [
                {"slide": 1, "start": 0.0, "end": 60.0},
                {"slide": 2, "start": 54.0, "end": 120.0},
            ]

        with (
            mock.patch.object(semantic_sync, "refine_segment_boundaries", side_effect=falso_refine),
            mock.patch.object(semantic_sync, "_salva_cache_voce"),
        ):
            semantic_sync.repair_segments_from_frame_mismatches(
                segmenti,
                [{"slide": 2, "time": 55.0, "shown": 2}, {"slide": 2, "time": 58.0, "shown": 2}],
                parole,
                ["slide uno", "slide due"],
                lambda _testi: np.zeros((1, 2)),
                3.0,
            )
        self.assertTrue(visti)
        self.assertLessEqual(visti[0][1][1], 55.0)

    def test_review_rifiuta_report_dry_run(self) -> None:
        import pipeline_review

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "sync_report.json").write_text(
                json.dumps({"senza_video": True, "segments": [{"slide": 1, "start": 0.0}]}),
                encoding="utf-8",
            )
            with self.assertLogs("slide2video", level="ERROR") as log:
                esito = pipeline_review._review_boundary_doubts(
                    Path("video.mp4"), cache_dir=cache
                )
            self.assertEqual(esito, 3)
            self.assertIn("--preview/--dry-run", "\n".join(log.output))

    def test_contatori_tempo_azzerati_tra_main(self) -> None:
        semantic_sync._EMBED_SECONDS = 99.0
        semantic_sync._MODEL_LOAD_SECONDS = 88.0
        semantic_sync.reset_embed_cache()
        self.assertEqual(semantic_sync.embed_seconds(), 0.0)
        self.assertEqual(semantic_sync.model_load_seconds(), 0.0)


class TestCi(unittest.TestCase):
    def test_tesseract_fallback_non_interattivo_e_verificato(self) -> None:
        testo = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("--silent", testo)
        self.assertIn("if ($LASTEXITCODE -ne 0)", testo)
        self.assertNotIn("tesseract --version | Select-Object", testo)


if __name__ == "__main__":
    unittest.main()
