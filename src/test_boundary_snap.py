#!/usr/bin/env python3
"""Test dell'aggancio dei confini stimati a pause e confini di parola.

Tre regole vanno difese, e sono queste le cose che si rompono in silenzio:
le ancore pronunciate non si toccano, un confine lontano da ogni pausa resta
dov'e', e nessuno spostamento puo' rendere illeggibile una slide.

Esegui con: python -m unittest test_boundary_snap -v
"""

import unittest

import boundary_snap
from boundary_snap import aggancia_confini, confini_disponibili


def _parole(coppie: list[tuple[str, float]]) -> list[dict[str, object]]:
    return [{"word": testo, "start": inizio} for testo, inizio in coppie]


class TestConfiniDisponibili(unittest.TestCase):
    def test_la_pausa_e_il_centro_del_silenzio(self) -> None:
        pause, _ = confini_disponibili(_parole([("a", 0.0), ("b", 10.0)]))
        self.assertEqual(len(pause), 1)
        atteso = (10.0 - boundary_snap.DURATA_PAROLA_DEFAULT + 10.0) / 2
        self.assertAlmostEqual(pause[0], atteso)

    def test_parole_ravvicinate_non_sono_pause(self) -> None:
        pause, _ = confini_disponibili(_parole([("a", 0.0), ("b", 0.4), ("c", 0.8)]))
        self.assertEqual(pause, [])

    def test_parola_senza_tempo_non_crea_una_pausa_fantasma(self) -> None:
        # Una voce senza "start" (trascrizione malformata) non e' una parola a
        # zero: se contasse, fra lei e la prima parola vera comparirebbe un
        # silenzio inesistente, e un confine potrebbe essere attirato li'.
        parole: list[dict[str, object]] = [
            {"word": "fantasma"},
            {"word": "a", "start": 10.0},
            {"word": "b", "start": 10.4},
        ]
        pause, _ = confini_disponibili(parole)
        self.assertEqual(pause, [])

    def test_gli_estremi_comprendono_la_fine_dell_ultima_parola(self) -> None:
        _, estremi = confini_disponibili(_parole([("a", 0.0), ("b", 0.4)]))
        self.assertEqual(estremi[0], 0.0)
        self.assertAlmostEqual(estremi[-1], 0.4 + boundary_snap.DURATA_PAROLA_DEFAULT)


class TestAggancio(unittest.TestCase):
    def test_confine_vicino_a_una_pausa_ci_si_sposta(self) -> None:
        parole = _parole([("a", 0.0), ("b", 20.0), ("c", 40.0)])
        nuova, mosse = aggancia_confini({1: 0.0, 2: 20.6}, {}, parole, min_secondi=3.0)
        self.assertEqual(mosse[0][3], "pausa")
        self.assertAlmostEqual(nuova[2], 19.85)

    def test_ancora_pronunciata_non_si_tocca(self) -> None:
        parole = _parole([("a", 0.0), ("b", 20.0), ("c", 40.0)])
        timeline = {1: 0.0, 2: 20.6}
        nuova, mosse = aggancia_confini(timeline, {2: 20.6}, parole, min_secondi=3.0)
        self.assertEqual(nuova, timeline)
        self.assertEqual(mosse, [])

    def test_confine_dentro_una_parola_va_al_bordo_piu_vicino(self) -> None:
        # Nessuna pausa (parole ravvicinate) e confine a meta' parola: 5.2 sta
        # dentro la parola che parte a 5.0 e finisce a 5.3.
        parole = _parole([("a", 5.0), ("b", 5.3), ("c", 5.6)])
        nuova, mosse = aggancia_confini({1: 0.0, 2: 5.2}, {}, parole, min_secondi=3.0)
        self.assertEqual(mosse[0][3], "parola")
        self.assertAlmostEqual(nuova[2], 5.3)

    def test_confine_gia_su_un_confine_di_parola_resta_dov_e(self) -> None:
        parole = _parole([("a", 5.0), ("b", 5.3), ("c", 5.6)])
        timeline = {1: 0.0, 2: 5.3}
        nuova, mosse = aggancia_confini(timeline, {}, parole, min_secondi=3.0)
        self.assertEqual(nuova, timeline)
        self.assertEqual(mosse, [])

    def test_confine_lontano_da_ogni_pausa_non_si_sposta(self) -> None:
        # La pausa e' a 19.85; il confine a 30.0 non ne ha una entro 1.5s.
        parole = _parole([("a", 0.0), ("b", 20.0), ("c", 20.5)])
        timeline = {1: 0.0, 2: 30.0, 3: 40.0}
        nuova, mosse = aggancia_confini(timeline, {}, parole, min_secondi=3.0)
        self.assertEqual(nuova, timeline)
        self.assertEqual(mosse, [])

    def test_non_rende_illeggibile_la_slide_successiva(self) -> None:
        parole = _parole([("a", 0.0), ("b", 10.0), ("c", 11.0)])
        timeline = {1: 0.0, 2: 9.5, 3: 12.0}
        nuova, mosse = aggancia_confini(timeline, {}, parole, min_secondi=3.0)
        # Agganciare la slide 2 a 9.85 lascerebbe 2.15s alla 3: si salta.
        self.assertEqual(nuova, timeline)
        self.assertEqual(mosse, [])

    def test_senza_parole_la_timeline_resta_com_e(self) -> None:
        timeline = {1: 0.0, 2: 5.0}
        nuova, mosse = aggancia_confini(timeline, {}, [], min_secondi=1.0)
        self.assertEqual(nuova, timeline)
        self.assertEqual(mosse, [])

    def test_una_sola_slide_non_ha_confini(self) -> None:
        nuova, mosse = aggancia_confini({1: 0.0}, {}, _parole([("a", 0.0)]))
        self.assertEqual(nuova, {1: 0.0})
        self.assertEqual(mosse, [])

    def test_la_slide_1_non_e_un_confine_da_agganciare(self) -> None:
        # Il video comincia con la slide 1: nessuna mossa puo' riguardarla.
        parole = _parole([("a", 0.0), ("b", 20.0)])
        _nuova, mosse = aggancia_confini({1: 0.0, 2: 20.0}, {}, parole, min_secondi=1.0)
        self.assertEqual([s for s, *_ in mosse if s == 1], [])


if __name__ == "__main__":
    unittest.main()
