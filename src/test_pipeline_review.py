#!/usr/bin/env python3
"""Regression mirate della revisione dei confini (`--review`).

Il comando rilegge report e trascrizione dalla cache: qui si verifica che
rispetti la chiave EFFETTIVA delle parole salvata dalla run e che distingua
"zero dubbi salvati" da "dato mancante, da ricalcolare".
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_cache import latest_transcript_words, save_sync_report
from pipeline_review import _review_boundary_doubts


def _report(
    *, mentions_off: list[dict[str, object]] | None = None, chiave: str = "transcript_finta"
) -> dict[str, object]:
    anchors: dict[str, object] = {
        "transitions": 1,
        "anchored": 1,
        "unanchored_slides": [2],
        "transcript_cache_key": chiave,
    }
    if mentions_off is not None:
        anchors["mentions_off"] = mentions_off
    return {
        "segments": [
            {"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0, "duration": 60.0},
        ],
        "anchors": anchors,
    }


def _cache(td: Path, report: dict[str, object], *, words: list[dict[str, object]] | None = None) -> Path:
    cache = td / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    save_sync_report(report, cache_dir=cache)
    if words is not None:
        (cache / "transcript_finta.json").write_text(
            json.dumps({"words_raw": words}), encoding="utf-8"
        )
    return cache


def _parole_che_dicono_slide_2() -> list[dict[str, object]]:
    return [
        {"word": "passiamo", "start": 300.0},
        {"word": "alla", "start": 300.4},
        {"word": "slide", "start": 300.7},
        {"word": "2", "start": 301.0},
    ]


class TestReviewMenzioniSalvate(unittest.TestCase):
    def _review(self, cache: Path) -> str:
        with self.assertLogs("slide2video", level="INFO") as logs:
            _review_boundary_doubts(cache / "video_assente.mp4", cache_dir=cache)
        return "\n".join(logs.output)

    def test_lista_vuota_salvata_non_viene_ricalcolata(self) -> None:
        """Un run che ha salvato 0 dubbi va rispettato, non ridetto "salvati 0"."""
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cache = _cache(
                td,
                _report(mentions_off=[]),
                words=_parole_che_dicono_slide_2(),
            )
            out = self._review(cache)
        self.assertIn("niente da rivedere", out)
        self.assertNotIn("dalle parole in cache", out)
        self.assertNotIn("slide 2 viene annunciata", out)

    def test_chiave_assente_ricalcola_e_lo_dice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cache = _cache(td, _report(), words=_parole_che_dicono_slide_2())
            out = self._review(cache)
        self.assertIn("slide 2 viene annunciata", out)
        self.assertIn("dalle parole in cache", out)

    def test_usala_chiave_effettiva_del_report(self) -> None:
        """La revisione chiede le parole alla chiave salvata dalla run."""
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cache = _cache(td, _report(chiave="transcript_acc"))
            with (
                patch("pipeline_review.latest_transcript_words", return_value=[]) as prese,
                self.assertLogs("slide2video", level="INFO"),
            ):
                _review_boundary_doubts(cache / "video_assente.mp4", cache_dir=cache)
        prese.assert_called_once()
        self.assertEqual(prese.call_args.kwargs["preferred_key"], "transcript_acc")


class TestChiaveTrascrizioneEffettiva(unittest.TestCase):
    def test_preferred_key_batte_la_trascrizione_piu_recente(self) -> None:
        """Se la run ha usato il beam accurato, --review deve leggere quello.

        Main salva la chiave effettiva (che puo' essere quella accurata, o
        quella veloce col suffisso _gap): la cache puo' contenere anche il
        testo veloce, scritto dopo, ma non e' quello della timeline.
        """
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "transcript_veloce.json").write_text(
                json.dumps({"words_raw": [{"word": "veloce", "start": 1.0}]}), encoding="utf-8"
            )
            (cache / "transcript_acc.json").write_text(
                json.dumps({"words_raw": [{"word": "accurata", "start": 2.0}]}), encoding="utf-8"
            )
            # La veloce e' piu' recente: senza preferred_key vincerebbe lei.
            os.utime(cache / "transcript_veloce.json", (2_000_000_000, 2_000_000_000))
            parole = latest_transcript_words(cache_dir=cache, preferred_key="transcript_acc")
            self.assertEqual([p["word"] for p in parole], ["accurata"])


class TestReviewPersisteGliArtefatti(unittest.TestCase):
    """`--review` non si limita a stampare: aggiorna report e riepilogo."""

    def test_anteprime_finite_vanno_nel_report_e_nel_riepilogo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cache = _cache(
                td,
                _report(mentions_off=[{"slide": 2, "mention": 300.0, "declared": 60.0}]),
            )
            video = td / "lezione.mp4"
            video.write_bytes(b"video finto")
            anteprima = cache / "review" / "confine.png"
            with (
                patch("main.boundary_review_sheet", return_value=anteprima),
                patch("pipeline_review.scrivi_riepilogo") as riepilogo,
                self.assertLogs("slide2video", level="INFO"),
            ):
                _review_boundary_doubts(video, cache_dir=cache)
            riepilogo.assert_called_once()
            self.assertEqual(riepilogo.call_args.args[1], video)
            dati = json.loads((cache / "sync_report.json").read_text(encoding="utf-8"))
            self.assertEqual(
                dati["anchors"]["mentions_off"][0]["preview"], str(anteprima)
            )

    def test_mentions_off_ricalcolati_vengono_salvati(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            cache = _cache(td, _report(), words=_parole_che_dicono_slide_2())
            with (
                patch("pipeline_review.scrivi_riepilogo") as riepilogo,
                self.assertLogs("slide2video", level="INFO"),
            ):
                _review_boundary_doubts(cache / "video_assente.mp4", cache_dir=cache)
            dati = json.loads((cache / "sync_report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(dati["anchors"]["mentions_off"]), 1)
            riepilogo.assert_called_once()


if __name__ == "__main__":
    unittest.main()
