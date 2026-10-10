#!/usr/bin/env python3
"""Test del controllo dello spazio disco al primo avvio (P2 #18).

Esegui con: python -m unittest test_disco -v

Il problema che copre: il progetto scarica ~6.9 GB al primo avvio senza
aver mai controllato che il disco li regga. Su un PC con poco spazio il
download parte, il disco si riempie e la run muore a meta' con un errore
che non nomina la causa.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import config
import hardware

GB = 1024**3


class TestDiskFreeBytes(unittest.TestCase):
    def test_ritorna_un_numero_positivo(self) -> None:
        libero = hardware.disk_free_bytes(Path(tempfile.gettempdir()))
        if libero is not None:
            self.assertGreater(libero, 0)

    def test_percorso_inesistente_risale_alla_cartella_esistente(self) -> None:
        # La cache non esiste ancora al primo avvio: shutil.disk_usage su un
        # percorso inesistente leverebbe, quindi si risale all'antenato.
        profondo = Path(tempfile.gettempdir()) / "non-esiste-xyz" / "a" / "b" / "c"
        libero = hardware.disk_free_bytes(profondo)
        assert libero is not None
        self.assertGreater(libero, 0)

    def test_nessun_dato_ritorna_none_e_non_solleva(self) -> None:
        with patch.object(hardware.shutil, "disk_usage", side_effect=OSError("drive non pronto")):
            self.assertIsNone(hardware.disk_free_bytes(Path("C:/")))


class TestSpazioNecessario(unittest.TestCase):
    """Le cifre mostrate all'utente e i byte usati dal controllo non
    possono divergere: altrimenti l'avviso promette una cosa e ne controlla
    un'altra."""

    def test_tabelle_coerenti(self) -> None:
        for chiave, testo in config._DIMENSIONI_WHISPER.items():
            trovato = re.search(r"([\d.]+)", testo)
            assert trovato is not None, testo
            numero = float(trovato.group(1))
            fattore = 1024**3 if "GB" in testo else 1024**2
            with self.subTest(modello=chiave):
                self.assertAlmostEqual(
                    config._BYTES_WHISPER[chiave] / fattore, numero, delta=numero * 0.02
                )
        trovato = re.search(r"([\d.]+)", config._CACHE_EMBEDDING_DEFAULT)
        assert trovato is not None
        numero = float(trovato.group(1))
        self.assertAlmostEqual(
            config._BYTES_CACHE_EMBEDDING / 1024**3, numero, delta=numero * 0.02
        )

    def test_somma_di_embedding_e_whisper(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"),
        ):
            totale = config._spazio_modelli_necessario(
                Path(d) / "emb", "small", config.DEFAULT_EMBEDDING_MODEL
            )
        assert totale is not None
        self.assertAlmostEqual(totale / GB, 6.88, delta=0.05)

    def test_il_modello_piu_piccolo_occupa_meno(self) -> None:
        importanti: dict[str, int] = {}
        with tempfile.TemporaryDirectory() as d:
            for modello in ("tiny", "base", "small"):
                with patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"):
                    got = config._spazio_modelli_necessario(
                        Path(d) / f"e_{modello}", modello, config.DEFAULT_EMBEDDING_MODEL
                    )
                assert got is not None, modello
                importanti[modello] = got
        self.assertLess(importanti["tiny"], importanti["base"])
        self.assertLess(importanti["base"], importanti["small"])

    def test_dimensione_ignora_ritorna_none(self) -> None:
        # whisper large non ha una dimensione misurata: meglio nessun
        # controllo che un controllo con una cifra inventata.
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"),
        ):
            got = config._spazio_modelli_necessario(
                Path(d) / "emb", "large", config.DEFAULT_EMBEDDING_MODEL
            )
        self.assertIsNone(got)

    def test_semantic_model_alterno_ignora_ritorna_none(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(config, "_cache_whisper_faster", return_value=None),
        ):
            got = config._spazio_modelli_necessario(Path(d) / "emb", "small", "altro/modello")
        self.assertIsNone(got)

    def test_già_scaricati_ritorna_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            emb = Path(d) / "emb"
            emb.mkdir()
            (emb / "modello.bin").write_bytes(b"x" * 10)
            with patch.object(config, "_cache_whisper_faster", return_value=None):
                got = config._spazio_modelli_necessario(
                    emb, "small", config.DEFAULT_EMBEDDING_MODEL
                )
        self.assertEqual(got, 0)


class TestAvvisoSpazio(unittest.TestCase):
    def _cattura(self, libero: int | None, modello_whisper: str = "small") -> list[str]:
        """Righe di WARNING emesse dall'avviso, con il disco finto a `libero`."""
        import logging

        with tempfile.TemporaryDirectory() as d:
            buf = StringIO()
            handler = logging.StreamHandler(buf)
            logger = logging.getLogger()
            vecchio = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)
            try:
                with (
                    patch.object(config, "disk_free_bytes", return_value=libero),
                    patch.object(config, "_cache_whisper_faster", return_value=Path(d) / "hf"),
                ):
                    config._avvisa_spazio_insufficiente(
                        Path(d) / "emb", modello_whisper, config.DEFAULT_EMBEDDING_MODEL
                    )
            finally:
                logger.removeHandler(handler)
                logger.setLevel(vecchio)
        return [riga for riga in buf.getvalue().splitlines() if riga.strip()]

    def test_avvisa_quando_lo_spazio_non_basta(self) -> None:
        righe = self._cattura(3 * GB)
        self.assertTrue(righe, "con 3 GB liberi e ~8.9 GB necessari deve avvisare")
        testo = "\n".join(righe)
        self.assertIn("Spazio", testo)
        self.assertIn("3.0 GB", testo)
        self.assertIn("8.9 GB", testo)

    def test_non_avvisa_quando_lo_spazio_basta(self) -> None:
        self.assertEqual(self._cattura(100 * GB), [])

    def test_avvisa_su_entrambe_le_destinazioni(self) -> None:
        # La cache embedding segue il progetto, quella whisper segue la home:
        # possono stare su dischi diversi, e vanno controllate entrambe.
        righe = self._cattura(3 * GB)
        testo = "\n".join(righe)
        self.assertIn("cartella del progetto", testo)
        self.assertIn("HuggingFace", testo)

    def test_dimensione_ignora_non_avvisa(self) -> None:
        # whisper large non e' misurato: niente avviso con cifra inventata.
        self.assertEqual(self._cattura(1 * GB, modello_whisper="large"), [])

    def test_spazio_non_rilevabile_non_avvisa(self) -> None:
        # Se il disco non e' interrogabile non si inventa nulla: l'avviso
        # sarebbe falso.
        self.assertEqual(self._cattura(None), [])


class TestIntegrazioneAnnuncio(unittest.TestCase):
    def test_modelli_mancanti_stessa_firma(self) -> None:
        # La coppia (nome, dimensione) e' il formato che i test e i chiamanti
        # fanno: il refactor non deve averla cambiata sotto di loro.
        with tempfile.TemporaryDirectory() as d:
            coppie = config._modelli_mancanti(Path(d) / "emb", "small")
            for voce in coppie:
                self.assertIsInstance(voce, tuple)
                self.assertEqual(len(voce), 2)
                self.assertIsInstance(voce[0], str)
                self.assertIsInstance(voce[1], str)

    def test_annuncio_puo_scattare_il_controllo(self) -> None:
        # _annuncia_primo_avvio non deve più essere un vicolo cieco sul disco.
        with patch.object(config, "_avvisa_spazio_insufficiente") as avviso:
            config._annuncia_primo_avvio([("modello embedding", "~6.4 GB")])
        avviso.assert_called_once()

    def test_annuncio_senza_modelli_non_controlla(self) -> None:
        with patch.object(config, "_avvisa_spazio_insufficiente") as avviso:
            config._annuncia_primo_avvio([])
        avviso.assert_not_called()


if __name__ == "__main__":
    unittest.main()
