#!/usr/bin/env python3
"""Regressioni della revisione batch 27.

Decima revisione: rumore maiuscolo, coerenza di `--review`, parziali e verdetto A/B.

Esegui con: python -m unittest test_review_batch27 -v
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

import pipeline_cache
import pipeline_review
import semantic_sync
import subtitles
import transcription

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "src" / "scripts"


class TestTitoliMaiuscoli(unittest.TestCase):
    def test_rumore_tre_lettere_non_diventa_titolo(self) -> None:
        for frammento in ("RRR", "SFF", "MMM"):
            with self.subTest(frammento=frammento):
                self.assertEqual(subtitles._titolo_capitolo(frammento, 7), "Slide 7")

    def test_rumore_multi_parola_non_diventa_titolo(self) -> None:
        self.assertEqual(subtitles._titolo_capitolo("SFFSITE QQQ", 7), "Slide 7")
        self.assertEqual(subtitles._titolo_capitolo("IEEARSI DIRLA", 7), "Slide 7")

    def test_acronimi_e_titoli_tecnici_sopravvivono(self) -> None:
        for titolo in (
            "PDF", "SQL", "XML", "CSS", "AI", "CD", "TV",
            "NASA", "FAQ", "SPRING BOOT", "SCRUM", "STRUTTURE",
        ):
            with self.subTest(titolo=titolo):
                self.assertEqual(subtitles._titolo_capitolo(titolo, 3), titolo)


class TestReviewRobusta(unittest.TestCase):
    def _cache(self, tmp: Path, report: dict[str, Any]) -> Path:
        cache = tmp / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        pipeline_cache.save_sync_report(report, cache_dir=cache)
        return cache

    def test_dubbio_sporco_non_crasha_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "video.mp4"
            video.write_bytes(b"video")
            report: dict[str, Any] = {
                "segments": [{"slide": 1, "start": 0.0, "end": 60.0}],
                "anchors": {
                    "mentions_off": [
                        {
                            "slide": "x",
                            "mention": "n/d",
                            "declared": float("inf"),
                        }
                    ]
                },
            }
            cache = self._cache(base, report)
            with (
                mock.patch("main.boundary_review_sheet", return_value=None),
                mock.patch.object(pipeline_review, "scrivi_riepilogo"),
            ):
                codice = pipeline_review._review_boundary_doubts(video, cache_dir=cache)
            self.assertIn(codice, (0, 3))

    def test_riepilogo_non_salvato_non_esce_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "video.mp4"
            video.write_bytes(b"video")
            report: dict[str, Any] = {
                "segments": [{"slide": 1, "start": 0.0, "end": 60.0}],
                "anchors": {"mentions_off": [{"slide": 2, "mention": 30.0, "declared": 60.0}]},
            }
            cache = self._cache(base, report)
            with (
                mock.patch("main.boundary_review_sheet", return_value=None),
                mock.patch.object(pipeline_review, "scrivi_riepilogo", return_value=None),
                self.assertLogs("slide2video", level="ERROR"),
            ):
                codice = pipeline_review._review_boundary_doubts(video, cache_dir=cache)
            self.assertNotEqual(codice, 0)


class TestStatoEParziali(unittest.TestCase):
    def test_prima_occorrenza_con_indice_uguale_non_persa(self) -> None:
        semantic_sync.reset_refine_diagnostics()
        try:
            semantic_sync._record_refine(2, 2, decision="A")
            semantic_sync._record_refine(3, 2, decision="B")
            self.assertEqual(
                {v["decision"] for v in semantic_sync.refine_diagnostics()}, {"A", "B"}
            )
        finally:
            semantic_sync.reset_refine_diagnostics()

    def test_due_occorenze_stessa_slide_restano_distinte(self) -> None:
        semantic_sync.reset_refine_diagnostics()
        try:
            semantic_sync._record_refine(2, 3, decision="A")
            semantic_sync._record_refine(5, 3, decision="B")
            voci = semantic_sync.refine_diagnostics()
            self.assertEqual([v["indice"] for v in voci], [2, 5])
            self.assertEqual([v["decision"] for v in voci], ["A", "B"])
        finally:
            semantic_sync.reset_refine_diagnostics()

    def test_guidato_sostituisce_solo_la_vecchia_decisione_per_numero(self) -> None:
        semantic_sync.reset_refine_diagnostics()
        try:
            semantic_sync._record_refine(4, 4, decision="vecchia")
            semantic_sync._record_refine(3, 4, decision="guidata")
            voci = semantic_sync.refine_diagnostics()
            self.assertEqual(len(voci), 1)
            self.assertEqual(voci[0]["decision"], "guidata")
        finally:
            semantic_sync.reset_refine_diagnostics()

    def test_parziale_coperto_nan_scartato(self) -> None:
        transcription.azzera_ripresa()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            audio = base / "podcast.m4a"
            audio.write_bytes(b"audio")
            with (
                mock.patch.object(transcription, "CACHE_DIR", base),
                mock.patch.object(transcription, "_taglia_audio", return_value=base / "cut.flac"),
            ):
                percorso = transcription.percorso_parziale(audio)
                percorso.parent.mkdir(parents=True, exist_ok=True)
                percorso.write_text(
                    json.dumps(
                        {
                            "audio": str(audio),
                            "words": [{"word": "x", "start": 1.0}],
                            "coperto_fino": "NaN",
                        }
                    ),
                    encoding="utf-8",
                )
                iniziali, sorgente, offset = transcription._prepara_ripresa(audio, params=None)
            self.assertEqual((iniziali, sorgente, offset), ([], audio, 0.0))
            self.assertEqual(transcription.ultima_ripresa(), 0.0)


class TestVerdettoAB(unittest.TestCase):
    def test_forme_diverse_scrive_verdetto_divergente(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import check_fastembed_upgrade as ab

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            report = base / "fastembed_ab.json"
            testi = {"slides": ["s1", "s2"], "blocks": ["b1", "b2"]}
            with (
                mock.patch.object(ab, "WORKER", mock.Mock(exists=mock.Mock(return_value=True))),
                mock.patch.object(ab, "_candidate_version_from_pypi", return_value="9.9.9"),
                mock.patch.object(ab, "collect_real_texts", return_value=testi),
                mock.patch.object(ab, "baseline_embeddings", return_value=np.ones((4, 2))),
                mock.patch.object(ab, "candidate_embeddings", return_value=(np.ones((3, 2)), None)),
                mock.patch.object(ab, "_embedding_cache_dir", return_value=base / "emb"),
                contextlib.redirect_stdout(io.StringIO()) as out,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                codice = ab.main(
                    ["--candidate", "9.9.9", "--report", str(report), "--cache-dir", str(base)]
                )
            self.assertEqual(codice, 1)
            self.assertIn("VERDETTO: DIVERGENTE", out.getvalue())
            dati = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(dati["verdict"], "DIVERGENTE")
            self.assertEqual(dati["candidate"], "9.9.9")
            self.assertIn("ts", dati)


if __name__ == "__main__":
    unittest.main()
