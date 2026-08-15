#!/usr/bin/env python3
"""
Test unitari per `check_fastembed_upgrade` (test A/B fastembed).
Non tocca la rete: usa dati sintetici per le funzioni di confronto e verifica.

Esegui con: python -m unittest test_fastembed_ab -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import check_fastembed_upgrade as ab


class TestCompare(unittest.TestCase):
    def _base(self, n_slides=8, n_blocks=30, dim=16, seed=0):
        rng = np.random.default_rng(seed)
        base = rng.normal(size=(n_slides + n_blocks, dim))
        base /= np.linalg.norm(base, axis=1, keepdims=True)
        return base

    def test_identical_is_equivalent(self):
        base = self._base()
        r = ab.compare(base, base.copy(), n_slides=8)
        self.assertEqual(r["verdict"], "EQUIVALENTE")
        self.assertAlmostEqual(r["cosine_mean"], 1.0, places=3)
        self.assertAlmostEqual(r["decision_match"], 1.0, places=3)

    def test_very_different_is_divergent(self):
        base = self._base(seed=1)
        cand = self._base(seed=2)
        r = ab.compare(base, cand, n_slides=8)
        self.assertEqual(r["verdict"], "DIVERGENTE")

    def test_decision_match_counts_blocks(self):
        base = self._base()
        r = ab.compare(base, base.copy(), n_slides=8)
        self.assertEqual(r["n_blocks"], 30.0)
        self.assertEqual(r["n_slides"], 8.0)
        self.assertIn("argmax_match", r)

    def test_real_decision_more_stable_than_argmax(self):
        # Con la DP monotona la decisione REALE è più robusta dell'argmax grezzo:
        # se un singolo blocco è disturbato l'argmax può ribaltarsi, ma la DP
        # monotona mantiene gli stessi confini di inizio slide.
        n_slides, dim = 4, 8
        # Slide = vettori ortonormali; ogni blocco è una copia quasi perfetta
        # della propria slide (4 blocchi per slide, ordine perfetto).
        rng = np.random.default_rng(7)
        slides = rng.normal(size=(n_slides, dim))
        slides /= np.linalg.norm(slides, axis=1, keepdims=True)
        blocks = np.repeat(slides, 4, axis=0)
        base = np.vstack([slides, blocks])
        base /= np.linalg.norm(base, axis=1, keepdims=True)

        cand = base.copy()
        # Disturbo SOLO il blocco 5 (seconda regione): lo sposto verso la slide 1.
        cand[n_slides + 5] += 2.0 * slides[0]
        cand /= np.linalg.norm(cand, axis=1, keepdims=True)

        r = ab.compare(base, cand, n_slides=n_slides)
        self.assertAlmostEqual(r["decision_match"], 1.0, places=6)
        self.assertLess(r["argmax_match"], 1.0)

    def test_low_decision_match_is_divergent(self):
        base = self._base(n_slides=3, n_blocks=6, dim=8, seed=5)
        # Perturbo solo i blocchi per rompere il matching senza cambiare tutto
        cand = base.copy()
        cand[3:] = np.random.default_rng(9).normal(size=(6, 8))
        cand[3:] /= np.linalg.norm(cand[3:], axis=1, keepdims=True)
        r = ab.compare(base, cand, n_slides=3, decision_match_threshold=0.95)
        self.assertEqual(r["verdict"], "DIVERGENTE")
        self.assertLess(r["decision_match"], 0.95)


class TestCollectRealTexts(unittest.TestCase):
    def _write(self, d: Path, name: str, data: dict):
        (d / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_none_without_cache(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            with mock.patch("check_fastembed_upgrade.CACHE_DIR", p):
                self.assertIsNone(ab.collect_real_texts())

    def test_reads_slides_and_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            self._write(p, "slides_a.json", {"slide_texts": ["S1", "S2", "S3"]})
            words = [
                {"word": "a", "start": 0.0},
                {"word": "b", "start": 0.5},
                {"word": "c", "start": 1.0},
                {"word": "d", "start": 1.5},
            ]
            self._write(p, "transcript_b.json", {"words_raw": words})
            with mock.patch("check_fastembed_upgrade.CACHE_DIR", p):
                texts = ab.collect_real_texts()
        self.assertEqual(len(texts["slides"]), 3)
        self.assertGreater(len(texts["blocks"]), 0)


if __name__ == "__main__":
    unittest.main()
