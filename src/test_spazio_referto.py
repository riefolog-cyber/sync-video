#!/usr/bin/env python3
"""Un controllo che non puo' calcolare non deve dire "tutto a posto".

Il buco che copre, misurato su una cartella appena estratta dal ZIP (quindi
un PC nuovo, senza modelli):

    [ ok ] Spazio disco   modelli gia' in cache      <- FALSO
    [avv] Modelli         non ancora scaricato       <- vero

Due righe che si contraddicono, e la falsa e' proprio quella sul controllo
che serve a non riempire il disco a meta' download.

La causa era in `_modelli_mancanti_dettagliati`: il nome del modello veniva
risolto dal default anche con il parametro vuoto (`embedding_model or
DEFAULT_...`), ma la dimensione no (`embedding_model == DEFAULT_...`). Quindi
la stessa funzione diceva "multilingual-e5-large" e "dimensione sconosciuta".
`_spazio_modelli_necessario` restituiva None, e `if not necessario` leggeva
quel None come "zero byte necessari", cioe' "tutto in cache".

Qui si verifica l'invariante alla fonte: nome e dimensione devono stare nella
stessa frase, qualunque sia la forma del parametro.

Esegui con: python -m unittest test_spazio_referto -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import verifica_pc

GB = 1024**3


class TestDimensioneCoerenteColNome(unittest.TestCase):
    """L'invariante: se il nome e' il default, anche la dimensione lo e'."""

    def _byte(self, embedding_model: str) -> int | None:
        """Byte del modello embedding mancante, None se ignoti."""
        with tempfile.TemporaryDirectory() as d:
            vuota = Path(d) / "cache_inesistente"
            dettagli = config._modelli_mancanti_dettagliati(vuota, "small", embedding_model)
        return next((b for nome, _, b in dettagli if nome.startswith("modello embedding")), None)

    def test_parametro_vuoto_equivale_al_default(self) -> None:
        """Il caso che rompeva: "" deve valere come il default esplicito."""
        vuoto = self._byte("")
        esplicito = self._byte(config.DEFAULT_EMBEDDING_MODEL)
        self.assertIsNotNone(
            vuoto,
            "col parametro vuoto la dimensione torna None: il nome dice il "
            "default ma la dimensione no",
        )
        self.assertEqual(vuoto, esplicito)

    def test_il_nome_e_la_dimensione_tornano_insieme(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            vuota = Path(d) / "cache_inesistente"
            dettagli = config._modelli_mancanti_dettagliati(vuota, "small", "")
            nome, dimensione, byte = dettagli[0]
        self.assertIn("multilingual-e5-large", nome)
        self.assertTrue(dimensione, "dimensione vuota pur essendo il modello di default")
        self.assertEqual(byte, config._BYTES_CACHE_EMBEDDING)

    def test_un_modello_diverso_davvero_non_si_inventa(self) -> None:
        """`None` resta il risultato onesto quando la dimensione e' ignota."""
        self.assertIsNone(self._byte("altro/modello-sconosciuto"))

    def test_spazio_necessario_coerente(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            vuota = Path(d) / "cache_inesistente"
            a = config._spazio_modelli_necessario(vuota, "small")
            b = config._spazio_modelli_necessario(vuota, "small", config.DEFAULT_EMBEDDING_MODEL)
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertGreater(a or 0, 6 * GB)


class TestSpazioZeroNonEIgnoto(unittest.TestCase):
    """0 e None non sono la stessa cosa, e il referto non li scambia."""

    def _esito(self, necessario: int | None) -> verifica_pc.Esito:
        r = verifica_pc.Relazione()
        with mock.patch.object(
            config,
            "_spazio_modelli_necessario",
            lambda *a, **k: necessario,
        ), mock.patch.object(verifica_pc, "disk_free_bytes", lambda *a, **k: 100 * GB):
            verifica_pc.prova_spazio(r, Path("C:/"))
        return next(e for e in r.esiti if e.nome == "Spazio disco")

    def test_zero_vuol_dire_solo_che_e_gia_scaricato(self) -> None:
        e = self._esito(0)
        self.assertIn("gia' in cache", e.dettaglio)
        self.assertEqual(e.stato, verifica_pc.OK)

    def test_none_non_puo_venir_letto_com_zero(self) -> None:
        """Il buco: `if not necessario` accettava None come se fosse 0."""
        e = self._esito(None)
        self.assertNotIn("gia' in cache", e.dettaglio)
        self.assertEqual(e.stato, verifica_pc.AVV)

    def test_il_dettaglio_ammette_che_non_lo_sa(self) -> None:
        e = self._esito(None)
        self.assertIn("non calcolabile", e.dettaglio)


class TestIlRefertoNonSiContraddice(unittest.TestCase):
    """La prova che vale: su una cartella senza modelli le due righe concordano.

    Riproduce la situazione reale (cache assente) e verifica che il referto non
    dica "modelli gia' in cache" mentre quello sotto dice che non lo sono.
    """

    def test_cartella_vuota(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d) / "embedding_model"
            con_cache = mock.patch.object(
                config, "DEFAULT_EMBEDDING_CACHE_DIR", str(cache)
            )
            with con_cache, mock.patch.object(verifica_pc, "disk_free_bytes", lambda *a, **k: 100 * GB):
                r = verifica_pc.Relazione()
                verifica_pc.prova_spazio(r, Path(d))
                verifica_pc.prova_modelli(r)
        esiti = {e.nome: e.dettaglio for e in r.esiti}
        spazio, modelli = esiti["Spazio disco"], esiti["Modelli"]
        scaricati = "non ancora scaricato" not in modelli
        if not scaricati:
            self.assertNotIn(
                "gia' in cache",
                spazio,
                f"si contraddicono: Spazio disco dice '{spazio}' mentre Modelli dice '{modelli}'",
            )


class TestIlRefertoNonEmenoPermissivoDellaGuardia(unittest.TestCase):
    """Il diagnostico non puo' essere piu' generoso del programma.

    Il buco: `verifica_pc` aveva il proprio margino di 1 GB mentre config ne
    usa 2, con un commento che giurava fossero la stessa soglia. Il referto
    diceva "ne servono ~7.4 GB" quando la guardia vera ne chiede 8.4: un
    collega con 8 GB liberi leggeva [ ok ] e poi il download gli veniva
    rifiutato. Il diagnostico prometteva il contrario di quello che il
    programma fa, che e' il modo peggiore in cui puo' mentire.
    """

    def _richiesto_dal_referto(self) -> float:
        """GB che il referto considera sufficienti, con i decimali.

        `_gb()` stampa una cifra dopo il punto ("8.4 GB"): leggerla come
        intero farebbe sembrare il referto piu' esigente di quanto sia, e il
        test passerebbe per il motivo sbagliato.
        """
        import re

        r = verifica_pc.Relazione()
        with (
            mock.patch.object(
                config,
                "_spazio_modelli_necessario",
                lambda *a, **k: config._BYTES_CACHE_EMBEDDING,
            ),
            mock.patch.object(verifica_pc, "disk_free_bytes", lambda *a, **k: 50 * GB),
        ):
            verifica_pc.prova_spazio(r, Path("C:/"))
        dettaglio = next(e for e in r.esiti if e.nome == "Spazio disco").dettaglio
        m = re.search(r"ne servoni?e? ~([\d.,]+) GB", dettaglio) or re.search(r"ne servono ~([\d.,]+) GB", dettaglio)
        assert m is not None, f"impossibile leggere la richiesta da: {dettaglio}"
        return float(m.group(1).replace(",", "."))

    def test_il_referto_non_chiede_meno_del_bootstrap(self) -> None:
        vero_gb = (config._BYTES_CACHE_EMBEDDING + config._MARGINE_DISCO) / GB
        self.assertGreaterEqual(
            self._richiesto_dal_referto(),
            vero_gb - 0.1,
            f"il referto chiede meno spazio di quanto il programma ne pretende davvero ({vero_gb:.1f} GB)",
        )

    def test_il_referto_e_il_bootstrap_usano_lo_stesso_margine(self) -> None:
        """Se i due numeri divergono di nuovo, qui si vede subito."""
        with mock.patch.object(
            config,
            "_spazio_modelli_necessario",
            lambda *a, **k: 1 * GB,
        ):
            r = verifica_pc.Relazione()
            with mock.patch.object(verifica_pc, "disk_free_bytes", lambda *a, **k: 50 * GB):
                verifica_pc.prova_spazio(r, Path("C:/"))
        dettaglio = next(e for e in r.esiti if e.nome == "Spazio disco").dettaglio
        # 1 GB di modelli + margine deve uscire > 1 GB
        self.assertIn("ne servono", dettaglio)
        numero = float(dettaglio.split("ne servono ~")[1].split(" GB")[0].replace(",", "."))
        atteso = (1 * GB + config._MARGINE_DISCO) / GB
        self.assertAlmostEqual(numero, atteso, delta=0.1)


if __name__ == "__main__":
    unittest.main()
