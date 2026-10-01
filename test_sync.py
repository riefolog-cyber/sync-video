#!/usr/bin/env python3
"""
Test unitari per la pipeline di sincronizzazione.
Esegui con: python -m unittest test_sync -v
Coprono la logica di "precisione assoluta": niente distribuzioni uniformi,
interruzione con avviso se la sincronizzazione è impossibile.
"""

import pathlib
import tempfile
import time
import unittest
from itertools import pairwise
from typing import ClassVar
from unittest import mock

import numpy as np

from semantic_sync import (
    SemanticOptions,
    build_candidates,
    make_anchor_remap_filter,
    merge_short_segments,
    refine_llm_segment_boundaries,
    refine_ordered_llm_timeline,
    semantic_timeline_from_texts,
    verify_anchor_mapping_embedding,
)
from timeline import (
    detect_flow_from_words,
    enforce_min_durations,
    extract_timeline_from_transcript,
    filter_anchor_remaps,
    reconcile_timeline,
)


def _words(items):
    """Converte [(word, start)] in lista di dict Whisper."""
    return [{"word": w, "start": t} for w, t in items]


class TestSlideAudioFlow(unittest.TestCase):
    """Flusso slide-audio: 'slide N' esplicito."""

    def test_complete(self):
        words = _words(
            [
                ("slide", 30.0),
                ("2", 30.3),
                ("slide", 80.0),
                ("3", 80.3),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=3, total_duration=120.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 30.3, 3: 80.3})

    def test_closing_recap_not_an_anchor(self):
        # Regressione: "e chiudiamo con la slide 3" a fine episodio è un ripasso
        # finale, NON una transizione. Prima del filtro diventava l'ancora della
        # slide 3 (spostata alla fine dell'audio, video troncato). L'ancora deve
        # restare quella del passaggio reale "passiamo alla slide 3".
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("2", 30.6),
                ("passiamo", 80.0),
                ("alla", 80.2),
                ("slide", 80.3),
                ("3", 80.6),
                ("e", 120.0),
                ("chiudiamo", 120.3),
                ("con", 120.6),
                ("la", 120.8),
                ("slide", 120.9),
                ("3", 121.2),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=3, flow="slide-audio")
        self.assertEqual(anchors, {2: 30.6, 3: 80.6})

    def test_closing_recap_with_transition_between_kept(self):
        # "chiudiamo questo argomento e passiamo alla slide 2": il verbo di
        # transizione più vicino alla slide indica un passaggio reale, non un
        # ripasso finale: l'ancora deve essere conservata.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("chiudiamo", 30.0),
                ("questo", 30.4),
                ("argomento", 30.7),
                ("e", 31.0),
                ("passiamo", 31.3),
                ("alla", 31.5),
                ("slide", 31.6),
                ("2", 31.9),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=2, flow="slide-audio")
        self.assertEqual(anchors, {2: 31.9})

    def test_early_total_slide_count_does_not_poison_real_anchor(self):
        # Regressione: "le 13 slide di questo documento" a inizio episodio
        # (numero prima di "slide") è un conteggio, non una transizione.
        # Prima del fix first-wins occupava la slide 13 e scartava la vera
        # ancora "passiamo alla slide 13" pronunciata dopo (video con slide 13
        # anticipata di ~16s). Deve vincere l'occorrenza più recente.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("ordine", 118.3),
                ("le", 118.8),
                ("13", 119.3),
                ("slide", 119.6),
                ("di", 120.3),
                ("questo", 120.5),
                ("documento", 120.8),
                ("passiamo", 2055.8),
                ("alla", 2056.4),
                ("slide", 2056.6),
                ("13", 2056.8),
                ("il", 2057.9),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=13, flow="slide-audio")
        self.assertEqual(anchors, {13: 2057.9})

    def test_count_and_recall_on_the_same_slide(self):
        # I due casi che il last-wins non poteva separare, sulla stessa slide:
        # un conteggio quantificato ("le 5 slide") e un richiamo ("guarda slide 5")
        # intorno alla transizione vera. La regola è: conteggi esclusi, e fra le
        # transizioni vince la PRIMA.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 10.0), ("alla", 10.2), ("slide", 10.3), ("tre", 10.5),
                ("le", 100.0), ("5", 100.3), ("slide", 100.6), ("del", 100.9),
                ("documento", 101.2),
                ("passiamo", 200.0), ("alla", 200.2), ("slide", 200.3), ("cinque", 200.5),
                ("guarda", 260.0), ("slide", 260.3), ("cinque", 260.5),
                ("passiamo", 300.0), ("alla", 300.2), ("slide", 300.3), ("sei", 300.5),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=6, flow="slide-audio")
        # Il conteggio delle "5 slide" non occupa la 5, e il richiamo a 260s non
        # sposta la transizione: l'ancora resta quella vera (200.5s, il numero).
        self.assertEqual(anchors[5], 200.5)
        self.assertEqual(anchors[3], 10.5)
        self.assertEqual(anchors[6], 300.5)

    def test_quantified_number_is_flagged_as_count(self):
        # La qualificazione è ciò che separa i due casi: senza di lei la
        # selezione non potrebbe scegliere.
        from timeline import _collect_slide_mention_kinds

        words = _words(
            [
                ("le", 100.0), ("13", 100.3), ("slide", 100.6),
                ("passiamo", 200.0), ("alla", 200.2), ("slide", 200.3), ("13", 200.5),
            ]
        )
        kinds = _collect_slide_mention_kinds(words, 13)
        # "le 13 slide" -> conteggio; "slide 13" -> riferimento. La finestra del
        # pattern 1 può rilevare due volte la stessa transizione (dalla parola
        # "slide" del conteggio e da quella dell'annuncio), quindi non si conta
        # la lunghezza: conta il FLAG del conteggio.
        flags = [is_count for _t, is_count in kinds[13]]
        self.assertIn(True, flags, "il conteggio quantificato deve essere marcato")
        self.assertFalse(
            all(flags), "le transizioni non devono essere marcate come conteggio"
        )
        # Il conteggio è anche il PRIMO: è quello che va escluso.
        self.assertTrue(kinds[13][0][1])

    def test_unquantified_number_before_slide_is_not_a_count(self):
        # "passiamo alla slide 3" non ha determinante davanti: è un riferimento,
        # anche se il numero arriva prima di "slide" (pattern 2).
        from timeline import _collect_slide_mention_kinds

        words = _words(
            [
                ("passiamo", 50.0), ("alla", 50.2), ("tre", 50.4), ("la", 50.6),
                ("slide", 50.9),
            ]
        )
        kinds = _collect_slide_mention_kinds(words, 5)
        self.assertEqual([is_count for _t, is_count in kinds[3]], [False])

    def test_slide_with_only_counts_is_not_left_without_anchor(self):
        # Se una slide ha SOLO conteggi non resta senza ancora: si prende
        # l'ultimo disponibile. Non si inventa un confine, ma non si abbandona
        # nemmeno la slide all'assegnamento per contenuto senza dirlo.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 10.0), ("alla", 10.2), ("slide", 10.3), ("due", 10.5),
                ("le", 100.0), ("4", 100.3), ("slide", 100.6),
                ("tutte", 150.0), ("le", 150.2), ("4", 150.4), ("slide", 150.7),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=4, flow="slide-audio")
        self.assertIn(4, anchors)
        # Ultimo conteggio disponibile: 150.7s (la parola "slide" che lo chiude).
        self.assertEqual(anchors[4], 150.7)

    def test_recap_out_of_order_recovered_from_first_mention(self):
        # "come dicevamo nella slide 3" pronunciata DOPO la slide 4: la
        # citazione a posteriori (last-wins) farebbe scartare la slide 3 dal
        # LIS, perdendo anche la menzione reale in ordine (80.6s). Con il
        # recupero della PRIMA menzione la slide 3 resta ancorata a 80.6s.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("2", 30.6),
                ("passiamo", 80.0),
                ("alla", 80.2),
                ("slide", 80.3),
                ("3", 80.6),
                ("passiamo", 130.0),
                ("alla", 130.2),
                ("slide", 130.3),
                ("4", 130.6),
                ("passiamo", 180.0),
                ("alla", 180.2),
                ("slide", 180.3),
                ("5", 180.6),
                ("come", 230.0),
                ("dicevamo", 230.3),
                ("nella", 230.6),
                ("slide", 230.9),
                ("3", 231.2),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=5, flow="slide-audio")
        self.assertEqual(anchors, {2: 30.6, 3: 80.6, 4: 130.6, 5: 180.6})

    def test_recap_only_mention_still_discarded(self):
        # La slide 3 è menzionata SOLO come citazione a posteriori (dopo le
        # slide 4 e 5): nessuna menzione in ordine da recuperare -> resta
        # scartata (l'LLM/DP semantico la posizionerà per contenuto).
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("2", 30.6),
                ("passiamo", 130.0),
                ("alla", 130.2),
                ("slide", 130.3),
                ("4", 130.6),
                ("passiamo", 180.0),
                ("alla", 180.2),
                ("slide", 180.3),
                ("5", 180.6),
                ("come", 230.0),
                ("dicevamo", 230.3),
                ("nella", 230.6),
                ("slide", 230.9),
                ("3", 231.2),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=5, flow="slide-audio")
        self.assertEqual(anchors, {2: 30.6, 4: 130.6, 5: 180.6})

    def test_content_number_does_not_displace_real_anchor(self):
        # Regressione (podcast reale 29/09): un numero di CONTENTO letto come
        # riferimento ("i tre concetti della slide", "la slide spiega il ciclo
        # in quattro fasi") finiva per pilotare il LIS, che scartava l'annuncio
        # vero e faceva durare 0.5s la slide 3 con 21s di ritardo sulla 5.
        # Una menzione vale come transizione solo se, al suo tempo, nessuna slide
        # di numero maggiore era già stata annunciata.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 93.0),
                ("alla", 93.2),
                ("slide", 93.3),
                ("2", 93.5),
                ("passiamo", 161.0),
                ("alla", 161.2),
                ("slide", 161.3),
                ("3", 161.7),
                ("passiamo", 283.0),
                ("alla", 283.2),
                ("slide", 283.3),
                ("5", 284.0),
                ("i", 295.4),
                ("tre", 295.5),
                ("concetti", 295.7),
                ("della", 296.1),
                ("slide", 296.3),
                ("sono", 296.7),
                ("perfetti", 296.9),
                ("qui.", 297.4),
                ("la", 416.6),
                ("slide", 416.8),
                ("spiega", 417.1),
                ("questo", 417.9),
                ("ciclo", 418.2),
                ("in", 418.5),
                ("quattro", 418.6),
                ("fasi.", 419.1),
                ("passiamo", 464.0),
                ("alla", 464.1),
                ("slide", 464.2),
                ("8", 464.3),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=8, flow="slide-audio")
        # Le due ancore fantasma non rubano il posto a quelle vere e la slide 4,
        # mai annunciata, resta l'unica senza ancora.
        self.assertEqual(anchors, {2: 93.5, 3: 161.7, 5: 284.0, 8: 464.3})

    def test_content_number_discarded_is_logged_with_its_time(self):
        # Il tempo scartato è la diagnosi del danno: senza di esso un confine
        # spostato di minuti sembra un difetto del motore embeddings.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("5", 30.6),
                ("la", 45.0),
                ("stanza", 45.05),
                ("senza", 45.1),
                ("porte,", 45.2),
                ("i", 45.3),
                ("gruppi", 45.35),
                ("whatsapp.", 45.4),
                ("qualcuno", 45.45),
                ("chiede", 45.5),
                ("e", 45.55),
                ("tre", 45.6),
                ("concetti", 45.8),
                ("della", 46.1),
                ("slide", 46.3),
                ("sono", 46.7),
            ]
        )
        with self.assertLogs("slide2video", level="WARNING") as logs:
            extract_slide_anchors(words, total_slides=5, flow="slide-audio")
        self.assertIn("slide 3 a 46.7s", "\n".join(logs.output))

    def test_displaced_citation_is_logged_with_its_time(self):
        # Qui la slide 3 esiste anche come transizione: la citazione più avanti
        # è la menzione fantasma e va segnalata con il suo tempo.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("3", 30.6),
                ("passiamo", 80.0),
                ("alla", 80.2),
                ("slide", 80.3),
                ("5", 80.6),
                ("la", 95.0),
                ("stanza", 95.05),
                ("senza", 95.1),
                ("porte,", 95.2),
                ("i", 95.3),
                ("gruppi", 95.35),
                ("whatsapp.", 95.4),
                ("qualcuno", 95.45),
                ("chiede", 95.5),
                ("e", 95.55),
                ("tre", 95.6),
                ("concetti", 95.8),
                ("della", 96.1),
                ("slide", 96.3),
                ("sono", 96.7),
            ]
        )
        with self.assertLogs("slide2video", level="INFO") as logs:
            anchors = extract_slide_anchors(words, total_slides=5, flow="slide-audio")
        # Il confine resta quello vero (30.6s): la menzione fantasma non lo sposta.
        self.assertEqual(anchors, {3: 30.6, 5: 80.6})
        # Resta però tracciata, per non perdere la diagnosi: è INFO, non WARNING,
        # perché non è più un errore (l'ancora è corretta).
        self.assertIn("'slide 3' a 96.7s", "\n".join(logs.output))
        self.assertIn("richiamate più volte", "\n".join(logs.output))

    def test_italian_sl_words_are_not_slide_references(self):
        # "slitta", "slogan", ... iniziano davvero per "sl" e passerebbero il
        # fuzzy fonetico: senza l'elenco di esclusione "la slitta ha tre ruote"
        # diventerebbe l'ancora "slide 3".
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("2", 30.6),
                ("la", 60.0),
                ("slitta", 60.2),
                ("ha", 60.5),
                ("tre", 60.7),
                ("ruote", 60.9),
                ("lo", 80.0),
                ("slogan", 80.2),
                ("costa", 80.5),
                ("cinque", 80.7),
                ("euro", 80.9),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=5, flow="slide-audio")
        self.assertEqual(anchors, {2: 30.6})

    def test_discarded_citations_are_reported_with_their_time(self):
        # Il tempo della citazione scartata finisce nel report: è l'unico modo
        # di collegare un confine di timeline spostato di minuti alla sua causa.
        from timeline import discarded_citations, extract_slide_anchors

        words = _words(
            [
                ("passiamo", 93.0),
                ("alla", 93.2),
                ("slide", 93.3),
                ("2", 93.5),
                ("passiamo", 160.0),
                ("alla", 160.2),
                ("slide", 160.3),
                ("4", 160.6),
                ("la", 200.0),
                ("stanza", 200.05),
                ("senza", 200.1),
                ("porte,", 200.2),
                ("i", 200.3),
                ("gruppi", 200.35),
                ("whatsapp.", 200.4),
                ("qualcuno", 200.45),
                ("chiede", 200.5),
                ("e", 200.55),
                ("tre", 200.6),
                ("concetti", 200.8),
                ("della", 201.1),
                ("slide", 201.3),
                ("sono", 201.7),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=4, flow="slide-audio")
        self.assertEqual(anchors, {2: 93.5, 4: 160.6})
        self.assertEqual(discarded_citations(words, 4, anchors), [{"slide": 3, "time": 201.7}])

    def test_discarded_citations_survive_a_renumbered_mapping(self):
        # Dopo il rimappaggio del mapping i numeri parlati e quelli del PDF
        # non coincidono: il confronto deve essere sui TEMPI, o tutte le ancore
        # legittime verrebbero segnalate come citazioni scartate.
        from timeline import discarded_citations

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("4", 30.6),
                ("passiamo", 200.0),
                ("alla", 200.2),
                ("slide", 200.3),
                ("5", 200.6),
            ]
        )
        # Il mapping ha spostato "slide 5" (200.6s) sulla slide 4 del PDF.
        corrected = {3: 30.6, 4: 200.6}
        self.assertEqual(discarded_citations(words, 5, corrected), [])

    def test_out_of_range_tail_anchor_recovers_last_transition(self):
        # Deck da 14 slide, podcast che annuncia "slide 15": la menzione è fuori
        # portata, ma il tempo è la misura dello speaker e senza di esso l'ultima
        # slide resta posizionata a tentativi (confine spostato di 17s nel caso
        # reale del 29/09).
        from timeline import extract_slide_anchors, out_of_range_tail_anchor

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("4", 30.6),
                ("passiamo", 200.0),
                ("alla", 200.2),
                ("slide", 200.3),
                ("14", 200.6),
                ("passiamo", 300.0),
                ("alla", 300.2),
                ("slide", 300.3),
                ("15", 300.6),
                ("abitare", 300.9),
                ("la", 301.0),
                ("maschera", 301.2),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=14, flow="slide-audio")
        self.assertEqual(anchors, {4: 30.6, 14: 200.6})
        # Caso reale: la verifica del mapping ha già corretto l'offset -1, quindi
        # l'annuncio "slide 14" ora pinza la slide 13 e la 14 (ultima) è libera.
        corrected = {4: 30.6, 13: 200.6}
        # L'ancora cade DOPO la parola numero (300.9s), non dentro la frase.
        self.assertEqual(out_of_range_tail_anchor(words, 14, corrected), {14: 300.9})

    def test_out_of_range_tail_anchor_not_used_when_last_slide_anchored(self):
        from timeline import out_of_range_tail_anchor

        words = _words(
            [
                ("passiamo", 200.0),
                ("alla", 200.2),
                ("slide", 200.3),
                ("14", 200.6),
                ("passiamo", 300.0),
                ("alla", 300.2),
                ("slide", 300.3),
                ("15", 300.6),
            ]
        )
        self.assertEqual(out_of_range_tail_anchor(words, 14, {14: 200.6}), {})

    def test_out_of_range_tail_anchor_ignores_early_count(self):
        # "le 20 slide di questo documento" all'inizio non è l'ultima
        # transizione: la regola accetta solo numeri appena oltre l'ultimo
        # annuncio valido.
        from timeline import out_of_range_tail_anchor

        words = _words(
            [
                ("questa", 5.0),
                ("puntata", 5.2),
                ("copre", 5.4),
                ("le", 5.6),
                ("20", 5.8),
                ("slide", 6.0),
                ("del", 6.2),
                ("documento", 6.4),
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("4", 30.6),
            ]
        )
        self.assertEqual(out_of_range_tail_anchor(words, 14, {4: 30.6}), {})

    def test_count_phrase_in_opening_does_not_wipe_the_anchors(self):
        # Regressione: "questa puntata copre le 13 slide" è la PRIMA menzione,
        # quindi la regola "in ordine" la scambia per una transizione e alza il
        # tetto a 13. Tutte le transizioni reali (2, 3, 4) diventavano citazioni
        # fuori ordine e il recupero non le poteva riprendere (la finestra era
        # chiusa dal conteggio): restava UNA sola ancora, la slide 13 a 6.6s.
        # Quando il set "in ordine" regge su una non-transizione, la catena del
        # LIS classico è più lunga e prova che il set è sbagliato.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("questa", 5.0),
                ("puntata", 5.3),
                ("copre", 5.6),
                ("le", 5.8),
                ("13", 6.0),
                ("slide", 6.3),
                ("del", 6.6),
                ("documento", 6.9),
                ("passiamo", 93.0),
                ("alla", 93.2),
                ("slide", 93.3),
                ("2", 93.5),
                ("passiamo", 161.0),
                ("alla", 161.2),
                ("slide", 161.3),
                ("3", 161.7),
                ("passiamo", 250.0),
                ("alla", 250.2),
                ("slide", 250.3),
                ("4", 250.6),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=13, flow="slide-audio")
        self.assertEqual(anchors, {2: 93.5, 3: 161.7, 4: 250.6})

    def test_order_filter_still_wins_when_it_finds_more_anchors(self):
        # L'inverso: qui le menzioni in ordine sono tutte transizioni vere e il
        # LIS classico (last-wins) ne trova meno, perché una citazione ruberebbe
        # il posto all'annuncio reale. La catena più lunga resta quella giusta.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 30.0),
                ("alla", 30.2),
                ("slide", 30.3),
                ("3", 30.6),
                ("passiamo", 80.0),
                ("alla", 80.2),
                ("slide", 80.3),
                ("5", 80.6),
                ("la", 120.0),
                ("stanza", 120.05),
                ("senza", 120.1),
                ("porte,", 120.2),
                ("i", 120.3),
                ("gruppi", 120.35),
                ("whatsapp.", 120.4),
                ("qualcuno", 120.45),
                ("chiede", 120.5),
                ("e", 120.55),
                ("tre", 120.6),
                ("concetti", 120.8),
                ("della", 121.1),
                ("slide", 121.3),
                ("sono", 121.7),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=5, flow="slide-audio")
        self.assertEqual(anchors, {3: 30.6, 5: 80.6})

    def test_discarded_citations_are_not_reported_twice(self):
        # "le 13 slide del documento" followed by "passiamo alla slide 2": the
        # "slide" of the count grabs the number of the next phrase within 7
        # words, so the same mention is collected twice. It does not change the
        # anchors, but the report must not list it twice.
        from timeline import discarded_citations

        words = _words(
            [
                ("questa", 5.0),
                ("puntata", 5.3),
                ("copre", 5.6),
                ("le", 5.8),
                ("13", 6.0),
                ("slide", 6.3),
                ("del", 6.6),
                ("documento", 6.9),
                ("passiamo", 93.0),
                ("alla", 93.2),
                ("slide", 93.3),
                ("2", 93.5),
            ]
        )
        out = discarded_citations(words, 13, {2: 93.5})
        self.assertEqual(out, [{"slide": 13, "time": 6.6}])

    def test_total_count_opening_does_not_steal_the_last_anchor(self):
        # Regressione: "questa puntata copre le quindici slide del documento" su
        # un deck da 15 pagine. Il numero del totale e' l'ultima pagina, quindi
        # il suo tempo veniva scambiato con quello della vera transizione (le
        # due menzioni arrivano in ordine inverso perche' le raggiungono due
        # pattern diversi) e la slide 15 restava senza ancora, con tutte le
        # altre iniziali a cascata. La lista delle menzioni va ordinata.
        from timeline import _collect_slide_mentions, extract_slide_anchors

        numeri = {
            2: "due", 3: "tre", 4: "quattro", 5: "cinque", 6: "sei", 7: "sette",
            8: "otto", 9: "nove", 10: "dieci", 11: "undici", 12: "dodici",
            13: "tredici", 14: "quattordici", 15: "quindici",
        }
        pairs = [
            ("questa", 1.0), ("puntata", 1.3), ("copre", 1.6), ("le", 1.8),
            ("quindici", 2.0), ("slide", 2.3), ("del", 2.6), ("documento", 2.9),
        ]
        tempi = {}
        t = 6.0
        for s in range(2, 16):
            pairs += [("passiamo", t), ("alla", t + 0.2), ("slide", t + 0.3), (numeri[s], t + 0.5)]
            tempi[s] = t + 1.0
            pairs.append(("contenuto", t + 1.0))
            t += 40.0
        words = _words(pairs)

        # L'ordine cronologico e' garantito, non incidentale.
        for s, times in _collect_slide_mentions(words, 15).items():
            self.assertEqual(times, sorted(times), f"menzioni di {s} fuori ordine")

        anchors = extract_slide_anchors(words, total_slides=15, flow="slide-audio")
        self.assertEqual(anchors, {s: tempi[s] for s in range(2, 16)})

    def test_recall_after_the_real_anchor_is_ignored(self):
        # "torniamo alla slide 7" a meta' percorso: l'annuncio vero e' la
        # transizione, quindi l'ancora deve restare li'. Prima l'ultima menzione
        # vinceva (150.5s) e la slide 7 partiva 50s tardi, lasciando la 6 a
        # schermo per tutto quel tempo.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 10.0), ("alla", 10.2), ("slide", 10.3), ("due", 10.5),
                ("passiamo", 100.0), ("alla", 100.2), ("slide", 100.3), ("sette", 100.5),
                ("torniamo", 150.0), ("alla", 150.2), ("slide", 150.3), ("sette", 150.5),
                ("passiamo", 200.0), ("alla", 200.2), ("slide", 200.3), ("otto", 200.5),
            ]
        )
        with self.assertLogs("slide2video", level="INFO") as logs:
            anchors = extract_slide_anchors(words, total_slides=8, flow="slide-audio")
        # L'ancora resta sulla TRANSIZIONE: il richiamo non la sposta piu'.
        self.assertEqual(anchors[7], 100.5)
        # Il richiamo resta tracciato (e' il difetto dell'audio, su cui il prompt
        # NotebookLM ha una regola dedicata), ma non e' piu' un errore.
        self.assertIn("richiamate più volte", "\n".join(logs.output))
        self.assertNotIn("superate da un richiamo", "\n".join(logs.output))

    def test_completion_clamp_never_moves_an_anchor(self):
        # L'estrapolazione dell'ultima slide senza ancora supera la durata
        # audio: il clamp accorcia l'INVENTATO e lascia le ancore ai tempi
        # pronunciati dallo speaker. Prima il clamp scalava tutto in
        # proporzione, spostando qui 300s -> 250s e 600s -> 500s senza
        # dichiararlo: la verifica frame passava lo stesso, perché confronta
        # i fotogrammi con una timeline dichiarata coerente ma sbagliata.
        from timeline import _complete_from_anchors

        out = _complete_from_anchors(
            {2: 300.0, 3: 600.0, 5: 900.0}, total_slides=6, total_duration=1000.0
        )
        self.assertIsNotNone(out)
        assert out is not None
        for slide, spoken in ((2, 300.0), (3, 600.0), (5, 900.0)):
            self.assertEqual(out[slide], spoken, f"ancora della slide {slide} spostata")
        self.assertLessEqual(out[6], 1000.0)
        self.assertGreater(out[6], out[5])

    def test_completion_declines_when_an_anchor_exceeds_the_audio(self):
        # Un'ancora oltre la durata dell'audio non ha risposta corretta:
        # spostarla falserebbe la misura dello speaker, ignorarla romperebbe la
        # monotonia. La funzione rinuncia e il chiamante ripiega.
        from timeline import _complete_from_anchors

        self.assertIsNone(
            _complete_from_anchors({2: 300.0, 3: 1200.0}, total_slides=4, total_duration=1000.0)
        )

    def test_italian_number_words(self):
        words = _words(
            [
                ("slide", 30.0),
                ("due", 30.3),
                ("slide", 80.0),
                ("tre", 80.3),
                ("slide", 130.0),
                ("quattro", 130.3),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=4, total_duration=200.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 30.3, 3: 80.3, 4: 130.3})

    def test_italian_ordinals(self):
        # Ordinali femminili italiani ("la terza diapositiva", "la sesta slide")
        # riconosciuti come riferimenti di slide.
        words = _words(
            [
                ("diapositiva", 30.0),
                ("la", 30.3),
                ("seconda", 30.6),
                ("diapositiva", 80.0),
                ("la", 80.3),
                ("terza", 80.6),
                ("diapositiva", 130.0),
                ("la", 130.3),
                ("quarta", 130.6),
                ("diapositiva", 180.0),
                ("la", 180.3),
                ("quinta", 180.6),
                ("diapositiva", 230.0),
                ("la", 230.3),
                ("sesta", 230.6),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=6, total_duration=300.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 30.6, 3: 80.6, 4: 130.6, 5: 180.6, 6: 230.6})

    def test_ordinal_masculine(self):
        words = _words(
            [
                ("slide", 30.0),
                ("il", 30.3),
                ("secondo", 30.6),
                ("slide", 80.0),
                ("il", 80.3),
                ("terzo", 80.6),
                ("slide", 130.0),
                ("il", 130.3),
                ("quarto", 130.6),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=4, total_duration=200.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 30.6, 3: 80.6, 4: 130.6})

    def test_ordinal_with_article_between(self):
        # "diapositiva la numero due": numero dopo articolo e "numero"
        words = _words(
            [
                ("diapositiva", 30.0),
                ("la", 30.3),
                ("numero", 30.6),
                ("due", 30.9),
                ("diapositiva", 80.0),
                ("la", 80.3),
                ("numero", 80.6),
                ("tre", 80.9),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=3, total_duration=120.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 30.9, 3: 80.9})

    def test_number_from_word_ordinals(self):
        from timeline import _number_from_word

        for word, expected in {
            "primo": 1,
            "prima": 1,
            "secondo": 2,
            "seconda": 2,
            "terzo": 3,
            "terza": 3,
            "quarto": 4,
            "quarta": 4,
            "quinto": 5,
            "quinta": 5,
            "sesto": 6,
            "sesta": 6,
            "settimo": 7,
            "settima": 7,
            "ottavo": 8,
            "ottava": 8,
            "nono": 9,
            "nona": 9,
            "decimo": 10,
            "decima": 10,
            "undicesimo": 11,
            "undicesima": 11,
            "dodicesimo": 12,
            "dodicesima": 12,
            "tredicesimo": 13,
            "tredicesima": 13,
            "ventesimo": 20,
            "ventesima": 20,
            "trentesimo": 30,
            "trentesima": 30,
            "ventunesimo": 21,
            "ventunesima": 21,
        }.items():
            with self.subTest(word=word):
                self.assertEqual(_number_from_word(word), expected)

    def test_number_before_slide(self):
        words = _words(
            [
                ("due", 100.0),
                ("alla", 100.4),
                ("rivoluzione", 100.8),
                ("del", 101.0),
                ("mahayana", 101.2),
                ("la", 101.5),
                ("slide", 101.8),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=2, total_duration=200.0, flow="slide-audio")
        # Timestamp coerente col pattern 1: quello della parola "slide"
        self.assertEqual(tl, {1: 0.0, 2: 101.8})

    def test_punctuation_in_number_word(self):
        words = _words(
            [
                ("slide", 30.0),
                ("due,", 30.3),
                ("slide", 80.0),
                ("tre.", 80.3),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=3, total_duration=120.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 30.3, 3: 80.3})

    def test_misheard_slide_variants_sla(self):
        # Whisper trascrive "slide due" come "sla e due": le varianti fonetiche
        # vanno riconosciute come ancore deterministiche, non affidate all'LLM.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 390.0),
                ("alla", 398.0),
                ("sla", 398.3),
                ("e", 398.4),
                ("due", 398.6),
                ("il", 399.0),
                ("pappagallo", 399.5),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=3, flow="slide-audio")
        self.assertEqual(anchors, {2: 399.0})

    def test_misheard_slide_variants_asl(self):
        # "slide cinque" trascritto da Whisper come "asl cinque".
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 1090.0),
                ("alla", 1095.0),
                ("asl", 1095.7),
                ("cinque", 1096.2),
                ("il", 1096.6),
                ("dissenso", 1097.0),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=6, flow="slide-audio")
        self.assertEqual(anchors, {5: 1096.6})

    def test_misheard_slide_variants_sallay(self):
        # "slide due" trascritto da whisper-small come "sallay 2".
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 390.0),
                ("alla", 397.0),
                ("sallay", 397.6),
                ("2", 398.5),
                ("il", 399.0),
                ("pappagallo", 399.5),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=6, flow="slide-audio")
        self.assertEqual(anchors, {2: 399.0})

    def test_misheard_slide_variant_embedded_digit(self):
        # "slide sei" trascritto come "slaib6" (numero incorporato nella parola).
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 1330.0),
                ("alla", 1337.5),
                ("slaib6", 1338.1),
                ("le", 1339.0),
                ("implicazioni", 1339.4),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=6, flow="slide-audio")
        self.assertEqual(anchors, {6: 1339.0})

    def test_fused_italian_number_embedded_word(self):
        # "slide otto" fuse in un'unica parola "slaidotto": il numero e' dentro
        # la parola e deve essere estratto come ancora, non perso (regressione:
        # _collect_slide_references usava _number_from_word che non vede i
        # cardinali fusi, quindi l'ancora veniva scartata e la slide affidata
        # al semantico, perdendo precisione).
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 400.0),
                ("alla", 407.0),
                ("slaidotto", 407.6),
                ("il", 408.3),
                ("controllo", 408.7),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=9, flow="slide-audio")
        self.assertEqual(anchors, {8: 408.3})

    def test_fused_italian_number_flow_detection(self):
        # L'auto-detection deve riconoscere il flusso slide-audio anche quando
        # il numero e' fuso nella parola (stessa regressione di sopra).
        from timeline import detect_flow_from_words

        words = _words(
            [
                ("passiamo", 400.0),
                ("alla", 407.0),
                ("slaidue", 407.6),
                ("il", 408.3),
            ]
        )
        self.assertEqual(detect_flow_from_words(words, 2.0), "slide-audio")

    def test_common_words_with_sl_sequence_not_anchors(self):
        # Regressione: "solo", "salvo", "sale" NON devono essere scambiate
        # per "slide". "in realtà sei solo un passeggero" produceva un falso
        # riferimento {6: 371.5} che faceva scartare la vera slide 6.
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("in", 370.0),
                ("realtà", 371.0),
                ("sei", 371.2),
                ("solo", 371.5),
                ("un", 371.8),
                ("passeggero", 372.0),
                ("passiamo", 1338.0),
                ("alla", 1338.4),
                ("slaib6", 1338.8),
            ]
        )
        anchors = extract_slide_anchors(words, total_slides=6, flow="slide-audio")
        self.assertEqual(anchors, {6: 1338.8})

    def test_misheard_slide_variant_flow_detection(self):
        # L'auto-detection deve riconoscere il flusso slide-audio anche con
        # la variante misheard "sla".
        from timeline import detect_flow_from_words

        words = _words(
            [
                ("passiamo", 398.0),
                ("alla", 398.1),
                ("sla", 398.3),
                ("e", 398.4),
                ("due", 398.6),
            ]
        )
        self.assertEqual(detect_flow_from_words(words), "slide-audio")

    def test_partial_returns_none(self):
        words = _words([("slide", 30.0), ("2", 30.3)])
        tl = extract_timeline_from_transcript(words, total_slides=3, total_duration=120.0, flow="slide-audio")
        self.assertIsNone(tl)

    def test_no_signals_returns_none(self):
        words = _words([("ciao", 1.0), ("mondo", 2.0)])
        tl = extract_timeline_from_transcript(words, total_slides=3, total_duration=120.0, flow="slide-audio")
        self.assertIsNone(tl)

    def test_empty_words_returns_none(self):
        self.assertIsNone(
            extract_timeline_from_transcript([], total_slides=3, total_duration=120.0, flow="slide-audio")
        )

    def test_out_of_order_reference_returns_none(self):
        # Falso positivo: "slide 8" citata in anticipo (39.2s) prima della
        # slide 7 (634.3s) — il riferimento va scartato e, mancando slide,
        # la timeline è None → il fallback LLM viene attivato dal chiamante.
        words = _words(
            [
                ("slide", 39.2),
                ("8", 39.5),
                ("slide", 634.3),
                ("7", 634.6),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=8, total_duration=925.4, flow="slide-audio")
        self.assertIsNone(tl)

    def test_out_of_order_reference_recuperato_con_ancore(self):
        # "slide 3" a 80.0s precede "slide 2" a 120.0s → 3 è un'anticipazione.
        # Le ancore coerenti sono {2: 120.0, 4: 160.0}; slide 3 interpolata tra 2 e 4.
        words = _words(
            [
                ("slide", 120.0),
                ("2", 120.3),
                ("slide", 80.0),
                ("3", 80.3),
                ("slide", 160.0),
                ("4", 160.3),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=4, total_duration=200.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 120.3, 3: 140.3, 4: 160.3})

    def test_valid_timeline_not_affected_by_post_filter(self):
        # Timeline già valida: il post-filtro non deve alterarla.
        words = _words(
            [
                ("slide", 30.0),
                ("2", 30.3),
                ("slide", 80.0),
                ("3", 80.3),
                ("slide", 130.0),
                ("4", 130.3),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=4, total_duration=200.0, flow="slide-audio")
        self.assertEqual(tl, {1: 0.0, 2: 30.3, 3: 80.3, 4: 130.3})


class TestAudioSlideFlow(unittest.TestCase):
    """Flusso audio-slide: 'passiamo al blocco successivo'."""

    def test_complete(self):
        words = _words(
            [
                ("passiamo", 30.0),
                ("al", 30.2),
                ("blocco", 30.4),
                ("successivo", 30.6),
                ("passiamo", 100.0),
                ("al", 100.2),
                ("blocco", 100.4),
                ("successivo", 100.6),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=3, total_duration=200.0, flow="audio-slide")
        self.assertEqual(tl, {1: 0.0, 2: 30.0, 3: 100.0})

    def test_insufficient_transitions_returns_none(self):
        words = _words(
            [
                ("passiamo", 30.0),
                ("al", 30.2),
                ("blocco", 30.4),
                ("successivo", 30.6),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=4, total_duration=200.0, flow="audio-slide")
        self.assertIsNone(tl)

    def test_procediamo_variant(self):
        words = _words(
            [
                ("procediamo", 30.0),
                ("al", 30.2),
                ("blocco", 30.4),
                ("successivo", 30.6),
                ("passiamo", 90.0),
                ("al", 90.2),
                ("blocco", 90.4),
                ("successivo", 90.6),
            ]
        )
        tl = extract_timeline_from_transcript(words, total_slides=3, total_duration=200.0, flow="audio-slide")
        self.assertEqual(tl, {1: 0.0, 2: 30.0, 3: 90.0})


class TestDetectFlow(unittest.TestCase):
    """Auto-detection flusso (robusta ai numeri in parole)."""

    def test_slide_with_digit(self):
        words = _words([("slide", 30.0), ("2", 30.3)])
        self.assertEqual(detect_flow_from_words(words), "slide-audio")

    def test_slide_with_italian_word(self):
        words = _words([("slide", 30.0), ("tre", 30.3)])
        self.assertEqual(detect_flow_from_words(words), "slide-audio")

    def test_number_before_slide_detected(self):
        words = _words(
            [
                ("due", 100.0),
                ("la", 101.5),
                ("slide", 101.8),
            ]
        )
        self.assertEqual(detect_flow_from_words(words), "slide-audio")

    def test_blocco_successivo(self):
        words = _words(
            [
                ("passiamo", 30.0),
                ("al", 30.2),
                ("blocco", 30.4),
                ("successivo", 30.6),
            ]
        )
        self.assertEqual(detect_flow_from_words(words), "audio-slide")

    def test_prossimo_variant(self):
        words = _words(
            [
                ("andiamo", 30.0),
                ("al", 30.2),
                ("blocco", 30.4),
                ("prossimo", 30.6),
            ]
        )
        self.assertEqual(detect_flow_from_words(words), "audio-slide")

    def test_no_signals_returns_none(self):
        words = _words([("ciao", 1.0), ("mondo", 2.0)])
        self.assertIsNone(detect_flow_from_words(words))

    def test_empty_returns_none(self):
        self.assertIsNone(detect_flow_from_words([]))


class TestReconcileTimeline(unittest.TestCase):
    """Riconciliazione: precisione assoluta, errore se non valida."""

    def test_valid(self):
        durations = reconcile_timeline({1: 0.0, 2: 100.0, 3: 250.0}, 3, 300.0)
        for got, expected in zip(durations, [100.0, 150.0, 50.0], strict=True):
            self.assertAlmostEqual(got, expected)

    def test_non_monotonic_raises(self):
        with self.assertRaises(ValueError):
            reconcile_timeline({1: 0.0, 2: 100.0, 3: 80.0}, 3, 300.0)

    def test_zero_duration_raises(self):
        with self.assertRaises(ValueError):
            reconcile_timeline({1: 0.0, 2: 100.0, 3: 100.0}, 3, 300.0)

    def test_negative_start_clamped_then_raises(self):
        with self.assertRaises(ValueError):
            reconcile_timeline({1: 0.0, 2: -5.0}, 2, 300.0)

    def test_incomplete_timeline_raises(self):
        # Manca la slide 2: starts[2] resta 0.0 → non crescente rispetto a slide 1
        with self.assertRaises(ValueError):
            reconcile_timeline({1: 0.0, 3: 100.0}, 3, 300.0)

    def test_last_slide_past_end_raises(self):
        # Ultima slide dopo la fine dell'audio → durata negativa
        with self.assertRaises(ValueError):
            reconcile_timeline({1: 0.0, 2: 350.0}, 2, 300.0)


class _FakeThemedEmbed:
    """Embedder finto condiviso: vettore one-hot per ogni parola-tema presente."""

    def __init__(self, themes):
        self.themes = themes

    def __call__(self, texts):
        out = []
        for t in texts:
            v = np.zeros(len(self.themes))
            for i, k in enumerate(self.themes):
                if k in t:
                    v[i] = 1.0
            norm = np.linalg.norm(v)
            out.append(v / norm if norm else v)
        return np.array(out)


class TestBeamAbQuality(unittest.TestCase):
    """Confronto fra le due trascrizioni: deve usare il METRO della pipeline.

    Se qualcuno cambia la catena di allineamento (blocchi, z-score,
    competizione, DP) senza aggiornare anche la misura di confronto, il primo
    test qui sotto cade: è la garanzia che il numero registrato nel report
    resti confrontabile con ``quality`` della sincronizzazione vera.
    """

    THEMES: ClassVar[list] = ["alfa", "beta", "gamma", "delta"]
    DURATION: ClassVar[float] = 20.0

    def _words(self):
        # 3 parole per finestra da 5s (il minimo per non essere scartata come
        # silenzio): un blocco per tema, in ordine.
        return [
            {"word": t, "start": float(i * 5 + k * 0.5)}
            for i, t in enumerate(self.THEMES)
            for k in range(3)
        ]

    def _options(self):
        return SemanticOptions(window_seconds=5.0, min_slide_duration=2.0)

    def test_measure_equals_real_sync_metric(self):
        from semantic_sync import (
            alignment_quality_from_words,
            build_semantic_blocks,
            last_quality,
            reset_weak_signal_flag,
        )

        slides = [f"{t} slide" for t in self.THEMES]
        words = self._words()
        qual = alignment_quality_from_words(
            slides,
            words,
            4,
            self.DURATION,
            options=self._options(),
            embed_fn=_FakeThemedEmbed(self.THEMES),
        )
        self.assertIsNotNone(qual)
        assert qual is not None

        reset_weak_signal_flag()
        tl = semantic_timeline_from_texts(
            slides,
            build_semantic_blocks(words, self.DURATION, 5.0),
            total_slides=4,
            total_duration=self.DURATION,
            embed_fn=_FakeThemedEmbed(self.THEMES),
            options=self._options(),
        )
        self.assertIsNotNone(tl)
        expected = last_quality()
        self.assertAlmostEqual(qual["avg_z"], expected["avg_z"], places=9)
        self.assertAlmostEqual(qual["avg_sim"], expected["avg_sim"], places=9)
        self.assertEqual(sorted(qual), ["avg_sim", "avg_z", "blocks", "concordance", "confusability"])

    def test_measure_has_no_side_effects(self):
        # È una misura, non una decisione: non deve toccare la timeline di
        # riferimento né il flag di segnale debole usato dal riepilogo.
        from semantic_sync import (
            alignment_quality_from_words,
            last_quality,
            reset_weak_signal_flag,
            weak_signal_seen,
        )

        slides = [f"{t} slide" for t in self.THEMES]
        reset_weak_signal_flag()
        before = last_quality()
        alignment_quality_from_words(
            slides,
            self._words(),
            4,
            self.DURATION,
            options=self._options(),
            embed_fn=_FakeThemedEmbed(self.THEMES),
        )
        self.assertEqual(last_quality(), before)
        self.assertFalse(weak_signal_seen())

    def test_measure_reports_none_when_signal_is_insufficient(self):
        from semantic_sync import alignment_quality_from_words

        # Nessun blocco: meglio None che un numero inventato da confrontare.
        self.assertIsNone(
            alignment_quality_from_words(
                ["alfa slide", "beta slide"],
                [],
                2,
                self.DURATION,
                options=self._options(),
                embed_fn=_FakeThemedEmbed(self.THEMES),
            )
        )


class TestEmbedCache(unittest.TestCase):
    """Cache content-addressed degli embedding: riusa i vettori, non la precisione.

    I vettori sono identici per costruzione (stessi testi + stesso modello),
    quindi ciò che va dimostrato è che la cache li riusi DAVVERO e che non possa
    mai servire vettori di un modello o di testi diversi.
    """

    def setUp(self):
        import semantic_sync

        self._ss = semantic_sync
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_dir = semantic_sync._EMBED_CACHE_DIR
        semantic_sync._EMBED_CACHE_DIR = pathlib.Path(self._tmp.name)
        semantic_sync.reset_embed_cache()
        self.addCleanup(self._restore)

    def _restore(self):
        self._ss._EMBED_CACHE_DIR = self._orig_dir
        self._ss.set_embed_cache_enabled(True)
        self._ss.reset_embed_cache()

    @staticmethod
    def _counting_embed(embed_id, dim=3):
        """Embedder finto deterministico che registra le chiamate ricevute."""
        calls = []

        def _embed(texts):
            calls.append(list(texts))
            out = np.zeros((len(texts), dim), dtype=np.float32)
            for i, t in enumerate(texts):
                out[i, sum(map(ord, t)) % dim] = 1.0
            return out

        _embed.embed_id = embed_id
        _embed.calls = calls
        return _embed

    def test_same_texts_and_model_hit_cache(self):
        embed = self._counting_embed("modello-A")
        first = self._ss._embed_list(embed, ["uno", "due"], "test")
        second = self._ss._embed_list(embed, ["uno", "due"], "test")
        self.assertEqual(len(embed.calls), 1)
        np.testing.assert_array_equal(first, second)

    def test_different_model_does_not_reuse(self):
        embed_a = self._counting_embed("modello-A")
        embed_b = self._counting_embed("modello-B")
        self._ss._embed_list(embed_a, ["uno"], "test")
        self._ss._embed_list(embed_b, ["uno"], "test")
        self.assertEqual(len(embed_a.calls), 1)
        self.assertEqual(len(embed_b.calls), 1)

    def test_different_texts_do_not_reuse(self):
        embed = self._counting_embed("modello-A")
        self._ss._embed_list(embed, ["uno"], "test")
        self._ss._embed_list(embed, ["due"], "test")
        self.assertEqual(len(embed.calls), 2)

    def test_without_identity_cache_is_disabled(self):
        embed = self._counting_embed("")
        first = self._ss._embed_list(embed, ["uno"], "test")
        second = self._ss._embed_list(embed, ["uno"], "test")
        self.assertEqual(len(embed.calls), 2)
        np.testing.assert_array_equal(first, second)

    def test_disk_cache_survives_memo_reset(self):
        embed = self._counting_embed("modello-A")
        self._ss._embed_list(embed, ["uno", "due"], "test")
        self._ss.reset_embed_cache()  # come una nuova run nello stesso processo
        again = self._ss._embed_list(embed, ["uno", "due"], "test")
        self.assertEqual(len(embed.calls), 1)
        self.assertEqual(again.shape, (2, 3))

    def test_corrupt_cache_file_is_ignored(self):
        embed = self._counting_embed("modello-A")
        key = self._ss._embed_cache_key("modello-A", ["uno", "due"])
        self._ss._EMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (self._ss._EMBED_CACHE_DIR / f"emb_{key}.npz").write_bytes(b"non un npz")
        out = self._ss._embed_list(embed, ["uno", "due"], "test")
        self.assertEqual(len(embed.calls), 1)
        self.assertEqual(out.shape, (2, 3))

    def test_shape_mismatch_is_recomputed(self):
        embed = self._counting_embed("modello-A")
        key = self._ss._embed_cache_key("modello-A", ["uno", "due"])
        self._ss._EMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(
            self._ss._EMBED_CACHE_DIR / f"emb_{key}.npz",
            emb=np.zeros((5, 3), dtype=np.float32),
            seconds=np.float32(1.0),
        )
        out = self._ss._embed_list(embed, ["uno", "due"], "test")
        self.assertEqual(len(embed.calls), 1)
        self.assertEqual(out.shape, (2, 3))

    def test_slide_embedding_reused_across_passes(self):
        """Le slide non vengono ri-embeddate dal confronto trascrizioni alla sync."""
        embed = self._counting_embed("modello-A")
        slides = ["alfa slide", "beta slide"]
        blocks = [
            {"time": 0.0, "text": "alfa " * 4},
            {"time": 5.0, "text": "beta " * 4},
        ]
        self._ss._embed_and_report(slides, blocks, 2, embed, "Semantico")
        self._ss._embed_and_report(slides, blocks, 2, embed, "Confronto trascrizioni")
        # slide (riusate) + blocchi (riusati): una sola chiamata per lista
        self.assertEqual(len(embed.calls), 2)

    def test_disabled_cache_always_recomputes(self):
        self._ss.set_embed_cache_enabled(False)
        self.addCleanup(self._ss.set_embed_cache_enabled, True)
        embed = self._counting_embed("modello-A")
        self._ss._embed_list(embed, ["uno"], "test")
        self._ss._embed_list(embed, ["uno"], "test")
        self.assertEqual(len(embed.calls), 2)

    def test_prune_keeps_most_recent(self):
        d = self._ss._EMBED_CACHE_DIR
        d.mkdir(parents=True, exist_ok=True)
        for i in range(self._ss._EMBED_CACHE_MAX + 3):
            np.savez(d / f"emb_{i:016d}.npz", emb=np.zeros((1, 3)), seconds=np.float32(0.1))
        self._ss._prune_embed_cache()
        self.assertEqual(len(list(d.glob("emb_*.npz"))), self._ss._EMBED_CACHE_MAX)

    def test_embed_seconds_counts_vector_time(self):
        class _StubModel:
            model_name = "stub"

            @staticmethod
            def embed(texts, batch_size=64):
                time.sleep(0.02)
                return [np.ones(3, dtype=np.float32) for _ in texts]

        # Il conteggio vive nell'embed_fn reale (quello che chiama il modello),
        # non nel finto: qui si usa proprio quel percorso.
        embed = self._ss._make_embed_fn(_StubModel())
        before = self._ss.embed_seconds()
        self._ss._embed_list(embed, ["uno"], "test")
        self.assertGreaterEqual(self._ss.embed_seconds() - before, 0.015)


class TestTimingTable(unittest.TestCase):
    """La tabella tempi deve mostrare l'embedding vero, non il caricamento."""

    def test_embedding_and_model_shown_separately(self):
        import main

        with (
            mock.patch.object(main, "_append_timing_history"),
            self.assertLogs(main.log, level="INFO") as cm,
        ):
            main._print_timing(1.0, 2.0, 30.0, 28.0, 3.0, 5.0, 40.0)
        printed = "\n".join(r.getMessage() for r in cm.records)
        self.assertIn("Embedding │ 28s", printed)
        self.assertIn("Modello   │ 3s", printed)

    def test_llm_time_is_shown(self):
        # Stessa ragione che ha fatto correggere la riga Embedding: senza la
        # voce LLM, 328s di cascata finivano dentro "Sincronizzaz." senza
        # attribuzione e lo spreco restava invisibile.
        import main

        with (
            mock.patch.object(main, "_append_timing_history"),
            self.assertLogs(main.log, level="INFO") as cm,
        ):
            main._print_timing(1.0, 2.0, 330.0, 2.0, 5.0, 49.0, 400.0, 328.0)
        printed = "\n".join(r.getMessage() for r in cm.records)
        self.assertIn("LLM       │ 5m28s", printed)

    def test_llm_row_absent_when_no_llm_was_called(self):
        # Una run senza LLM non deve mostrare una riga a zero.
        import main

        with (
            mock.patch.object(main, "_append_timing_history"),
            self.assertLogs(main.log, level="INFO") as cm,
        ):
            main._print_timing(1.0, 2.0, 30.0, 28.0, 3.0, 5.0, 40.0)
        printed = "\n".join(r.getMessage() for r in cm.records)
        self.assertNotIn("LLM  ", printed)

    def test_llm_time_is_persisted_in_the_history(self):
        # Lo storico serve a monitorare le regressioni: senza la voce LLM il
        # costo non era confrontabile fra una run e l'altra.
        import main

        with mock.patch.object(main, "_append_timing_history") as history:
            main._print_timing(1.0, 2.0, 330.0, 2.0, 5.0, 49.0, 400.0, 328.0)
        self.assertEqual(history.call_args.args[-1], 328.0)


class TestSemanticSync(unittest.TestCase):
    """Sincronizzazione semantica (embeddings): DP monotona senza LLM."""

    @staticmethod
    def _fake_embed(themes):
        """Embedder finto: vettore one-hot per ogni parola-tema presente."""
        return _FakeThemedEmbed(themes)

    def _blocks_sequential(self, themes, window=5.0):
        # Ogni coppia di blocchi parla dello stesso tema (transizioni ogni 10s)
        return [{"time": i * window, "text": (themes[i // 2] + " ") * 4} for i in range(len(themes) * 2)]

    def test_in_order_perfect_match(self):
        themes = ["alfa", "beta", "gamma", "delta"]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            self._blocks_sequential(themes),
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )
        self.assertEqual(tl, {1: 0.0, 2: 10.0, 3: 20.0, 4: 30.0})

    def test_monotonic_with_out_of_order_topics(self):
        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [
            {
                "time": i * 5.0,
                "text": ("alpha" if i < 2 else ("gamma" if i < 4 else ("beta" if i < 6 else "delta"))) * 4,
            }
            for i in range(8)
        ]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )
        self.assertIsNotNone(tl)
        times = [tl[s] for s in sorted(tl)]
        self.assertTrue(all(b > a for a, b in pairwise(times)))

    def test_anchor_respected(self):
        themes = ["alfa", "beta", "gamma", "delta"]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            self._blocks_sequential(themes),
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
            anchors={2: 25.0},
        )
        # La slide 2 è vincolata vicino a 25s (blocco 4/5)
        self.assertLessEqual(abs(tl[2] - 25.0), 5.0)

    def test_anchor_refined_to_exact_time(self):
        # Con il refinamento, una slide con ancora parte ESATTAMENTE all'ancora
        # (non al multiplo di finestra del blocco).
        themes = ["alfa", "beta", "gamma", "delta"]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            self._blocks_sequential(themes),
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
            anchors={2: 12.3, 3: 27.8},
        )
        self.assertEqual(tl[2], 12.3)
        self.assertEqual(tl[3], 27.8)
        times = [tl[s] for s in sorted(tl)]
        self.assertTrue(all(b > a for a, b in pairwise(times)))

    def test_too_few_blocks_returns_none(self):
        themes = ["alfa", "beta", "gamma", "delta"]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            self._blocks_sequential(themes)[:3],
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0),
        )
        self.assertIsNone(tl)

    def test_build_blocks_skips_silence(self):
        from semantic_sync import build_semantic_blocks

        words = [{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 1.5}]
        blocks = build_semantic_blocks(words, total_duration=20.0, window_seconds=4.0, min_words=2)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["time"], 0.0)
        self.assertIn("ciao", blocks[0]["text"])

    def test_build_blocks_first_time_is_word_timestamp(self):
        from semantic_sync import build_semantic_blocks

        words = [{"word": "uno", "start": 0.7}, {"word": "due", "start": 1.2}, {"word": "tre", "start": 5.3}]
        blocks = build_semantic_blocks(words, total_duration=20.0, window_seconds=4.0, min_words=1)
        self.assertEqual(blocks[0]["first_time"], 0.7)
        self.assertEqual(blocks[1]["first_time"], 5.3)

    def test_unanchored_slide_refined_to_first_word(self):
        # Senza ancora, la slide parte al primo timestamp reale di parola
        # del blocco di inizio (non al multiplo di finestra).
        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [
            {"time": i * 5.0, "first_time": i * 5.0 + 1.7, "text": t * 4}
            for i, t in enumerate(["alfa", "beta", "gamma", "delta"] * 2)
        ]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )
        self.assertIsNotNone(tl)
        # Verifica che le slide partano dal first_time (non dal time di finestra)
        for s in range(2, 5):
            block_time = blocks[int(tl[s] / 5.0)]["time"]
            self.assertNotEqual(
                tl[s], block_time, f"Slide {s}: first_time {tl[s]} non deve coincidere con time {block_time}"
            )
            self.assertGreater(tl[s], block_time, f"Slide {s}: first_time {tl[s]} > time {block_time}")

    def test_low_normalized_peak_is_reported_not_discarded(self):
        # Il presidio reale è lo z-score, non la cosine grezza. Su blocchi di
        # rumore (nessun tema, similarity ~0 ovunque) le colonne hanno std ~0,
        # quindi lo z-score è neutro: non si "scarta" niente, si SEGALA con
        # `weak_signal` e la pipeline continua con la timeline stimata.
        from semantic_sync import reset_weak_signal_flag, weak_signal_seen

        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [{"time": i * 5.0, "text": "zappa qwerty nullo"} for i in range(8)]
        reset_weak_signal_flag()
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )
        self.assertIsNotNone(tl)
        self.assertTrue(weak_signal_seen())

    def test_no_guard_exists_on_the_raw_cosine_scale(self):
        # La cosine grezza non può presidiare niente: con l'embedder finto i
        # blocchi di rumore danno 0.0, ma con e5 reale anche un testo avverso
        # dà ~0.75 (misurato). Una soglia su quella scala sarebbe un presidio
        # finto: per questo non ne esiste una, e questo test lo fissa.
        from semantic_sync import SemanticOptions

        self.assertFalse(hasattr(SemanticOptions(), "min_avg_similarity"))


    def test_zscore_neutralizes_uniform_slide(self):
        # Slide riassuntiva con similarità uniformemente alta su tutti i
        # blocchi: lo z-score la porta a ~0 (neutra), mentre i picchi locali
        # delle altre slide emergono sopra il loro baseline.
        from semantic_sync import zscore_matrix

        sim = np.array(
            [
                [0.45, 0.10],  # blocco 0: picco locale slide 1
                [0.45, 0.10],  # blocco 1: picco locale slide 1
                [0.45, 0.95],  # blocco 2: picco locale slide 2
                [0.45, 0.10],  # blocco 3
            ],
            dtype=np.float64,
        )
        z = zscore_matrix(sim)
        # Slide 1 (uniforme): tutti gli z ~0 -> non vince mai
        self.assertTrue(np.all(np.abs(z[:, 0]) < 1e-6))
        # Slide 2: il picco al blocco 2 ha lo z più alto della colonna
        self.assertEqual(int(np.argmax(z[:, 1])), 2)

    def test_zscore_constant_column_is_zero(self):
        # Colonna costante (std=0) -> z = 0 (neutra, nessuna divisione per 0)
        from semantic_sync import zscore_matrix

        sim = np.array([[0.3, 0.1], [0.3, 0.9]], dtype=np.float64)
        z = zscore_matrix(sim)
        self.assertTrue(np.all(np.abs(z[:, 0]) < 1e-6))

    def test_segment_verdict_uses_zscore_not_raw_cosine(self):
        # Regressione (verifica analysis_sync): la slide 3 e' un riepilogo con
        # similarita' coseno uniformemente ALTA (0.65) su TUTTI i segmenti:
        # sulla cosine grezza risulterebbe "best" ovunque (falso
        # disallineamento). Lo z-score per-slide la neutralizza (colonna
        # costante -> z=0) e fanno emergere i picchi veri delle altre slide.
        from semantic_sync import segment_verdict

        sim = np.array(
            [
                [0.5, 0.3, 0.65],  # seg 0: picco slide 1
                [0.3, 0.5, 0.65],  # seg 1: picco slide 2
                [0.6, 0.3, 0.65],  # seg 2: picco slide 1
                [0.3, 0.6, 0.65],  # seg 3: picco slide 2
            ],
            dtype=np.float64,
        )
        # La cosine grezza si inganna: slide 3 (riepilogo) vince per ogni riga.
        self.assertTrue(np.all(np.argmax(sim, axis=1) == 2))
        verdicts = segment_verdict(sim, shown=[1, 2, 1, 2])
        self.assertEqual([v["best"] for v in verdicts], [1, 2, 1, 2])
        # La slide mostrata ha rank 1 per ogni segmento (sync corretta) e il
        # suo z non e' mai sotto il best (subject to rounding).
        self.assertTrue(all(v["rank"] == 1 for v in verdicts))
        self.assertTrue(all(v["shown_z"] >= v["best_z"] - 1e-6 for v in verdicts))

    def test_segment_verdict_shown_rank_when_not_best(self):
        # Nell'ultimo segmento la slide mostrata (2) non e' il picco: il
        # verdetto deve riportare il rank reale (>1), non forzare "OK".
        from semantic_sync import segment_verdict

        sim = np.array(
            [
                [0.9, 0.3, 0.3],  # seg 0: picco slide 1
                [0.3, 0.9, 0.3],  # seg 1: picco slide 2
                [0.4, 0.5, 0.9],  # seg 2: picco slide 3 (mostrata la 2)
            ],
            dtype=np.float64,
        )
        verdicts = segment_verdict(sim, shown=[1, 2, 2])
        self.assertEqual([v["best"] for v in verdicts], [1, 2, 3])
        self.assertEqual(verdicts[2]["rank"], 2)
        self.assertEqual(verdicts[0]["rank"], 1)
        self.assertEqual(verdicts[1]["rank"], 1)

    def test_segment_verdict_without_shown_defaults_to_best(self):
        # ``shown`` opzionale: verdetto della sola lettura (niente slide mostrata).
        from semantic_sync import segment_verdict

        sim = np.array([[0.9, 0.2], [0.2, 0.9]], dtype=np.float64)
        verdicts = segment_verdict(sim)
        self.assertEqual([v["best"] for v in verdicts], [1, 2])
        self.assertEqual(verdicts[0]["rank"], 1)

    def test_competition_neutralizes_uniform_slide(self):
        # Slide 3 "riepilogo" con similarità uniforme su tutti i blocchi:
        # la competizione softmax deve favorire i picchi locali delle altre slide.
        from semantic_sync import competition_matrix

        sim = np.array(
            [
                [0.50, 0.30, 0.40, 0.40],  # blocco 0: picco slide 1
                [0.30, 0.55, 0.40, 0.40],  # blocco 1: picco slide 2
                [0.35, 0.35, 0.40, 0.60],  # blocco 2: picco slide 4
            ],
            dtype=np.float64,
        )
        comp = competition_matrix(sim, temperature=0.2)
        for row, winner in zip(comp, (0, 1, 3), strict=True):
            self.assertEqual(int(np.argmax(row)), winner)
        # Con temperature alta la competizione si smorza (tende all'uniforme)
        soft = competition_matrix(sim, temperature=50.0)
        self.assertAlmostEqual(float(soft.max(axis=1).mean()), 0.25, delta=0.02)

    def test_weak_signal_detects_out_of_order_audio(self):
        # Guard-rail: audio che non segue l'ordine delle slide -> segnale debole.
        from semantic_sync import signal_quality_report, weak_signal

        # Slide 1..4 con picchi nel parlato in ordine diverso (5,2,4,3)
        sim = np.zeros((5, 4))
        sim[:, 0] = np.arange(5)  # picco slide 1 = blocco 4
        sim[:, 1] = np.arange(5)[::-1]  # picco slide 2 = blocco 0
        sim[:, 2] = np.concatenate([np.arange(3) + 1, [0, 0]])  # picco blocco 2
        sim[:, 3] = np.concatenate([np.arange(2) + 1, [0, 0, 0]])  # picco blocco 1
        report = signal_quality_report(sim)
        self.assertTrue(weak_signal(report))

    def test_signal_quality_detects_confusable_slides(self):
        # Guard-rail: slide quasi-duplicati + concordanza moderata -> debole.
        from semantic_sync import signal_quality_report, weak_signal

        # Due slide con embedding IDENTICI -> cosine 1.0 (quasi-duplicati)
        slide_emb = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float64)
        # Concordanza moderata (0.5): picco slide 2 in blocco 0, slide 1 in 1
        sim = np.array(
            [
                [0.2, 1.0],  # blocco 0: picco slide 2
                [1.0, 0.2],  # blocco 1: picco slide 1
                [0.5, 0.5],
                [0.5, 0.5],
            ],
            dtype=np.float64,
        )
        report = signal_quality_report(sim, slide_emb)
        self.assertEqual(report["confusability"], 1.0)
        self.assertTrue(weak_signal(report))

    def test_confusable_alone_does_not_trigger(self):
        # Slide simili MA audio che segue perfettamente l'ordine:
        # nessun falso positivo (caso slide derivate dal podcast).
        from semantic_sync import signal_quality_report, weak_signal

        slide_emb = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float64)
        sim = np.array(
            [
                [1.0, 0.2],  # blocco 0: picco slide 1
                [0.2, 1.0],  # blocco 1: picco slide 2
            ],
            dtype=np.float64,
        )
        report = signal_quality_report(sim, slide_emb)
        self.assertEqual(report["confusability"], 1.0)
        self.assertFalse(weak_signal(report))

    def test_signal_quality_ok_in_order(self):
        # Guard-rail: audio in ordine -> segnale buono (nessun falso positivo).
        from semantic_sync import signal_quality_report, weak_signal

        themes = ["alfa", "beta", "gamma", "delta"]
        embed_fn = self._fake_embed(themes)
        slide_emb = embed_fn([f"{t} slide" for t in themes])
        # Picchi in ordine: slide 1..4 ai blocchi 0,2,4,6
        sim = np.zeros((7, 4))
        for s in range(4):
            sim[2 * s, s] = 1.0
        report = signal_quality_report(sim, slide_emb)
        self.assertFalse(weak_signal(report))

    def test_weak_signal_flag_after_weak_sync(self):
        # Il guard-rail semantico espone il flag a main.py: con parlato fuori
        # ordine il flag deve restare True dopo semantic_timeline_from_texts.
        from semantic_sync import (
            reset_weak_signal_flag,
            semantic_timeline_from_texts,
            weak_signal_seen,
        )

        reset_weak_signal_flag()
        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [
            {"time": i * 5.0, "text": (t + " ") * 4}
            for i, t in enumerate(["gamma", "gamma", "delta", "delta", "alfa", "alfa", "beta", "beta"])
        ]
        semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )
        self.assertTrue(weak_signal_seen())

    def test_weak_signal_flag_clear_after_strong_sync(self):
        # Con parlato in ordine il flag resta False (nessun falso positivo).
        from semantic_sync import (
            reset_weak_signal_flag,
            semantic_timeline_from_texts,
            weak_signal_seen,
        )

        reset_weak_signal_flag()
        themes = ["alfa", "beta", "gamma", "delta"]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            self._blocks_sequential(themes),
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )
        self.assertIsNotNone(tl)
        self.assertFalse(weak_signal_seen())


class TestNormalizedQualityGuard(unittest.TestCase):
    """Guard-rail di qualità sulla scala normalizzata (z-score per slide).

    La cosine grezza di e5 ha una baseline altissima tra due testi italiani
    qualsiasi: sui dati reali vale 0.80-0.88 sia con le slide nell'ordine
    giusto sia mescolate, quindi il valore assoluto non contiene informazione
    sull'allineamento. Questi test fissano la misura che discrimina davvero
    (picco medio normalizzato) e il fatto che un valore basso è un SEGNALE
    (escalation al LLM, --strict-sync, report) e non un verdetto che scarta la
    timeline già costruita.
    """

    def setUp(self):
        from semantic_sync import reset_weak_signal_flag

        reset_weak_signal_flag()

    def test_raw_similarity_does_not_discriminate_zscore_does(self):
        from semantic_sync import _mean_pair_scores, zscore_matrix

        # Matrice realistica: baseline alta (0.89 tra qualunque testo) e picchi
        # sull'allineamento giusto (0.90). È il caso misurato sui dati reali,
        # dove la cosine vale 0.84 con le slide giuste e 0.84 con quelle
        # mescolate: la differenza assoluta è di pochi millesimi.
        sim = np.full((4, 4), 0.89)
        np.fill_diagonal(sim, 0.90)
        norm = zscore_matrix(sim)
        correct = [(i, i) for i in range(4)]
        shuffled = [(0, 1), (1, 0), (2, 3), (3, 2)]

        raw_ok, z_ok = _mean_pair_scores(sim, norm, correct)
        raw_bad, z_bad = _mean_pair_scores(sim, norm, shuffled)

        # La cosine grezza dice quasi la stessa cosa nei due casi (sui dati
        # reali 0.842 vs 0.840) e sta comunque sopra la vecchia soglia (0.10):
        # non può mai scattare.
        self.assertLess(abs(raw_ok - raw_bad), 0.05)
        self.assertGreater(raw_bad, 0.10)
        # Lo z-score separa senza ambiguità: picco netto vs valore negativo.
        self.assertGreater(z_ok, 1.0)
        self.assertLess(z_bad, 0.0)
        self.assertGreater(z_ok - z_bad, 2.0)

    def test_no_pairs_scores_zero(self):
        from semantic_sync import _mean_pair_scores

        sim = np.zeros((2, 2))
        self.assertEqual(_mean_pair_scores(sim, sim, []), (0.0, 0.0))

    def test_default_threshold_shared_with_options(self):
        from config import DEFAULT_SEMANTIC_MIN_Z
        from semantic_sync import SemanticOptions

        # La soglia tarata sui dati reali non deve divergere dal default delle
        # opzioni: una divergenza cambierebbe in silenzio quali run avvisano.
        self.assertEqual(DEFAULT_SEMANTIC_MIN_Z, 0.45)
        self.assertEqual(SemanticOptions().min_avg_z, DEFAULT_SEMANTIC_MIN_Z)

    def test_low_quality_is_a_signal_not_a_verdict(self):
        # Slide tutte diverse, parlato che nomina tutti i temi allo stesso modo:
        # nessuna colonna ha un picco (z = 0) mentre la cosine resta ~0.5, sopra
        # la vecchia soglia. La timeline NON va scartata (era il comportamento
        # precedente): va segnalata, perché ancore ed LLM possono ancora
        # correggerla.
        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [{"time": i * 5.0, "text": "alfa beta gamma delta"} for i in range(8)]

        with self.assertLogs("slide2video", level="WARNING") as logs:
            tl = semantic_timeline_from_texts(
                [f"{t} slide" for t in themes],
                blocks,
                total_slides=4,
                total_duration=40.0,
                embed_fn=TestSemanticSync._fake_embed(themes),
                options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
            )

        from semantic_sync import last_quality, weak_signal_seen

        self.assertIsNotNone(tl)
        self.assertTrue(weak_signal_seen())
        self.assertTrue(any("bassa fiducia" in m for m in logs.output))
        quality = last_quality()
        self.assertLess(quality["avg_z"], quality["min_avg_z"])
        # La vecchia guardia sulla scala grezza tace: è il motivo del cambio.
        self.assertGreater(quality["avg_sim"], 0.10)

    def test_coherent_deck_reports_high_quality(self):
        themes = ["alfa", "beta", "gamma", "delta"]
        # Un blocco ogni 5s, due per tema, nell'ordine delle slide.
        blocks = [
            {"time": i * 5.0, "text": (themes[i // 2] + " ") * 4}
            for i in range(len(themes) * 2)
        ]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=TestSemanticSync._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )

        from config import DEFAULT_SEMANTIC_MIN_Z
        from semantic_sync import last_quality, weak_signal_seen

        self.assertIsNotNone(tl)
        self.assertFalse(weak_signal_seen())
        quality = last_quality()
        self.assertGreater(quality["avg_z"], quality["min_avg_z"])
        self.assertEqual(quality["min_avg_z"], DEFAULT_SEMANTIC_MIN_Z)
        self.assertEqual(sorted(quality), ["avg_sim", "avg_z", "min_avg_z"])


class TestFrameGuidedRepair(unittest.TestCase):
    """Riparazione dei confini guidata dal controllo del video.

    Il mismatch dice quale slide è DAVVERO a schermo in un istante preciso: se
    è quella di un segmento adiacente, il confine è fuori posto e la direzione
    dell'errore è nota. Questi test fissano che il nuovo confine venga cercato
    dal motore embedding (non indovinato), solo nella direzione dell'evidenza, e
    che un mismatch NON interpretabile (slide non adiacente, frame di
    transizione) non produca correzioni inventate.
    """

    THEMES: ClassVar[list[str]] = ["alfa", "beta"]

    @staticmethod
    def _words(split: float, end: float, step: float = 2.0):
        """Parlato di 'alfa' fino a ``split``, poi di 'beta' (parole ogni 2s)."""
        out = []
        t = 0.0
        while t < end:
            out.append({"word": "alfa" if t < split else "beta", "start": t})
            t += step
        return out

    def _repair(self, segments, mismatches, words, **kwargs):
        from semantic_sync import repair_segments_from_frame_mismatches

        params = {"min_segment_seconds": 5.0, "context_seconds": 12.0, "max_shift_seconds": 180.0}
        params.update(kwargs)
        return repair_segments_from_frame_mismatches(
            segments,
            mismatches,
            words,
            [f"{t} slide" for t in self.THEMES],
            embed_fn=TestSemanticSync._fake_embed(self.THEMES),
            **params,
        )

    def test_next_neighbour_mismatch_anticipates_the_boundary(self):
        # Il video a 40s mostra la slide 2 dentro il segmento della slide 1:
        # il confine è in ritardo e deve tornare dove il parlato cambia tema (30s).
        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        mismatches = [{"slide": 1, "shown": 2, "similarity": 1.0, "time": 40.0}]

        refined, applied = self._repair(segments, mismatches, self._words(split=30.0, end=120.0))

        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["slide"], 2)
        self.assertEqual(applied[0]["shown"], 2)
        self.assertAlmostEqual(applied[0]["new_start"], 30.0, delta=2.0)
        self.assertEqual(refined[0]["start"], 0.0)  # la prima slide non si muove
        self.assertAlmostEqual(float(refined[1]["start"]), float(applied[0]["new_start"]))

    def test_previous_neighbour_mismatch_postpones_the_boundary(self):
        # Il video a 90s mostra ancora la slide 1 dentro il segmento della 2:
        # il confine è in anticipo e il tema cambia a 90s.
        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        mismatches = [{"slide": 2, "shown": 1, "similarity": 1.0, "time": 90.0}]

        refined, applied = self._repair(segments, mismatches, self._words(split=90.0, end=120.0))

        self.assertEqual(len(applied), 1)
        self.assertAlmostEqual(applied[0]["new_start"], 90.0, delta=2.0)
        self.assertGreater(float(refined[1]["start"]), 60.0)

    def test_non_adjacent_shown_slide_is_not_repaired(self):
        # Slide 3 a schermo nel segmento della slide 1: non è un confine
        # spostato ma un problema di rendering. Meglio nessuna correzione.
        segments = [
            {"slide": 1, "start": 0.0, "end": 40.0},
            {"slide": 2, "start": 40.0, "end": 80.0},
            {"slide": 3, "start": 80.0, "end": 120.0},
        ]
        mismatches = [{"slide": 1, "shown": 3, "similarity": 1.0, "time": 20.0}]

        with self.assertLogs("slide2video", level="WARNING") as logs:
            refined, applied = self._repair(segments, mismatches, self._words(split=30.0, end=120.0))

        self.assertEqual(applied, [])
        self.assertEqual(refined, segments)
        self.assertTrue(any("non è un confine adiacente" in m for m in logs.output))

    def test_low_similarity_on_the_declared_slide_is_not_repaired(self):
        # shown == slide (frame di transizione poco riconoscibile): il confine
        # non c'entra, nessuno spostamento.
        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        mismatches = [{"slide": 1, "shown": 1, "similarity": 0.55, "time": 30.0}]

        with self.assertLogs("slide2video", level="WARNING") as logs:
            refined, applied = self._repair(segments, mismatches, self._words(split=30.0, end=120.0))

        self.assertEqual(applied, [])
        self.assertEqual(refined, segments)
        self.assertTrue(any("frame non è riconoscibile" in m for m in logs.output))

    def test_no_better_position_leaves_timeline_untouched(self):
        # Il parlato è tutto sullo stesso tema: nessuna posizione è migliore
        # dell'attuale, quindi la timeline resta com'era (nessun re-render).
        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        mismatches = [{"slide": 2, "shown": 1, "similarity": 1.0, "time": 90.0}]
        words = [{"word": "alfa", "start": t * 2.0} for t in range(60)]

        with self.assertLogs("slide2video", level="WARNING") as logs:
            refined, applied = self._repair(segments, mismatches, words)

        self.assertEqual(applied, [])
        self.assertEqual(refined, segments)
        self.assertTrue(any("timeline invariata" in m for m in logs.output))

    def test_repaired_timeline_stays_valid(self):
        # Monotonicità e durata minima restano garantite: il video riparato
        # deve poter essere ricostruito senza buchi né sovrapposizioni.
        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        mismatches = [{"slide": 1, "shown": 2, "similarity": 1.0, "time": 40.0}]

        refined, applied = self._repair(segments, mismatches, self._words(split=30.0, end=120.0))

        self.assertTrue(applied)
        starts = [float(s["start"]) for s in refined]
        self.assertTrue(all(b > a for a, b in pairwise(starts)))
        self.assertTrue(
            all(
                float(seg["end"]) - float(seg["start"]) >= 5.0
                for seg in refined
            )
        )
        # La sequenza delle slide mostrate non cambia: solo i tempi.
        self.assertEqual([s["slide"] for s in refined], [1, 2])

    def test_search_window_reaches_beyond_the_default_window(self):
        # La finestra simmetrica di default limita lo spostamento: con la
        # ricerca guidata dall'evidenza il confine raggiunge il punto reale.
        from semantic_sync import refine_llm_segment_boundaries

        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        words = self._words(split=30.0, end=120.0)
        slide_texts = [f"{t} slide" for t in self.THEMES]
        embed = TestSemanticSync._fake_embed(self.THEMES)

        default = refine_llm_segment_boundaries(
            segments, words, slide_texts, embed,
            window_seconds=20.0, min_segment_seconds=5.0, refine_slides={2},
        )
        guided = refine_llm_segment_boundaries(
            segments, words, slide_texts, embed,
            window_seconds=20.0, min_segment_seconds=5.0, refine_slides={2},
            search_window={2: (0.0, 60.0)},
        )

        # Senza ricerca guidata il confine non può andare oltre la finestra.
        self.assertGreater(float(default[1]["start"]), 30.0 + 5.0)
        # Con la ricerca guidata trova il cambio di tema reale (30s).
        self.assertAlmostEqual(float(guided[1]["start"]), 30.0, delta=2.0)


class TestRepairGlue(unittest.TestCase):
    """Il collegamento tra riparazione e rigenerazione non deve mai peggiorare
    il video: se le nuove durate non sono valide o la durata totale cambierebbe
    (audio disallineato), la riparazione viene annullata e resta il video già
    generato."""

    @staticmethod
    def _call(repaired, applied, durations=(60.0, 60.0)):
        from unittest.mock import patch

        from main import _repair_durations_from_frames
        from semantic_sync import SemanticOptions

        with patch("main.repair_segments_from_frames_from_words") as patched:
            patched.return_value = (repaired, applied)
            return _repair_durations_from_frames(
                list(durations),
                [1, 2],
                [{"slide": 1, "shown": 2, "time": 40.0, "similarity": 1.0}],
                [],
                ["alfa slide", "beta slide"],
                120.0,
                SemanticOptions(),
            )

    def test_valid_repair_returns_new_durations(self):
        out = self._call(
            [{"slide": 1, "start": 0.0, "end": 30.0}, {"slide": 2, "start": 30.0, "end": 120.0}],
            [{"slide": 2, "old_start": 60.0, "new_start": 30.0}],
        )
        self.assertIsNotNone(out)
        durations, applied = out
        self.assertEqual(durations, [30.0, 90.0])
        self.assertEqual(applied[0]["slide"], 2)

    def test_repair_that_changes_total_duration_is_rejected(self):
        # Total diverso = audio disallineato rispetto al video: si annulla.
        self.assertIsNone(
            self._call(
                [{"slide": 1, "start": 0.0, "end": 60.0}, {"slide": 2, "start": 60.0, "end": 200.0}],
                [{"slide": 2}],
            )
        )

    def test_repair_with_invalid_durations_is_rejected(self):
        self.assertIsNone(
            self._call(
                [{"slide": 1, "start": 0.0, "end": 120.0}, {"slide": 2, "start": 120.0, "end": 120.0}],
                [{"slide": 2}],
            )
        )

    def test_no_applied_change_means_no_render(self):
        self.assertIsNone(
            self._call(
                [{"slide": 1, "start": 0.0, "end": 60.0}, {"slide": 2, "start": 60.0, "end": 120.0}],
                [],
            )
        )


class TestAutoRepairFlag(unittest.TestCase):
    """La riparazione automatica è attiva di default, disattivabile esplicitamente."""

    def test_default_on_and_disabled_with_flag(self):
        from config import parse_args

        self.assertTrue(parse_args([]).auto_repair)
        self.assertFalse(parse_args(["--no-auto-repair"]).auto_repair)


class TestPlainSummary(unittest.TestCase):
    """Riepilogo finale in parole semplici: cosa c'è nel video e cosa dubitare."""

    def setUp(self):
        from semantic_sync import reset_weak_signal_flag

        reset_weak_signal_flag()

    def _render(self, **kwargs):
        """Testo del riepilogo, catturato dal logger reale della pipeline."""
        from main import _log_plain_summary

        with self.assertLogs("slide2video", level="INFO") as logs:
            _log_plain_summary(
                kwargs.pop("durations", [60.0, 60.0, 60.0]),
                kwargs.pop("slide_ids", [1, 2, 3]),
                kwargs.pop("total_duration", 180.0),
                **kwargs,
            )
        return "\n".join(logs.output)

    def test_lists_slides_and_ok_check(self):
        out = self._render(
            frame_check={"checked": 3, "coherent": 3, "mismatches": []},
            quality={"avg_sim": 0.84, "avg_z": 0.69, "min_avg_z": 0.45},
        )
        self.assertIn("IL VIDEO È PRONTO", out)
        self.assertIn("3 slide", out)
        self.assertIn("slide  1", out)
        self.assertIn("1m00s", out)
        self.assertIn("OK, 3 segmenti su 3 mostrano", out)
        self.assertIn("nessuno, la sincronizzazione è", out)
        self.assertIn("alta (picco medio 0.69", out)

    def test_frame_mismatch_is_reported_as_problem_and_doubt(self):
        out = self._render(
            frame_check={
                "checked": 3,
                "coherent": 2,
                "mismatches": [{"slide": 2, "shown": 3, "similarity": 1.0, "time": 90.0}],
            }
        )
        self.assertIn("PROBLEMA", out)
        self.assertIn("slide sbagliata nei segmenti 2", out)

    def test_content_verdicts_become_doubts(self):
        out = self._render(verdicts={2: "disallineata", 3: "incerto"})
        self.assertIn("slide 2 somiglia di più", out)
        self.assertIn("slide 3 la durata è anomala", out)
        self.assertIn("Da controllare a mano", out)

    def test_weak_signal_and_review_diffs_are_doubts(self):
        from semantic_sync import _set_weak_signal

        _set_weak_signal()
        out = self._render(
            quality={"avg_sim": 0.83, "avg_z": 0.20, "min_avg_z": 0.45},
            review_diffs=2,
        )
        self.assertIn("somiglianza tra parlato e slide è risultata debole", out)
        self.assertIn("contesta 2 scelte di slide", out)
        self.assertIn("bassa (slide confondibili per il motore, picco medio 0.20", out)

    def test_low_confidence_names_the_reason_not_only_the_peak(self):
        # Caso reale (13/09): il motore segnala slide confondibili mentre il
        # picco normalizzato è ALTO (0.75 su soglia 0.45). La frase non deve
        # sembrare in contraddizione con se stessa: "bassa" va motivata.
        from semantic_sync import _set_weak_signal

        _set_weak_signal()
        out = self._render(quality={"avg_sim": 0.83, "avg_z": 0.75, "min_avg_z": 0.45})
        self.assertIn("bassa (slide confondibili per il motore, picco medio 0.75", out)
        self.assertNotIn("picco medio 0.75, soglia", out)

    def test_missing_check_is_stated(self):
        out = self._render()
        self.assertIn("Controllo del video finito: non eseguito", out)

    def test_frame_check_states_how_many_were_verified(self):
        # "tutte le 8 slide" si legge come se il video fosse tutto verificato,
        # mentre 8 era il numero dei segmenti ESTRATTI. Il totale lo dice il
        # numero di segmenti: devono comparire entrambi.
        out = self._render(
            total_duration=600.0,
            slide_ids=[1, 2, 3, 4, 5],
            durations=[120.0, 120.0, 120.0, 120.0, 120.0],
            frame_check={"checked": 5, "coherent": 5, "mismatches": []},
        )
        self.assertIn("OK, 5 segmenti su 5 mostrano la slide prevista", out)
        self.assertNotIn("tutte le", out)

    def test_partial_frame_check_is_flagged(self):
        out = self._render(
            total_duration=600.0,
            slide_ids=[1, 2, 3, 4, 5],
            durations=[120.0, 120.0, 120.0, 120.0, 120.0],
            frame_check={"checked": 2, "coherent": 2, "mismatches": []},
        )
        self.assertIn("PARZIALE, 2 segmenti su 5 controllati", out)

    def test_anchor_coverage_is_stated(self):
        # La misura del motore dice quanto è stato MISURATO: con le transizioni
        # inchiodate dalle ancore la copertura va detta insieme, altrimenti un
        # "alta" fiducia sembra una garanzia su confini che non sono stati misurati.
        out = self._render(
            total_duration=600.0,
            slide_ids=[1, 2, 3, 4, 5],
            durations=[120.0, 120.0, 120.0, 120.0, 120.0],
            quality={"avg_sim": 0.84, "avg_z": 0.69, "min_avg_z": 0.45},
            anchors={
                "anchored": 4,
                "transitions": 4,
                "unanchored_slides": [3],
                "unconfirmed": [],
                "mapping_suspicious": False,
            },
        )
        self.assertIn("Confini ancorati: 4 su 4", out)
        self.assertIn("tutti i cambi di slide sono quelli dichiarati nel podcast", out)

    def test_partial_anchor_coverage_names_the_missing_slide(self):
        out = self._render(
            total_duration=600.0,
            slide_ids=[1, 2, 3, 4, 5],
            durations=[120.0, 120.0, 120.0, 120.0, 120.0],
            anchors={
                "anchored": 3,
                "transitions": 4,
                "unanchored_slides": [3],
                "unconfirmed": [],
                "mapping_suspicious": False,
            },
        )
        self.assertIn("Confini ancorati: 3 su 4", out)
        self.assertIn("l'altra transizione è posizionata dal contenuto", out)
        self.assertIn("Senza ancora esplicita: la slide 3", out)

    def test_unconfirmed_anchor_becomes_a_doubt(self):
        # Il video può essere perfino rispetto a una numerazione parlata
        # sbagliata: se il parlato non conferma l'annuncio, va detto.
        out = self._render(
            anchors={
                "anchored": 4,
                "transitions": 4,
                "unanchored_slides": [],
                "unconfirmed": [{"slide": 3, "time": 296.7, "points_to": 5}],
                "mapping_suspicious": False,
            },
        )
        self.assertIn("per la slide 3 il parlato che segue l'annuncio", out)
        self.assertIn("anchors.unconfirmed", out)

    def test_suspicious_mapping_becomes_a_doubt(self):
        out = self._render(
            anchors={
                "anchored": 4,
                "transitions": 4,
                "unanchored_slides": [],
                "unconfirmed": [],
                "mapping_suspicious": True,
            },
        )
        self.assertIn("non è uniforme rispetto alla presentazione", out)

    def test_automatic_repairs_are_shown(self):
        # Una correzione automatica deve essere visibile in chiaro: l'utente ha
        # in mano un video diverso da quello che la timeline dichiarava.
        out = self._render(
            frame_check={"checked": 3, "coherent": 3, "mismatches": []},
            repairs=[{"slide": 2, "old_start": 120.0, "new_start": 90.0}],
        )
        self.assertIn(
            "Correzione automatica: la slide 2 entrava a 2m00s, ora entra a 1m30s",
            out,
        )

    def test_floor_guaranteed_duration_is_declared_and_not_a_doubt(self):
        # La durata di 8s viene dal pavimento anti-flicker, non dal parlato: va
        # DETTA (l'utente vede "8s" e non sa da dove viene) e non deve finire
        # tra i dubbi da controllare a mano, perché non c'è nulla da correggere.
        out = self._render(
            durations=[8.0, 60.0, 60.0],
            verdicts={1: "incerto"},
            floor_report={
                "min_seconds": 8.0,
                "guaranteed": [{"slide": 1, "before": 4.3, "duration": 8.0}],
                "unguaranteed": [],
            },
        )
        self.assertIn("Durata garantita dall'anti-flicker", out)
        self.assertIn("slide 1", out)
        self.assertNotIn("controlla a mano", out)
        self.assertIn("nessuno, la sincronizzazione è", out)

    def test_slide_too_short_even_for_the_floor_is_still_a_doubt(self):
        # Incastrata fra due ancore: il pavimento non può allungarla. Qui sì che
        # serve un intervento (un'ancora pronunciata o più parlato).
        out = self._render(
            durations=[2.0, 60.0, 60.0],
            floor_report={
                "min_seconds": 8.0,
                "guaranteed": [],
                "unguaranteed": [{"slide": 1, "duration": 2.0}],
            },
        )
        self.assertIn("sotto il minimo leggibile", out)
        self.assertIn("la slide 1", out)
        self.assertIn("Da controllare a mano", out)

    def test_slide_numbers_are_listed_in_italian(self):
        # Il riepilogo è testo per l'utente: "per le slide 13" è sbagliato, non
        # solo poco elegante.
        from main import _slide_list_text

        self.assertEqual(_slide_list_text([13]), "la slide 13")
        self.assertEqual(_slide_list_text([6, 12]), "le slide 6 e 12")
        self.assertEqual(_slide_list_text([3, 6, 12]), "le slide 3, 6 e 12")
        self.assertEqual(_slide_list_text([2], di=True), "della slide 2")
        self.assertEqual(_slide_list_text([2, 5], di=True), "delle slide 2 e 5")

    def test_slide_that_paid_for_the_floor_is_declared(self):
        # Il pavimento è a risorse nulle: la slide 11 ha perso 4.8s per farne
        # respirare la 12. Prima non compariva da nessuna parte (non era né
        # "garantita" né anomala): alterata in silenzio.
        out = self._render(
            durations=[60.0, 8.1, 8.0, 60.0],
            slide_ids=[1, 2, 3, 4],
            total_duration=136.1,
            floor_report={
                "min_seconds": 8.0,
                "guaranteed": [{"slide": 3, "before": 3.2, "duration": 8.0}],
                "shortened": [{"slide": 2, "before": 12.9, "duration": 8.1}],
                "unguaranteed": [],
            },
        )
        self.assertIn("L'anti-flicker ha accorciato", out)
        self.assertIn("la slide 2", out)

    def test_single_anomalous_slide_is_worded_in_the_singular(self):
        out = self._render(verdicts={2: "disallineata", 13: "incerto"})
        self.assertIn("il parlato della slide 2 somiglia", out)
        self.assertIn("per la slide 13 la durata è anomala", out)
        self.assertNotIn("per le slide 13", out)

    def test_no_floor_report_keeps_the_old_summary(self):
        # Nessun pavimento applicato (flusso libero): nessuna riga in più.
        out = self._render(verdicts={3: "incerto"})
        self.assertNotIn("Durata garantita dall'anti-flicker", out)
        self.assertIn("slide 3 la durata è anomala", out)


class TestAnomalousDurations(unittest.TestCase):
    """Guard-rail durate anomale del riepilogo finale (main._find_anomalous_durations)."""

    @staticmethod
    def _find(durations, slide_ids):
        from main import _find_anomalous_durations

        # Il risultato porta anche la POSIZIZIONE del segmento: si verifica
        # qui che le triple siano coerenti con l'input, così i test restano
        # leggibili come "quale slide" senza rinunciare al dato posizionale.
        out = _find_anomalous_durations(durations, slide_ids)
        assert all(0 <= pos < len(slide_ids) for pos, _, _ in out), out
        return [(s, d) for _, s, d in out]

    def test_long_slide_flagged(self):
        self.assertEqual(self._find([100.0, 100.0, 100.0, 400.0], [1, 2, 3, 4]), [(4, 400.0)])

    def test_short_slide_flagged(self):
        self.assertEqual(self._find([100.0, 100.0, 100.0, 20.0], [1, 2, 3, 4]), [(4, 20.0)])

    def test_balanced_no_warning(self):
        self.assertEqual(self._find([120.0, 130.0, 110.0, 140.0], [1, 2, 3, 4]), [])

    def test_too_few_slides_ignored(self):
        self.assertEqual(self._find([100.0, 400.0], [1, 2]), [])

    def test_position_travels_with_the_result(self):
        # La posizione è ciò che permette a chi consuma il risultato di sapere
        # QUALE occorrenza della slide è anomala (la stessa slide può comparire
        # più volte nel flusso free-order).
        from main import _find_anomalous_durations

        out = _find_anomalous_durations([100.0, 100.0, 100.0, 400.0], [1, 2, 3, 4])
        self.assertEqual(out, [(3, 4, 400.0)])

    def test_floor_duration_is_not_an_alignment_anomaly(self):
        # Caso reale (25/09): la soglia "breve" (0.25 * mediana) e il pavimento
        # anti-flicker (8s) si toccavano, e una durata di 8.0s veniva dichiarata
        # anomala mentre una di 8.1s no. Il pavimento è conosciuto: passandolo,
        # le due soglie non possono più contraddirsi.
        from main import _find_anomalous_durations

        # Durate reali della run: mediana 32.2s -> soglia breve 8.05s.
        durations = [13.8, 62.6, 39.7, 52.1, 32.2, 8.0, 15.6, 127.0, 141.1, 27.9, 8.1, 8.0, 8.0, 58.9, 222.2]
        slide_ids = list(range(1, 16))
        without_floor = _find_anomalous_durations(durations, slide_ids)
        with_floor = _find_anomalous_durations(durations, slide_ids, min_seconds=8.0)
        # Le quattro slide fermi al pavimento (6, 12, 13 a 8.0s e 11 a 8.1s)
        # spariscono: il loro tempo è concesso, non misurato.
        self.assertEqual(
            sorted((s for _, s, _ in without_floor)), [6, 8, 9, 12, 13, 15]
        )
        self.assertEqual(sorted((s for _, s, _ in with_floor)), [8, 9, 15])
        # Le durate lunghe restano segnalate: quelle sono davvero da verificare.
        self.assertIn((8, 127.0), [(s, d) for _, s, d in with_floor])
        self.assertIn((9, 141.1), [(s, d) for _, s, d in with_floor])
        self.assertIn((15, 222.2), [(s, d) for _, s, d in with_floor])

    def test_floor_exclusion_does_not_swallow_a_genuinely_short_slide(self):
        # Escludere il pavimento non deve nascondere una slide VERAMENTE breve:
        # una durata sotto il pavimento non è concessa, è un problema.
        from main import _find_anomalous_durations

        out = _find_anomalous_durations([100.0, 100.0, 100.0, 2.0], [1, 2, 3, 4], min_seconds=8.0)
        self.assertEqual(out, [(3, 4, 2.0)])

    def test_floor_zero_keeps_the_historical_behaviour(self):
        # Senza informazione sul pavimento il comportamento non cambia: la
        # soglia corta resta quella storica.
        self.assertEqual(self._find([100.0, 100.0, 100.0, 8.0], [1, 2, 3, 4]), [(4, 8.0)])


class TestAnomalousContentValidation(unittest.TestCase):
    """Verifica di contenuto dei segmenti anomali (A2): il parlato del
    segmento viene confrontato con l'OCR delle slide per distinguere una
    durata anomala REALE da un allineamento errato."""

    SLIDES: ClassVar[list[str]] = [
        "Introduzione alla fisica quantistica",
        "Meccanica newtoniana leggi del moto",
        "Elettromagnetismo campi elettrici e magnetici",
        "Termodinamica entropia e calore",
    ]

    @staticmethod
    def _validate(anomalous, durations, slide_ids, words_raw):
        from main import _validate_anomalous_segments

        # `anomalous` resta in forma (slide, durata) per leggibilità: la
        # posizione si ricava cercando la slide nella sequenza mostrata.
        positions = [
            (slide_ids.index(s), s, d) for s, d in anomalous
        ]
        return _validate_anomalous_segments(
            positions, TestAnomalousContentValidation.SLIDES, words_raw, durations
        )

    def test_long_segment_with_coherent_content_downgraded(self):
        # Slide 3 dura 400s ma il parlato nel suo segmento parla davvero di
        # elettromagnetismo: durata reale, nessun allarme.
        words = _words(
            [
                ("parliamo", 10.0),
                ("della", 11.0),
                ("fisica", 12.0),
                ("elettromagnetismo", 210.0),
                ("campi", 211.0),
                ("elettrici", 212.0),
                ("magnetici", 213.0),
                ("elettromagnetismo", 300.0),
                ("campi", 301.0),
                ("elettrici", 302.0),
                ("magnetici", 303.0),
            ]
        )
        verdicts = self._validate([(3, 400.0)], [100.0, 100.0, 400.0, 100.0], [1, 2, 3, 4], words)
        self.assertEqual(verdicts[3], "coerente")

    def test_long_segment_matching_other_slide_flagged(self):
        # Slide 3 dura 400s ma il parlato del segmento parla di termodinamica
        # (slide 4): probabile allineamento errato.
        words = _words(
            [
                ("entropia", 210.0),
                ("calore", 211.0),
                ("termodinamica", 212.0),
                ("entropia", 300.0),
                ("calore", 301.0),
            ]
        )
        verdicts = self._validate([(3, 400.0)], [100.0, 100.0, 400.0, 100.0], [1, 2, 3, 4], words)
        self.assertEqual(verdicts[3], "disallineata")

    def test_repeated_slide_uses_the_right_occurrence(self):
        # Nel flusso free-order la stessa slide può comparire più volte nella
        # sequenza mostrata. Gli offset sono posizionali, quindi il verdetto va
        # calcolato sulla POSIZIONE del segmento anomalo: prima si cercava la
        # slide con `.index()`, che restituisce la prima occorrenza e faceva
        # guardare al segmento sbagliato.
        #
        # I dati sono scelti perché le due posizioni DANO verdetti diversi:
        # la durata anomala è breve (2s) e il parlato di ciascuna occorrenza è
        # diverso. Leggendo il segmento 0 si concluderebbe "coerente" (parla di
        # fisica, che è la slide 1) mentre il segmento 2 parla di newtoniana,
        # cioè la slide 2: il verdetto vero è "disallineata". Il difetto non
        # era solo "la posizione sbagliata": produceva un falso "tutto regola"
        # su un allineamento errato, cioè nascondeva il difetto che il
        # guard-rail esiste per trovare.
        from main import (
            _find_anomalous_durations,
            _validate_anomalous_segments,
        )

        slides = ["Introduzione alla fisica quantistica", "Meccanica newtoniana"]
        words = _words(
            [
                # segmento 0 (0-100s): parla di fisica -> la slide 1
                ("fisica", 0.5),
                ("quantistica", 1.0),
                # segmento 2 (200-202s): parla di newtoniana -> la slide 2
                ("meccanica", 200.5),
                ("newtoniana", 201.0),
            ]
        )
        durations = [100.0, 100.0, 2.0]
        slide_ids = [1, 2, 1]  # la slide 1 ripresa
        anomalous = _find_anomalous_durations(durations, slide_ids)
        self.assertEqual(anomalous, [(2, 1, 2.0)])  # la 2a occorrenza della slide 1
        verdicts = _validate_anomalous_segments(anomalous, slides, words, durations)
        self.assertEqual(verdicts[1], "disallineata")
        # Sanity: leggendo la posizione sbagliata il verdetto sarebbe l'altro,
        # quindi il test distingue davvero le due finestre.
        sbagliato = _validate_anomalous_segments(
            [(0, 1, 2.0)], slides, words, durations
        )
        self.assertEqual(sbagliato[1], "coerente")

    def test_no_lexical_overlap_uncertain(self):
        # Parlato senza alcuna parola in comune con le slide: segnale debole,
        # si conserva l'avviso generico.
        words = _words(
            [
                ("qualcosa", 210.0),
                ("altro", 211.0),
                ("diverso", 212.0),
                ("completamente", 213.0),
            ]
        )
        verdicts = self._validate([(3, 400.0)], [100.0, 100.0, 400.0, 100.0], [1, 2, 3, 4], words)
        self.assertEqual(verdicts[3], "incerto")

    def test_sync_report_lists_segments_and_verdicts(self):
        # Il report è l'artefatto verificabile: una voce per slide con
        # inizio/fine/durata e il verdetto dei soli segmenti anomali.
        from main import _build_sync_report

        report = _build_sync_report(
            [100.0, 50.0, 150.0],
            [1, 2, 3],
            300.0,
            {2: "disallineata"},
        )
        self.assertEqual(report["audio_duration"], 300.0)
        segments = report["segments"]
        self.assertEqual([s["slide"] for s in segments], [1, 2, 3])
        self.assertEqual(
            segments[0],
            {"slide": 1, "start": 0.0, "end": 100.0, "duration": 100.0},
        )
        self.assertEqual(segments[1]["start"], 100.0)
        self.assertEqual(segments[1]["end"], 150.0)
        self.assertEqual(segments[1]["verdict"], "disallineata")
        self.assertNotIn("verdict", segments[2])
        # Senza note né revisione il report resta minimale.
        self.assertEqual(sorted(report), ["audio_duration", "segments"])

    def test_sync_report_carries_notes_and_review_diffs(self):
        # Le scelte della run (es. escalation per segnale debole) e le
        # discrepanze del secondo passaggio LLM devono restare sull'artefatto:
        # prima esistevano solo nei log e andavano perse.
        from main import _build_sync_report

        report = _build_sync_report(
            [60.0, 40.0],
            [1, 2],
            100.0,
            None,
            notes={"engine": "llm_escalated_weak_signal"},
            review_diffs=[{"chunk": 3, "slide": 7}],
        )
        self.assertEqual(report["engine"], "llm_escalated_weak_signal")
        self.assertEqual(report["review_diffs"], [{"chunk": 3, "slide": 7}])

    def test_should_escalate_weak_signal(self):
        # Segnale debole + slide da posizionare + LLM disponibile: si passa
        # all'LLM invece di generare un video potenzialmente disallineato.
        from main import _should_escalate_weak_signal

        self.assertTrue(_should_escalate_weak_signal(True, 2, True))
        # Tutte le slide hanno un'ancora: il segnale debole non cambia nulla.
        self.assertFalse(_should_escalate_weak_signal(True, 0, True))
        # --llm off: resta il motore locale.
        self.assertFalse(_should_escalate_weak_signal(True, 2, False))
        # Segnale buono: nessuna escalation.
        self.assertFalse(_should_escalate_weak_signal(False, 2, True))

    def test_local_engine_runs_first_and_llm_is_only_an_escalation(self):
        # Il caso reale del 25/09: 12 slide senza ancora, oltre la soglia (2).
        # Prima l'LLM era un PREREQUISITO (si saltava il motore locale) e se
        # falliva si ricalcolava da capo: ~328s per arrivare al fallback locale.
        # Ora il motore locale gira SEMPRE per primo e l'LLM è un tentativo di
        # migliorare una timeline che esiste già.
        from main import _needs_llm_escalation

        # Motore locale riuscito con segnale buono, poche slide mancanti:
        # nessuna ragione di pagare l'LLM.
        self.assertFalse(
            _needs_llm_escalation(
                local_failed=False,
                weak_local=False,
                missing_count=2,
                llm_local_threshold=2,
            )
        )

    def test_escalates_when_the_local_engine_failed(self):
        from main import _needs_llm_escalation

        self.assertTrue(
            _needs_llm_escalation(
                local_failed=True,
                weak_local=False,
                missing_count=1,
                llm_local_threshold=2,
            )
        )

    def test_escalates_when_too_many_slides_are_unanchored(self):
        from main import _needs_llm_escalation

        self.assertTrue(
            _needs_llm_escalation(
                local_failed=False,
                weak_local=False,
                missing_count=12,
                llm_local_threshold=2,
            )
        )

    def test_escalates_on_a_weak_signal(self):
        from main import _needs_llm_escalation

        self.assertTrue(
            _needs_llm_escalation(
                local_failed=False,
                weak_local=True,
                missing_count=3,
                llm_local_threshold=10,
            )
        )

    def test_llm_off_never_escalates(self):
        # --llm off: la timeline locale è quella definitiva, per quanto debole
        # sia il segnale.
        from main import _needs_llm_escalation

        self.assertFalse(
            _needs_llm_escalation(
                local_failed=True,
                weak_local=True,
                missing_count=12,
                llm_local_threshold=2,
                llm_enabled=False,
            )
        )


class TestFreeOrderSelection(unittest.TestCase):
    """Selezione libera: slide in qualsiasi ordine, ripetute, anti-flicker."""

    @staticmethod
    def _fake_embed(themes):
        def _embed(texts):
            out = []
            for t in texts:
                v = np.zeros(len(themes))
                for i, k in enumerate(themes):
                    if k in t:
                        v[i] = 1.0
                norm = np.linalg.norm(v)
                out.append(v / norm if norm else v)
            return np.array(out)

        return _embed

    @staticmethod
    def _blocks(themes, window=5.0):
        """Blocchi: 2 per tema, in un ordine volutamente FUORI sequenza."""
        seq = []
        for i, t in enumerate(themes):
            seq.append({"time": i * window, "first_time": i * window + 1.0, "text": t * 4})
            seq.append({"time": i * window + window / 2, "first_time": i * window + window / 2 + 0.5, "text": t * 4})
        return seq

    def test_out_of_order_repeats(self):
        # Ordine audio: alfa, gamma, beta, gamma (NON 1,2,3,4).
        # La selezione libera deve seguire il contenuto: 1, 3, 2, 3.
        from semantic_sync import free_order_segments_from_texts

        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = self._blocks(["alfa"]) + self._blocks(["gamma"]) + self._blocks(["beta"]) + self._blocks(["gamma"])
        segs = free_order_segments_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_segment_seconds=5.0),
        )
        self.assertIsNotNone(segs)
        order = [int(s["slide"]) for s in segs]
        # 4 blocchi da 2 (alfa, gamma, beta, gamma) -> ordine atteso
        self.assertEqual(order, [1, 3, 2, 3])
        # La slide 3 compare due volte: riordino + ripetizione
        self.assertEqual(order.count(3), 2)

    def test_anchor_example_overt_covert(self):
        # Scenario del caso reale: si parla di "overt" (slide 4) a inizio e
        # di nuovo a fine audio; nel mezzo altri temi. Slide 4 appare 2 volte.
        from semantic_sync import free_order_segments_from_texts

        themes = ["alfa", "overt", "beta", "gamma"]
        blocks = (
            self._blocks(["overt"])  # slide 2 (overt)
            + self._blocks(["beta"])  # slide 3
            + self._blocks(["gamma"])  # slide 4
            + self._blocks(["overt"])  # slide 2 di nuovo
        )
        segs = free_order_segments_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_segment_seconds=5.0),
        )
        self.assertIsNotNone(segs)
        order = [int(s["slide"]) for s in segs]
        self.assertEqual(order, [2, 3, 4, 2])

    def test_antiflicker_merges_short_segments(self):
        # Alternanza rapida 1,2,1,2 con segmenti da 1 blocco (5s < min 12s):
        # l'anti-flicker deve fonderli in segmenti lunghi, non alternare.
        from semantic_sync import free_order_segments_from_texts

        themes = ["alfa", "beta"]
        blocks = [
            {"time": i * 5.0, "first_time": i * 5.0 + 1.0, "text": ("alfa" if i % 2 == 0 else "beta") * 4}
            for i in range(8)
        ]
        segs = free_order_segments_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=2,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_segment_seconds=12.0),
        )
        self.assertIsNotNone(segs)
        # Con min_segment_seconds=12 (2.4 blocchi) l'alternanza si stabilizza
        # in al massimo 3 segmenti: non può alternare ogni blocco
        self.assertLessEqual(len(segs), 3)
        for s in segs:
            self.assertGreaterEqual(float(s["end"]) - float(s["start"]), 5.0)

    def test_adjacent_same_slide_merged(self):
        # Dopo l'anti-flicker due segmenti adiacenti della stessa slide non
        # devono restare separati (es. blocchi 8-8 o 14-14-14 consecutivi).
        import numpy as np

        from semantic_sync import _smooth_segments

        # 12 blocchi: 4x slide1, 1x slide2 (corto), 7x slide1
        best = np.array([1, 1, 1, 1, 2, 1, 1, 1, 1, 1, 1, 1])
        segs = _smooth_segments(best, min_blocks=2)
        # Il blocco singolo di slide 2 viene fuso e resta un unico segmento
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0][0], 1)
        self.assertEqual((segs[0][1], segs[0][2]), (0, 12))

    def test_too_few_blocks_returns_none(self):
        from semantic_sync import free_order_segments_from_texts

        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = self._blocks(["alfa"])[:1]  # 1 solo blocco
        segs = free_order_segments_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0),
        )
        self.assertIsNone(segs)

    def test_first_segment_starts_at_zero(self):
        # Con silenzio iniziale (prima parola a 2.0s) il video deve comunque
        # partire da 0.0, come nel flusso classico (niente audio perso).
        from semantic_sync import free_order_segments_from_texts

        themes = ["alfa", "beta"]
        blocks = [
            {"time": 0.0, "first_time": 2.0, "text": "alfa alfa alfa"},
            {"time": 5.0, "first_time": 5.5, "text": "beta beta beta"},
            {"time": 10.0, "first_time": 10.5, "text": "beta beta beta"},
        ]
        segs = free_order_segments_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=2,
            total_duration=20.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_segment_seconds=5.0),
        )
        self.assertIsNotNone(segs)
        self.assertEqual(float(segs[0]["start"]), 0.0)

    def test_low_normalized_peak_is_signalled_not_discarded(self):
        # Come nel flusso ordinato: nessuna guardia può "scartare" sulla scala
        # grezza dei coseni (su dati reali è sempre >0.75, anche con rumore), e
        # la selezione libera non la renderebbe inutilizzabile. Si segnala.
        from semantic_sync import (
            free_order_segments_from_texts,
            reset_weak_signal_flag,
            weak_signal_seen,
        )

        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [
            {"time": i * 5.0, "first_time": i * 5.0 + 0.5, "text": "zappa qwerty nullo"}
            for i in range(10)
        ]
        reset_weak_signal_flag()
        segs = free_order_segments_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=50.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0),
        )
        self.assertIsNotNone(segs)
        self.assertTrue(weak_signal_seen())



class TestEmbedModelFallback(unittest.TestCase):
    """Fallback automatico del modello embedding (mpnet -> MiniLM)."""

    @staticmethod
    def _patch_text_embedding(side_effect):
        from unittest.mock import patch

        return patch("semantic_sync.TextEmbedding", side_effect=side_effect)

    def _load(
        self,
        primary="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        alternate="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    ):
        from semantic_sync import _load_embed_model

        return _load_embed_model(primary, ".cache/embedding_model", alternate_name=alternate)

    def test_fallback_used_when_primary_fails(self):
        from semantic_sync import _load_embed_model

        class FakeModel:
            pass

        def side_effect(model_name, cache_dir, **_kwargs):
            if "mpnet" in model_name:
                raise RuntimeError("download interrotto")
            return FakeModel()

        with self._patch_text_embedding(side_effect):
            model = _load_embed_model(
                "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
                ".cache/embedding_model",
                alternate_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            )
        self.assertIsInstance(model, FakeModel)

    def test_primary_preferred_over_alternate(self):
        from semantic_sync import _load_embed_model

        class FakeModel:
            pass

        called = []

        def side_effect(model_name, cache_dir, **_kwargs):
            called.append(model_name)
            return FakeModel()

        with self._patch_text_embedding(side_effect):
            _load_embed_model("primary-model", ".cache", alternate_name="alternate-model")
        self.assertEqual(called, ["primary-model"])

    def test_same_primary_and_alternate_tried_once(self):
        from semantic_sync import _load_embed_model

        class FakeModel:
            pass

        called = []

        def side_effect(model_name, cache_dir, **_kwargs):
            called.append(model_name)
            return FakeModel()

        with self._patch_text_embedding(side_effect):
            _load_embed_model("only-model", ".cache", alternate_name="only-model")
        self.assertEqual(called, ["only-model"])

    def test_both_fail_returns_none(self):
        def side_effect(model_name, cache_dir, **_kwargs):
            raise RuntimeError("rete assente")

        with self._patch_text_embedding(side_effect):
            model = self._load()
        self.assertIsNone(model)


class TestLlmSegmentPostProcessing(unittest.TestCase):
    """Post-elaborazione dei segmenti LLM (flusso libero): confini a livello
    di parola (refine) + merge anti-flicker dei segmenti corti."""

    # ------------------------------------------------------------------
    # merge_short_segments
    # ------------------------------------------------------------------
    def test_merge_short_trailing_segment(self):
        # L'ultimo chunk parziale (10s) viene assorbito dal precedente.
        segments = [
            {"slide": 1, "start": 0.0, "end": 100.0},
            {"slide": 2, "start": 100.0, "end": 110.0},
        ]
        out = merge_short_segments(segments, min_seconds=15.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["slide"], 1)
        self.assertAlmostEqual(out[0]["start"], 0.0)
        self.assertAlmostEqual(out[0]["end"], 110.0)

    def test_merge_short_internal_absorbs_into_longer_neighbor(self):
        segments = [
            {"slide": 1, "start": 0.0, "end": 100.0},
            {"slide": 3, "start": 100.0, "end": 110.0},  # corto
            {"slide": 2, "start": 110.0, "end": 300.0},
        ]
        out = merge_short_segments(segments, min_seconds=15.0)
        # Assorbito dal vicino più lungo (slide 2, 190s > 100s).
        self.assertEqual([s["slide"] for s in out], [1, 2])
        self.assertAlmostEqual(out[1]["start"], 100.0)
        self.assertAlmostEqual(out[1]["end"], 300.0)

    def test_merge_keeps_long_segments(self):
        segments = [
            {"slide": 1, "start": 0.0, "end": 50.0},
            {"slide": 2, "start": 50.0, "end": 100.0},
        ]
        self.assertEqual(merge_short_segments(segments, min_seconds=15.0), segments)

    def test_merge_unites_adjacent_same_slide_after_absorption(self):
        # Il corto (slide 3, 100-110) assorbito dal vicino più lungo (slide 1)
        # lascia due segmenti adiacenti con la STESSA slide (1): uniti in uno.
        segments = [
            {"slide": 1, "start": 0.0, "end": 100.0},
            {"slide": 3, "start": 100.0, "end": 110.0},  # corto
            {"slide": 1, "start": 110.0, "end": 200.0},
        ]
        out = merge_short_segments(segments, min_seconds=15.0)
        self.assertEqual([s["slide"] for s in out], [1])
        self.assertAlmostEqual(out[0]["start"], 0.0)
        self.assertAlmostEqual(out[0]["end"], 200.0)

    # ------------------------------------------------------------------
    # refine_llm_segment_boundaries
    # ------------------------------------------------------------------
    def test_refine_moves_boundary_to_topic_change(self):
        # Fino a 40s si parla del tema A (slide 1), poi del tema B (slide 2).
        words = []
        t = 0.0
        for _ in range(20):
            words.append({"word": "tema_a", "start": t})
            t += 2.0
        for _ in range(20):
            words.append({"word": "tema_b", "start": t})
            t += 2.0
        segments = [
            {"slide": 1, "start": 0.0, "end": 30.0},
            {"slide": 2, "start": 30.0, "end": 80.0},
        ]

        def embed_fn(texts):
            vecs = []
            for text in texts:
                v = np.array([text.count("tema_a"), text.count("tema_b")], dtype=np.float32)
                v = v / max(np.linalg.norm(v), 1e-9)
                vecs.append(v)
            return np.stack(vecs)

        out = refine_llm_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b"],
            embed_fn,
            window_seconds=30.0,
            min_segment_seconds=5.0,
            context_seconds=10.0,
        )
        # Il confine si sposta al punto di cambio argomento (40s) e i due
        # segmenti restano contigui.
        self.assertGreater(out[1]["start"], 30.0)
        self.assertLessEqual(out[1]["start"], 42.0)
        self.assertAlmostEqual(out[0]["end"], out[1]["start"])

    def test_refine_no_move_without_improvement(self):
        # Embedding piatto: nessun candidato è meglio del confine attuale.
        words = [{"word": "x", "start": float(i)} for i in range(100)]
        segments = [
            {"slide": 1, "start": 0.0, "end": 50.0},
            {"slide": 2, "start": 50.0, "end": 100.0},
        ]

        def embed_fn(texts):
            return np.full((len(texts), 2), 1.0 / np.sqrt(2), dtype=np.float32)

        out = refine_llm_segment_boundaries(segments, words, ["a", "b"], embed_fn)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[1]["start"], 50.0)

    def test_refine_boundary_never_leaves_segment_limits(self):
        # Il cambio argomento (tema_b) avviene a 75s, OLTRE il limite hi del
        # confine (70s = t_current + window): il confine si sposta al massimo
        # fino a hi, senza superare i limiti del segmento.
        words = [{"word": "tema_a", "start": float(i)} for i in range(75)]
        words += [{"word": "tema_b", "start": float(i)} for i in range(75, 130)]
        segments = [
            {"slide": 1, "start": 0.0, "end": 50.0},
            {"slide": 2, "start": 50.0, "end": 100.0},
        ]

        def embed_fn(texts):
            vecs = []
            for text in texts:
                v = np.array([text.count("tema_a"), text.count("tema_b")], dtype=np.float32)
                v = v / max(np.linalg.norm(v), 1e-9)
                vecs.append(v)
            return np.stack(vecs)

        out = refine_llm_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b"],
            embed_fn,
            window_seconds=20.0,
            min_segment_seconds=8.0,
            context_seconds=10.0,
        )
        # Clampa a hi = t_current + window = 70 (il cambio argomento è a 75s).
        self.assertAlmostEqual(out[1]["start"], 70.0)
        self.assertAlmostEqual(out[0]["end"], out[1]["start"])

    def test_refine_fallback_on_embedding_error(self):
        segments = [
            {"slide": 1, "start": 0.0, "end": 30.0},
            {"slide": 2, "start": 30.0, "end": 60.0},
        ]

        def embed_fn(texts):
            raise RuntimeError("modello non disponibile")

        out = refine_llm_segment_boundaries(segments, [{"word": "x", "start": 5.0}], ["a", "b"], embed_fn)
        self.assertEqual(out, segments)

    def test_refine_restricted_to_refine_slides(self):
        # ``refine_slides`` limita il raffinamento: il confine della slide 2
        # (non candidata) resta ESATTAMENTE dov'è, quello della slide 3
        # (candidata) si sposta al cambio argomento reale (80s).
        words = [{"word": "tema_a", "start": float(i)} for i in range(0, 40, 2)]
        words += [{"word": "tema_b", "start": float(i)} for i in range(40, 80, 2)]
        words += [{"word": "tema_c", "start": float(i)} for i in range(80, 120, 2)]
        segments = [
            {"slide": 1, "start": 0.0, "end": 50.0},
            {"slide": 2, "start": 50.0, "end": 90.0},
            {"slide": 3, "start": 90.0, "end": 130.0},
        ]

        def embed_fn(texts):
            vecs = []
            for text in texts:
                v = np.array(
                    [text.count("tema_a"), text.count("tema_b"), text.count("tema_c")], dtype=np.float32
                )
                v = v / max(np.linalg.norm(v), 1e-9)
                vecs.append(v)
            return np.stack(vecs)

        out = refine_llm_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b", "tema_c"],
            embed_fn,
            window_seconds=30.0,
            min_segment_seconds=5.0,
            context_seconds=10.0,
            refine_slides={3},
        )
        # La slide 2 non era candidata: confine invariato.
        self.assertAlmostEqual(out[1]["start"], 50.0)
        # La slide 3 era candidata: confine spostato al cambio argomento (80s).
        self.assertAlmostEqual(out[2]["start"], 80.0)
        self.assertAlmostEqual(out[1]["end"], out[2]["start"])

    def test_refine_ordered_llm_timeline(self):
        # Flusso ordinato: la slide 2 ha ancora esatta a 25s (vincolo
        # inviolabile); le slide 3 e 4 (senza ancora) partono su una griglia
        # chunk (55s e 95s) e vengono raffinati ai cambi argomento reali.
        words = [{"word": "tema_a", "start": float(i)} for i in range(0, 40, 2)]
        words += [{"word": "tema_b", "start": float(i)} for i in range(40, 80, 2)]
        words += [{"word": "tema_c", "start": float(i)} for i in range(80, 120, 2)]
        words += [{"word": "tema_d", "start": float(i)} for i in range(120, 160, 2)]
        timeline = {1: 0.0, 2: 25.0, 3: 55.0, 4: 95.0}
        anchors = {2: 25.0}

        def embed_fn(texts):
            vecs = []
            for text in texts:
                v = np.array(
                    [
                        text.count("tema_a"),
                        text.count("tema_b"),
                        text.count("tema_c"),
                        text.count("tema_d"),
                    ],
                    dtype=np.float32,
                )
                v = v / max(np.linalg.norm(v), 1e-9)
                vecs.append(v)
            return np.stack(vecs)

        out = refine_ordered_llm_timeline(
            timeline,
            anchors,
            words,
            ["tema_a", "tema_b", "tema_c", "tema_d"],
            total_duration=160.0,
            embed_fn=embed_fn,
            window_seconds=30.0,
            min_segment_seconds=5.0,
        )
        # Ancora esatta preservata; slide senza ancora ai cambi argomento (80/120).
        self.assertAlmostEqual(out[1], 0.0)
        self.assertAlmostEqual(out[2], 25.0)
        self.assertAlmostEqual(out[3], 80.0)
        self.assertAlmostEqual(out[4], 120.0)
        # Monotonicita strettamente crescente conservata.
        times = [out[s] for s in sorted(out)]
        self.assertTrue(all(b > a for a, b in pairwise(times)))

    def test_refine_ordered_all_anchored_no_change(self):
        # Tutte le slide hanno ancora esplicita: nessun candidato, timeline
        # restituita invariata (le ancore non si toccano mai).
        words = [{"word": "x", "start": float(i)} for i in range(100)]
        timeline = {1: 0.0, 2: 30.0, 3: 60.0}
        anchors = {2: 30.0, 3: 60.0}

        def embed_fn(texts):
            return np.full((len(texts), 2), 1.0 / np.sqrt(2), dtype=np.float32)

        out = refine_ordered_llm_timeline(
            timeline,
            anchors,
            words,
            ["a", "b", "c"],
            total_duration=100.0,
            embed_fn=embed_fn,
        )
        self.assertEqual(out, timeline)


class TestSemanticAnchorInvariants(unittest.TestCase):
    """Ancore vs programmazione dinamica nella timeline semantica.

    Un'ancora fuori dalla finestra fattibile non deve sciogliere il vincolo
    (la DP poteva piazzare la slide ovunque), e un'ancora spostata dal clamp
    di monotonicità deve essere segnalata: erano violazioni silenziose.
    """

    @staticmethod
    def _fake_embed(themes):
        """Embedder finto: vettore one-hot per ogni parola-tema presente."""

        def _embed(texts):
            out = []
            for t in texts:
                v = np.zeros(len(themes))
                for i, k in enumerate(themes):
                    if k in t:
                        v[i] = 1.0
                norm = np.linalg.norm(v)
                out.append(v / norm if norm else v)
            return np.array(out)

        return _embed

    def test_anchor_outside_feasible_window_keeps_nearest_block(self):
        # 10 blocchi, 5 slide, min_gap=2: per la slide 5 la finestra fattibile è
        # [8, 9]. Un'ancora a t=0 cade prima di lo: il candidato deve essere il
        # blocco fattibile PIÙ VICINO (8), non l'intero intervallo libero.
        blocks = [{"time": i * 5.0, "text": "x"} for i in range(10)]
        cands = build_candidates(10, 5, min_gap=2, blocks=blocks, anchors={5: 0.0})
        self.assertIsNotNone(cands)
        self.assertEqual(cands[4], [8])

    def test_anchor_inside_window_restricts_to_neighbourhood(self):
        blocks = [{"time": i * 5.0, "text": "x"} for i in range(10)]
        cands = build_candidates(10, 3, min_gap=1, blocks=blocks, anchors={2: 20.0})
        self.assertIsNotNone(cands)
        # Il blocco più vicino a 20s è l'indice 4 -> {3, 4, 5}.
        self.assertEqual(cands[1], [3, 4, 5])

    def test_displaced_anchor_is_clamped_and_warned(self):
        # Ancore non monotone: la slide 3 è pronunciata a 5.0s ma la slide 2 a
        # 12.3s. Il clamp di monotonicità porta la 3 a 12.8s e lo SEGNALA,
        # invece di spostarla in silenzio.
        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [
            {"time": i * 5.0, "text": (themes[i // 2] + " ") * 4} for i in range(8)
        ]
        with self.assertLogs(level="WARNING") as captured:
            tl = semantic_timeline_from_texts(
                [f"{t} slide" for t in themes],
                blocks,
                total_slides=4,
                total_duration=40.0,
                embed_fn=self._fake_embed(themes),
                options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
                anchors={2: 12.3, 3: 5.0},
            )
        self.assertIsNotNone(tl)
        self.assertAlmostEqual(tl[2], 12.3, places=3)
        self.assertAlmostEqual(tl[3], 12.8, places=3)
        self.assertTrue(any("spostata" in m for m in captured.output))

    def test_slides_between_two_anchors_cannot_collapse_on_one_block(self):
        # Ancore a 100s e 110s con tre slide non ancorate in mezzo: senza il
        # vincolo fra ancore la similarità può assegnare a tutte e tre lo stesso
        # blocco e produrre segmenti di mezzo secondo, con confini che il
        # pavimento anti-flicker non può spostare (entrambi i vicini ancorati).
        from semantic_sync import build_candidates

        blocks = [{"time": i * 5.0, "text": "x"} for i in range(20)]
        cands = build_candidates(
            20, 6, 1, blocks=blocks, anchors={2: 100.0, 6: 110.0}
        )
        self.assertIsNotNone(cands)
        # slide 2 -> blocco 20, slide 6 -> blocco 22: le slide 3, 4, 5 devono
        # stare dentro l'intervallo (20, 22)... che non ha spazio: il vincolo
        # non viene applicato e i candidati restano quelli globali.
        self.assertGreater(len(cands[2]), 1)

    def test_unanchored_slides_are_confined_between_anchors(self):
        from semantic_sync import build_candidates

        blocks = [{"time": i * 5.0, "text": "x"} for i in range(40)]
        cands = build_candidates(
            40, 6, 1, blocks=blocks, anchors={2: 20.0, 6: 140.0}
        )
        self.assertIsNotNone(cands)
        lo = 4  # blocco di 20.0s
        hi = 28  # blocco di 140.0s
        for s in (3, 4, 5):
            self.assertTrue(cands[s - 1])
            self.assertGreaterEqual(min(cands[s - 1]), lo + 1)
            self.assertLessEqual(max(cands[s - 1]), hi - 1)

    def test_anchor_gap_vincolo_never_empties_a_candidate_set(self):
        # Due ancore troppo vicine per le slide in mezzo: stringere i candidati
        # lascerebbe la slide senza opzioni, cioè peggio di non vincolare.
        from semantic_sync import build_candidates

        blocks = [{"time": i * 5.0, "text": "x"} for i in range(20)]
        cands = build_candidates(
            20, 8, 1, blocks=blocks, anchors={2: 20.0, 8: 35.0}
        )
        self.assertIsNotNone(cands)
        for s in range(3, 8):
            self.assertTrue(cands[s - 1])


class TestVerifyAnchorMappingEmbedding(unittest.TestCase):
    """Verifica deterministica del mapping ancore (offset numerazione parlata)."""

    @staticmethod
    def _embed_fn(num_slides):
        """Embedder finto: vettore one-hot per slide, il parlato e' sempre la
        slide (s+1) rispetto al numero pronunciato (copertina esclusa)."""

        def _embed(texts):
            out = []
            for t in texts:
                v = np.zeros(num_slides)
                for k in range(num_slides):
                    if f"tema{k + 1}" in t:
                        v[k] = 1.0
                norm = np.linalg.norm(v)
                out.append(v / norm if norm else v)
            return np.array(out)

        return _embed

    def test_systematic_offset_detected_even_with_every_slide_anchored(self):
        # Il caso che il gating saltava: TUTTE le transizioni annunciate, quindi
        # nessuna slide senza ancora. È proprio lì che uno sfasamento di
        # numerazione resta invisibile (niente da completare, l'LLM non avrebbe
        # niente da fare), eppure sposta l'intero video di una slide.
        slides = [f"tema{i} slide" for i in range(1, 6)]
        words = []
        for s in range(1, 6):
            start = 100.0 * s
            # Dopo l'annuncio di "slide s" il parlato e' gia' quello di s+1.
            words += [{"word": f"tema{s + 1}", "start": start + i} for i in range(5)]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=5,
            window_seconds=40.0,
            embed_fn=self._embed_fn(5),
        )
        # Ogni ancora slitta di +1, tranne l'ultima fuori range: nessuna
        # correzione possibile, ma il caso deve restare ispezionabile.
        self.assertIsNone(out)

    def test_full_anchor_set_aligned_is_left_alone(self):
        # Con tutte le transizioni ancorate e la numerazione corretta, la
        # verifica non deve toccare nulla (è la base per poterla eseguire
        # sempre senza costo per le run già allineate).
        slides = [f"tema{i} slide" for i in range(1, 6)]
        words = []
        for s in range(1, 6):
            start = 100.0 * s
            words += [{"word": f"tema{s}", "start": start + i} for i in range(5)]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=5,
            window_seconds=40.0,
            embed_fn=self._embed_fn(5),
        )
        self.assertIsNone(out)

    def test_systematic_plus_one_offset(self):
        # 4 slide PDF; lo speaker dice "slide 1..4" ma la finestra dopo ogni
        # riferimento parla del contenuto della slide successiva (+1: copertina
        # esclusa). L'euristica deve correggere il mapping a 2..5.
        slides = [f"tema{i} slide" for i in range(1, 7)]
        words = []
        for s in range(1, 5):
            start = 100.0 * s
            words += [{"word": f"tema{s + 1}", "start": start + i} for i in range(5)]
        anchors = {1: 100.0, 2: 200.0, 3: 300.0, 4: 400.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=6,
            window_seconds=40.0,
            embed_fn=self._embed_fn(6),
        )
        self.assertEqual(out, {2: 100.0, 3: 200.0, 4: 300.0, 5: 400.0})

    def test_no_offset_returns_none(self):
        # Numerazione corretta: il parlato dopo "slide N" parla di tema N.
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = []
        for s in range(1, 5):
            start = 100.0 * s
            words += [{"word": f"tema{s}", "start": start + i} for i in range(5)]
        anchors = {1: 100.0, 2: 200.0, 3: 300.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
        )
        self.assertIsNone(out)

    def test_inconsistent_offsets_return_none(self):
        # Offset non sistematico: la prima ancora punta a +1, le altre a 0.
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = [{"word": "tema2", "start": 100.0}, {"word": "tema2", "start": 102.0}]
        words += [{"word": "tema2", "start": 200.0}, {"word": "tema3", "start": 202.0}]
        words += [{"word": "tema3", "start": 300.0}, {"word": "tema4", "start": 302.0}]
        anchors = {1: 100.0, 2: 200.0, 3: 300.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
        )
        self.assertIsNone(out)

    def test_report_lists_anchors_not_confirmed_by_content(self):
        # Il segnale che resta sull'artefatto: quale ancora il parlato non
        # conferma. Serve a distinguere un allineamento misurato da una
        # numerazione parlata sbagliata quando quasi tutto è ancorato.
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = [{"word": "tema2", "start": 100.0}, {"word": "tema2", "start": 102.0}]
        words += [{"word": "tema2", "start": 200.0}, {"word": "tema3", "start": 202.0}]
        words += [{"word": "tema3", "start": 300.0}, {"word": "tema4", "start": 302.0}]
        anchors = {1: 100.0, 2: 200.0, 3: 300.0}

        report: dict = {}
        verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
            report=report,
        )
        # "slide 1" a 100s è seguita dal parlato di tema 2, quindi non confermata.
        self.assertEqual(
            report["unconfirmed"], [{"slide": 1, "time": 100.0, "points_to": 2}]
        )

    def test_few_anchors_returns_none(self):
        # Serve almeno 1 ancora valutabile (minimo 2 riferimenti richiesti).
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = [{"word": "tema2", "start": 100.0}, {"word": "tema2", "start": 102.0}]
        anchors = {1: 100.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
        )
        self.assertIsNone(out)

    def test_offset_mapping_partially_shifted(self):
        # Offset +1 su tutte le ancore: {1,2,3} -> {2,3,4}, tutti validi.
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = []
        for s in range(1, 4):
            start = 100.0 * s
            words += [{"word": f"tema{s + 1}", "start": start + i} for i in range(5)]
        anchors = {1: 100.0, 2: 200.0, 3: 300.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
        )
        # tema4 per ancora 3 -> offset +1 darebbe slide 4 (ok), ma ancora 1 -> tema2
        # (+1) e ancora 2 -> tema3 (+1): mapping {2,3,4} valido -> restituito.
        self.assertEqual(out, {2: 100.0, 3: 200.0, 4: 300.0})

    def test_out_of_range_fully_invalid_returns_none(self):
        # Offset +1 porterebbe la prima ancora a slide 5 > 4 (nessuna valida).
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = []
        for s in range(4, 6):
            start = 100.0 * s
            words += [{"word": f"tema{s + 1}", "start": start + i} for i in range(5)]
        anchors = {4: 400.0, 5: 500.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
        )
        self.assertIsNone(out)

    def test_step_offset_midway_returns_partial_mapping(self):
        # La numerazione e' corretta per le prime ancore e slitta di +1 dopo
        # (es. il podcast salta una slide del PDF in mezzo: dice "slide 4"
        # mostrando la slide 5). Gli offset misti farebbero fallire la verifica
        # uniforme; la correzione parziale deve rimappare SOLO il tratto
        # sfasato e lasciare intatte le ancore gia' allineate.
        slides = [f"tema{i} slide" for i in range(1, 7)]
        words = []
        for s, tema in [(2, 2), (3, 3), (4, 5), (5, 6)]:
            start = 100.0 * s
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=6,
            window_seconds=40.0,
            embed_fn=self._embed_fn(6),
        )
        self.assertEqual(out, {2: 200.0, 3: 300.0, 5: 400.0, 6: 500.0})

    def test_step_offset_single_shifted_anchor_returns_none(self):
        # Una sola ancora sfasata in coda (run di 1) non basta per dichiarare
        # uno slittamento sistematico: niente correzione (troppo rumoroso).
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = []
        for s, tema in [(2, 2), (3, 3)]:
            start = 100.0 * s
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        # l'ultima ancora parla della slide successiva: offset +1 isolato
        words += [{"word": "tema5", "start": 401.0}, {"word": "tema5", "start": 403.0}]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=5,
            window_seconds=40.0,
            embed_fn=self._embed_fn(5),
        )
        self.assertIsNone(out)

    def test_step_offset_multiple_runs_returns_none(self):
        # Due tratti sfasati separati da ancore allineate non sono un segnale
        # affidabile: niente correzione (troppo ambiguo).
        slides = [f"tema{i} slide" for i in range(1, 9)]
        words = []
        for s, tema in [(2, 3), (3, 4), (4, 4), (5, 5), (6, 7), (7, 8)]:
            start = 100.0 * s
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0, 6: 600.0, 7: 700.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=8,
            window_seconds=40.0,
            embed_fn=self._embed_fn(8),
        )
        self.assertIsNone(out)

    def test_report_suspicious_when_single_shifted_anchor(self):
        # Una sola ancora sfasata: niente correzione (run di 1), ma il mapping
        # è SOSPETTO: il chiamante deve poter chiedere la verifica LLM.
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = [
            {"word": "tema2", "start": 200.0},
            {"word": "tema3", "start": 300.0},
            {"word": "tema5", "start": 401.0},
        ]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0}
        report: dict[str, bool] = {}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=5,
            window_seconds=40.0,
            embed_fn=self._embed_fn(5),
            report=report,
        )
        self.assertIsNone(out)
        self.assertTrue(report["suspicious"])

    def test_report_suspicious_when_multiple_runs(self):
        # Due tratti sfasati separati: ambiguo e sospetto -> segnale True.
        slides = [f"tema{i} slide" for i in range(1, 9)]
        words = []
        for s, tema in [(2, 3), (3, 4), (4, 4), (5, 5), (6, 7), (7, 8)]:
            start = 100.0 * s
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0, 6: 600.0, 7: 700.0}
        report: dict[str, bool] = {}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=8,
            window_seconds=40.0,
            embed_fn=self._embed_fn(8),
            report=report,
        )
        self.assertIsNone(out)
        self.assertTrue(report["suspicious"])

    def test_report_clean_when_mapping_coherent(self):
        # Numerazione confermata dal contenuto (offset 0): nessun sospetto.
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = []
        for s in range(2, 5):
            start = 100.0 * s
            words += [{"word": f"tema{s}", "start": start + i} for i in range(5)]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0}
        report: dict[str, bool] = {}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
            report=report,
        )
        self.assertIsNone(out)
        self.assertFalse(report["suspicious"])

    def test_report_clean_when_corrected(self):
        # Corretto deterministicamente: non serve l'LLM, nessun sospetto.
        slides = [f"tema{i} slide" for i in range(1, 7)]
        words = []
        for s, tema in [(2, 2), (3, 3), (4, 5), (5, 6)]:
            start = 100.0 * s
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        anchors = {2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0}
        report: dict[str, bool] = {}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=6,
            window_seconds=40.0,
            embed_fn=self._embed_fn(6),
            report=report,
        )
        self.assertEqual(out, {2: 200.0, 3: 300.0, 5: 400.0, 6: 500.0})
        self.assertFalse(report["suspicious"])

    def test_report_not_suspicious_when_too_few_anchors(self):
        # Segnale insufficiente: non sospetto (niente chiamate LLM spurie).
        slides = [f"tema{i} slide" for i in range(1, 5)]
        words = [{"word": "tema2", "start": 100.0}]
        anchors = {1: 100.0}
        report: dict[str, bool] = {}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=4,
            window_seconds=40.0,
            embed_fn=self._embed_fn(4),
            report=report,
        )
        self.assertIsNone(out)
        self.assertFalse(report["suspicious"])

    def test_progressive_drift_corrected_by_content(self):
        # Il podcast ha MENO slide del PDF: lo speaker è sempre più indietro
        # ("slide 5" mostra la 6, più avanti "slide 9" mostra la 11). Gli offset
        # non costanti formano più run e le regole a offset uniforme/gradino
        # rinunciano: la correzione deve arrivare dal CONTENUTO (slide "best"),
        # in modo deterministico e senza LLM.
        slides = [f"tema{i} slide" for i in range(1, 15)]
        plan = [
            (1, 0.0, 1),
            (3, 100.0, 4),
            (5, 200.0, 6),
            (6, 300.0, 7),
            (7, 400.0, 9),
            (9, 500.0, 11),
            (10, 600.0, 12),
        ]
        words = []
        for _, start, tema in plan:
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        anchors = {s: t for s, t, _ in plan}
        report: dict[str, bool] = {}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=14,
            window_seconds=40.0,
            embed_fn=self._embed_fn(14),
            report=report,
        )
        self.assertEqual(
            out,
            {1: 0.0, 4: 100.0, 6: 200.0, 7: 300.0, 9: 400.0, 11: 500.0, 12: 600.0},
        )
        self.assertFalse(report["suspicious"])

    def test_progressive_drift_drops_noisy_anchor(self):
        # Drift progressivo + ancora di chiusura "rumorosa" (il parlato somiglia
        # di più a una slide già assegnata): l'outlier va scartato e il resto
        # corretto per contenuto, senza far fallire la mappa.
        slides = [f"tema{i} slide" for i in range(1, 15)]
        plan = [
            (1, 0.0, 1),
            (3, 100.0, 4),
            (5, 200.0, 6),
            (6, 300.0, 7),
            (7, 400.0, 9),
            (9, 500.0, 11),
        ]
        words = []
        for _, start, tema in plan:
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        words += [{"word": "tema4", "start": 600.0 + i} for i in range(5)]
        anchors = {1: 0.0, 3: 100.0, 5: 200.0, 6: 300.0, 7: 400.0, 9: 500.0, 12: 600.0}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=14,
            window_seconds=40.0,
            embed_fn=self._embed_fn(14),
        )
        self.assertEqual(out, {1: 0.0, 4: 100.0, 6: 200.0, 7: 300.0, 9: 400.0, 11: 500.0})

    def test_progressive_drift_with_plateau_not_corrected(self):
        # Offset a più run (drift) MA due ancore che il contenuto manda sulla
        # STESSA slide senza salti all'indietro: ambiguità reale, non drift ->
        # nessuna correzione, il mapping resta sospetto per la verifica LLM.
        slides = [f"tema{i} slide" for i in range(1, 15)]
        plan = [
            (5, 100.0, 6),
            (6, 200.0, 7),
            (7, 300.0, 9),
            (8, 400.0, 10),
            (9, 500.0, 10),
            (10, 600.0, 11),
        ]
        words = []
        for _, start, tema in plan:
            words += [{"word": f"tema{tema}", "start": start + i} for i in range(5)]
        anchors = {s: t for s, t, _ in plan}
        report: dict[str, bool] = {}

        out = verify_anchor_mapping_embedding(
            slides,
            words,
            anchors,
            total_slides=14,
            window_seconds=40.0,
            embed_fn=self._embed_fn(14),
            report=report,
        )
        self.assertIsNone(out)
        self.assertTrue(report["suspicious"])


class TestAnchorRemapFilter(unittest.TestCase):
    """Validatore dei rimappi ancore LLM: il contenuto deve confermare il
    rimappo, altrimenti l'ancora esplicita (vincolo ad alta precisione) resta."""

    @staticmethod
    def _embed_fn(num_slides):
        def _embed(texts):
            out = []
            for t in texts:
                v = np.zeros(num_slides)
                for k in range(num_slides):
                    if f"tema{k + 1}" in t:
                        v[k] = 1.0
                norm = np.linalg.norm(v)
                out.append(v / norm if norm else v)
            return np.array(out)

        return _embed

    def test_remap_supported_when_content_matches_target(self):
        # Speaker dice "slide 4" a 100s ma il parlato dopo parla di tema5:
        # il rimappo 4 -> 5 è supportato dal contenuto.
        slides = [f"tema{i} slide" for i in range(1, 6)]
        words = [{"word": "tema5", "start": 105.0 + i} for i in range(5)]
        filtro = make_anchor_remap_filter(
            slides,
            words,
            total_slides=5,
            window_seconds=40.0,
            embed_fn=self._embed_fn(5),
        )
        self.assertIsNotNone(filtro)
        self.assertTrue(filtro(4, 100.0, 5))

    def test_remap_rejected_when_content_matches_spoken(self):
        # Speaker dice "slide 4" a 100s e il parlato parla davvero di tema4:
        # il rimappo 4 -> 5 contraddice il contenuto e va rifiutato.
        slides = [f"tema{i} slide" for i in range(1, 6)]
        words = [{"word": "tema4", "start": 105.0 + i} for i in range(5)]
        filtro = make_anchor_remap_filter(
            slides,
            words,
            total_slides=5,
            window_seconds=40.0,
            embed_fn=self._embed_fn(5),
        )
        self.assertIsNotNone(filtro)
        self.assertFalse(filtro(4, 100.0, 5))

    def test_empty_window_returns_none(self):
        slides = [f"tema{i} slide" for i in range(1, 6)]
        filtro = make_anchor_remap_filter(
            slides,
            [],
            total_slides=5,
            window_seconds=40.0,
            embed_fn=self._embed_fn(5),
        )
        self.assertIsNotNone(filtro)
        self.assertIsNone(filtro(4, 100.0, 5))

    def test_embedder_unavailable_returns_none(self):
        # Senza embedder il filtro è None: l'LLM fa fede (comportamento storico).
        from unittest.mock import patch

        with patch("semantic_sync._load_embed_model", return_value=None):
            filtro = make_anchor_remap_filter(
                ["tema1 slide", "tema2 slide"],
                [{"word": "tema1", "start": 105.0}],
                total_slides=2,
                window_seconds=40.0,
            )
        self.assertIsNone(filtro)


class TestFilterAnchorRemaps(unittest.TestCase):
    """Guardia strutturale sui rimappi delle ancore: solo DERIVE coerenti.

    Regressione della run del 15/09/2026: la verifica del mapping aveva
    "corretto" due ancore già corrette ('slide 1' -> slide 3 e 'slide 12' ->
    slide 11) perché il contenuto subito dopo un annuncio è già quello della
    slide SUCCESSIVA. Un rimappo isolato non è una deriva di numerazione.
    """

    # Ancore reali della run: numerazione identica a quella del PDF.
    ANCHORS: ClassVar[dict[int, float]] = {
        1: 111.9,
        4: 176.5,
        5: 260.5,
        7: 335.7,
        9: 424.1,
        12: 521.2,
        13: 594.0,
    }

    def test_isolated_remaps_rejected_when_other_anchors_confirm(self):
        accepted, rejected = filter_anchor_remaps(self.ANCHORS, {1: 3, 12: 11})
        self.assertEqual(accepted, {})
        self.assertEqual(rejected, [(1, 3), (12, 11)])

    def test_isolated_remap_accepted_without_identity_evidence(self):
        # Una sola ancora: nessuna evidenza di identità dalle altre, resta il
        # contenuto (già filtrato dal validatore semantico) a fare fede.
        accepted, rejected = filter_anchor_remaps({4: 100.0}, {4: 5})
        self.assertEqual(accepted, {4: 5})
        self.assertEqual(rejected, [])

    def test_coherent_run_accepted(self):
        # Deriva reale: il podcast salta una slide del PDF, tutte le ancore di
        # quel tratto sono sfasate dello stesso delta (run contigua >= 2).
        accepted, rejected = filter_anchor_remaps(
            {4: 100.0, 5: 180.0, 8: 260.0}, {4: 5, 5: 6, 8: 9}
        )
        self.assertEqual(accepted, {4: 5, 5: 6, 8: 9})
        self.assertEqual(rejected, [])

    def test_spoken_slide_one_never_remapped(self):
        # Copertina esclusa: tutte le ancore sfasate di +1 tranne la 'slide 1'
        # (che non è un confine di transizione: la slide 1 reale è a 0.0s).
        accepted, rejected = filter_anchor_remaps(
            {1: 100.0, 2: 200.0, 3: 300.0}, {1: 2, 2: 3, 3: 4}
        )
        self.assertEqual(accepted, {2: 3, 3: 4})
        self.assertEqual(rejected, [(1, 2)])

    def test_run_broken_by_aligned_anchor_is_rejected(self):
        # Due sfasate NON contigue (una confermata in mezzo): non è una deriva.
        accepted, rejected = filter_anchor_remaps(
            {4: 100.0, 5: 180.0, 6: 260.0, 7: 340.0}, {4: 5, 6: 7}
        )
        self.assertEqual(accepted, {})
        self.assertEqual(rejected, [(4, 5), (6, 7)])


class TestEnforceMinDurations(unittest.TestCase):
    """Anti-flicker: nessuna slide a video per un lampo (1-4s).

    Regressione della run del 15/09/2026: la timeline conteneva la slide 10 da
    1.1s e le slide 5 e 8 da 3.6s, perché le slide senza ancora erano state
    posizionate a ridosso dell'ancora successiva.
    """

    TIMELINE: ClassVar[dict[int, float]] = {
        1: 0.0,
        2: 4.46,
        3: 111.9,
        4: 176.5,
        5: 260.54,
        6: 264.18,
        7: 335.74,
        8: 420.5,
        9: 424.1,
        10: 520.04,
        11: 521.16,
        12: 572.26,
        13: 593.96,
    }
    ANCHORS: ClassVar[dict[int, float]] = {
        3: 111.9,
        4: 176.5,
        5: 260.54,
        7: 335.74,
        9: 424.1,
        11: 521.16,
        13: 593.96,
    }
    TOTAL = 681.62
    TOTAL_SLIDES = 13

    def test_short_slides_lengthened_and_anchors_untouched(self):
        out, moved = enforce_min_durations(
            self.TIMELINE, self.TOTAL, 8.0, anchors=self.ANCHORS
        )
        self.assertTrue(moved)
        for s, t in self.ANCHORS.items():
            self.assertAlmostEqual(out[s], t, msg=f"ancora slide {s} spostata")
        ordered = sorted(out)
        for i, s in enumerate(ordered):
            end = out[ordered[i + 1]] if i + 1 < len(ordered) else self.TOTAL
            self.assertGreaterEqual(end - out[s], 8.0 - 1e-9, msg=f"slide {s} corta")
        # La timeline resta riconciliabile (tempi crescenti, tutte le slide).
        durations = reconcile_timeline(out, self.TOTAL_SLIDES, self.TOTAL)
        self.assertEqual(len(durations), self.TOTAL_SLIDES)

    def test_anchored_slide_keeps_time_next_slide_moves(self):
        # La slide 5 è ancorata (260.5s) e quella dopo (senza ancora) era a 264.2s:
        # si ritarda la successiva, l'ancora non si tocca.
        out, _moved = enforce_min_durations(
            self.TIMELINE, self.TOTAL, 8.0, anchors=self.ANCHORS
        )
        self.assertAlmostEqual(out[5], 260.54)
        self.assertGreaterEqual(out[6] - out[5], 8.0 - 1e-9)

    def test_no_invention_when_no_boundary_can_move(self):
        # Slide ancorata incastrata tra due ancore: nessuno spazio per allungarla
        # (spostare la vicina la renderebbe corta a sua volta) -> nessuna
        # posizione inventata, la timeline resta quella data.
        timeline = {1: 0.0, 2: 10.0, 3: 10.5, 4: 20.0}
        out, moved = enforce_min_durations(
            timeline, 30.0, 8.0, anchors={2: 10.0, 4: 20.0}
        )
        self.assertEqual(out, timeline)
        self.assertEqual(moved, [])

    def test_long_slides_untouched(self):
        timeline = {1: 0.0, 2: 60.0, 3: 120.0}
        out, moved = enforce_min_durations(timeline, 180.0, 8.0)
        self.assertEqual(out, timeline)
        self.assertEqual(moved, [])

    def test_float_noise_is_not_recorded_as_a_move(self):
        # Osservato nella run del 25/09/2026: la lista degli spostamenti
        # conteneva "slide 14: 544.1->544.1s", cioè uno spostamento nullo (o
        # all'indietro di 1e-13s) nato dal rumore numerico dei confini. Non è
        # uno spostamento: sporcava il log e consumava le passate senza
        # cambiare nulla.
        timeline = {1: 0.0, 2: 8.0 - 1e-9, 3: 16.0 - 1e-9}
        out, moved = enforce_min_durations(timeline, 24.0, 8.0)
        self.assertEqual(moved, [])
        self.assertEqual(out, timeline)

    def test_negligible_shift_does_not_count_as_a_move(self):
        # La slide 2 è corta di 2 centesimi: il guadagno di leggibilità è nullo,
        # quindi il confine resta dov'è invece di registrare una modifica
        # inesistente (e di spostare la vicina per nulla).
        timeline = {1: 0.0, 2: 100.0, 3: 107.98}
        out, moved = enforce_min_durations(timeline, 200.0, 8.0)
        self.assertEqual(moved, [])
        self.assertEqual(out, timeline)


class TestFloorDurations(unittest.TestCase):
    """Durate dal pavimento anti-flicker: dichiarate, non spacciate per misure.

    Il pavimento allunga una slide corta prendendo tempo alle vicine: la durata
    risultante è una garanzia di leggibilità, non una misura del parlato. Il
    report deve saperlo dire (dati reali della run del 25/09/2026).
    """

    def test_raised_slide_is_declared_as_guaranteed(self):
        from main import _floor_report

        before = {1: 0.0, 2: 204.1, 3: 208.4}
        after = {1: 0.0, 2: 200.4, 3: 208.4}
        report = _floor_report(before, after, 268.4, 8.0)
        self.assertEqual(report["min_seconds"], 8.0)
        self.assertEqual(
            report["guaranteed"], [{"slide": 2, "before": 4.3, "duration": 8.0}]
        )
        self.assertEqual(report["unguaranteed"], [])

    def test_naturally_long_slides_are_not_called_guaranteed(self):
        from main import _floor_report

        before = {1: 0.0, 2: 60.0}
        report = _floor_report(before, dict(before), 120.0, 8.0)
        self.assertEqual(report["guaranteed"], [])
        self.assertEqual(report["unguaranteed"], [])

    def test_slide_the_floor_could_not_save_is_reported_separately(self):
        from main import _floor_report

        before = {1: 0.0, 2: 10.0, 3: 10.5, 4: 20.0}
        report = _floor_report(before, dict(before), 30.0, 8.0)
        self.assertEqual(report["guaranteed"], [])
        self.assertEqual(report["unguaranteed"], [{"slide": 2, "duration": 0.5}])

    def test_last_slide_duration_uses_the_audio_end(self):
        # L'ultima slide finisce con l'audio: la sua durata non ha un confine
        # successivo, quindi va misurata contro ``total_duration``.
        from main import _floor_report

        before = {1: 0.0, 2: 60.0, 3: 100.0}
        after = {1: 0.0, 2: 60.0, 3: 98.0}
        report = _floor_report(before, after, 106.0, 8.0)
        self.assertEqual(
            report["guaranteed"], [{"slide": 3, "before": 6.0, "duration": 8.0}]
        )

    def test_split_is_tolerant_of_a_missing_or_broken_report(self):
        from main import _floor_split

        for broken in (
            None,
            {},
            {"min_seconds": "n/d"},
            {"guaranteed": "n/d"},
            {"guaranteed": [{"slide": "x"}]},
            {"guaranteed": [None]},
        ):
            guaranteed, unguaranteed, min_seconds = _floor_split(broken)
            self.assertEqual(guaranteed, {})
            self.assertEqual(unguaranteed, {})
            self.assertEqual(min_seconds, 0.0)

    def test_split_round_trips_the_report(self):
        from main import _floor_report, _floor_split

        before = {1: 0.0, 2: 204.1, 3: 208.4}
        after = {1: 0.0, 2: 200.4, 3: 208.4}
        guaranteed, unguaranteed, min_seconds = _floor_split(
            _floor_report(before, after, 268.4, 8.0)
        )
        self.assertEqual(guaranteed, {2: 8.0})
        self.assertEqual(unguaranteed, {})
        self.assertEqual(min_seconds, 8.0)

    def test_slide_that_lost_time_to_the_floor_is_declared(self):
        # Caso reale (25/09): il pavimento ha allungato la slide 12 a 8s
        # STACCANDO 4.8s alla slide 11 (12.9s -> 8.1s). La slide 11 non era né
        # "garantita" né anomala (8.1s > soglia 8.05s): alterata in silenzio.
        from main import _floor_report, _floor_shortened

        # Slide 11: 520.0 -> 532.9 (12.9s, perde 4.8s)
        # Slide 12: 532.9 -> 536.1 (3.2s, viene allungata a 8.0s)
        before = {1: 0.0, 2: 520.0, 3: 532.9, 4: 536.1}
        after = {1: 0.0, 2: 520.0, 3: 528.1, 4: 536.1}
        report = _floor_report(before, after, 544.1, 8.0)
        # La 3 (slide 12) e' stata allungata: garantita.
        self.assertEqual(
            report["guaranteed"], [{"slide": 3, "before": 3.2, "duration": 8.0}]
        )
        # La 2 (slide 11) ha PAGATO: dichiarata come vittima, non come misura.
        self.assertEqual(
            report["shortened"], [{"slide": 2, "before": 12.9, "duration": 8.1}]
        )
        self.assertEqual(_floor_shortened(report), {2: 8.1})

    def test_untouched_slides_are_neither_guaranteed_nor_shortened(self):
        from main import _floor_report, _floor_shortened

        before = {1: 0.0, 2: 60.0}
        report = _floor_report(before, dict(before), 120.0, 8.0)
        self.assertEqual(report["guaranteed"], [])
        self.assertEqual(report["shortened"], [])
        self.assertEqual(_floor_shortened(report), {})

    def test_shortened_tolerates_a_broken_report(self):
        from main import _floor_shortened

        self.assertEqual(_floor_shortened(None), {})
        self.assertEqual(_floor_shortened({"shortened": "non una lista"}), {})
        self.assertEqual(_floor_shortened({"shortened": ["non un dict"]}), {})
