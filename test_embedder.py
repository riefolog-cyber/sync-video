#!/usr/bin/env python3
"""Test dell'interfaccia embedder (P2).

Esegui con: python -m unittest test_embedder -v

Verifica il Protocol e la delega a semantic_sync con un modello finto:
nessun download, nessuna rete.
"""

import unittest
from collections.abc import Iterator, Sequence
from unittest.mock import patch

import numpy as np

from embedder import Embedder, _FastEmbedEmbedder, load_embedder


class _FakeModel:
    """Modello fastembed finto: vettori deterministici di dimensione 4."""

    model_name = "fake-e5-small"

    def embed(self, texts: Sequence[str], batch_size: int = 64) -> Iterator[np.ndarray]:
        for t in texts:
            base = np.zeros(4, dtype=np.float32)
            base[hash(t) % 4] = 1.0
            yield base


class TestProtocol(unittest.TestCase):
    def test_fastembed_backend_soddisfa_il_protocol(self) -> None:
        from semantic_sync import _make_embed_fn

        emb = _FastEmbedEmbedder(_FakeModel(), _make_embed_fn(_FakeModel()))
        self.assertIsInstance(emb, Embedder)

    def test_embed_restituisce_matrice_normalizzata(self) -> None:
        from semantic_sync import _make_embed_fn

        emb = _FastEmbedEmbedder(_FakeModel(), _make_embed_fn(_FakeModel()))
        out = emb.embed(["uno", "due", "tre"])
        self.assertEqual(out.shape, (3, 4))
        norms = np.linalg.norm(out, axis=1)
        np.testing.assert_allclose(norms, 1.0, rtol=1e-5)

    def test_embed_id_passa_dalla_config(self) -> None:
        from semantic_sync import _make_embed_fn

        emb = _FastEmbedEmbedder(_FakeModel(), _make_embed_fn(_FakeModel()))
        self.assertIn("fake-e5-small", emb.embed_id)


class TestLoadEmbedder(unittest.TestCase):
    def test_delega_il_caricamento_a_semantic_sync(self) -> None:
        fake_fn = lambda texts: np.zeros((len(list(texts)), 4), dtype=np.float32)  # noqa: E731
        with (
            patch("semantic_sync._load_embed_model", return_value=_FakeModel()) as loader,
            patch("semantic_sync._make_embed_fn", return_value=fake_fn) as maker,
        ):
            emb = load_embedder("modello-a", "/cache", alternate_name="alt")
        loader.assert_called_once_with("modello-a", "/cache", alternate_name="alt")
        maker.assert_called_once()
        self.assertIsInstance(emb, Embedder)

    def test_modello_non_caricabile_restituisce_none(self) -> None:
        with patch("semantic_sync._load_embed_model", return_value=None):
            self.assertIsNone(load_embedder("mancante", "/cache"))


if __name__ == "__main__":
    unittest.main()
