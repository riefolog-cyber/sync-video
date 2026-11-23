#!/usr/bin/env python3
"""Regressions mirate della review del core.

Quattro punti, uno per problema: percorso output risolto, sottotitoli scritti
dopo ``--skip-slides``, riparazione guidata dai frame con slide ripetute e
ultimo segmento incluso nel punteggio di allineamento. I test girano con::

    python -B -m unittest test_review_core2 -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from chunks import Word


class _EmbedOneHot:
    """Embedder finto: un asse per tema, come negli altri test di sync."""

    def __init__(self, temi: list[str]) -> None:
        self.temi = temi

    def __call__(self, testi: Any) -> np.ndarray:
        vettori = []
        for testo in testi:
            v = np.zeros(len(self.temi))
            for i, tema in enumerate(self.temi):
                if tema in testo:
                    v[i] = 1.0
            norma = np.linalg.norm(v)
            vettori.append(v / norma if norma else v)
        return np.array(vettori)


class TestPercorsoOutput(unittest.TestCase):
    """config.parse_args risolve args.output in BASE_DIR / args.output.

    Usare il valore grezzo nei punti che scrivono file manda .srt, capitoli e
    timeline_override.json nella cartella da cui e' stato lanciato il comando.
    """

    def test_override_sta_accanto_al_video(self) -> None:
        import main

        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp)
            with mock.patch.object(main, "BASE_DIR", cartella):
                atteso = cartella / "timeline_override.json"
                self.assertEqual(
                    main._percorso_override("", cartella / "video.mp4"), atteso
                )


class TestSottotitoliDopoSkipSlides(unittest.TestCase):
    """I capitoli devono descrivere le slide che il video mostra davvero."""

    def test_scrittura_sulla_sequenza_mostrata(self) -> None:
        """L'helper usa la slide mostrata in ogni posizione, non la posizione.

        Prima della correzione i capitoli venivano scritti prima di
        ``--skip-slides`` e descrivevano slide che nel video non compaiono.
        Qui si verifica il comportamento dell'helper, non la forma del sorgente.
        """
        import main

        slide_ids = [1, 2, 2, 4, 5]  # la 3 e' saltata: la 2 viene mostrata due volte
        slide_texts = ["Prima", "Seconda", "Terza saltata", "Quarta", "Quinta"]
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video.mp4"
            main._scrivi_sottotitoli_mostrati(
                video,
                [],
                [10.0] * len(slide_ids),
                slide_ids,
                slide_texts,
                total_duration=50.0,
            )
            capitoli = (Path(tmp) / "video.capitoli.txt").read_text(encoding="utf-8")

        self.assertNotIn("Terza saltata", capitoli)
        self.assertIn("Seconda", capitoli)
        self.assertIn("0:20 Seconda", capitoli)

    def test_capitoli_escludono_la_slide_saltata(self) -> None:
        from subtitles import build_capitoli

        slide_ids = [1, 2, 2, 4, 5]  # la 3 e' saltata: la 2 viene mostrata due volte
        slide_texts = ["Prima", "Seconda", "Terza saltata", "Quarta", "Quinta"]
        durations = [10.0] * len(slide_ids)
        timeline_mostrata = {
            pos: sum(durations[: pos - 1]) for pos in range(1, len(slide_ids) + 1)
        }
        testi_mostrati = [slide_texts[s - 1] for s in slide_ids]
        capitoli = build_capitoli(timeline_mostrata, testi_mostrati).splitlines()
        self.assertNotIn("Terza saltata", "\n".join(capitoli))
        self.assertEqual(capitoli[2], "0:20 Seconda")


class TestRiparazioneSlideRipetute(unittest.TestCase):
    """Due mismatch sulla stessa slide ma in posizioni diverse = due finestre."""

    def test_finestre_e_evidenze_per_indice_di_segmento(self) -> None:
        from semantic_sync import repair_segments_from_frame_mismatches

        temi = ["alfa", "beta", "gamma", "delta"]
        embed = _EmbedOneHot(temi)
        # La slide 2 compare due volte (posizioni 1 e 3), come con --skip-slides.
        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
            {"slide": 3, "start": 120.0, "end": 180.0},
            {"slide": 2, "start": 180.0, "end": 240.0},
            {"slide": 5, "start": 240.0, "end": 300.0},
        ]

        def tema(t: int) -> str:
            if t < 40:
                return "alfa"
            if t < 100 or 160 <= t < 240:
                return "beta"
            if t < 160:
                return "gamma"
            return "delta"

        words: list[Word] = [{"word": tema(t), "start": float(t)} for t in range(0, 300, 10)]
        slide_texts = [f"{t} slide" for t in ["alfa", "beta", "gamma", "beta", "delta"]]
        mismatches = [
            {"time": 50.0, "shown": 2, "similarity": 1.0},
            {"time": 170.0, "shown": 2, "similarity": 1.0},
        ]

        refined, applied = repair_segments_from_frame_mismatches(
            segments,
            mismatches,
            words,
            slide_texts,
            embed,
            min_segment_seconds=5.0,
            context_seconds=12.0,
            max_shift_seconds=180.0,
        )

        self.assertEqual(len(applied), 2)
        # Primo confine: 60 -> 40, con l'evidenza del mismatch a 50s.
        self.assertEqual(applied[0]["old_start"], 60.0)
        self.assertAlmostEqual(float(applied[0]["new_start"]), 40.0, delta=2.0)
        self.assertEqual(applied[0]["frame_time"], 50.0)
        # Secondo confine: 180 -> 160, con l'evidenza del mismatch a 170s.
        # Con chiavi per numero di slide qui arrivava il frame_time del primo
        # mismatch (50.0) e la finestra veniva applicata all'occorrenza sbagliata.
        self.assertEqual(applied[1]["old_start"], 180.0)
        self.assertAlmostEqual(float(applied[1]["new_start"]), 160.0, delta=2.0)
        self.assertEqual(applied[1]["frame_time"], 170.0)
        self.assertAlmostEqual(float(refined[1]["start"]), 40.0, delta=2.0)
        self.assertAlmostEqual(float(refined[3]["start"]), 160.0, delta=2.0)


class TestUltimaSlideNelPunteggio(unittest.TestCase):
    """L'ultimo segmento non va escluso dal controllo di qualita'."""

    def test_pairs_includono_l_ultimo_blocco(self) -> None:
        import semantic_sync as ss

        temi = ["alfa", "beta", "gamma", "delta"]
        slides = [f"{t} slide" for t in temi]
        words: list[Word] = [
            {"word": t, "start": float(i * 5 + k * 0.5)}
            for i, t in enumerate(temi)
            for k in range(3)
        ]
        blocks = ss.build_semantic_blocks(words, 20.0, 5.0)
        self.assertEqual(len(blocks), 4)

        visti: list[tuple[int, int]] = []

        def cattura(sim: Any, sim_norm: Any, pairs: Any) -> tuple[float, float]:
            visti.extend(pairs)
            return 1.0, 1.0

        with mock.patch.object(ss, "_mean_pair_scores", cattura):
            timeline = ss.semantic_timeline_from_texts(
                slides,
                blocks,
                len(slides),
                20.0,
                _EmbedOneHot(temi),
                options=ss.SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
            )
        self.assertIsNotNone(timeline)
        # range(1, total_slides) lasciava fuori (3, 3): qui c'e' anche l'ultimo.
        self.assertIn((3, 3), visti)


if __name__ == "__main__":
    unittest.main()
