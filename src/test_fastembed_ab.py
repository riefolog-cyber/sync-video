#!/usr/bin/env python3
"""
Test unitari per `check_fastembed_upgrade` (test A/B fastembed).
Non tocca la rete: usa dati sintetici per le funzioni di confronto e verifica.

Esegui con: python -m unittest test_fastembed_ab -v
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

# Il modulo e' in scripts/ (non e' un pacchetto: si esegue come script), quindi
# la sua cartella va aggiunta al sys.path per poterlo importare da qui.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

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

    def test_both_alignments_failed_is_divergent(self):
        # -1 == -1 non e' una prova: senza allineamento valido il verdetto
        # deve essere DIVERGENTE anche se i vettori sono identici e le
        # soglie sono a zero.
        base = self._base()
        with mock.patch("semantic_sync.monotonic_alignment", return_value=None):
            r = ab.compare(base, base.copy(), n_slides=8, sim_mean_threshold=0.0,
                           decision_match_threshold=0.0)
        self.assertEqual(r["verdict"], "DIVERGENTE")
        self.assertEqual(r["decision_match"], 0.0)
        self.assertFalse(r["alignment_valid"])
        self.assertAlmostEqual(r["cosine_mean"], 1.0, places=3)

    def test_one_alignment_failed_is_divergent(self):
        base = self._base(n_slides=4, n_blocks=10, dim=8, seed=11)
        with mock.patch("semantic_sync.monotonic_alignment", side_effect=[None, np.arange(4)]):
            r = ab.compare(base, base.copy(), n_slides=4, sim_mean_threshold=0.0,
                           decision_match_threshold=0.0)
        self.assertEqual(r["verdict"], "DIVERGENTE")
        self.assertEqual(r["decision_match"], 0.0)
        self.assertFalse(r["alignment_valid"])


class TestCollectRealTexts(unittest.TestCase):
    def _write(self, d: Path, name: str, data: dict, mtime: float | None = None):
        path = d / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    @staticmethod
    def _words(word: str) -> list[dict]:
        # Servono più parole: con due sole build_semantic_blocks non produce blocchi.
        return [
            {"word": word, "start": 0.0},
            {"word": "un", "start": 0.5},
            {"word": "po", "start": 1.0},
            {"word": "fine", "start": 1.5},
        ]

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

    def test_pairs_files_with_same_run_token(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            self._write(p, "slides_20260402_153012_300_ita_v3.json", {"slide_texts": ["S1", "S2"]})
            # La trascrizione della stessa run e' PIU' VECCHIA di un'altra:
            # il token, non la data di modifica, deve decidere l'accoppiamento.
            self._write(p, "transcript_20260401_100000_small.json",
                        {"words_raw": self._words("vecchio")}, mtime=9000)
            self._write(p, "transcript_20260402_153012_small.json",
                        {"words_raw": self._words("nuovo")}, mtime=8000)
            texts = ab.collect_real_texts(cache_dir=p)
        self.assertIsNotNone(texts)
        self.assertIn("nuovo", " ".join(texts["blocks"]))
        self.assertNotIn("vecchio", " ".join(texts["blocks"]))

    def test_mismatched_run_tokens_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            self._write(p, "slides_20260402_153012_300_ita_v3.json", {"slide_texts": ["S1"]})
            self._write(p, "transcript_20260403_153012_small.json", {"words_raw": self._words("x")})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                texts = ab.collect_real_texts(cache_dir=p)
        self.assertIsNone(texts)
        self.assertIn("run diverse", err.getvalue())

    def test_run_id_mismatch_rejected_with_explicit_paths(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            sl = p / "slides_a.json"
            tr = p / "transcript_b.json"
            self._write(p, sl.name, {"run_id": "AAA", "slide_texts": ["S1"]})
            self._write(p, tr.name, {"run_id": "BBB", "words_raw": self._words("x")})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                texts = ab.collect_real_texts(slide_cache=sl, transcript_cache=tr)
        self.assertIsNone(texts)
        self.assertIn("run diverse", err.getvalue())

    def test_explicit_cache_dir_overrides_global(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            self._write(p, "slides_a.json", {"slide_texts": ["S1", "S2"]})
            self._write(p, "transcript_b.json", {"words_raw": self._words("x")})
            with mock.patch.object(ab, "CACHE_DIR", Path(td) / "inesistente"):
                texts = ab.collect_real_texts(cache_dir=p)
        self.assertIsNotNone(texts)
        self.assertEqual(texts["slides"], ["S1", "S2"])


class TestBatchParity(unittest.TestCase):
    def test_worker_riceve_batch_size(self):
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(ab, "setup_candidate_venv", return_value=Path("python")),
            mock.patch.object(ab.subprocess, "check_call") as call,
            mock.patch.object(ab.np, "load", return_value=np.zeros((2, 3), dtype=np.float32)),
        ):
            emb, venv = ab.candidate_embeddings(
                ["a", "b"], "0.7.4", venv_dir=Path(td), batch_size=17
            )
        args = call.call_args.args[0]
        self.assertEqual(args[1], str(ab.WORKER))
        self.assertEqual(args[-1], "17")
        self.assertIsNone(venv)
        self.assertEqual(emb.shape, (2, 3))

    def test_worker_usa_batch_da_argv(self):
        import _embed_candidate_worker as worker

        with tempfile.TemporaryDirectory() as td:
            texts = Path(td) / "texts.json"
            texts.write_text(json.dumps({"texts": ["a"]}), encoding="utf-8")
            fake_mod = types.SimpleNamespace(TextEmbedding=mock.Mock(return_value=mock.Mock()))
            with mock.patch.dict(sys.modules, {"fastembed": fake_mod}), \
                 mock.patch.object(worker, "_normalized_embed", return_value=np.zeros((1, 2))) as norm, \
                 mock.patch.object(worker.np, "save"), \
                 mock.patch.object(sys, "argv",
                                   ["worker", str(texts), "m", "c", str(Path(td) / "emb.npy"), "11"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = worker.main()
        self.assertEqual(rc, 0)
        self.assertEqual(norm.call_args.kwargs["batch_size"], 11)

    def test_baseline_cache_dir_esplicito(self):
        with mock.patch("embedder.load_embedder", return_value=None) as load:
            self.assertIsNone(ab.baseline_embeddings(["a"], cache_dir="/tmp/emb"))
        self.assertEqual(load.call_args.args[1], "/tmp/emb")


class TestCandidateVersion(unittest.TestCase):
    def test_riutilizza_regola_di_updates(self):
        import updates

        data = {"releases": {"0.7.4": [{"yanked": False}]}}
        with mock.patch.object(updates, "_fetch_pypi_json", return_value=data) as fetch, \
             mock.patch.object(updates, "_latest_compatible", return_value="0.7.4") as latest:
            version = ab._candidate_version_from_pypi()
        self.assertEqual(version, "0.7.4")
        fetch.assert_called_once_with("fastembed")
        latest.assert_called_once_with(data)

    def test_fallback_esclude_prerelease_e_python_incompatibile(self):
        data = {"releases": {
            "0.8.0rc1": [{"yanked": False}],
            "0.7.5": [{"yanked": False, "requires_python": ">=99"}],
            "0.7.4": [{"yanked": False, "requires_python": ">=3.8"}],
            "0.7.3": [{"yanked": True}],
        }}
        self.assertEqual(ab._fallback_latest_compatible(data), "0.7.4")

    def test_config_value_con_fallback(self):
        with mock.patch.dict(sys.modules, {"config": None}):
            self.assertEqual(ab._config_value("CACHE_DIR", Path("x")), Path("x"))
        fake = types.SimpleNamespace(CACHE_DIR=Path("y"))
        with mock.patch.dict(sys.modules, {"config": fake}):
            self.assertEqual(ab._config_value("CACHE_DIR", Path("x")), Path("y"))
        self.assertEqual(ab._embedding_cache_dir("z"), "z")


class TestMainReport(unittest.TestCase):
    @staticmethod
    def _fixture():
        base = np.random.default_rng(0).normal(size=(12, 8))
        base /= np.linalg.norm(base, axis=1, keepdims=True)
        texts = {"slides": [f"S{i}" for i in range(4)], "blocks": [f"B{i}" for i in range(8)]}
        return texts, base

    def test_report_registra_batch_e_cache(self):
        texts, base = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "ab.json"
            cache = str(Path(td) / "emb")
            with mock.patch.object(ab, "collect_real_texts", return_value=texts) as collect, \
                 mock.patch.object(ab, "baseline_embeddings", return_value=base) as base_fn, \
                 mock.patch.object(ab, "candidate_embeddings",
                                   return_value=(base.copy(), None)) as cand_fn, \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = ab.main(["--candidate", "0.7.4", "--report", str(report),
                              "--cache-dir", cache])
            data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(data["batch_size"], ab.DEFAULT_EMBED_BATCH)
        self.assertEqual(data["cache_dir"], str(Path(cache) / "embedding_model"))
        self.assertEqual(data["candidate"], "0.7.4")
        self.assertEqual(collect.call_args.kwargs["cache_dir"], Path(cache))
        self.assertEqual(base_fn.call_args.kwargs["cache_dir"], str(Path(cache) / "embedding_model"))
        self.assertEqual(cand_fn.call_args.kwargs["batch_size"], ab.DEFAULT_EMBED_BATCH)
        self.assertEqual(cand_fn.call_args.kwargs["cache_dir"], str(Path(cache) / "embedding_model"))

    def test_senza_candidate_usa_versione_installabile(self):
        with mock.patch.object(ab, "_candidate_version_from_pypi", return_value="0.7.4") as getver, \
             mock.patch.object(ab, "collect_real_texts", return_value=None), \
             contextlib.redirect_stderr(io.StringIO()):
            rc = ab.main([])
        self.assertEqual(rc, 4)
        getver.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
