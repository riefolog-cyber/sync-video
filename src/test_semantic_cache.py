#!/usr/bin/env python3
"""Indice di riuso embedding e helper condivisi della sincronizzazione.

Esegui con: python -m unittest test_semantic_cache -v

Nessuna rete e nessun modello: le voci di cache sono npz scritti a mano in una
cartella temporanea e l'embed_fn e' sostituito dove serve.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import semantic_sync


def _voce(cache: Path, nome: str, testi: list[str], valori: list[float]) -> Path:
    """Scrive una voce di cache con i vettori e le chiavi dei testi dati."""
    path = cache / f"emb_{nome}.npz"
    np.savez(
        path,
        emb=np.array([[v, 0.0, 0.0, 0.0] for v in valori], dtype=np.float32),
        seconds=np.float32(1.0),
        keys=np.array([semantic_sync._chiave_testo(t) for t in testi], dtype="U16"),
        model=np.array("fake|b16"),
    )
    return path


class TestIndiceEmbedding(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)
        semantic_sync.reset_embed_cache()
        self.addCleanup(semantic_sync.reset_embed_cache)
        self._patch = mock.patch.object(semantic_sync, "_EMBED_CACHE_DIR", self.cache)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_indice_evita_di_aprire_tutti_gli_npz(self) -> None:
        # Otto voci, ma solo due contengono i testi cercati: la prima chiamata
        # paga l'indice (8 aperture) piu' i 2 file utili; la seconda deve
        # aprire SOLO i 2 file che servono davvero.
        for i in range(8):
            _voce(self.cache, f"altre{i:02d}", [f"testo-{i}"], [float(i + 1)])
        utile_a = _voce(self.cache, "utile-a", ["alfa"], [3.0])
        utile_b = _voce(self.cache, "utile-b", ["beta"], [4.0])

        con_indice = mock.patch.object(semantic_sync.np, "load", wraps=np.load)
        with con_indice as aperto:
            trovati, mancanti = semantic_sync._riuso_parziale("fake|b16", ["alfa", "beta"])
        aperture_prima = aperto.call_count
        aperto.reset_mock()

        with con_indice as aperto:
            trovati2, mancanti2 = semantic_sync._riuso_parziale("fake|b16", ["alfa", "beta"])
        aperture_seconda = aperto.call_count

        self.assertEqual(mancanti, [])
        self.assertEqual(mancanti2, [])
        self.assertEqual(aperture_prima, 12)  # 8 indice + 2 indice + 2 vettori
        self.assertEqual(aperture_seconda, 2)
        np.testing.assert_array_equal(trovati[0], trovati2[0])
        np.testing.assert_array_equal(trovati[1], trovati2[1])
        self.assertEqual(trovati[1][0], 4.0)
        self.assertTrue(utile_a.exists() and utile_b.exists())

    def test_mtime_cambiato_rifà_l_indice(self) -> None:
        _voce(self.cache, "uno", ["gamma"], [1.0])
        semantic_sync._riuso_parziale("fake|b16", ["gamma"])
        _voce(self.cache, "uno", ["gamma"], [9.0])
        vecchio = (self.cache / "emb_uno.npz").stat().st_mtime
        import os

        os.utime(self.cache / "emb_uno.npz", (vecchio + 10, vecchio + 10))
        trovati, mancanti = semantic_sync._riuso_parziale("fake|b16", ["gamma"])
        self.assertEqual(mancanti, [])
        self.assertEqual(trovati[0][0], 9.0)


class TestHelperCondivisi(unittest.TestCase):
    def test_ancore_fuori_portata_vengono_avvisate(self) -> None:
        with self.assertLogs("slide2video", level="WARNING") as catturati:
            valide = semantic_sync._ancore_valide(
                {2: 10.0, 9: 5.0, 3: -1.0}, total_slides=5, total_duration=60.0
            )
        self.assertEqual(valide, {2: 10.0})
        self.assertIn("Scartate 2 ancore", "\n".join(catturati.output))

    def test_avviso_arriva_anche_nel_ramo_tutto_ancorato(self) -> None:
        # Il ramo "tutte le ancore" non embedda i blocchi: per questo prima
        # l'avviso mancava. Basta che il filtro venga chiamato prima del ramo.
        con_timeline = mock.patch.object(
            semantic_sync, "_timeline_solo_ancore", return_value={1: 0.0, 2: 10.0, 3: 20.0}
        )
        with con_timeline as solo, self.assertLogs("slide2video", level="WARNING") as catturati:
            semantic_sync.semantic_timeline_from_texts(
                ["a", "b", "c"],
                [{"time": i * 10.0, "text": "x"} for i in range(3)],
                total_slides=3,
                total_duration=60.0,
                embed_fn=lambda texts: np.zeros((len(list(texts)), 4), dtype=np.float32),
                anchors={2: 10.0, 3: 20.0, 9: 5.0},
            )
        solo.assert_called_once()
        self.assertEqual(solo.call_args.args[0], {2: 10.0, 3: 20.0})
        self.assertIn("Scartate 1 ancore", "\n".join(catturati.output))

    def test_pairs_assegnate_ultimo_segmento_fino_alla_fine(self) -> None:
        starts = [0, 3, 7]
        atteso = [(blk, s - 1) for s in range(1, 4) for blk in range(starts[s - 1], 10 if s == 3 else starts[s])]
        self.assertEqual(semantic_sync._assigned_pairs(starts, 3, 10), atteso)
        self.assertIn((9, 2), atteso)  # l'ultima slide prende i blocchi finali


if __name__ == "__main__":
    unittest.main()
