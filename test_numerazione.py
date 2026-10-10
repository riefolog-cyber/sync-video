"""Il numero stampato sulla slide non coincide con la sua posizione.

Caso reale (deck del 04/10): copertina non numerata in testa, poi sezioni
numerate 1..11 sulle pagine fisiche 2..12. Il video era sincronizzato al
nallisecondo ma sullo schermo si leggeva "1." mentre il podcast diceva "slide 2",
e l'utente lo aveva notato a occhio. Il programma non lo diceva.
"""

import unittest
from typing import cast

import main


class TestNumerazioneStampata(unittest.TestCase):
    def test_copertina_non_numerata_viene_segnalata(self) -> None:
        slide_texts = [
            "L'esperienza della perdita - Cartografia dell'assenza",  # copertina
            "1. Portico: la condizione umana",
            "2. Finitudine ed esistenza",
            "3. Poetica del ricordo",
        ]
        avviso = main._check_numerazione_stampata(slide_texts)
        self.assertIsNotNone(avviso)
        # Lo scarto e' -1 e va detto con il segno, per non doverlo reindovinare.
        self.assertIn("-1", cast(str, avviso))
        self.assertIn("copertina", cast(str, avviso))

    def test_sfasamento_positivo(self) -> None:
        # Numerazione che parte da 2: stesso problema, segno opposto.
        avviso = main._check_numerazione_stampata(["2. Uno", "3. Due", "4. Tre"])
        self.assertIsNotNone(avviso)
        self.assertIn("+1", cast(str, avviso))

    def test_numerazione_coerente_non_avvisa(self) -> None:
        self.assertIsNone(
            main._check_numerazione_stampata(["1. Uno", "2. Due", "3. Tre"])
        )

    def test_deck_senza_numeri_non_avvisa(self) -> None:
        # Il caso normale di un deck non numerato: nessun numero da confrontare,
        # quindi non c'e' niente che non torna e niente da segnalare.
        self.assertIsNone(
            main._check_numerazione_stampata(["Portico", "Finitudine", "Sipario"])
        )

    def test_numerazione_irregolare_non_avvisa(self) -> None:
        # Non e' uno sfasamento sistematico: descriverlo come offset sarebbe
        # un'informazione sbagliata.
        self.assertIsNone(
            main._check_numerazione_stampata(["1. Uno", "9. Due", "3. Tre"])
        )

    def test_una_sola_slide_numerata_non_avvisa(self) -> None:
        # Con un solo numero non si distingue un offset da un caso isolato.
        self.assertIsNone(main._check_numerazione_stampata(["2. Uno", "Testo", "Altro"]))

    def test_il_controllo_non_tocca_i_tempi(self) -> None:
        # Il difetto e' solo di etichetta: la funzione non restituisce tempi ne'
        # segmenti, quindi non puo' spostare un taglio per sbaglio.
        slide_texts = ["Copertina", "1. Uno", "2. Due", "3. Tre"]
        self.assertIsInstance(main._check_numerazione_stampata(slide_texts), str)


class TestLetturaNumeroStampato(unittest.TestCase):
    def test_separatori_accettati(self) -> None:
        for sep in (".", ")", " -", ":"):
            with self.subTest(sep=sep):
                # Il numero va letto sulla terza pagina di un deck di tre: un
                # "3." su un deck di una sola pagina non può essere la pagina 3.
                testo = f"3{sep} Titolo"
                self.assertEqual(main._numeri_stampati(["a", "b", testo]), [None, None, 3])

    def test_anno_non_e_un_etichetta(self) -> None:
        # "2024. Lezioni" in cima a una pagina non e' il numero della slide: se
        # fosse accettato, un deck che parte dal 2024 sembrerebbe sfasato di
        # 2021 slide e l'avviso sarebbe rumore.
        self.assertEqual(main._numeri_stampati(["2024. Lezioni d'autunno"]), [None])

    def test_numero_troppo_grande_ignorato(self) -> None:
        self.assertEqual(main._numeri_stampati(["99. Troppo"]), [None])

    def test_titolo_senza_numero(self) -> None:
        self.assertEqual(main._numeri_stampati(["Sipario: l'ultimo abbraccio"]), [None])


if __name__ == "__main__":
    unittest.main()
