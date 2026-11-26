#!/usr/bin/env python3
"""Qualita' del file .capitoli.txt generato dalle slide e dal parlato.

Esegui con: python -m unittest test_capitoli_qualita -v
"""

from __future__ import annotations

import unittest

import subtitles


class TestTitoloDallInizioDellaSlide(unittest.TestCase):
    def test_non_prende_un_frammento_del_corpo(self) -> None:
        testo = (
            "È LCUAIZIOI TA LEGGE 7 I pregiudizi nell’interpretazione delle leggi "
            "Il Fatto: La giustizia vive nelle parole | fatti. Il Problema: Il patriarcato"
        )
        titolo = subtitles._titolo_capitolo(testo, 1)
        self.assertEqual(titolo, "I pregiudizi nell’interpretazione delle leggi")
        self.assertNotIn("fatti", titolo)
        self.assertNotIn("LCUAIZIOI", titolo)

    def test_rimuove_garbage_iniziale_e_numero_di_pagina(self) -> None:
        self.assertEqual(
            subtitles._titolo_capitolo("muss. e ion. = La comprensibilità umana dei motivi Il Caso", 4),
            "La comprensibilità umana dei motivi",
        )
        self.assertEqual(
            subtitles._titolo_capitolo("15 La via d’uscita: Dallo stereotipo alla competenza", 15),
            "La via d’uscita",
        )

    def test_ricongiunge_le_colonne_spezzate_da_pipe(self) -> None:
        testo = "Lo Stato che non prende | sul serio il rischio Il Caso (Talpis contro Italia)"
        self.assertEqual(
            subtitles._titolo_capitolo(testo, 13),
            "Lo Stato che non prende sul serio il rischio",
        )

    def test_taglia_all_intestazione_di_sezione(self) -> None:
        testo = "La vittima processata al posto degli imputati Il Caso (J.L. contro Italia)"
        self.assertEqual(
            subtitles._titolo_capitolo(testo, 6),
            "La vittima processata al posto degli imputati",
        )

    def test_non_lascia_virgolette_aperte(self) -> None:
        titolo = subtitles._titolo_capitolo("Quando la violenza viene chiamata ‘Conflitto’ Il Contesto", 10)
        self.assertNotIn("‘", titolo)
        self.assertIn("Conflitto", titolo)


class TestFonteParlato(unittest.TestCase):
    def test_annuncio_sostituisce_ocr_inaffidabile(self) -> None:
        timeline = {1: 0.0, 2: 50.0}
        slide_texts = ["Titolo buono", "Int. xcepiro a ditto legali, menno la violenza domestica"]
        parole = [
            {"word": "Passiamo", "start": 48.0},
            {"word": "alla", "start": 48.4},
            {"word": "slide", "start": 48.8},
            {"word": "2.", "start": 49.0},
            {"word": "Vediamo", "start": 50.2},
            {"word": "un", "start": 50.5},
            {"word": "attacco", "start": 50.8},
            {"word": "diretto", "start": 51.1},
            {"word": "alla", "start": 51.4},
            {"word": "prospettiva", "start": 51.7},
            {"word": "di", "start": 52.0},
            {"word": "genere,", "start": 52.3},
            {"word": "un", "start": 52.6},
            {"word": "altro", "start": 52.9},
        ]
        capitoli = subtitles.build_capitoli(timeline, slide_texts, words=parole)
        self.assertIn("0:00 Titolo buono", capitoli)
        self.assertIn("0:50 un attacco diretto alla prospettiva di genere\n", capitoli + "\n")
        self.assertNotIn("un altro", capitoli)
        self.assertNotIn("xcepiro", capitoli)

    def test_narrazione_non_sostituisce_un_titolo_buono(self) -> None:
        timeline = {1: 0.0, 2: 40.0}
        slide_texts = ["Il DNA culturale del problema", "Un problema strutturale"]
        parole = [
            {"word": "Benvenuti", "start": 0.0},
            {"word": "ragazzi", "start": 0.4},
            {"word": "oggi", "start": 0.8},
            {"word": "parliamo", "start": 1.2},
            {"word": "di", "start": 1.6},
            {"word": "legge", "start": 2.0},
            {"word": "Passiamo", "start": 38.0},
            {"word": "alla", "start": 38.4},
            {"word": "slide", "start": 38.8},
            {"word": "2.", "start": 39.0},
            {"word": "Il", "start": 40.5},
            {"word": "problema", "start": 40.9},
        ]
        capitoli = subtitles.build_capitoli(timeline, slide_texts, words=parole)
        self.assertIn("0:00 Il DNA culturale del problema", capitoli)
        self.assertNotIn("Benvenuti", capitoli)


class TestRegoleStoricheNonRotte(unittest.TestCase):
    def test_titolo_breve_reale_non_diventa_slide(self) -> None:
        self.assertEqual(subtitles._titolo_capitolo("Prima", 1), "Prima")
        self.assertEqual(subtitles._titolo_capitolo("Terza", 3), "Terza")

    def test_titolo_sulla_seconda_riga_non_perso(self) -> None:
        testo = "+ ——-__ 87% 7 >90% Raffinazione\nIl Fisiologico 10%"
        self.assertEqual(subtitles.build_capitoli({1: 0.0}, [testo]), "0:00 Il Fisiologico 10%")

    def test_nome_proprio_non_taglia_titolo(self) -> None:
        self.assertEqual(
            subtitles._titolo_capitolo("LI illusione del Risotto - Menù degustazione stellare.", 1),
            "illusione del Risotto",
        )

    def test_titolo_tecnico_in_testa_non_rimosso(self) -> None:
        self.assertEqual(subtitles._titolo_capitolo("SPRING BOOT: cosa cambia", 3), "SPRING BOOT")
        self.assertEqual(subtitles._titolo_capitolo("STRUTTURE", 3), "STRUTTURE")

    def test_parola_di_testo_non_scambiata_per_rumore(self) -> None:
        self.assertEqual(subtitles._titolo_capitolo("Fatti e misfatti", 1), "Fatti e misfatti")
        self.assertEqual(subtitles._titolo_capitolo("Bene comune", 1), "Bene comune")

    def test_sigla_e_x_legittime_non_sostituite(self) -> None:
        parole = [{"word": "buongiorno", "start": float(i)} for i in range(3)]
        for titolo, atteso in (("Art. 4 La prova", "Art. 4"), ("Xenofobia e pregiudizi", "Xenofobia")):
            with self.subTest(titolo=titolo):
                capitoli = subtitles.build_capitoli({1: 0.0}, [titolo], words=parole)
                self.assertIn(atteso, capitoli)
                self.assertNotIn("buongiorno", capitoli)

    def test_typo_corretta_con_annuncio_e_con_frase_lunga(self) -> None:
        parole = [
            {"word": "Passiamo", "start": 0.0},
            {"word": "alla", "start": 0.2},
            {"word": "slide", "start": 0.4},
            {"word": "1.", "start": 0.6},
            {"word": "Vediamo", "start": 0.8},
            {"word": "la", "start": 1.0},
            {"word": "curiosita", "start": 1.2},
            {"word": "anche", "start": 1.4},
            {"word": "se", "start": 1.6},
            {"word": "lunga", "start": 1.8},
            {"word": "la", "start": 2.0},
            {"word": "frase", "start": 2.2},
            {"word": "continua", "start": 2.4},
        ]
        capitoli = subtitles.build_capitoli({1: 0.0}, ["la cunosita'"], words=parole)
        self.assertIn("curiosita", capitoli)
        self.assertNotIn("cunosita", capitoli)

    def test_annuncio_non_prende_la_slide_successiva(self) -> None:
        parole = [
            {"word": "Nel", "start": 0.0},
            {"word": "caso", "start": 0.3},
            {"word": "nostro", "start": 0.6},
            {"word": "Passiamo", "start": 58.0},
            {"word": "alla", "start": 58.3},
            {"word": "slide", "start": 58.6},
            {"word": "2.", "start": 58.9},
            {"word": "il", "start": 59.2},
            {"word": "Titanic", "start": 59.5},
        ]
        capitoli = subtitles.build_capitoli(
            {1: 0.0, 2: 60.0}, ["xcepiro rumore", "Altro"], words=parole
        )
        self.assertEqual(capitoli.splitlines()[0], "0:00 Nel caso nostro")

    def test_rumore_senza_trascrizione_non_diventa_titolo(self) -> None:
        self.assertEqual(subtitles.build_capitoli({1: 0.0}, ["a b c d e f g"]), "0:00 Slide 1")
        self.assertEqual(subtitles.build_capitoli({1: 0.0}, ["Titolo della slide — sottotitolo lungo"]),
                         "0:00 Titolo della slide")


class TestTitoliRiassuntivi(unittest.TestCase):
    def test_il_caso_diventa_parte_del_titolo(self) -> None:
        testo = (
            "L'uomo travolto: Lo stereotipo della «Tempesta Emotiva» "
            "Il Caso (Castaldo/Matei): Un uomo strangola la compagna."
        )
        capitoli = subtitles.build_capitoli({1: 0.0}, [testo])
        self.assertEqual(capitoli, "0:00 L'uomo travolto: caso Castaldo/Matei")

    def test_sentenza_del_corpo_nel_titolo(self) -> None:
        testo = "Il DNA culturale del problema: La «Sentenza dei Jeans» L'Antecedente (1998)"
        capitoli = subtitles.build_capitoli({1: 0.0}, [testo])
        self.assertEqual(capitoli, "0:00 Il DNA culturale del problema: sentenza dei jeans")

    def test_frase_generica_non_ripete_il_titolo(self) -> None:
        testo = "La commissione parlamentare: Commissione Parlamentare"
        # Il candidato esiste ed e' abbastanza lungo: e' la dedup a scartarlo.
        self.assertEqual(subtitles._sintesi_da_slide(testo, ""), "commissione parlamentare")
        self.assertEqual(
            subtitles.build_capitoli({1: 0.0}, [testo]),
            "0:00 La commissione parlamentare",
        )

    def test_cognome_breve_entra_nella_sintesi(self) -> None:
        testo = "Gli abusi nella scuola Il Caso (Rossi): Un uomo aggredisce la moglie."
        self.assertEqual(
            subtitles.build_capitoli({1: 0.0}, [testo]), "0:00 Gli abusi nella scuola: caso Rossi"
        )

    def test_caso_con_due_punti_lungo_non_perde_la_sintesi(self) -> None:
        testo = (
            "Il ‘Rapporto Altalenante'’ non è simmetrico | Il Caso: La Cassazione "
            "annulla l'assoluzione d'appello che definiva anni di abusi."
        )
        capitoli = subtitles.build_capitoli({1: 0.0}, [testo])
        self.assertIn("cassazione", capitoli.lower())

    def test_sintesi_non_lascia_virgolette_aperte(self) -> None:
        testo = ("Il corpo della donna come argomento giudiziario "
                 "Il Caso (La ‘scaltra peruviana’)")
        titolo = subtitles.build_capitoli({1: 0.0}, [testo]).split(" ", 1)[1]
        self.assertEqual(titolo.count("‘"), titolo.count("’"))

    def test_ellissi_non_rimasta_in_mezzo_al_titolo(self) -> None:
        testo = " ".join(["parola"] * 12) + " Il Caso (Castaldo/Matei)"
        titolo = subtitles.build_capitoli({1: 0.0}, [testo]).split(" ", 1)[1]
        self.assertNotIn("…:", titolo)
        self.assertLessEqual(len(titolo), subtitles.MAX_CARATTERI_CAPITOLO)

    def test_annuncio_lungo_rientra_nel_limite_youtube(self) -> None:
        parole = [
            {"word": "Passiamo", "start": 0.0},
            {"word": "alla", "start": 0.1},
            {"word": "slide", "start": 0.2},
            {"word": "1.", "start": 0.3},
        ]
        parole += [
            {"word": "argomento", "start": 0.4 + i * 0.1} for i in range(14)
        ]
        titolo = subtitles.build_capitoli({1: 0.0}, ["a b c d e f g"], words=parole).split(" ", 1)[1]
        self.assertLessEqual(len(titolo), subtitles.MAX_CARATTERI_CAPITOLO)
        self.assertTrue(titolo.endswith("…"))

    def test_senza_elemento_riassuntivo_il_titolo_resta(self) -> None:
        self.assertEqual(subtitles.build_capitoli({1: 0.0}, ["Titolo semplice"]), "0:00 Titolo semplice")

    def test_titolo_riassuntivo_rientra_nel_limite_youtube(self) -> None:
        self.assertEqual(subtitles.MAX_CARATTERI_CAPITOLO, 100)
        titolo = (
            "La vittima processata al posto degli imputati davanti "
            "alla corte europea dei diritti umani"
        )
        out = subtitles._titolo_riassuntivo(titolo, "Il Caso (J.L. contro Italia)")
        self.assertEqual(
            out,
            "La vittima processata al posto degli imputati davanti "
            "alla corte europea: caso J.L. contro Italia",
        )
        self.assertLessEqual(len(out), 100)
        self.assertFalse(out.endswith("…"))

    def test_altri_motivi_di_sintesi(self) -> None:
        self.assertEqual(
            subtitles.build_capitoli({1: 0.0}, ["15 La via d’uscita: Dallo stereotipo alla competenza"]),
            "0:00 La via d’uscita: dallo stereotipo alla competenza",
        )
        self.assertIn(
            "tribunale di roma",
            subtitles.build_capitoli({1: 0.0}, ["Un tema Il Fatto (Tribunale di Roma)"]).lower(),
        )


class TestFormatoFile(unittest.TestCase):
    def test_tempi_ascendenti_e_primo_a_zero(self) -> None:
        timeline = {1: 12.0, 2: 40.0, 3: 90.0}
        righe = subtitles.build_capitoli(timeline, ["Uno", "Due", "Tre"]).splitlines()
        self.assertTrue(righe[0].startswith("0:00 "))
        secondi = []
        for riga in righe:
            parti = [int(p) for p in riga.split(" ", 1)[0].split(":")]
            secondi.append(parti[0] * 60 + parti[1] if len(parti) == 2 else parti[0])
        self.assertEqual(secondi, sorted(secondi))
        self.assertEqual(len(righe), 3)


if __name__ == "__main__":
    unittest.main()
