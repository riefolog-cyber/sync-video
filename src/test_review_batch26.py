#!/usr/bin/env python3
"""Regressioni della revisione batch 26.

Nona revisione: acronimi, review coerente, parziali e wrapper concorrente.

Esegui con: python -m unittest test_review_batch26 -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import main
import pipeline_cache
import pipeline_review
import riepilogo
import semantic_sync
import subtitles
import transcription

ROOT = Path(__file__).resolve().parent.parent


class TestCapitoliAcronimi(unittest.TestCase):
    def test_acronimi_senza_vocale_sopravvive(self) -> None:
        for acronimo in ("PDF", "SQL", "XML", "CSS", "NASA", "FAQ"):
            with self.subTest(acronimo=acronimo):
                self.assertEqual(subtitles._titolo_capitolo(acronimo, 3), acronimo)

    def test_titoli_tecnici_maiuscoli_sopravvivono(self) -> None:
        for titolo in ("SPRING BOOT", "SCRUM", "STRUTTURE", "BISOGNI PRIMARI"):
            with self.subTest(titolo=titolo):
                self.assertEqual(subtitles._titolo_capitolo(titolo, 3), titolo)

    def test_rumore_maiuscolo_non_diventa_titolo(self) -> None:
        self.assertEqual(subtitles._titolo_capitolo("IEEARSI DIRLA", 3), "Slide 3")
        self.assertEqual(subtitles._titolo_capitolo("SFFSITE", 3), "Slide 3")
        self.assertEqual(subtitles._titolo_capitolo("RRAR", 3), "Slide 3")

    def test_timestamp_sporco_non_rompe_capitoli(self) -> None:
        capitoli = subtitles.build_capitoli(
            {1: 0.0}, ["Titolo"], words=[{"word": "ciao", "start": "n/d"}]
        )
        self.assertEqual(capitoli, "0:00 Titolo")

    def test_srt_fallito_non_riporta_file_vecchio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "video.srt").write_text("vecchio", encoding="utf-8")
            with mock.patch.object(subtitles, "atomic_write_text", side_effect=OSError("ro")):
                srt, cap = subtitles.scrivi_sottotitoli(
                    base / "video.mp4", [{"word": "ciao", "start": 0.0}], {1: 0.0}, ["T"]
                )
            self.assertIsNone(srt)
            self.assertIsNone(cap)


class TestReviewCoerente(unittest.TestCase):
    def _cache(self, tmp: Path, report: dict[str, Any], video: Path) -> Path:
        cache = tmp / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        pipeline_cache.save_sync_report(report, cache_dir=cache)
        return cache

    def test_anteprima_morta_non_resta_nel_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "video.mp4"
            video.write_bytes(b"video")
            vecchia = base / ".cache" / "review" / "confine_slide02_00.png"
            vecchia.parent.mkdir(parents=True)
            vecchia.write_bytes(b"png")
            report = {
                "segments": [{"slide": 1, "start": 0.0, "end": 60.0}],
                "anchors": {
                    "mentions_off": [
                        {"slide": 2, "mention": 30.0, "declared": 60.0, "preview": str(vecchia)}
                    ]
                },
            }
            cache = self._cache(base, report, video)
            with (
                mock.patch("main.boundary_review_sheet", return_value=None),
                mock.patch.object(pipeline_review, "scrivi_riepilogo"),
                self.assertLogs("slide2video", level="INFO"),
            ):
                codice = pipeline_review._review_boundary_doubts(video, cache_dir=cache)
            self.assertEqual(codice, 0)
            self.assertFalse(vecchia.exists())
            dati = json.loads((cache / "sync_report.json").read_text(encoding="utf-8"))
            self.assertNotIn("preview", dati["anchors"]["mentions_off"][0])

    def test_video_assente_rimuove_anteprime_e_collegamenti(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vecchia = base / ".cache" / "review" / "confine_slide02_00.png"
            vecchia.parent.mkdir(parents=True)
            vecchia.write_bytes(b"png")
            report = {
                "segments": [{"slide": 1, "start": 0.0, "end": 60.0}],
                "anchors": {
                    "mentions_off": [{"slide": 2, "mention": 30.0, "declared": 60.0, "preview": str(vecchia)}]
                },
            }
            cache = self._cache(base, report, base / "video.mp4")
            with (
                mock.patch.object(pipeline_review, "scrivi_riepilogo"),
                self.assertLogs("slide2video", level="INFO"),
            ):
                pipeline_review._review_boundary_doubts(base / "video.mp4", cache_dir=cache)
            self.assertFalse(vecchia.exists())
            dati = json.loads((cache / "sync_report.json").read_text(encoding="utf-8"))
            self.assertNotIn("preview", dati["anchors"]["mentions_off"][0])

    def test_report_sporco_non_crasha_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "video.mp4"
            video.write_bytes(b"video")
            for rotto in (
                {"segments": [{"slide": "x", "start": 0.0, "end": 1.0}], "anchors": {}},
                {"segments": [{"slide": 1, "start": 0.0}], "anchors": ["x"]},
                {"segments": [{"slide": 1, "start": 0.0, "end": float("inf")}], "anchors": {}},
            ):
                with self.subTest(rotto=rotto):
                    cache = self._cache(base, rotto, video)
                    with self.assertLogs("slide2video", level="WARNING"):
                        codice = pipeline_review._review_boundary_doubts(video, cache_dir=cache)
                    self.assertIn(codice, (1, 3))

    def test_scrittura_report_fallita_non_esce_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "video.mp4"
            video.write_bytes(b"video")
            report = {
                "segments": [{"slide": 1, "start": 0.0, "end": 60.0}],
                "anchors": {"mentions_off": [{"slide": 2, "mention": 30.0, "declared": 60.0}]},
            }
            cache = self._cache(base, report, video)
            with (
                mock.patch.object(pipeline_review, "save_sync_report", side_effect=OSError("ro")),
                mock.patch.object(pipeline_review, "scrivi_riepilogo", return_value=None),
                mock.patch("main.boundary_review_sheet", return_value=None),
                self.assertLogs("slide2video", level="ERROR"),
            ):
                codice = pipeline_review._review_boundary_doubts(video, cache_dir=cache)
            self.assertNotEqual(codice, 0)


class TestRiepilogoEStato(unittest.TestCase):
    def test_solo_riepilogo_preserva_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            video = base / "video.mp4"
            video.write_bytes(b"vecchio video")
            report = {
                "segments": [{"slide": 1, "start": 0.0, "end": 60.0}],
                "senza_video": True,
            }
            pipeline_cache.save_sync_report(report, cache_dir=base)
            self.assertEqual(riepilogo.rigenera(base, video), 0)
            testo = (base / "riepilogo.md").read_text(encoding="utf-8")
            self.assertIn("non creato", testo)

    def test_refine_ultima_decisione_vince(self) -> None:
        semantic_sync.reset_refine_diagnostics()
        try:
            semantic_sync._record_refine(4, 4, decision="vecchia")
            semantic_sync._record_refine(3, 4, decision="nuova")
            voci = semantic_sync.refine_diagnostics()
            self.assertEqual(len(voci), 1)
            self.assertEqual(voci[0]["decision"], "nuova")
        finally:
            semantic_sync.reset_refine_diagnostics()

    def test_wrapper_blocco_e_per_thread(self) -> None:
        # Due main() in thread diversi non devono rilasciarsi il blocco
        # a vicenda: lo stato e' thread-local, non un global condiviso.
        self.assertTrue(hasattr(main, "_STATO_RUN"))
        self.assertFalse(hasattr(main, "_BLOCCO_ATTIVO"))

    def test_parziale_di_altro_audio_scartato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = base / "lezione.m4a"
            b = base / "altra" / "lezione.m4a"
            b.parent.mkdir()
            a.write_bytes(b"audio")
            b.write_bytes(b"audio")
            with (
                mock.patch.object(transcription, "CACHE_DIR", base),
                mock.patch.object(transcription, "_taglia_audio", return_value=base / "cut.flac"),
            ):
                transcription._salva_parziale(
                    a, [{"word": "SOLO_A", "start": 1.0}], coperto_fino=10.0, params=None
                )
                iniziali, _sorgente, _offset = transcription._prepara_ripresa(b, params=None)
            self.assertEqual(iniziali, [])

    def test_parziale_timestamp_sporco_scartato(self) -> None:
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
                    json.dumps({"audio": str(audio), "words": [{"word": "x", "start": "n/d"}], "coperto_fino": 10.0}),
                    encoding="utf-8",
                )
                iniziali, sorgente, offset = transcription._prepara_ripresa(audio, params=None)
            self.assertEqual((iniziali, sorgente, offset), ([], audio, 0.0))


if __name__ == "__main__":
    unittest.main()
