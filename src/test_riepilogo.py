#!/usr/bin/env python3
"""Il riepilogo per lezione (`riepilogo.md`).

Il referto JSON resta tecnico; qui si verifica che la pagina leggibile dica
le cose giuste: quante pagine, quante ancore, la sezione piu' corta e i
confini da guardare a mano. Non deve mai interrompere la run.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from riepilogo import riepilogo_markdown, scrivi_riepilogo


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

    def test_scrive_il_file_accanto_al_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video_finale.mp4"
            video.write_bytes(b"x")
            path = scrivi_riepilogo(_report(), video)
            assert path is not None
            self.assertEqual(path.name, "riepilogo.md")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
