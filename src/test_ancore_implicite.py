#!/usr/bin/env python3
"""Le ancore senza numero: le regole che nessun esempio mostra.

"Passiamo alla prossima slide" diventa un'ancora con il numero implicito
("l'ultima menzionata + 1"). La funzione ha tre condizioni (aggettivo, parola
slide/pagina, verbo di transizione) e un vincolo forte: **non deve mai
spostare indietro** la numerazione. I test di `test_sync.py` coprono le frasi
vere; qui si controllano gli invarianti su casi costruiti e su sequenze
casuali, che e' dove un ritocco alla grammatica puo' rompere in silenzio.
"""

from __future__ import annotations

import random
import unittest

from timeline import _risolvi_mentioni_implicite

# Un evento e' (tempo, e' un conteggio): il conteggio non sposta il "numero
# implicito" successivo.
Eventi = list[tuple[float, bool]]


def _risolvi(
    kinds: dict[int, Eventi], implicite: Eventi, total: int
) -> dict[int, list[tuple[float, bool]]]:
    return _risolvi_mentioni_implicite({k: list(v) for k, v in kinds.items()}, implicite, total)


class TestNumeroImplicito(unittest.TestCase):
    def test_una_menzione_implicita_prende_l_ultima_piu_uno(self) -> None:
        risolte = _risolvi({3: [(10.0, False)]}, [(20.0, False)], 10)
        self.assertEqual(sorted(risolte), [3, 4])
        self.assertIn((20.0, False), risolte[4])

    def test_oltre_l_ultima_pagina_non_aggiunge_nulla(self) -> None:
        risolte = _risolvi({3: [(10.0, False)]}, [(20.0, False)], 3)
        self.assertEqual(sorted(risolte), [3])

    def test_senza_una_menzione_precedente_non_aggiunge_nulla(self) -> None:
        risolte = _risolvi({}, [(20.0, False)], 10)
        self.assertEqual(risolte, {})

    def test_un_richiamo_non_fa_impazzire_il_numero_implicito(self) -> None:
        # 1, 2, poi un RICHIAMO della 1, poi "prossima": deve valere 3.
        risolte = _risolvi({1: [(10.0, False), (30.0, False)], 2: [(20.0, False)]}, [(40.0, False)], 10)
        self.assertEqual(sorted(risolte), [1, 2, 3])

    def test_un_conteggio_non_sposta_il_numero_implicito(self) -> None:
        # "le 13 slide di questo documento" (conteggio) non e' una transizione.
        risolte = _risolvi({2: [(10.0, False)]}, [(20.0, False)], 10)
        self.assertIn(3, risolte)
        # Il conteggio non entra nelle menzioni usate.
        self.assertNotIn(10, risolte)

    def test_i_numeri_assegnati_restano_dentro_il_deck(self) -> None:
        rng = random.Random(7)
        for _ in range(300):
            total = rng.randint(3, 12)
            kinds: dict[int, Eventi] = {}
            for _ in range(rng.randint(0, 6)):
                kinds.setdefault(rng.randint(1, total), []).append((rng.uniform(0, 100), False))
            implicite: Eventi = [
                (rng.uniform(0, 100), rng.random() < 0.2) for _ in range(rng.randint(0, 6))
            ]
            risolte = _risolvi(kinds, implicite, total)
            self.assertTrue(all(1 <= n <= total for n in risolte), risolte)
            self.assertTrue(all(v for v in risolte.values()), "nessuna lista vuota")
            # Nessun evento e' stato inventato: le menzioni esplicite restano.
            for numero, eventi in kinds.items():
                for evento in eventi:
                    self.assertIn(evento, risolte[numero])


if __name__ == "__main__":
    unittest.main()
