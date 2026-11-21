#!/usr/bin/env python3
"""Test dei sottotitoli SRT e dei capitoli.

Sono file di testo: un errore di formato non fa fallire niente, produce un .srt
che il player ignora o dei capitoli che YouTube rifiuta. Qui si guarda il
formato e le regole di leggibilita', non il contenuto delle frasi.

Esegui con: python -m unittest test_subtitles -v
"""

import itertools
import tempfile
import unittest
from pathlib import Path

import subtitles
from subtitles import (
    build_capitoli,
    build_srt,
    costruisci_cue,
    scrivi_sottotitoli,
    timestamp_capitolo,
    timestamp_srt,
)


def _parole(coppie: list[tuple[str, float]]) -> list[dict[str, object]]:
    return [{"word": testo, "start": inizio} for testo, inizio in coppie]


class TestTimestamp(unittest.TestCase):
    def test_srt_sempre_con_ore_e_millisecondi(self) -> None:
        self.assertEqual(timestamp_srt(0), "00:00:00,000")
        self.assertEqual(timestamp_srt(3661.5), "01:01:01,500")

    def test_arrotondamento_non_produce_1000_millesimi(self) -> None:
        # 59.9999 e' 1:00, non 00:00:59,1000: il formato deve reggere il riporto.
        self.assertEqual(timestamp_srt(59.9999), "00:01:00,000")

    def test_capitoli_senza_ore_finche_bastano_i_minuti(self) -> None:
        self.assertEqual(timestamp_capitolo(0), "0:00")
        self.assertEqual(timestamp_capitolo(61), "1:01")
        self.assertEqual(timestamp_capitolo(3725), "1:02:05")


class TestCue(unittest.TestCase):
    def test_frase_breve_resta_un_sottotitolo(self) -> None:
        cue = costruisci_cue(_parole([("Ciao", 0.0), ("a", 0.4), ("tutti", 0.6)]))
        self.assertEqual(len(cue), 1)
        self.assertEqual(cue[0][2], "Ciao a tutti")

    def test_riga_troppo_lunga_si_spezza(self) -> None:
        parole = _parole(
            [
                ("Il", 0.0),
                ("monopolio", 0.4),
                ("delle", 0.8),
                ("piattaforme", 1.1),
                ("e", 1.5),
                ("le", 1.7),
                ("economie", 1.9),
                ("di", 2.2),
                ("rete", 2.4),
            ]
        )
        cue = costruisci_cue(parole, max_caratteri=20)
        self.assertGreater(len(cue), 1)
        for _, _, testo in cue:
            self.assertLessEqual(len(testo), 20, testo)

    def test_sottotitolo_che_dura_troppo_si_chiude(self) -> None:
        parole = _parole([("uno", 0.0), ("due", 3.0), ("tre", 6.0), ("quattro", 9.0)])
        cue = costruisci_cue(parole, max_secondi=5.0)
        self.assertGreater(len(cue), 1)
        for inizio, fine, _ in cue:
            self.assertLessEqual(fine - inizio, 5.0 + 1e-9)

    def test_due_sottotitoli_non_si_sovrappongono(self) -> None:
        parole = _parole([(f"parola{i}", i * 0.7) for i in range(40)])
        cue = costruisci_cue(parole, max_caratteri=18)
        for (_, fine_prima, _), (inizio_dopo, _, _) in itertools.pairwise(cue):
            self.assertLessEqual(fine_prima, inizio_dopo + 1e-9)

    def test_fine_frase_chiude_la_riga_quando_e_gia_piena(self) -> None:
        parole = _parole(
            [
                ("Questa", 0.0),
                ("e", 0.3),
                ("la", 0.4),
                ("prima", 0.5),
                ("frase.", 0.9),
                ("Questa", 1.2),
                ("e", 1.4),
                ("la", 1.5),
                ("seconda.", 1.6),
            ]
        )
        cue = costruisci_cue(parole, max_caratteri=40)
        # Il punto non chiude una riga da tre parole, ma chiude questa.
        self.assertEqual(len(cue), 2)
        self.assertTrue(cue[0][2].endswith("frase."))

    def test_l_ultima_riga_ha_una_coda(self) -> None:
        parole = _parole([("Ultima", 10.0), ("parola", 10.4)])
        cue = costruisci_cue(parole)
        self.assertGreater(cue[-1][1] - cue[-1][0], 0.5)

    def test_un_silenzio_lungo_chiude_la_riga(self) -> None:
        # "end" dichiarata: dopo "prima" c'e' quasi un secondo di silenzio, e un
        # sottotitolo che resta a schermo mentre nessuno parla si legge come un
        # errore. La fine del cue resta l'inizio del parlato (1.5), mai oltre.
        parole: list[dict[str, object]] = [
            {"word": "prima", "start": 0.0, "end": 0.4},
            {"word": "dopo", "start": 1.5, "end": 1.8},
        ]
        cue = costruisci_cue(parole)
        self.assertEqual([testo for _, _, testo in cue], ["prima", "dopo"])
        self.assertAlmostEqual(cue[0][1], 1.5)

    def test_parole_ravvicinate_non_si_speziano_per_il_silenzio(self) -> None:
        cue = costruisci_cue(_parole([("a", 0.0), ("b", 0.2), ("c", 0.4)]))
        self.assertEqual(len(cue), 1)

    def test_riga_lunga_va_su_due_righe_bilanciate(self) -> None:
        parole = _parole(
            [
                ("Questa", 0.0),
                ("frase", 0.3),
                ("va", 0.5),
                ("spezzata", 0.7),
                ("in", 1.0),
                ("due", 1.2),
                ("righe", 1.4),
                ("pari", 1.6),
                ("davvero", 1.8),
            ]
        )
        cue = costruisci_cue(parole)
        righe = cue[0][2].split("\n")
        self.assertEqual(len(righe), 2)
        for riga in righe:
            self.assertLessEqual(len(riga), subtitles.MAX_CARATTERI_RIGA, riga)
        # Bilanciate: nessuna riga meno della meta' dell'altra.
        self.assertGreater(min(len(r) for r in righe), max(len(r) for r in righe) // 2)

    def test_parole_senza_tempo_o_vuote_si_scartano(self) -> None:
        parole: list[dict[str, object]] = [
            {"word": "  ", "start": 0.0},
            {"word": "senza-tempo"},
            {"word": "ok", "start": 1.0},
        ]
        cue = costruisci_cue(parole)
        self.assertEqual(len(cue), 1)
        self.assertEqual(cue[0][2], "ok")

    def test_senza_parole_non_ci_sono_sottotitoli(self) -> None:
        self.assertEqual(costruisci_cue([]), [])
        self.assertEqual(build_srt([]), "")


class TestFile(unittest.TestCase):
    def test_srt_numerato_e_separato_da_riga_vuota(self) -> None:
        testo = build_srt(_parole([("Ciao", 0.0), ("mondo", 0.5)]))
        righe = testo.split("\n")
        self.assertEqual(righe[0], "1")
        self.assertEqual(righe[1], "00:00:00,000 --> 00:00:01,300")
        self.assertEqual(righe[2], "Ciao mondo")
        self.assertEqual(righe[3], "")

    def test_la_coda_non_supera_la_durata_dell_audio(self) -> None:
        # L'ultimo cue ha una coda di 0.8s, ma se l'audio e' finito prima il
        # sottotitolo non deve restare a schermo dopo la fine del video.
        testo = build_srt(_parole([("Ultima", 0.5)]), durata=1.0)
        self.assertIn("00:00:00,500 --> 00:00:01,000", testo)

    def test_un_cue_denso_non_viene_spezzato_invano(self) -> None:
        # Parlato veloce: spezzare in proporzione lascerebbe 22 c/s in due cue
        # piu' corti, cioe' nessun miglioramento. Meglio un cue, onesto.
        parole = _parole(
            [("parolalunga", 0.0), ("parolalunga", 0.25), ("parolalunga", 0.5), ("parolalunga", 0.75)]
        )
        blocchi = [b for b in build_srt(parole).strip().split("\n\n") if b.strip()]
        self.assertEqual(len(blocchi), 1)

    def test_cue_lampo_si_unisce_al_successivo(self) -> None:
        # Primo cue da 0.9s (sotto il secondo): si legge male, quindi si unisce
        # al successivo invece di lampeggiare.
        parole: list[dict[str, object]] = [
            {"word": "ciao", "start": 0.0, "end": 0.1},
            {"word": "a", "start": 0.9, "end": 1.2},
        ]
        blocchi = [b for b in build_srt(parole).strip().split("\n\n") if b.strip()]
        self.assertEqual(len(blocchi), 1)
        self.assertIn("ciao a", blocchi[0])

    def test_il_srt_scrive_le_parole_che_riceve(self) -> None:
        # main assegna words_raw = correct_transcript_names(...) PRIMA di
        # chiamare qui: il modulo non deve rimetterci mano. Se qualcuno sposta
        # la correzione dopo la scrittura, i nomi sbagliati finiscono nel .srt.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "video_finale.mp4"
            srt, _ = scrivi_sottotitoli(
                output,
                _parole([("Sigmund", 0.0), ("Freud", 0.4)]),
                {1: 0.0},
                ["Slide"],
            )
            self.assertIsNotNone(srt)
            assert srt is not None
            self.assertIn("Sigmund Freud", srt.read_text(encoding="utf-8"))

    def test_capitoli_partono_da_zero_e_usano_il_testo_della_slide(self) -> None:
        capitoli = build_capitoli({1: 0.0, 2: 76.0}, ["Prima slide", "Seconda slide"])
        self.assertEqual(capitoli.split("\n"), ["0:00 Prima slide", "1:16 Seconda slide"])

    def test_una_riga_di_simboli_non_diventa_titolo(self) -> None:
        # Caso reale da una slide infografica: senza il filtro questo diventava
        # il titolo del capitolo.
        capitoli = build_capitoli(
            {1: 0.0}, ["+ \u2014\u2014\u2014-__ 87% 7 >90% Raffinazione globale x (Gallio, Litio"]
        )
        self.assertEqual(capitoli, "0:00 Slide 1")

    def test_sceglie_la_riga_di_testo_e_non_la_prima(self) -> None:
        # Sulla slide il grafico sta in cima e il titolo sotto: conta il titolo.
        capitoli = build_capitoli(
            {1: 0.0}, ["+ \u2014\u2014\u2014-__ 87% 7 >90% Raffinazione\nIl Fisiologico 10%"]
        )
        self.assertEqual(capitoli, "0:00 Il Fisiologico 10%")

    def test_slide_senza_testo_ha_un_titolo_utile(self) -> None:
        capitoli = build_capitoli({1: 0.0}, ["   "])
        self.assertEqual(capitoli, "0:00 Slide 1")

    def test_il_titolo_si_ferma_alla_prima_frase(self) -> None:
        capitoli = build_capitoli({1: 0.0}, ["Titolo della slide — sottotitolo lungo che non serve"])
        self.assertEqual(capitoli, "0:00 Titolo della slide")

    def test_le_briciole_dell_ocr_non_entrano_nel_titolo(self) -> None:
        # Il caso reale: OCR di una slide decorativa.
        capitoli = build_capitoli({1: 0.0}, ["LI illusione del Risotto - Menù degustazione stellare. -…"])
        self.assertEqual(capitoli, "0:00 illusione del Risotto")

    def test_articolo_in_testa_non_viene_scambiato_per_una_briciola(self) -> None:
        capitoli = build_capitoli({1: 0.0}, ["Il Fisiologico 10%: la mappa della complessità"])
        self.assertEqual(capitoli, "0:00 Il Fisiologico 10%")

    def test_testo_senza_parole_vere_ripiega_sul_numero(self) -> None:
        capitoli = build_capitoli({1: 0.0}, ["~~ --- 99 ~~"])
        self.assertEqual(capitoli, "0:00 Slide 1")

    def test_titolo_lungo_si_accorcia_a_un_confine_di_parola(self) -> None:
        lungo = " ".join(["parola"] * 30)
        titolo = build_capitoli({1: 0.0}, [lungo]).split(" ", 1)[1]
        self.assertLessEqual(len(titolo), subtitles.MAX_CARATTERI_CAPITOLO + 1)
        self.assertTrue(titolo.endswith("…"))
        self.assertFalse(titolo.rstrip("…").endswith("parol"), "tagliato a meta' parola")

    def test_i_due_file_si_scrivono_accanto_al_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "video_finale.mp4"
            srt, capitoli = scrivi_sottotitoli(
                output,
                _parole([("Ciao", 0.0), ("mondo", 0.5)]),
                {1: 0.0, 2: 3.0},
                ["Prima", "Seconda"],
            )
            self.assertEqual(srt, Path(tmp) / "video_finale.srt")
            self.assertEqual(capitoli, Path(tmp) / "video_finale.capitoli.txt")
            assert srt is not None and capitoli is not None  # restringe i tipi per mypy
            self.assertTrue(srt.exists())
            self.assertIn("Ciao mondo", srt.read_text(encoding="utf-8"))
            self.assertIn("0:00 Prima", capitoli.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
