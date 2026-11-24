#!/usr/bin/env python3
"""Casi limite delle ancore emersi nella revisione approfondita.

Esegui con: python -m unittest test_timeline_edge -v
"""

from __future__ import annotations

import unittest

from chunks import Word, build_windows
from timeline import (
    _normalize,
    _number_from_word,
    announcements_off_timeline,
    out_of_range_tail_anchor,
    split_ordered_mentions,
)


class TestAnnunciConOffset(unittest.TestCase):
    def test_numero_parlato_sfalsato_non_viene_scartato(self) -> None:
        # 14 pagine PDF, relatore che conta da 1 (offset -1): dice "slide 15"
        # a 900s per la pagina 14, dichiarata a 700s. Prima il numero veniva
        # filtrato con 2 <= 15 <= 14, quindi il controllo non segnalava niente.
        parole: list[Word] = [
            {"word": "passiamo", "start": 895.0},
            {"word": "alla", "start": 895.5},
            {"word": "slide", "start": 896.0},
            {"word": "15", "start": 896.5},
        ]
        trovati = announcements_off_timeline(
            parole, {14: 700.0}, 14, slide_offset=-1
        )
        self.assertEqual(len(trovati), 1)
        self.assertEqual(trovati[0][0], 14)

    def test_conteggio_con_numero_prima_di_slide_non_e_annuncio(self) -> None:
        # "ora le 13 slide del documento" e' un conteggio, non un annuncio:
        # la nuova grammatica all'indietro non deve creare un falso dubbio.
        parole: list[Word] = [
            {"word": "ora", "start": 9.0},
            {"word": "le", "start": 9.5},
            {"word": "13", "start": 10.0},
            {"word": "slide", "start": 10.5},
            {"word": "del", "start": 11.0},
            {"word": "documento", "start": 11.5},
        ]
        self.assertEqual(
            announcements_off_timeline(parole, {13: 500.0}, 14), []
        )

    def test_annunci_duplicati_non_gonfiano_i_dubbi(self) -> None:
        parole: list[Word] = [
            {"word": "passiamo", "start": 100.0},
            {"word": "slide", "start": 100.5},
            {"word": "slide", "start": 100.7},
            {"word": "10", "start": 101.0},
        ]
        trovati = announcements_off_timeline(
            parole, {10: 500.0}, 12
        )
        self.assertEqual(len(trovati), 1)


class TestConteggiNonAncore(unittest.TestCase):
    def test_conteggio_basso_non_ruba_la_transizione(self) -> None:
        # "le 2 slide del documento" a 5s e' un conteggio, non l'ingresso
        # nella slide 2: il vero "passiamo slide 2" e' a 100s. Il conteggio
        # non deve alzare `highest` e far classificare la transizione come
        # richiamo fuori ordine.
        # Il conteggio di un numero PIU' ALTO prima delle transizioni vere e'
        # il caso che rompeva: alzava `highest` e faceva classificare come
        # richiami fuori ordine i veri annunci della slide 2 e 3.
        ancore, _ = split_ordered_mentions(
            {
                10: [(5.0, True)],
                2: [(100.0, False)],
                3: [(150.0, False)],
            }
        )
        self.assertAlmostEqual(ancore[2], 100.0)
        self.assertAlmostEqual(ancore[3], 150.0)


class TestNumeriECode(unittest.TestCase):
    def test_decimali_e_intervalli_non_diventano_slide(self) -> None:
        self.assertIsNone(_number_from_word(_normalize("1.5")))
        self.assertIsNone(_number_from_word(_normalize("3-4")))
        self.assertEqual(_number_from_word(_normalize("3,")), 3)

    def test_build_windows_ordina_le_parole(self) -> None:
        parole: list[Word] = [
            {"word": "prima", "start": 5.0},
            {"word": "seconda", "start": 1.0},
            {"word": "terza", "start": 3.0},
        ]
        finestre = build_windows(parole, total_duration=10.0, window_seconds=4.0)
        self.assertTrue(any("seconda" in f["words"] and "terza" in f["words"] for f in finestre))

    def test_conteggio_iniziale_non_diventa_ultima_ancora(self) -> None:
        parole: list[Word] = [
            {"word": "le", "start": 9.0},
            {"word": "15", "start": 9.5},
            {"word": "slide", "start": 10.0},
            {"word": "del", "start": 10.5},
            {"word": "documento", "start": 11.0},
        ]
        self.assertEqual(out_of_range_tail_anchor(parole, 14, {3: 5.0}), {})

    def test_coda_con_poche_ancore(self) -> None:
        parole: list[Word] = [
            {"word": "slide", "start": 900.0},
            {"word": "15", "start": 900.5},
        ]
        self.assertEqual(
            out_of_range_tail_anchor(parole, 14, {4: 30.6}),
            {14: 900.5},
        )


if __name__ == "__main__":
    unittest.main()
