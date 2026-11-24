#!/usr/bin/env python3
"""Test della prova sul materiale reale (il confronto con il riferimento).

Lo script `scripts/prova_materiale_reale.py` lancia la pipeline sul materiale
vero e confronta la timeline con un file golden. Qui si verifica solo il
CONFRONTO: che dica "identica" quando lo e', e che nomini la differenza
quando un confine si e' spostato.

Non si lancia la pipeline (ci vuole il materiale): il confronto e' una
funzione pura.

Esegui con: python -m unittest test_prova_materiale -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Il modulo e' in scripts/ (non e' un pacchetto: si esegue come script), quindi
# la sua cartella va aggiunta al sys.path per poterlo importare da qui.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import prova_materiale_reale as prova


class TestPercorsoCache(unittest.TestCase):
    def test_la_prova_legge_la_stessa_cache_della_pipeline(self) -> None:
        # Con SYNC_VIDEO_CACHE_DIR la pipeline scrive altrove: uno script che
        # si ostina a leggere RADICE/".cache" confronta una timeline vecchia
        # (o non trova niente) senza dirlo.
        import config

        self.assertEqual(prova.CACHE, config.CACHE_DIR)


class TestConfrontoConIlRiferimento(unittest.TestCase):
    """Il confronto deve dire la verita' su due cose: uguale o diversa."""

    def _timeline(self, *inizi: float) -> list[dict[str, object]]:
        segmenti: list[dict[str, object]] = []
        for i, inizio in enumerate(inizi):
            segmenti.append(
                {"slide": i + 1, "start": inizio, "end": inizio + 60.0}
            )
        return segmenti

    def test_timeline_uguale_non_problema(self) -> None:
        segmenti = self._timeline(0.0, 60.0, 120.0)
        self.assertEqual(prova._differenze(segmenti, segmenti), [])

    def test_un_confine_spostato_e_segnalato(self) -> None:
        prima = self._timeline(0.0, 60.0, 120.0)
        adesso = self._timeline(0.0, 62.5, 120.0)
        problemi = prova._differenze(adesso, prima)
        self.assertEqual(len(problemi), 1)
        self.assertIn("slide 2", problemi[0])
        self.assertIn("+2.50", problemi[0])

    def test_scarto_minimo_non_e_un_problema(self) -> None:
        # Tolleranza: 20 millisecondi non sono uno spostamento di confine.
        prima = self._timeline(0.0, 60.0)
        adesso = self._timeline(0.0, 60.02)
        self.assertEqual(prova._differenze(adesso, prima), [])

    def test_numero_di_segmenti_diverso_e_segnalato(self) -> None:
        problemi = prova._differenze(self._timeline(0.0, 60.0), self._timeline(0.0))
        self.assertEqual(len(problemi), 1)
        self.assertIn("segmenti", problemi[0])

    def test_slide_diverse_sono_segnalate(self) -> None:
        prima: list[dict[str, object]] = [{"slide": 1, "start": 0.0, "end": 60.0}]
        adesso: list[dict[str, object]] = [{"slide": 2, "start": 0.0, "end": 60.0}]
        problemi = prova._differenze(adesso, prima)
        self.assertEqual(len(problemi), 1)
        self.assertIn("slide", problemi[0])


class TestEsiti(unittest.TestCase):
    """Tre esiti distinti: identica (0), cambiata (1), non verificabile (2).

    "Non verificabile" non e' un fallimento del programma: senza lo stesso
    materiale (non versionato) o senza riferimento non c'e' niente da
    confrontare. Prima usciva 1 anche qui, e un checkout nuovo sembrava
    rotto.
    """

    def _esegui(
        self,
        riferimento: dict[str, object],
        segmenti: list[dict[str, object]],
        materiale_hash: str,
    ) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            rif = Path(tmp) / "rif.json"
            rif.write_text(json.dumps(riferimento), encoding="utf-8")
            with (
                mock.patch.object(prova, "RIFERIMENTO", rif),
                mock.patch.object(
                    prova,
                    "_materiale",
                    return_value=(Path("deck.pdf"), Path("podcast.m4a")),
                ),
                mock.patch.object(prova, "_pipeline"),
                mock.patch.object(prova, "_timeline", return_value=segmenti),
                mock.patch.object(prova, "_ancore", return_value=0),
                mock.patch.object(prova, "file_hash", return_value=materiale_hash),
            ):
                return prova.main([])

    def test_timeline_identica_esce_con_zero(self) -> None:
        segmenti: list[dict[str, object]] = [{"slide": 1, "start": 0.0, "end": 60.0}]
        riferimento: dict[str, object] = {
            "materiale": {"pdf": "h", "audio": "h"},
            "segmenti": segmenti,
        }
        self.assertEqual(self._esegui(riferimento, segmenti, "h"), prova.ESITO_OK)

    def test_confine_spostato_esce_con_uno(self) -> None:
        riferimento: dict[str, object] = {
            "materiale": {"pdf": "h", "audio": "h"},
            "segmenti": [{"slide": 1, "start": 0.0, "end": 60.0}],
        }
        adesso: list[dict[str, object]] = [{"slide": 1, "start": 5.0, "end": 60.0}]
        self.assertEqual(self._esegui(riferimento, adesso, "h"), prova.ESITO_CAMBIATA)

    def test_materiale_diverso_non_e_un_fallimento(self) -> None:
        riferimento: dict[str, object] = {
            "materiale": {"pdf": "altro", "audio": "altro"},
            "segmenti": [],
        }
        self.assertEqual(
            self._esegui(riferimento, [], "h"), prova.ESITO_NON_VERIFICABILE
        )

    def test_senza_riferimento_non_e_un_fallimento(self) -> None:
        self.assertEqual(self._esegui({}, [], "h"), prova.ESITO_NON_VERIFICABILE)


if __name__ == "__main__":
    unittest.main()
