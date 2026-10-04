"""Verifica della PRIMA sezione: l'unico pezzo di video senza controlli.

La slide 1 parte a 0.0s, quindi non è una transizione e nessuna ancora la
vincola; il confronto di contenuto esiste solo per i segmenti con durata
anomala. Con il flusso slide -> podcast ogni pagina ha una sezione (una sezione
per pagina, con l'annuncio solo dalla seconda in poi), quindi l'attesa è
definita e il controllo ha senso.

I testi sono quelli reali del run del 04/10: la pagina 1 e le prime frasi del
conduttore. Non sono una prova sui file originali (le cache sono state
eliminate), ma la prova che il controllo, applicato a quei testi, dà la
risposta che sappiamo essere giusta.
"""

import unittest
from typing import ClassVar

import main

SLIDE1 = (
    "L'esperienza della perdita Cartografia dell'assenza e filosofia della "
    "finitudine Joan-Carles Melich"
)
SLIDE2 = "1. Portico: La condizione umana e il nomadismo Ordine (Creazione/Armonia)"
SLIDE3 = "2. Finitudine ed esistenza: Grammatica contro Vita"
SLIDE4 = "3. Una filosofia letteraria: Smantellare l'Assoluto"

INTRO_REALE = (
    "Ciao ragazzi e benvenuti a questo nuovo profondimento. Oggi, insomma, ci "
    "tuffiamo in un libro davvero pazzesco. L'esperienza della perdita del "
    "filosofo Juan Carlos Melich. Non stiamo per fare un inventario sulla "
    "tristezza. La missione di oggi e' mappare quelle sensazioni di mancanze "
    "vuoto che tutti noi proviamo."
)
INTRO_PAGINA2 = (
    "Portico: la condizione umana e il nomadismo. Melik dice che non siamo mai "
    "pienamente presenti o sistemati da qualche parte, e che l'ordine e la "
    "creazione sono solo armonia apparente davanti a una vita disordinata."
)
INTRO_STRANIO = (
    "Benvenuti. Prima di cominciare voglio spiegare come funziona questo "
    "corso, le modalita' d'esame, i crediti formativi e il calendario delle "
    "lezioni pubblicato sul portale."
)


def _parla(frase, secondi):
    parole = frase.split()
    dur = secondi / max(len(parole), 1)
    return [{"word": p, "start": i * dur, "end": (i + 1) * dur}
            for i, p in enumerate(parole)]


class TestPrimaSezione(unittest.TestCase):
    DUR = 71.0
    SLIDE_TEXTS: ClassVar[list[str]] = [SLIDE1, SLIDE2, SLIDE3, SLIDE4]

    def _verdetto(self, introduzione):
        durations = [self.DUR, 60.0, 60.0, 60.0]
        return main._validate_anomalous_segments(
            [(0, 1, self.DUR)], self.SLIDE_TEXTS, _parla(introduzione, self.DUR), durations
        ).get(1)

    def test_audio_coerente_con_la_primapagina(self):
        # Il caso reale del 04/10: il conduttore apre parlando del libro e la
        # pagina 1 e' la copertina del libro. Qui il controllo deve TACERE.
        self.assertEqual(self._verdetto(INTRO_REALE), "coerente")

    def test_aperto_sulla_pagina_sbagliata_e_fuqariato(self):
        # Lo scenario pericoloso: l'audio parla nettamente della pagina 2
        # mentre il video apre con la pagina 1. Il verdetto deve essere netto,
        # altrimenti l'avviso non scatterebbe e il buco resterebbe aperto.
        self.assertEqual(self._verdetto(INTRO_PAGINA2), "disallineata")

    def test_audio_senza_riferimenti_non_provocca_allarmi(self):
        # Un'introduzione che non parla di nessuna pagina (crediti, esame) non
        # e' un errore di sincronizzazione: il verdetto resta incerto e non
        # parte nessun avviso. Un controllo che suona a caso peggiorerebbe la
        # situazione.
        self.assertEqual(self._verdetto(INTRO_STRANIO), "incerto")


if __name__ == "__main__":
    unittest.main()
