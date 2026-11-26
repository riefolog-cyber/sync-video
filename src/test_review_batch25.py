#!/usr/bin/env python3
"""Regressioni della revisione batch 25.

Ottava revisione: regressioni dei capitoli, report costante, blocco run,
script A/B e robustezza CI.

Esegui con: python -m unittest test_review_batch25 -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

import subtitles

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "src" / "scripts"


class TestCapitoliRegressioni(unittest.TestCase):
    def test_rumore_maiuscolo_non_e_titolo(self) -> None:
        parlato = [
            {"word": "buongiorno", "start": 0.0},
            {"word": "a", "start": 1.0},
            {"word": "tutti", "start": 2.0},
        ]
        for ocr in ("SFFSITE", "IEEARSI DIRLA", "ST", "RRAR"):
            with self.subTest(ocr=ocr):
                capitoli = subtitles.build_capitoli({1: 0.0}, [ocr], words=parlato)
                self.assertNotIn(ocr, capitoli)

    def test_titolo_ufficiale_non_sostituito_se_ripetuto(self) -> None:
        for titolo, parlato in (
            ("NASA", ["nasa", "oggi", "parliamo"]),
            ("BISOGNI PRIMARI", ["bisogni", "primari", "oggi"]),
            ("Prima", ["prima", "parliamo", "oggi"]),
        ):
            with self.subTest(titolo=titolo):
                parole = [
                    {"word": p, "start": float(i)} for i, p in enumerate(parlato)
                ]
                capitoli = subtitles.build_capitoli(
                    {1: 0.0}, [titolo], words=parole
                )
                self.assertIn(titolo.lower(), capitoli.lower())
                self.assertNotIn("oggi", capitoli)

    def test_typo_minuscolo_usato_dal_parlato(self) -> None:
        capitoli = subtitles.build_capitoli(
            {1: 0.0},
            ["La cunosita senza fine"],
            words=[
                {"word": "la", "start": 0.0},
                {"word": "curiosita", "start": 1.0},
                {"word": "senza", "start": 2.0},
                {"word": "fine", "start": 3.0},
            ],
        )
        self.assertIn("curiosita", capitoli)
        self.assertNotIn("cunosita", capitoli)


class TestReportEBlocco(unittest.TestCase):
    def test_report_riscritto_anche_senza_dubbi(self) -> None:
        testo = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("        _save_sync_report(sync_report)\n\n        # Pulizia cache orfana", testo)

    def test_abort_prima_render_marca_report(self) -> None:
        testo = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("report[\"senza_video\"] = True", testo)
        self.assertEqual(testo.count("_abort_senza_video("), 3)  # definizione + 2 usi

    def test_blocco_rilasciato_dal_wrapper(self) -> None:
        testo = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("def _main_impl(", testo)
        self.assertIn("        blocco.release()", testo)

    def test_dry_run_pulisce_anteprime(self) -> None:
        testo = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("_write_boundary_previews(args.output_video, [], CACHE_DIR / \"review\")", testo)


class TestScriptERobustezza(unittest.TestCase):
    def test_worker_embeda_slide_e_blocchi(self) -> None:
        codice = (SCRIPTS / "_embed_candidate_worker.py").read_text(encoding="utf-8")
        self.assertIn("list(data.get(\"slides\") or []) + list(data.get(\"blocks\") or [])", codice)

    def test_confronto_rifiuta_vettori_diversi(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import check_fastembed_upgrade as ab

        with self.assertRaises(ValueError):
            ab.compare(np.ones((3, 2)), np.ones((2, 2)), n_slides=1)

    def test_analysis_sync_video_mancante_esce_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "timeline_finale.json").write_text("[]", encoding="utf-8")
            (base / "slides_finte.json").write_text(
                json.dumps({"slide_texts": ["una slide"]}), encoding="utf-8"
            )
            (base / "transcript_finta.json").write_text(
                json.dumps({"words_raw": [{"word": "ciao", "start": 0.0}]}), encoding="utf-8"
            )
            ambiente = {
                **dict(__import__("os").environ),
                "SYNC_VIDEO_CACHE_DIR": tmp,
            }
            esito = subprocess.run(
                [sys.executable, str(SCRIPTS / "analysis_sync.py"), "--video", str(Path(tmp) / "assente.mp4")],
                capture_output=True,
                text=True,
                env=ambiente,
                cwd=ROOT / "src",
            )
            self.assertEqual(esito.returncode, 2)
            self.assertIn("--video", esito.stderr)

    def test_ci_controlla_ffmpeg_e_tag_unico(self) -> None:
        testo = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("ffmpeg -version", testo)
        self.assertIn("GITHUB_RUN_ATTEMPT", testo)


if __name__ == "__main__":
    unittest.main()
