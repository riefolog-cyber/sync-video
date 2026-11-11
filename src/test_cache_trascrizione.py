#!/usr/bin/env python3
"""La cache della trascrizione non si perde quando cambia il nome della chiave.

Il 02/11/2026 e' stato aggiunto un tag al nome della chiave (il percorso di
decodifica dell'audio): la cache valida c'era, ma con un nome diverso, e la
run ha rifatto 3 minuti e 27 di Whisper per un cambio di nome. I parametri
della trascrizione sono ora dentro la cache: una cache con gli stessi
parametri e un nome diverso vale quanto quella con il nome giusto.

Esegui con: python -m unittest test_cache_trascrizione -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main

PARAMETRI = {
    "audio": "abc123",
    "lang": "ita",
    "model": "small",
    "engine": "openvino",
    "engine_params": "GPU,decode-ffmpeg-f32le",
}


class TestCacheTrasrizioneEquivalente(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _cache(self, nome: str, params: dict[str, str] | None) -> None:
        dati: dict[str, object] = {"transcript": "ciao", "words_raw": []}
        if params is not None:
            dati["params"] = params
        (self.base / f"{nome}.json").write_text(json.dumps(dati), encoding="utf-8")

    def test_cache_con_nome_diverso_e_stessi_parametri(self) -> None:
        self._cache("transcript_chiave_vecchia", PARAMETRI)
        with mock.patch.object(main, "CACHE_DIR", self.base):
            trovata = main._trascrizione_equivalente("transcript_chiave_nuova", PARAMETRI)
        self.assertEqual(trovata, "transcript_chiave_vecchia")

    def test_cache_con_parametri_diversi_non_vale(self) -> None:
        self._cache("transcript_chiave_vecchia", {**PARAMETRI, "model": "medium"})
        with mock.patch.object(main, "CACHE_DIR", self.base):
            trovata = main._trascrizione_equivalente("transcript_chiave_nuova", PARAMETRI)
        self.assertIsNone(trovata, "parametri diversi: la cache non vale")

    def test_cache_senza_parametri_non_vale(self) -> None:
        # Le cache scritte prima di questa chiave non hanno i parametri dentro:
        # non si possono riconoscere, e vengono potate come prima.
        self._cache("transcript_chiave_vecchia", None)
        with mock.patch.object(main, "CACHE_DIR", self.base):
            trovata = main._trascrizione_equivalente("transcript_chiave_nuova", PARAMETRI)
        self.assertIsNone(trovata)

    def test_le_varianti_gap_e_accurate_non_sono_candidate(self) -> None:
        # La cache "ricucita" e quella "accurata" sono varianti della stessa
        # decodifica: non sono la trascrizione di base, quindi non fungono.
        self._cache("transcript_chiave_vecchia_gap", PARAMETRI)
        with mock.patch.object(main, "CACHE_DIR", self.base):
            trovata = main._trascrizione_equivalente("transcript_chiave_nuova", PARAMETRI)
        self.assertIsNone(trovata)

    def test_cache_illegibile_non_rompe(self) -> None:
        (self.base / "transcript_rotta.json").write_text("{non è json", encoding="utf-8")
        with mock.patch.object(main, "CACHE_DIR", self.base):
            trovata = main._trascrizione_equivalente("transcript_chiave_nuova", PARAMETRI)
        self.assertIsNone(trovata)


if __name__ == "__main__":
    unittest.main()
