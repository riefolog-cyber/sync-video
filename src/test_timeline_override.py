#!/usr/bin/env python3
"""Test delle correzioni manuali della timeline.

Il valore di questo modulo e' il RIFIUTO: un override sbagliato deve fermare la
run con un messaggio che dice cosa fare, non produrre un video con due slide
insieme. Meta' dei test qui sotto riguarda gli errori.

Esegui con: python -m unittest test_timeline_override -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from timeline_override import (
    aggiorna_file,
    applica_override,
    fissaggi_da_voci,
    leggi_override,
    override_su_ancore,
)


class TestLettura(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _file(self, contenuto: str) -> Path:
        path = self.base / "timeline_override.json"
        path.write_text(contenuto, encoding="utf-8")
        return path

    def test_oggetto_con_chiavi_stringa(self) -> None:
        path = self._file(json.dumps({"5": 252.0, "9": 431.5}))
        self.assertEqual(leggi_override(path), {5: 252.0, 9: 431.5})

    def test_lista_copiata_da_timeline_finale(self) -> None:
        path = self._file(json.dumps([{"slide": 5, "start": 252}, {"slide": 9, "start": 431.5}]))
        self.assertEqual(leggi_override(path), {5: 252.0, 9: 431.5})

    def test_file_vuoto_e_un_errore_dichiarato(self) -> None:
        with self.assertRaises(ValueError):
            leggi_override(self._file("{}"))
        with self.assertRaises(ValueError):
            leggi_override(self._file("[]"))

    def test_json_rotto_dice_dove(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            leggi_override(self._file("{5: 252"))
        self.assertIn("riga", str(ctx.exception))

    def test_chiave_non_numerica(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            leggi_override(self._file(json.dumps({"cinque": 252})))
        self.assertIn("cinque", str(ctx.exception))

    def test_valore_non_numerico(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            leggi_override(self._file(json.dumps({"5": "presto"})))
        self.assertIn("slide 5", str(ctx.exception))

    def test_chiave_ripetuta_una_volta_normalizzata(self) -> None:
        # "5" e "05" sono la stessa slide: l'ultima vincerebbe in silenzio.
        path = self._file('{"5": 10.0, "05": 20.0}')
        with self.assertRaises(ValueError) as ctx:
            leggi_override(path)
        self.assertIn("piu' di una volta", str(ctx.exception))

    def test_lista_senza_start(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            leggi_override(self._file(json.dumps([{"slide": 5}])))
        self.assertIn("manca", str(ctx.exception))

    def test_lista_con_slide_frazionaria_non_tronca(self) -> None:
        # `int(_numero(...))` trasformava 5.7 in 5: l'override finiva sulla
        # slide sbagliata senza che nessuno lo dicesse.
        with self.assertRaises(ValueError) as ctx:
            leggi_override(self._file(json.dumps([{"slide": 5.7, "start": 252}])))
        self.assertIn("intero", str(ctx.exception))
        self.assertIn("slide", str(ctx.exception))

    def test_tipo_sbagliato_in_cima(self) -> None:
        with self.assertRaises(ValueError):
            leggi_override(self._file('"slide 5"'))


class TestFissaggi(unittest.TestCase):
    """Il formato di --fix-slide: un errore qui deve dire cosa scrivere."""

    def test_voci_valide_con_spazi(self) -> None:
        self.assertEqual(fissaggi_da_voci(["5=252.0", " 9 = 431.25 "]), {5: 252.0, 9: 431.25})

    def test_voce_senza_uguale(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            fissaggi_da_voci(["5-252"])
        self.assertIn("slide=secondi", str(ctx.exception))

    def test_slide_non_numerica(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            fissaggi_da_voci(["cinque=252"])
        self.assertIn("numero di slide", str(ctx.exception))

    def test_slide_indicata_due_volte(self) -> None:
        with self.assertRaises(ValueError):
            fissaggi_da_voci(["5=10", "5=20"])

    def test_slide_frazionaria_rifiutata(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            fissaggi_da_voci(["5.7=252"])
        self.assertIn("intero", str(ctx.exception))
        self.assertIn("slide", str(ctx.exception))


class TestAggiornamentoFile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "timeline_override.json"

    def test_crea_il_file_e_poi_aggiunge(self) -> None:
        aggiorna_file(self.path, {5: 252.0})
        aggiorna_file(self.path, {9: 431.25})
        # Aggiungere non cancella: le due correzioni convivono.
        self.assertEqual(leggi_override(self.path), {5: 252.0, 9: 431.25})

    def test_la_stessa_slide_viene_sovrascritta(self) -> None:
        aggiorna_file(self.path, {5: 252.0})
        aggiorna_file(self.path, {5: 260.0})
        self.assertEqual(leggi_override(self.path), {5: 260.0})

    def test_il_file_prodotto_e_rileggibile(self) -> None:
        aggiorna_file(self.path, {5: 252.0, 9: 431.25})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"5": 252.0, "9": 431.25})


class TestApplicazione(unittest.TestCase):
    TIMELINE: ClassVar[dict[int, float]] = {1: 0.0, 2: 76.0, 3: 132.0, 4: 220.0}

    def test_override_sposta_solo_la_slide_indicata(self) -> None:
        nuova, mosse = applica_override(self.TIMELINE, {3: 140.0}, 921.0, 3.0)
        self.assertEqual(nuova[3], 140.0)
        self.assertEqual(nuova[2], 76.0)
        self.assertEqual(nuova[4], 220.0)
        self.assertEqual(mosse, [(3, 132.0, 140.0)])

    def test_la_timeline_originale_non_viene_toccata(self) -> None:
        originale = dict(self.TIMELINE)
        applica_override(self.TIMELINE, {3: 140.0}, 921.0, 3.0)
        self.assertEqual(self.TIMELINE, originale)

    def test_senza_override_resta_com_e(self) -> None:
        nuova, mosse = applica_override(self.TIMELINE, {}, 921.0, 3.0)
        self.assertEqual(nuova, self.TIMELINE)
        self.assertEqual(mosse, [])

    def test_valore_uguale_non_e_uno_spostamento(self) -> None:
        _, mosse = applica_override(self.TIMELINE, {3: 132.0}, 921.0, 3.0)
        self.assertEqual(mosse, [])

    def test_la_slide_1_non_si_sposta(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            applica_override(self.TIMELINE, {1: 5.0}, 921.0, 3.0)
        self.assertIn("slide 1", str(ctx.exception))

    def test_slide_inesistente(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            applica_override(self.TIMELINE, {9: 100.0}, 921.0, 3.0)
        self.assertIn("non esiste", str(ctx.exception))

    def test_tempo_fuori_dall_audio(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            applica_override(self.TIMELINE, {2: 1000.0}, 921.0, 3.0)
        self.assertIn("fuori dall'audio", str(ctx.exception))

    def test_ordine_invertito_viene_rifiutato(self) -> None:
        # 200s metterebbe la slide 2 dopo la 3 (132s): il video andrebbe indietro.
        with self.assertRaises(ValueError) as ctx:
            applica_override(self.TIMELINE, {2: 200.0}, 921.0, 3.0)
        self.assertIn("finirebbe dopo", str(ctx.exception))

    def test_durata_sotto_il_minimo_viene_rifiutata(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            applica_override(self.TIMELINE, {3: 219.0}, 921.0, 3.0)
        self.assertIn("sotto il minimo", str(ctx.exception))

    def test_ultima_slide_sotto_il_minimo_viene_rifiutata(self) -> None:
        # L'ultima slide non ha un successore: senza il controllo sulla coda
        # dell'audio poteva restare 1,5s e il video chiudersi subito dopo.
        with self.assertRaises(ValueError) as ctx:
            applica_override(self.TIMELINE, {4: 919.5}, 921.0, 3.0)
        self.assertIn("sotto il minimo", str(ctx.exception))
        self.assertIn("slide 4", str(ctx.exception))

    def test_ultima_slide_esattamente_al_minimo_e_accettata(self) -> None:
        nuova, _ = applica_override(self.TIMELINE, {4: 918.0}, 921.0, 3.0)
        self.assertEqual(nuova[4], 918.0)

    def test_timeline_vuota_dice_cosa_fare(self) -> None:
        # Con nessuna slide il vecchio `max(nuove)` sollevava
        # "max() arg is an empty sequence", che non dice ne' cosa e' successo
        # ne' cosa correggere. L'errore deve parlare di timeline vuota.
        with self.assertRaises(ValueError) as ctx:
            applica_override({}, {2: 10.0}, 100.0, 3.0)
        self.assertIn("vuota", str(ctx.exception))
        self.assertIn("override", str(ctx.exception))

    def test_le_ancore_pronunciate_vengono_segnalate(self) -> None:
        _, mosse = applica_override(self.TIMELINE, {3: 140.0}, 921.0, 3.0)
        self.assertEqual(mosse, [(3, 132.0, 140.0)])
        self.assertEqual(override_su_ancore([(2, 76.0, 80.0)], {2: 76.0}), [2])
        self.assertEqual(override_su_ancore([(3, 132.0, 140.0)], {2: 76.0}), [])
        self.assertEqual(override_su_ancore([(3, 132.0, 140.0)], None), [])


if __name__ == "__main__":
    unittest.main()
