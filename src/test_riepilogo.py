#!/usr/bin/env python3
"""Il riepilogo per lezione (`riepilogo.md`).

Il referto JSON resta tecnico; qui si verifica che la pagina leggibile dica le
cose giuste: quante pagine, quante ancore, la sezione piu' corta, da dove
vengono slide e voce, e cosa guardare a mano. Non deve MAI interrompere la run,
nemmeno con un report malformato.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from riepilogo import riepilogo_markdown, rigenera, scrivi_riepilogo


def _report(**extra: object) -> dict[str, object]:
    report: dict[str, object] = {
        "versione": "1.2.3",
        "segments": [
            {"slide": 1, "start": 0.0, "end": 120.0, "duration": 120.0},
            {"slide": 2, "start": 120.0, "end": 150.0, "duration": 30.0},
        ],
        "anchors": {
            "transitions": 1,
            "anchored": 1,
            "unanchored_slides": [],
            "mentions_off": [],
        },
        "notes": {"tempi": {"ocr": 8, "trascrizione": 207, "sincronizzazione": 3}},
    }
    report.update(extra)
    return report


class TestRiepilogo(unittest.TestCase):
    def test_dice_pagine_ancore_e_pagina_piu_corta(self) -> None:
        testo = riepilogo_markdown(_report(), "video_finale.mp4")
        self.assertIn("| Pagine | 2 |", testo)
        self.assertIn("| Ancore usate | 1 su 1 |", testo)
        self.assertIn("| Pagina più breve | 2 (30s) |", testo)
        self.assertIn("video_finale.mp4", testo)

    def test_senza_dubbi_lo_dice(self) -> None:
        testo = riepilogo_markdown(_report())
        self.assertIn("Niente: tutti i confini sono quelli annunciati a voce.", testo)

    def test_i_dubbi_sono_elencati_con_i_due_tempi(self) -> None:
        report = _report(
            anchors={
                "transitions": 1,
                "anchored": 1,
                "unanchored_slides": [],
                "mentions_off": [{"slide": 2, "mention": 60.0, "declared": 75.0}],
            }
        )
        testo = riepilogo_markdown(report)
        self.assertIn("slide 2: annunciata a 1m00s ma mostrata da 1m15s", testo)
        self.assertIn("in ritardo", testo)

    def test_una_sola_pagina_non_parla_di_confini(self) -> None:
        # Con una pagina sola non ci sono confini: dire "stimati dal contenuto"
        # sarebbe una diagnosi inventata.
        report = _report(
            segments=[{"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0}],
            anchors={"transitions": 0, "anchored": 0, "unanchored_slides": []},
        )
        testo = riepilogo_markdown(report)
        self.assertIn("Una sola pagina", testo)
        self.assertNotIn("stimati dal contenuto", testo)

    def test_dice_da_dove_vengono_slide_e_voce(self) -> None:
        testo = riepilogo_markdown(
            _report(sorgenti={"pdf": "lezione 3.pdf", "audio": "voce.m4a"})
        )
        self.assertIn("slide da `lezione 3.pdf`", testo)
        self.assertIn("voce da `voce.m4a`", testo)

    def test_un_report_malformato_non_esplode(self) -> None:
        # La promessa e' "mai fatale": dopo il video, un traceback qui
        # rovinerebbe una run gia' riuscita.
        rotto = {
            "segments": [{"slide": "x", "start": "a", "end": "b", "duration": "c"}],
            "anchors": ["non", "un", "dict"],
            "notes": "nemmeno",
        }
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video_finale.mp4"
            video.write_bytes(b"x")
            self.assertIsNone(scrivi_riepilogo(rotto, video))

    def test_scrive_il_file_accanto_al_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video_finale.mp4"
            video.write_bytes(b"x")
            path = scrivi_riepilogo(_report(), video)
            self.assertIsNotNone(path)
            assert path is not None  # per il type-checker
            self.assertEqual(path.name, "riepilogo.md")
            self.assertTrue(path.exists())
            self.assertIn("Lezione del", path.read_text(encoding="utf-8"))


class TestRigenera(unittest.TestCase):
    """`--solo-riepilogo`: la pagina dal report, senza pipeline."""

    def test_riscrive_dal_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "sync_report.json").write_text(
                json.dumps(_report()), encoding="utf-8"
            )
            video = base / "video_finale.mp4"
            video.write_bytes(b"x")
            self.assertEqual(rigenera(base, video), 0)
            self.assertTrue((base / "riepilogo.md").exists())

    def test_senza_report_esce_con_uno(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(rigenera(base, base / "video_finale.mp4"), 1)


if __name__ == "__main__":
    unittest.main()
