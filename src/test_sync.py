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
import zlib
from itertools import pairwise
from typing import ClassVar
from unittest import mock

import numpy as np

from config import DEFAULT_RECOVERY_MAX_ZONE_SHARE
from semantic_sync import (
    SemanticOptions,
    build_candidates,
    refine_segment_boundaries,
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
        # scartata (il DP semantico la posizionerà per contenuto).
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

    def test_number_from_word_english_mishearings(self):
        # Run del 13/10: "Passiamo alla slide set" (per "sette") veniva perso
        # e il confine della slide 7 stimato 25s prima del vero annuncio.
        from timeline import _number_from_word

        for word, expected in {
            "tree": 3,
            "three": 3,
            "six": 6,
            "set": 7,
            "seven": 7,
        }.items():
            with self.subTest(word=word):
                self.assertEqual(_number_from_word(word), expected)

    def test_anchor_english_misheard_seven(self):
        from timeline import extract_slide_anchors

        words = _words(
            [
                ("passiamo", 558.0),
                ("alla", 558.3),
                ("slide", 558.5),
                ("set", 558.7),
            ]
        )
        self.assertEqual(
            extract_slide_anchors(words, total_slides=15, flow="slide-audio"), {7: 558.7}
        )

    def test_anchor_fused_number_tolerates_vowels(self):
        # Run del 13/10: "Passiamo alla slaritridici" (Whisper fonde "slide
        # tredici" e mangia una vocale): l'ancora della slide 13 veniva persa.
        from timeline import _embedded_slide_number, extract_slide_anchors

        self.assertEqual(_embedded_slide_number("slaritridici"), 13)
        words = _words(
            [
                ("passiamo", 1015.8),
                ("alla", 1016.2),
                ("slaritridici.", 1016.3),
            ]
        )
        self.assertEqual(
            extract_slide_anchors(words, total_slides=15, flow="slide-audio"), {13: 1016.3}
        )

    def test_anchor_gaps_buco_interno_localizzato(self):
        from timeline import anchor_gaps

        # Run del 13/10: manca la 3, con la 2 a 102s e la 4 a 302s.
        self.assertEqual(
            anchor_gaps({2: 102.0, 4: 302.0, 5: 390.0}, 15),
            [([3], 102.0, 302.0)],
        )

    def test_anchor_gaps_consecutivi_una_zona_sola(self):
        from timeline import anchor_gaps

        self.assertEqual(
            anchor_gaps({2: 100.0, 5: 400.0}, 15),
            [([3, 4], 100.0, 400.0)],
        )

    def test_anchor_gaps_testa_dalla_parte_della_slide_1(self):
        from timeline import anchor_gaps

        self.assertEqual(
            anchor_gaps({3: 200.0}, 15),
            [([2], 0.0, 200.0)],
        )

    def test_anchor_gaps_coda_senza_destra_niente_zona(self):
        from timeline import anchor_gaps

        self.assertEqual(anchor_gaps({2: 100.0}, 15), [])

    def test_anchor_gaps_senza_buchi(self):
        from timeline import anchor_gaps

        self.assertEqual(
            anchor_gaps({2: 100.0, 3: 200.0, 4: 300.0}, 4), []
        )

    def test_anchor_gaps_coda_non_si_apre_ma_la_zona_di_coda_si(self):
        """I due meccanismi sono complementari, non alternativi.

        ``anchor_gaps`` non apre niente in coda (a destra c'è solo la fine
        dell'audio): la transizione finale la localizza
        ``tail_recovery_zone`` con la cadenza degli annunci. Senza il secondo,
        l'annuncio dell'ultima slide era recuperabile solo se il decoder
        produceva ANCHE un ciclo ripetuto nella stessa zona, cioè per caso.
        """
        from timeline import anchor_gaps, tail_recovery_zone

        # Ancore della run del 14/10, senza slide 1 (non annunciata) e senza la
        # 10: la 7 manca per un buco INTERNO, la 10 perché è la coda.
        anchors = {2: 135.0, 3: 256.25, 4: 364.65, 5: 477.37, 6: 587.76, 8: 799.82, 9: 900.24}
        self.assertEqual(anchor_gaps(anchors, 10), [([7], 587.76, 799.82)])
        self.assertEqual(tail_recovery_zone(anchors, 10, 1121.25)[:1], ([10],))

    def test_tail_recovery_zone_copre_l_annuncio_della_run_14_10(self):
        """La zona di coda arriva all'annuncio che il greedy aveva distrutto.

        Numeri reali: sezione finale di 157.9s contro una cadenza mediana di
        111.6s. Il "Passiamo alla slide 10" delle 16:56 (1017.8s) sta dentro
        la zona; con un fattore 1.0 la zona si fermerebbe a 1011.8s e mancherebbe
        l'annuncio di 6s, cioè il recupero sarebbe inutile proprio nel caso da
        cui nasce (è il motivo del default 1.5). E non si prende tutta la coda:
        la zona non tocca la fine dell'audio.
        """
        from timeline import tail_recovery_zone

        anchors = {2: 135.0, 3: 256.25, 4: 364.65, 5: 477.37, 6: 587.76, 8: 799.82, 9: 900.24}
        missing, lo, hi = tail_recovery_zone(anchors, 10, 1121.25)
        self.assertEqual(missing, [10])
        self.assertEqual(lo, 900.24)
        self.assertAlmostEqual(hi, 1067.5725, places=3)
        self.assertLess(lo, 1017.8)
        self.assertLessEqual(1017.8, hi)
        self.assertLess(hi, 1121.25)  # mirata, non "tutta la coda"
        corte = tail_recovery_zone(anchors, 10, 1121.25, ratio=1.0)
        self.assertLess(corte[2], 1017.8)

    def test_tail_recovery_zone_coda_ancorata_niente_zona(self):
        from timeline import tail_recovery_zone

        # L'ultima slide ha la sua ancora: non c'è nessun annuncio da cercare.
        self.assertIsNone(tail_recovery_zone({5: 400.0, 10: 1000.0}, 10, 1121.25))

    def test_tail_recovery_zone_senza_cadenza_niente_zona(self):
        """Se la cadenza non è misurabile non si sa dove cercare: nessuna zona.

        Una sola ancora (o più ancore tutte nello stesso istante) non dice
        quanto dura una sezione: aprire una zona sarebbe tirare a indovinare su
        tutta la coda, al costo di una ritrascrizione intera.
        """
        from timeline import tail_recovery_zone

        self.assertIsNone(tail_recovery_zone({2: 135.0}, 10, 1121.25))
        self.assertIsNone(tail_recovery_zone({2: 135.0, 3: 135.0}, 10, 1121.25))
        self.assertIsNone(tail_recovery_zone({}, 10, 1121.25))
        self.assertIsNone(tail_recovery_zone({2: 135.0, 3: 300.0}, 10, 0.0))

    def test_tail_recovery_zone_non_esce_dall_audio(self):
        from timeline import tail_recovery_zone

        # Cadenza 600s, coda di 100s: la zona si ferma alla fine dell'audio.
        self.assertEqual(
            tail_recovery_zone({1: 0.0, 2: 600.0}, 3, 700.0),
            ([3], 600.0, 700.0),
        )

    def test_tail_recovery_zone_troppo_corta_niente_zona(self):
        from timeline import tail_recovery_zone

        # 2s di zona: un annuncio non ci sta. Meglio niente che una
        # ritrascrizione inutile.
        self.assertIsNone(tail_recovery_zone({1: 0.0, 2: 600.0}, 3, 602.0))
        # La stessa coda con una finestra sufficiente si apre.
        self.assertEqual(tail_recovery_zone({1: 0.0, 2: 600.0}, 3, 606.0)[1:], (600.0, 606.0))

    def test_tail_recovery_zone_piu_slide_mancanti_una_zona_sola(self):
        """Con più slide scoperte la zona copre la prima (chiude la precedente).

        Le altre restano stimate e il chiamante le dichiara nel log: coprire
        tutte le mancanti vorrebbe dire ritrascrivere mezza coda.
        """
        from timeline import tail_recovery_zone

        self.assertEqual(
            tail_recovery_zone({1: 0.0, 2: 100.0}, 5, 1000.0),
            ([3, 4, 5], 100.0, 250.0),
        )

    def test_fused_number_coverage(self):
        """Audit blindato delle storpiature numeriche (run del 13/10).

        Tutte le fusioni stem noto + numero esatto si risolvono; il wobble
        vocalico si risolve dove non ambiguo (scheletro unico). Restano fuori
        DI PROPOSITO: wobble isolati ("tra" è una preposizione), wobble di
        "sette" (ambiguo con "sesto") e parole comuni ("sloveno").
        """
        from timeline import _embedded_slide_number as f

        card = ["due", "tre", "quattro", "cinque", "sei", "sette", "otto",
                "nove", "dieci", "undici", "dodici", "tredici",
                "quattordici", "quindici"]
        for stem in ("sla", "slai", "slay", "slaid", "slad"):
            for c in card:
                with self.subTest(fuso=f"{stem}+{c}"):
                    self.assertIsNotNone(f(stem + c))
        for wob in ("slaitri", "slatro", "slainove", "slaitredici", "slaritridici"):
            with self.subTest(fuso=wob):
                self.assertIsNotNone(f(wob))
        for no in ("sloveno", "slavina", "slogan", "slancio", "tra"):
            with self.subTest(parola=no):
                self.assertIsNone(f(no))

    def test_repetition_loops_ciclo_biparola(self):
        # Run del 13/10: "la c' la c'..." per ~15s al posto dell'annuncio.
        from timeline import repetition_loops

        words = _words(
            [("ok", 1.0)]
            + [(w, 10.0 + i * 0.3) for i, w in enumerate(["la", "c"] * 8)]
            + [("fine", 20.0)]
        )
        zone = repetition_loops(words)
        self.assertEqual(len(zone), 1)
        self.assertLessEqual(zone[0][0], 10.5)
        self.assertGreaterEqual(zone[0][1], 14.0)

    def test_repetition_loops_parlato_normale_niente_zone(self):
        from timeline import repetition_loops

        words = _words(
            [
                ("molto", 1.0),
                ("molto", 1.3),
                ("bene", 1.6),
                ("si", 2.0),
                ("si", 2.2),
                ("pero", 2.5),
                ("no", 3.0),
                ("no", 3.2),
                ("no", 3.4),
            ]
        )
        self.assertEqual(repetition_loops(words), [])

    def test_repetition_loops_sotto_soglia_niente_zona(self):
        from timeline import repetition_loops

        words = _words([(w, 1.0 + i * 0.3) for i, w in enumerate(["a", "b"] * 5)])
        self.assertEqual(repetition_loops(words), [])

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
        # vanno riconosciute come ancore deterministiche.
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
        # la timeline è None.
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


    def test_repetition_loops_frase_intera_ripetuta(self):
        # La frase di 11 parole ripetuta 6 volte in 15.5s al posto di tre frasi
        # diverse (run del 14/10: è così che l'annuncio "passiamo alla slide 10"
        # è stato cancellato dal beam greedy). Il ciclo lungo va riconosciuto,
        # altrimenti la zona non viene ritrascritta e il confine resta stimato.
        from timeline import repetition_loops

        frase = (
            "la", "tecnologia", "è", "un", "semplice", "strumento", "al", "nostro",
            "servizio", "o", "finisce",
        )
        passo = (116.0 - 100.5) / (len(frase) * 6 - 1)
        words = []
        for i in range(len(frase) * 6):
            words.append({"word": frase[i % len(frase)], "start": 100.5 + i * passo})
        zone = repetition_loops(words)
        self.assertEqual(len(zone), 1)
        self.assertAlmostEqual(zone[0][0], 100.5)
        self.assertAlmostEqual(zone[0][1], 116.0)

    def test_repetition_loops_frase_sotto_soglia_niente_zona(self):
        # 5 ripetizioni: sotto la soglia (6) non è un'allucinazione del decoder.
        from timeline import repetition_loops

        frase = ("e", "chiudiamo", "con", "questa", "parte", "del", "discorso", "qui")
        words = []
        for i in range(len(frase) * 5):
            words.append({"word": frase[i % len(frase)], "start": 10.0 + i * 0.3})
        self.assertEqual(repetition_loops(words), [])

    def test_repetition_loops_parlato_scorrevole_niente_zone(self):
        # Parlato normale: nessun ciclo, nemmeno di parole singole.
        from timeline import repetition_loops

        words = _words(
            [
                ("il", 0.0), ("progetto", 0.3), ("nasce", 0.7), ("da", 1.0),
                ("una", 1.3), ("esigenza", 1.7), ("concreta", 2.2), ("e", 2.6),
                ("misurabile", 2.9), ("ogni", 3.4), ("volta", 3.8), ("che", 4.2),
                ("il", 4.5), ("video", 4.9), ("esce", 5.3), ("sbagliato", 5.7),
            ]
        )
        self.assertEqual(repetition_loops(words), [])

    def test_annunci_fuori_timeline_annuncio_lontano(self):
        # "Passiamo alla slide 10" a 900s, il confine dichiarato a 1058.2s: è
        # esattamente il caso del 14/10. L'annuncio è un fatto, il confine una
        # stima: la contraddizione va segnalata.
        from timeline import announcements_off_timeline

        words = _words(
            [("passiamo", 899.0), ("alla", 899.4), ("slide", 899.8), ("dieci", 900.0)]
        )
        out = announcements_off_timeline(words, {10: 1058.2}, 10)
        self.assertEqual([s for s, _, _ in out], [10])
        self.assertAlmostEqual(out[0][1], 900.0)
        self.assertAlmostEqual(out[0][2], 1058.2)

    def test_annunci_fuori_timeline_richiamo_non_segnalato(self):
        # "Come dicevo nella slide 4" è un richiamo: cade lontano dal confine
        # per costruzione e senza verbo di transizione non è una contraddizione.
        from timeline import announcements_off_timeline

        words = _words(
            [("come", 99.0), ("dicevo", 99.3), ("nella", 99.6), ("slide", 99.8), ("quattro", 100.0)]
        )
        self.assertEqual(announcements_off_timeline(words, {4: 300.0}, 10), [])

    def test_annunci_fuori_timeline_annuncio_al_confine_non_segnalato(self):
        # Annuncio e confine dichiarato quasi coincidono: nessuna contraddizione.
        from timeline import announcements_off_timeline

        words = _words(
            [("passiamo", 100.0), ("alla", 100.4), ("slide", 100.7), ("quattro", 101.0)]
        )
        self.assertEqual(announcements_off_timeline(words, {4: 101.0}, 10), [])

    def test_annunci_fuori_timeline_conteggio_non_segnalato(self):
        # "Le 13 slide" è un conteggio, non una transizione: resta fuori.
        from timeline import announcements_off_timeline

        words = _words([("le", 799.0), ("13", 799.4), ("slide", 799.8)])
        self.assertEqual(announcements_off_timeline(words, {13: 800.0}, 14), [])


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


class TestThinSlideAdvice(unittest.TestCase):
    """Il rimedio per una slide troppo breve suggerisce l'ancora esplicita."""

    def test_advice_suggests_the_anchor(self):
        from main import _thin_slide_advice

        advice = _thin_slide_advice()
        self.assertIn("ancora esplicita", advice)


class TestMinAnchorCoverage(unittest.TestCase):
    """Fail-fast sulla copertura delle ancore (run del 06/10: 4 su 13).

    Con --no-confirm (genera_video.bat) la pausa interattiva viene saltata e
    il video degradato veniva generato in silenzio: sotto soglia la run deve
    fermarsi PRIMA della sincronizzazione, quando rigenerare l'audio costa
    ancora poco.
    """

    def test_run_06_10_sotto_soglia(self):
        from main import _anchor_coverage_ok

        self.assertFalse(_anchor_coverage_ok(4, 14, 0.5))

    def test_meta_transizioni_ok(self):
        from main import _anchor_coverage_ok

        self.assertTrue(_anchor_coverage_ok(7, 14, 0.5))

    def test_copertura_piena_ok(self):
        from main import _anchor_coverage_ok

        self.assertTrue(_anchor_coverage_ok(13, 14, 0.5))

    def test_soglia_zero_disattiva(self):
        from main import _anchor_coverage_ok

        self.assertTrue(_anchor_coverage_ok(0, 14, 0))

    def test_una_sola_slide_sempre_ok(self):
        # Nessuna transizione: copertura vacua, niente da ancorare.
        from main import _anchor_coverage_ok

        self.assertTrue(_anchor_coverage_ok(0, 1, 0.5))


class TestAccurateBeamDopoIlRecupero(unittest.TestCase):
    """La decodifica accurata si decide sull'esito del recupero, non prima.

    Prima la scelta arrivava sulla sola trascrizione veloce, e sotto soglia non
    arrivava affatto: il gate di copertura fermava la run. L'ordine nuovo è
    recupero -> decisione -> gate, e la decisione usa la copertura che c'è DOPO
    la ritrascrizione mirata.
    """

    def test_recupero_che_basta_non_paga_l_audio_intero(self):
        # Il caso della run del 14/10: la copertura veloce è 7/9, il recupero
        # delle zone cieche riporta le due ancore mancanti e la copertura arriva
        # a 9/9. La decodifica accurata di 18 minuti di audio non serve.
        from main import _accurate_beam_worth_it, _needs_accurate_beam

        self.assertTrue(_needs_accurate_beam(7, 10) is False)  # 7/9 = 0.78
        self.assertFalse(_accurate_beam_worth_it(9, 10, 0.5))

    def test_copertura_ancora_insufficiente_paga_l_audio_intero(self):
        from main import _accurate_beam_worth_it

        self.assertTrue(_accurate_beam_worth_it(4, 10, 0.5))  # 4/9 = 0.44
        self.assertTrue(_accurate_beam_worth_it(0, 10, 0.5))  # nessuna ancora

    def test_soglia_dichiarata_piu_severa_del_criterio(self):
        # Chi chiede il 90% di copertura non accetta l'8/9: la decodifica
        # accurata va tentata PRIMA di dichiarare il fallimento.
        from main import _accurate_beam_worth_it, _needs_accurate_beam

        self.assertFalse(_needs_accurate_beam(8, 10))
        self.assertTrue(_accurate_beam_worth_it(8, 10, 0.9))
        self.assertFalse(_accurate_beam_worth_it(9, 10, 0.9))  # 1.0 >= 0.9

    def test_soglia_zero_lascia_solo_il_criterio_qualitativo(self):
        from main import _accurate_beam_worth_it

        self.assertFalse(_accurate_beam_worth_it(8, 10, 0.0))
        self.assertTrue(_accurate_beam_worth_it(4, 10, 0.0))

    def test_una_sola_slide_non_escalate_mai(self):
        from main import _accurate_beam_worth_it

        self.assertFalse(_accurate_beam_worth_it(0, 1, 1.0))

    def test_soglia_in_vigore_e_quella_dichiarata_se_piu_severa(self):
        # Il log dice con che metro si è deciso: senza questo, una run forzata
        # (AUTO_BEAM_PINNED_RATIO=1.1) stampa "le ancore non bastano" con la
        # copertura piena, e sembra un difetto invece di una soglia scelta.
        import main as m
        from config import AUTO_BEAM_PINNED_RATIO

        self.assertEqual(m._beam_escalation_threshold(0.0), AUTO_BEAM_PINNED_RATIO)
        self.assertEqual(m._beam_escalation_threshold(0.5), AUTO_BEAM_PINNED_RATIO)
        self.assertEqual(m._beam_escalation_threshold(0.9), 0.9)
        # Una soglia dichiarata più bassa del criterio non lo ammorbidisce.
        self.assertEqual(m._beam_escalation_threshold(0.2), AUTO_BEAM_PINNED_RATIO)

    def test_decisione_e_soglia_danno_lo_stesso_verdetto(self):
        """Il grado di guardia contro la deriva fra decisione e log.

        `_accurate_beam_worth_it` è scritta in due rami (criterio qualitativo +
        soglia dichiarata) e la soglia in una riga sola: se un giorno le due
        formule divergono, il log mentirebbe sulla decisione presa.
        """
        import main as m

        for declared in (0.0, 0.2, 0.5, 0.9, 1.0):
            for pinned in range(11):
                with self.subTest(soglia=declared, ancore=pinned):
                    atteso = pinned / 9 < m._beam_escalation_threshold(declared)
                    self.assertEqual(
                        m._accurate_beam_worth_it(pinned, 10, declared),
                        atteso,
                        f"{pinned}/9 con soglia {declared}",
                    )


class TestRecuperoZoneCieche(unittest.TestCase):
    """`_recover_anchor_zones`: zone con o senza buco, e cosa torna indietro."""

    def setUp(self):
        import main as m

        self._m = m
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._audio = pathlib.Path(self._tmp.name) / "podcast.m4a"
        self._audio.touch()
        self._args = mock.MagicMock(whisper_model="small", lang="ita")

    def _recover(
        self,
        words,
        anchors,
        total_slides,
        zone,
        duration=1000.0,
        max_zone_share=DEFAULT_RECOVERY_MAX_ZONE_SHARE,
        differito=False,
    ):
        """Chiama il recupero con la ritrascrizione finta al posto di Whisper.

        ``zone`` è la lista di parole che la zona "produce", oppure una funzione
        ``(lo, hi) -> parole`` quando le zone sono più di una e ognuna deve
        riportare la propria menzione. ``max_zone_share=None`` disattiva il
        vincolo di quota (è quello che serve ai test che devono vedere le zone
        ritrascritte per davvero, con qualunque copertura).
        """
        calls = []

        def _fake(audio_path, lo, hi, **kwargs):
            calls.append((lo, hi))
            return zone(lo, hi) if callable(zone) else zone

        with mock.patch.object(self._m, "retranscribe_zone", side_effect=_fake):
            if differito:
                out = self._m._deferred_zone_recovery(
                    words, anchors, total_slides, duration, self._audio, self._args, "slide-audio"
                )
            else:
                out = self._m._recover_anchor_zones(
                    words,
                    anchors,
                    total_slides,
                    duration,
                    self._audio,
                    self._args,
                    "slide-audio",
                    max_zone_share=max_zone_share,
                )
        return out, calls

    def test_buco_interno_riporta_l_ancora_mancante(self):
        words = _words([("ciao", 10.0), ("slide", 100.5), ("due", 101.0), ("fine", 410.0)])
        zone = _words([("passiamo", 305.0), ("alla", 305.4), ("slide", 305.8), ("tre", 306.2)])
        out, calls = self._recover(
            words, {2: 101.0, 4: 420.0}, 4, zone
        )
        self.assertEqual(out.recovered, [3])
        self.assertEqual(out.anchors[3], 306.2)
        self.assertIn(3, out.anchors)
        self.assertGreaterEqual(out.seconds, 0.0)
        self.assertFalse(out.skipped)
        self.assertEqual(len(calls), 1)
        lo, hi = calls[0]
        self.assertEqual((lo, hi), (101.0, 420.0))
        # Le out.words dentro la zona sono quelle nuove: il vecchio "fine" a 410s
        # è stato ritrascritto (la zona arriva all'ancora successiva).
        self.assertNotIn(("fine", 410.0), [(w["word"], w["start"]) for w in out.words])
        self.assertIn(("tre", 306.2), [(w["word"], w["start"]) for w in out.words])

    def test_zona_senza_menzione_non_cambia_le_ancore(self):
        words = _words([("slide", 100.5), ("due", 101.0), ("bla", 300.0)])
        out, calls = self._recover(
            words, {2: 101.0, 4: 420.0}, 4, _words([("bla", 300.0)])
        )
        self.assertEqual(out.recovered, [])
        self.assertEqual(out.anchors, {2: 101.0, 4: 420.0})
        self.assertEqual(len(calls), 1)
        # La zona è [101, 420] e comprende il confine: le out.words dentro (compresa
        # "due" a 101.0) sono sostituite da quelle ritrascritte, quindi l'ancora
        # della slide 2 non si rilegge più nel testo. Le out.anchors di partenza
        # restano comunque: il recupero non può farne sparire una.
        self.assertEqual(out.words, _words([("slide", 100.5), ("bla", 300.0)]))

    def test_ritrascrizione_fallita_non_cambia_niente(self):
        words = _words([("slide", 100.5), ("due", 101.0)])
        out, calls = self._recover(
            words, {2: 101.0, 4: 420.0}, 4, None
        )
        self.assertEqual(out.recovered, [])
        self.assertEqual(out.anchors, {2: 101.0, 4: 420.0})
        self.assertEqual(out.words, words)
        self.assertGreaterEqual(out.seconds, 0.0)
        self.assertEqual(len(calls), 1)

    def test_loop_dentro_la_zona_di_coda_una_passata_sola(self):
        # Coda dalle slide 4 (500-1100s) e loop allucinato a metà: le due zone si
        # sovrappongono, quindi si ritrascrive una volta sola. È il caso della
        # run del 14/10, dove il loop delle 16:56 stava dentro la coda.
        ripetute = [(w, 700.0 + i * 0.3) for i, w in enumerate(["la", "c"] * 8)]
        words = _words([("slide", 100.5), ("due", 101.0), ("slide", 500.5), ("tre", 501.0), *ripetute])
        zone = _words([("passiamo", 705.0), ("alla", 705.4), ("slide", 705.8), ("quattro", 706.2)])
        out, calls = self._recover(
            words, {2: 101.0, 3: 501.0}, 4, zone, duration=1200.0
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (501.0, 1101.0))
        self.assertEqual(out.recovered, [4])
        self.assertEqual(out.anchors[4], 706.2)

    def test_zona_interna_e_coda_adiacenti_una_passata_sola(self):
        # Buco fra la 2 e la 5 (mancano 3 e 4) e coda della 6: la coda parte
        # dall'ultima ancora, cioè esattamente dove finisce il buco, quindi le
        # due zone sono adiacenti e si fondono: una passata che porta a casa
        # entrambe le out.anchors. Il loop dentro il buco non aggiunge una terza zona.
        ripetute = [(w, 300.0 + i * 0.3) for i, w in enumerate(["la", "c"] * 8)]
        words = _words(
            [("slide", 100.5), ("due", 101.0), *ripetute, ("slide", 1000.5), ("cinque", 1001.0)]
        )
        trovate = _words(
            [
                ("passiamo", 305.0), ("alla", 305.4), ("slide", 305.8), ("tre", 306.2),
                ("passiamo", 1105.0), ("alla", 1105.4), ("slide", 1105.8), ("sei", 1106.2),
            ]
        )
        out, calls = self._recover(
            words, {2: 101.0, 5: 1001.0}, 6, trovate, duration=1200.0, max_zone_share=None
        )
        self.assertEqual(calls, [(101.0, 1200.0)])
        self.assertEqual(out.recovered, [3, 6])
        self.assertEqual(out.anchors[3], 306.2)
        self.assertEqual(out.anchors[6], 1106.2)
        self.assertFalse(out.skipped)
        # Le out.anchors di partenza non si perdono.
        self.assertEqual(out.anchors[2], 101.0)
        self.assertEqual(out.anchors[5], 1001.0)

    def test_due_zone_lontane_due_passate(self):
        # Loop a metà audio e coda lontana: due zone separate, quindi due passate.
        # (Un buco interno e la coda invece si toccano sempre — il buco finisce
        # sull'ultima ancora, da cui parte la coda — e si fondono in una sola.)
        ripetute = [(w, 150.0 + i * 0.3) for i, w in enumerate(["la", "c"] * 8)]
        words = _words([("slide", 100.5), ("due", 101.0), *ripetute, ("slide", 500.5), ("tre", 501.0)])

        def zone(lo, hi):
            if lo < 300:
                return None  # il loop non nascondeva nessun annuncio
            return _words([("passiamo", 700.0), ("alla", 700.4), ("slide", 700.8), ("quattro", 701.2)])

        out, calls = self._recover(
            words, {2: 101.0, 3: 501.0}, 4, zone, duration=2400.0, max_zone_share=None
        )
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[0][1], 501.0)  # la prima è il loop
        self.assertEqual(calls[1], (501.0, 1101.0))  # la seconda è la coda
        self.assertEqual(out.recovered, [4])
        self.assertEqual(out.anchors[4], 701.2)

    def test_zone_quasi_tutta_la_coda_il_recupero_si_salta(self):
        """Con poche ancore lontane "la zona" è quasi tutto l'audio.

        Non è più un recupero mirato: costa come decodificare tutto, e per di
        più senza il confronto fra le due trascrizioni. Il recupero si salta (0
        secondi spesi, nomi invariati) e lo lascia alla decodifica accurata
        intera, che sta per arrivare comunque.
        """
        import main as m

        words = _words([("slide", 100.5), ("due", 101.0), ("slide", 1000.5), ("cinque", 1001.0)])
        out, calls = self._recover(
            words, {2: 101.0, 5: 1001.0}, 6, _words([("mai", 5.0)]), duration=1200.0
        )
        self.assertEqual(calls, [])
        self.assertEqual(out.words, words)
        self.assertEqual(out.anchors, {2: 101.0, 5: 1001.0})
        self.assertEqual(out.recovered, [])
        self.assertEqual(out.seconds, 0.0)
        # `skipped` è la differenza fra "non ritrascritto perché troppo largo" e
        # "ritrascritto senza trovare niente": nel primo caso il rimedio è ancora
        # da fare, ed è quello che main.py usa per rifarlo dopo un revert.
        self.assertTrue(out.skipped)
        # Il vincolo si disattiva esplicitamente, e allora la stessa chiamata
        # ritrascrive (i due test sopra mostrano cosa succede).
        with mock.patch.object(m, "retranscribe_zone", return_value=None) as finto:
            m._recover_anchor_zones(
                words, {2: 101.0, 5: 1001.0}, 6, 1200.0, self._audio, self._args,
                "slide-audio", max_zone_share=None,
            )
        self.assertTrue(finto.called)

    def test_rimedio_differito_dopo_un_revert(self):
        """Le zone saltate per quota si rifanno se la decodifica accurata non vince.

        È l'ultimo rimedio prima del gate: senza, una run che ha pagato la
        decodifica accurata e l'ha scartata resterebbe con meno ancore di quante
        ne avrebbe avute senza pagarla. Il vincolo di quota qui è disattivato di
        proposito, quindi la stessa chiamata che si era rifiutata di ritrascrivere
        adesso lo fa.
        """
        words = _words([("slide", 100.5), ("due", 101.0), ("slide", 1000.5), ("cinque", 1001.0)])
        zone = _words(
            [
                ("passiamo", 305.0), ("alla", 305.4), ("slide", 305.8), ("tre", 306.2),
                ("passiamo", 1105.0), ("alla", 1105.4), ("slide", 1105.8), ("sei", 1106.2),
            ]
        )
        out, calls = self._recover(
            words, {2: 101.0, 5: 1001.0}, 6, zone, duration=1200.0, differito=True
        )
        self.assertFalse(out.skipped)
        self.assertEqual(calls, [(101.0, 1200.0)])
        self.assertEqual(out.recovered, [3, 6])
        self.assertEqual(out.anchors[6], 1106.2)

    def test_quota_esattamente_al_limite_si_ritrascrive(self):
        # Il confronto è strettamente "maggiore": una zona larga esattamente
        # metà audio è ancora mirata, quindi si ritrascrive.
        words = _words([("slide", 100.5), ("due", 101.0), ("slide", 500.5), ("tre", 501.0)])
        zone = _words([("passiamo", 700.0), ("alla", 700.4), ("slide", 700.8), ("quattro", 701.2)])
        out, calls = self._recover(
            words, {2: 101.0, 3: 501.0}, 4, zone, duration=1200.0
        )
        self.assertEqual(calls, [(501.0, 1101.0)])  # 600s su 1200s = 50%
        self.assertEqual(out.recovered, [4])


class TestNotaTentativiDiRecupero(unittest.TestCase):
    """`_recovery_attempts_note`: l'avviso dice cosa è stato già provato."""

    def test_niente_tentato_nota_vuota(self):
        from main import _recovery_attempts_note

        self.assertEqual(_recovery_attempts_note([], False), "")

    def test_ancore_recuperate_elencate(self):
        from main import _recovery_attempts_note

        nota = _recovery_attempts_note([7, 10], False)
        self.assertIn("slide 7", nota)
        self.assertIn("slide 10", nota)
        self.assertIn("zone cieche", nota)
        self.assertNotIn("decodifica accurata", nota)

    def test_recupero_a_vuoto_dichiarato(self):
        from main import _recovery_attempts_note

        nota = _recovery_attempts_note([], True)
        self.assertIn("nessuna ancora recuperata", nota)
        self.assertIn("decodifica accurata", nota)

    def test_entrambi_i_tentativi(self):
        from main import _recovery_attempts_note

        nota = _recovery_attempts_note([3], True)
        self.assertIn("slide 3", nota)
        self.assertIn("decodifica accurata", nota)
        self.assertTrue(nota.endswith(".\n"))


class TestAntiFlickerFloor(unittest.TestCase):
    """Soglia unica dell'anti-flicker (era il letterale 8.0 in 5 punti).

    Il fondo di leggibilità vive in un solo posto
    (config.DEFAULT_MIN_SEGMENT_SECONDS): qui si fissa il comportamento,
    non il valore.
    """

    def test_default_resta_il_fondo(self):
        from timeline import anti_flicker_floor

        self.assertEqual(anti_flicker_floor(3.0), 8.0)

    def test_durata_minima_alta_trascina_il_pavimento(self):
        from timeline import anti_flicker_floor

        self.assertEqual(anti_flicker_floor(5.0), 10.0)

    def test_soglia_zero_non_abbatte_mai_il_fondo(self):
        from timeline import anti_flicker_floor

        self.assertEqual(anti_flicker_floor(0.0), 8.0)


class TestStarvedSlides(unittest.TestCase):
    """Slide senza tempo proprio nel podcast (il caso "scorre velocemente").

    Valori reali della run del 01/10 (15 slide, minimo 8s): la slide 2 partiva
    da 3.7s e la 13 da 7.8s. La prima non aveva tempo proprio nel podcast, la
    seconda era solo un arrotondamento: segnalarle insieme sarebbe stato un
    falso allarme che avrebbe reso ignorabile l'avviso vero.
    """

    REPORT: ClassVar[dict[str, object]] = {
        "min_seconds": 8.0,
        "guaranteed": [
            {"slide": 2, "before": 3.7, "duration": 8.0},
            {"slide": 13, "before": 7.8, "duration": 8.0},
            {"slide": 7, "before": 12.0, "duration": 8.0},
        ],
        "shortened": [{"slide": 1, "before": 76.4, "duration": 72.2}],
        "unguaranteed": [],
    }

    def test_flags_only_the_slides_without_their_own_time(self):
        from main import _starved_slides

        # Solo la 2 (3.7s, sotto metà minimo). La 13 a 7.8s e la 7 a 12s no.
        self.assertEqual(_starved_slides(self.REPORT, 8.0), [2])

    def test_before_duration_is_recoverable(self):
        # Il dato che spiega il difetto: la durata NATURALE, non quella finale.
        from main import _floor_before

        before = _floor_before(self.REPORT)
        self.assertAlmostEqual(before[2], 3.7)
        self.assertAlmostEqual(before[13], 7.8)

    def test_no_floor_means_nothing_flagged(self):
        from main import _starved_slides

        self.assertEqual(_starved_slides(self.REPORT, 0.0), [])

    def test_missing_or_malformed_report_is_survivable(self):
        # Il report arriva da JSON: non deve far fallire la run.
        from main import _starved_slides

        for vuoto in (None, {}, {"guaranteed": None}, {"guaranteed": ["x", 3]}):
            self.assertEqual(_starved_slides(vuoto, 8.0), [])

    def test_slide_at_exactly_half_is_not_flagged(self):
        # 4.0s su minimo 8.0: al limite, non "saziate". La soglia e' aperta.
        from main import _starved_slides

        report = {"min_seconds": 8.0, "guaranteed": [{"slide": 1, "before": 4.0, "duration": 8.0}]}
        self.assertEqual(_starved_slides(report, 8.0), [])


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


class _FixtureConfondibile:
    """Deck con tutte le slide sullo stesso tema, e parlato che lo segue.

    È la situazione reale incontrata su un podcast di 24 minuti con 7 slide
    tutte dedicate allo stesso argomento: la cosine fra le slide supera la
    soglia di duplicato su ogni coppia, e il parlato di ogni sezione contiene
    le stesse parole generiche piu' parole specifiche. La matrice di
    similarita' resta quindi quasi piatta e l'argmax cade dove il vocabolario
    per caso esplode, non dove la sezione inizia.

    Un fixture del genere serve per una cosa sola: verificare che il programma
    **dica** che non sa, invece di produrre confini sbagliati in silenzio. Il
    caso felice (slide distinguibili, confini entro tolleranza) e' coperto da
    ``TestSemanticSync``; quello che mancava e' il caso in cui la timeline non
    e' sbagliata per caso ma perche' l'informazione non c'e', e li' la sola
    cosa che il programma puo' fare e' dichiararlo.
    """

    # Quattro parole condivise da tutte le sezioni (il tema unico) e una parola
    # specifica per sezione: il rapporto e' 4:1, quindi la cosine fra due slide
    # diverse resta alta (oltre la soglia di 0.6) ma non identica.
    COMUNE = ("prompt", "modello", "istruzioni", "contesto")
    SEZIONI = ("teletpatia", "parrucchiere", "vincoli", "medico", "premortem")

    # Le parole che NON stanno su nessuna slide. Nel parlato reale la sezione
    # parla *attorno* al tema: quasi tutte le parole del blocco non sono sulla
    # slide, e la differenza fra "parla di questo" e "parla di altro" si
    # assottiglia fino a sparire. Sono il rumore di fondo che rende piatta la
    # matrice di similarita'.
    RUMORE = ("oggi", "secondo", "conversazione", "riguardo", "ascoltiamo", "tempo")

    # Sezione -> sezione di cui l'ultimo blocco parla invece di chiudere la
    # propria. In un dibattito succede ("prima di chiudere, un accenno a
    # quello che verra"): e' la ragione per cui nella run vera l'ordine dei
    # picchi non era monotono (concordanza 62%) e il motore non poteva sapere
    # quale confine fosse giusto.
    ANTICIPAZIONI: ClassVar[dict[int, int]] = {2: 4, 3: 5}

    def __init__(self, *, window=4.0, blocchi_per_sezione=2):
        self.window = window
        self.n = len(self.SEZIONI)
        self.blocchi_per_sezione = blocchi_per_sezione
        self.durata_sezione = window * blocchi_per_sezione
        self.durata_totale = self.n * self.durata_sezione

    def slide_texts(self):
        """Una slide per sezione: il tema unico piu' la parola che la distingue."""
        return [" ".join((*self.COMUNE, sez)) for sez in self.SEZIONI]

    def blocks(self):
        """Il parlato segue l'ordine delle sezioni, ma non combacia mai.

        Tre cose lo rendono realistico e insieme ambiguo:

        1. ogni blocco contiene il rumore di fondo, quindi la cosine grezza
           resta alta con tutte le slide (0.75-0.83 come sui dati reali);
        2. la parola che distingue la sezione compare solo nell'ULTIMO blocco
           della sezione, non all'inizio: il picco cade dove la sezione sta
           per finire, non dove cambia. E' il difetto che sulla run vera ha
           spostato il confine 1->2 di due minuti;
        3. due sezioni anticipano il tema di una successiva (``ANTICIPAZIONI``),
           quindi l'ordine dei picchi non e' monotono.
        """
        out = []
        for i, sez in enumerate(self.SEZIONI, start=1):
            anticipa = self.ANTICIPAZIONI.get(i)
            for b in range(self.blocchi_per_sezione):
                parole = list(self.COMUNE) + list(self.RUMORE)
                if b == self.blocchi_per_sezione - 1:
                    parole.append(self.SEZIONI[anticipa - 1] if anticipa else sez)
                out.append({"time": len(out) * self.window, "text": " ".join(parole)})
        return out

    def confini_veri(self):
        """Dove l'ascolto umano direbbe che la sezione cambia."""
        return {i + 1: i * self.durata_sezione for i in range(1, self.n)}

    def embed(self, testi):
        """Embedder BoW deterministico sul vocabolario del fixture.

        Non e' un trucco: il vocabolario contiene le parole comuni, quelle
        distinctive e il rumore, quindi la geometria che ne esce e' proprio
        quella che rende il deck confondibile -- l'unica cosa che
        ``confusability`` misura.
        """
        vocabolario = (*self.COMUNE, *self.SEZIONI, *self.RUMORE)
        out = np.zeros((len(testi), len(vocabolario)), dtype=np.float64)
        for riga, testo in enumerate(testi):
            parole = set(testo.lower().split())
            for c, parola in enumerate(vocabolario):
                if parola in parole:
                    out[riga, c] = 1.0
            norma = np.linalg.norm(out[riga])
            if norma:
                out[riga] /= norma
        return out


class TestFixtureConfondibile(unittest.TestCase):
    """Il fixture di una run reale: cosa il programma promette e cosa fa.

    La run da cui viene questo fixture ha prodotto un video in cui la slide 1
    restava a schermo perdue minuti di contenuto della slide 2, e la slide 2
    si beveva 56 secondi. Tutti i controlli automatici passavano: il frame
    check diceva 7/7 segmenti corretti (perche' verifica che il video mostri
    la timeline, non che la timeline sia giusta) e ``starved_slides`` non
    scattava (la slide 2 non era troppo corta in assoluto: era troppo corta
    rispetto al suo contenuto).

    Qui si fissa il contratto che mancava, in due pezzi distinti:

    1. il deck di questo fixture e' oggettivamente confondibile, e i confini
       che il motore ne ricava NON coincidono con quelli che l'ascolto umano
       riconoscerebbe: non e' un caso, e' la definizione del segnale debole;
    2. quando il programma sa di non sapere, il riepilogo non annuncia
       solidita'. Il secondo e' il test che dimostra il difetto: prima della
       modifica fallisce, e non fallisce per caso.
    """

    def _timeline(self):
        from semantic_sync import reset_weak_signal_flag, semantic_timeline_from_texts

        reset_weak_signal_flag()
        fx = _FixtureConfondibile()
        tl = semantic_timeline_from_texts(
            fx.slide_texts(),
            fx.blocks(),
            total_slides=fx.n,
            total_duration=fx.durata_totale,
            embed_fn=fx.embed,
            options=SemanticOptions(window_seconds=fx.window, min_slide_duration=3.0),
        )
        return fx, tl

    def test_il_deck_non_consente_di_determinare_i_confini(self):
        # Parti dal fatto geometrico, non da una soglia: il fixture serve per
        #che' il deck non distingue le sezioni, e questo e' un numero. Se un
        # giorno questo test fallisce, il fixture ha smesso di riprodurre il
        # caso reale e il test qui sotto non vale piu' niente.
        fx, tl = self._timeline()
        self.assertIsNotNone(tl)
        emb = fx.embed(fx.slide_texts())
        da_fare = emb @ emb.T
        coppie = da_fare[np.triu_indices(fx.n, 1)]
        # Ogni coppia di slide e' oltre la soglia di duplicato (0.6): il
        # report lo deve dire, non solo che i due testi sono "un po'" simili.
        self.assertGreater(float(coppie.min()), 0.6)

        # E i confini del motore non sono quelli dell'ascolto umano: almeno
        # una transizione cade una finestra piu' in la'. Non e' un bug da
        # correggere qui: e' l'informazione che manca, ed e' il motivo per cui
        # il riepilogo deve dichiararla.
        veri = fx.confini_veri()
        spostamenti = [abs(tl[s] - veri[s]) for s in veri if s in tl]
        self.assertGreaterEqual(max(spostamenti), fx.window)

    def test_il_report_non_dichiara_solidita_quando_non_sa(self):
        # Il difetto, riprodotto sulla run vera: il frame check da' il pieno,
        # il segnale e' debole, e il riepilogo scrive "nessuno, la
        # sincronizzazione e' risultata solida" tre righe sopra "fiducia del
        # motore: bassa".
        #
        # Il segnale debole qui viene impostato a mano e non calcolato: il
        # difetto non e' che l'euristica scatta, e' che il riepilogo ignora
        # il flag quando scatta. Legare il test all'euristica lo renderebbe
        # fragile e gli darebbe un secondo compito che non e' suo.
        import main
        from semantic_sync import _set_weak_signal, reset_weak_signal_flag

        reset_weak_signal_flag()
        _set_weak_signal()
        self.addCleanup(reset_weak_signal_flag)
        fx = _FixtureConfondibile()
        n = fx.n

        with self.assertLogs(main.log, level="INFO") as cm:
            main._log_plain_summary(
                [fx.durata_sezione] * n,
                list(range(1, n + 1)),
                fx.durata_totale,
                frame_check={"checked": n, "coherent": n, "mismatches": []},
                quality={"avg_sim": 0.83, "avg_z": 0.36, "min_avg_z": 0.45},
                anchors={
                    "anchored": 0,
                    "transitions": n - 1,
                    "unanchored_slides": list(range(2, n + 1)),
                },
            )
        testo = "\n".join(r.getMessage() for r in cm.records)

        self.assertIn("somiglianza tra parlato e slide", testo)
        self.assertNotIn("risultata solida", testo)


class TestMinGapNonDistruggeLaTimeline(unittest.TestCase):
    """Il minimo di durata per slide non può rendere impossibile la timeline.

    `build_candidates` chiede, per ogni slide, una finestra di blocchi che
    riservi `min_gap` alle slide successive. Se il minimo è più grande
    dell'audio, la finestra si chiude e la funzione restituisce None: la run
    muore con "sincronizzazione impossibile senza distribuzioni inventate", che
    è una diagnosi falsa — il problema è il vincolo, non l'informazione mancante.

    Nota sulla scala: con i default (minimo 3s, finestra 4s) il rapporto è 0.75
    e il troncamento dava 0, corretto poi a 1: risultato identico a `ceil`.
    Il clamp serve per le configurazioni in cui il rapporto supera davvero 1.
    """

    def _timeline(self, *, n_slides, n_blocks, min_slide_duration, window):
        from semantic_sync import reset_weak_signal_flag, semantic_timeline_from_texts

        reset_weak_signal_flag()
        temi = [f"tema{i}" for i in range(n_slides)]
        blocks = [
            {"time": i * window, "text": (temi[min(i, n_slides - 1)] + " ") * 3}
            for i in range(n_blocks)
        ]
        return semantic_timeline_from_texts(
            [f"{t} slide" for t in temi],
            blocks,
            total_slides=n_slides,
            total_duration=n_blocks * window,
            embed_fn=_FakeThemedEmbed(temi),
            options=SemanticOptions(window_seconds=window, min_slide_duration=min_slide_duration),
        )

    def test_minimo_piu_grande_del_blocco_non_produce_none(self):
        # min_slide_duration (60s) > finestra (4s): il vincolo sarebbe 15
        # blocchi per slide su 10 blocchi disponibili.
        tl = self._timeline(
            n_slides=3, n_blocks=10, min_slide_duration=60.0, window=4.0
        )
        self.assertIsNotNone(tl, "un minimo di durata irrealizzabile non deve far fallire la run")
        self.assertEqual(sorted(tl), [1, 2, 3])

    def test_la_clamp_cerca_un_minimo_piu_tardi_invece_di_silenzio(self):
        tl = self._timeline(
            n_slides=4, n_blocks=12, min_slide_duration=16.0, window=4.0
        )
        self.assertIsNotNone(tl)
        # 16s / 4s = 4 blocchi per slide, 12 blocchi disponibili: il clamp
        # porta il minimo a 3 blocchi (12 // 4), cioè il massimo che sta in
        # piedi senza violare la durata totale.
        for s in range(1, 4):
            self.assertGreaterEqual(tl[s + 1] - tl[s], 12.0)

    def test_i_default_restano_identici(self):
        # Il caso di ogni giorno: qui il clamp non deve toccare niente, e la
        # timeline deve restare quella di prima. Il minimo con i default è un
        # blocco (3s su finestre da 4s), quindi l'unica cosa che si afferma è
        # che la timeline resta valida e monotona: su questo input degenere la
        # DP può scegliere confini diversi da quelli "naturali" e va bene,
        # perché non c'è un confine giusto da rispettare.
        tl = self._timeline(n_slides=4, n_blocks=20, min_slide_duration=3.0, window=4.0)
        self.assertIsNotNone(tl)
        self.assertEqual(tl[1], 0.0)
        starts = [tl[s] for s in sorted(tl)]
        self.assertEqual(starts, sorted(starts), "la timeline deve restare monotona")
        for a, b in pairwise(starts):
            self.assertGreaterEqual(b - a, 4.0)


class TestConfusabilityNelReport(unittest.TestCase):
    """La causa della bassa fiducia deve finire nel report — ed essere vera.

    `avg_z` dice quanto i segmenti sono picchi; `confusability` dice PERCHÉ il
    motore fatica (quante slide si somigliano fra loro). Prima stava solo
    dentro il confronto A/B del beam, quindi un report senza beam automatico
    non portava traccia del problema.

    Ma la misura stessa era rotta, ed è il caso reale a dirlo: su un deck di 7
    slide tutte sullo stesso argomento, la cosine GREZZA fra due slide
    qualsiasi sta a 0.85-0.91, e "Il gatto dorme sul divano" sta a 0.88 da
    "L'Illusione della Telepatia". Con la soglia a 0.6 la misura valeva 1.0 per
    QUALSIASI deck, e il riepilogo accusava l'utente di slide confondibili che
    non aveva.

    Qui si verifica che, tolto il basale comune, le slide del deck reale
    risultino diverse: zero coppie duplicate.
    """

    @staticmethod
    def _deck_reale():
        """Embedding con lo stesso difetto del modello vero: basale altissimo.

        Non serve il modello: ciò che conta è che tutte le slide condividano
        una componente dominante (il "testo italiano generico") e si
        differenzino solo per una piccola parte. Sulla cosine grezza superano
        tutte la soglia di duplicato, esattamente come sul deck reale.
        """
        rng = np.random.default_rng(20260903)
        base = np.array([1.0] * 40)
        v = []
        for _ in range(7):
            distintivo = rng.normal(size=40) * 0.06
            x = base * 3.0 + distintivo
            v.append(x / np.linalg.norm(x))
        return np.array(v)

    def test_il_basale_alto_non_trasforma_ogni_deck_in_duplicate(self):
        from semantic_sync import signal_quality_report

        slide_emb = self._deck_reale()
        sim = np.eye(len(slide_emb))
        # Precondizione: sulla metrica vecchia (cosine grezza) questo deck era
        # 100% duplicati. È il difetto, non una proprietà del deck.
        grezza = slide_emb @ slide_emb.T
        iu = np.triu_indices(len(slide_emb), 1)
        self.assertGreater(float(grezza[iu].min()), 0.6)

        report = signal_quality_report(sim, slide_emb)
        self.assertEqual(report["confusability"], 0.0)

    def test_slide_identiche_restano_duplicate(self):
        # Il centro non deve cancellare il caso peggiore: due slide identiche
        # non hanno direzione propria e sono duplicati di tutto, anche dopo la
        # normalizzazione (che altrimenti le renderebbe "diverse" per definizione).
        from semantic_sync import signal_quality_report

        slide_emb = np.array([[1.0, 0.0], [1.0, 0.0]], dtype=np.float64)
        sim = np.array([[1.0, 0.2], [0.2, 1.0]], dtype=np.float64)
        report = signal_quality_report(sim, slide_emb)
        self.assertEqual(report["confusability"], 1.0)

    def test_una_slide_identica_alle_altre_resta_il_legittimo_dubbio(self):
        # Cinque slide diverse e una quasi identica a una delle altre: una sola
        # coppia su quindici è duplicata, e la misura deve dirlo. Le
        # dimensioni sono alte (24) perché dopo il centraggio due vettori
        # casuali sono quasi ortogonali: l'unica similarità reale deve essere
        # quella della copia.
        from semantic_sync import signal_quality_report

        rng = np.random.default_rng(7)
        vettori = rng.normal(size=(6, 24))
        vettori[5] = vettori[1] + 0.01 * rng.normal(size=24)
        vettori /= np.linalg.norm(vettori, axis=1, keepdims=True)
        report = signal_quality_report(np.eye(6), vettori)
        self.assertAlmostEqual(report["confusability"], 1 / 15, places=6)

    def test_quality_contiene_la_causa(self):
        from semantic_sync import (
            last_quality,
            reset_weak_signal_flag,
            semantic_timeline_from_texts,
        )

        reset_weak_signal_flag()
        fx = _FixtureConfondibile()
        semantic_timeline_from_texts(
            fx.slide_texts(),
            fx.blocks(),
            total_slides=fx.n,
            total_duration=fx.durata_totale,
            embed_fn=fx.embed,
            options=SemanticOptions(window_seconds=fx.window, min_slide_duration=3.0),
        )
        q = last_quality()
        self.assertIn("confusability", q)
        self.assertIn("concordance", q)
        # Il fixture ha tutte le slide sullo stesso tema: è il caso in cui il
        # dubbio è legittimo, e i titoli sono già distinti. Quindi qui la misura
        # deve confermare che sono DIVERSE (non duplicati), e il dubbio del
        # riepilogo verrà da avg_z, non da confusability.
        self.assertLessEqual(q["confusability"], 0.5)


class TestNormalizedQualityGuard(unittest.TestCase):
    """Guard-rail di qualità sulla scala normalizzata (z-score per slide).

    La cosine grezza di e5 ha una baseline altissima tra due testi italiani
    qualsiasi: sui dati reali vale 0.80-0.88 sia con le slide nell'ordine
    giusto sia mescolate, quindi il valore assoluto non contiene informazione
    sull'allineamento. Questi test fissano la misura che discrimina davvero
    (picco medio normalizzato) e il fatto che un valore basso è un SEGNALE
    (--strict-sync, report) e non un verdetto che scarta la
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
        # precedente): va segnalata, perché le ancore possono ancora
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
        # Lo schema porta anche la CAUSA (quante slide si somigliano fra loro)
        # e non solo il sintomo: su un deck coerente devono essere entrambe
        # buone, ed è il loro assenza che rendeva il report muto sul problema
        # reale (7 slide tutte sullo stesso argomento, confusability 100%).
        self.assertEqual(
            sorted(quality),
            ["avg_sim", "avg_z", "concordance", "confusability", "min_avg_z"],
        )
        self.assertLessEqual(quality["confusability"], 0.5)
        self.assertGreaterEqual(quality["concordance"], 0.5)


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
        from semantic_sync import refine_segment_boundaries

        segments = [
            {"slide": 1, "start": 0.0, "end": 60.0},
            {"slide": 2, "start": 60.0, "end": 120.0},
        ]
        words = self._words(split=30.0, end=120.0)
        slide_texts = [f"{t} slide" for t in self.THEMES]
        embed = TestSemanticSync._fake_embed(self.THEMES)

        default = refine_segment_boundaries(
            segments, words, slide_texts, embed,
            window_seconds=20.0, min_segment_seconds=5.0, refine_slides={2},
        )
        guided = refine_segment_boundaries(
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


class TestArtifactVerified(unittest.TestCase):
    """Il frame check sul pieno vale più della fiducia del motore.

    Caso reale del 02/10: 8 slide su 30 minuti, il motore dichiarava
    segnale debole (avg_z 0.38 sotto la soglia 0.45) e il riepilogo avvisava
    "sincronizzazione a bassa fiducia", mentre il frame check dava 8/8: il
    video mostrava le slide giuste. L'avviso era un falso allarme su un
    artefatto già verificato.
    """

    OK: ClassVar[dict[str, object]] = {"checked": 3, "coherent": 3, "mismatches": []}

    def test_full_check_without_mismatch_verifies(self):
        from main import _artifact_verified

        self.assertTrue(_artifact_verified(self.OK, 3))

    def test_no_check_does_not_verify(self):
        from main import _artifact_verified

        self.assertFalse(_artifact_verified(None, 3))

    def test_zero_segments_does_not_verify(self):
        # Un run senza segmenti non può essere "verificato": eviterebbe che
        # l'avviso del motore spari del tutto.
        from main import _artifact_verified

        self.assertFalse(_artifact_verified(self.OK, 0))

    def test_partial_check_does_not_verify(self):
        # 2 segmenti controllati su 3: il terzo non è guardato, non importa
        # che i due controllati siano coerenti.
        from main import _artifact_verified

        self.assertFalse(_artifact_verified({"checked": 2, "coherent": 2, "mismatches": []}, 3))

    def test_mismatch_does_not_verify(self):
        from main import _artifact_verified

        check = {"checked": 3, "coherent": 2, "mismatches": [{"slide": 2}]}
        self.assertFalse(_artifact_verified(check, 3))

    def test_more_checked_than_total_still_verifies(self):
        # Difesa: un frame check che copre piu' dei segmenti attesi resta valido.
        from main import _artifact_verified

        self.assertTrue(_artifact_verified({"checked": 5, "coherent": 5, "mismatches": []}, 3))


    def test_momenti_anteprima_sono_annuncio_e_confine(self):
        # Il foglio di controllo deve mostrare i due istanti che il dubbio mette
        # in contraddizione: dove il parlato annuncia la slide e dove la timeline
        # la fa comparire.
        from main import _boundary_preview_moments

        momenti = _boundary_preview_moments({"slide": 10, "mention": 1017.8, "declared": 1058.2})
        self.assertEqual(len(momenti), 2)
        self.assertIn("slide 10", momenti[0][0])
        self.assertIn("16m57s", momenti[0][0])
        self.assertAlmostEqual(momenti[0][1], 1017.8)
        self.assertIn("17m38s", momenti[1][0])
        self.assertAlmostEqual(momenti[1][1], 1058.2)

    def test_anteprima_scritta_e_legata_al_dubbio(self):
        # Se il foglio viene scritto, il dubbio lo cita: senza questo il file
        # resterebbe lì e l'utente non saprebbe dove guardare.
        import main

        with tempfile.TemporaryDirectory() as tmp:
            td = pathlib.Path(tmp)
            item: dict[str, object] = {"slide": 10, "mention": 1017.8, "declared": 1058.2}
            with mock.patch(
                "main.boundary_review_sheet", return_value=td / "review" / "foglio.png"
            ) as sheet:
                main._write_boundary_previews(td / "video.mp4", [item], td / "review")
            self.assertEqual(item["preview"], str(td / "review" / "foglio.png"))
            scritto = pathlib.Path(str(sheet.call_args[0][2]))
            self.assertEqual(scritto.name, "confine_slide10_00.png")
            self.assertEqual(sheet.call_args[0][0], td / "video.mp4")

    def test_video_assente_niente_anteprima(self):
        # Senza video (dry-run, output spostato) non c'è anteprima: il dubbio
        # resta scritto ma non cita un file che non esiste.
        import main

        with tempfile.TemporaryDirectory() as tmp:
            td = pathlib.Path(tmp)
            item: dict[str, object] = {"slide": 10, "mention": 1017.8, "declared": 1058.2}
            main._write_boundary_previews(td / "non_esiste.mp4", [item], td / "review")
            self.assertNotIn("preview", item)


class TestReviewOnly(unittest.TestCase):
    """Revisione dei confini dubbi di una run già fatta (``main.py --review``).

    Il report e la trascrizione sono in cache: la revisione legge quelli e
    rigenera solo le anteprime dal video esistente. Questi test tengono fermo il
    contratto del comando: cosa stampa, cosa NON ricalcola, e che non esploda
    quando il report non c'è (una revisione che si rompe è peggio di nessuna
    revisione: l'utente non sa più cosa guardare).
    """

    def _cache(self, td: pathlib.Path, *, mentions_off=None, words=None):
        """Report (e trascrizione) di una run finta, scritti col serializzatore vero."""
        import json

        from pipeline_cache import save_sync_report

        cache = td / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        report: dict[str, object] = {
            "audio_duration": 120.0,
            "segments": [
                {"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0},
                {"slide": 2, "start": 60.0, "end": 120.0, "duration": 60.0},
            ],
            "anchors": {"anchored": 1, "transitions": 1, "unanchored_slides": [2]},
        }
        anchors = report["anchors"]
        if mentions_off is not None and isinstance(anchors, dict):
            anchors["mentions_off"] = mentions_off
        save_sync_report(report, cache_dir=cache)
        if words is not None:
            (cache / "transcript_finta.json").write_text(
                json.dumps({"words_raw": words}), encoding="utf-8"
            )
        return cache

    def _review(self, cache: pathlib.Path, video: pathlib.Path) -> str:
        import main

        with mock.patch.object(main, "CACHE_DIR", cache), mock.patch(
            "main.boundary_review_sheet", return_value=cache / "review" / "foglio.png"
        ), self.assertLogs("slide2video", level="INFO") as logs:
            main._review_boundary_doubts(video)
        return "\n".join(logs.output)

    def test_menzioni_salvate_stampate_con_anteprima(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = pathlib.Path(tmp)
            cache = self._cache(
                td,
                mentions_off=[{"slide": 2, "mention": 1017.8, "declared": 1058.2}],
            )
            video = td / "video_finale.mp4"
            video.write_bytes(b"video finto: serve solo che esista")
            out = self._review(cache, video)
        self.assertIn("REVISIONE DEI CONFINI", out)
        self.assertIn("Confini ancorati nell'ultima run: 1 su 1", out)
        self.assertIn("slide 2 viene annunciata a 16m57s", out)
        self.assertIn("il video la mostra da 17m38s", out)
        self.assertIn("anteprima:", out)

    def test_report_vecchio_ricalcola_dalla_trascrizione(self):
        # Report scritto PRIMA del controllo incrociato (come quello del 14/10):
        # `mentions_off` non c'è, quindi si ricava dalle parole in cache.
        with tempfile.TemporaryDirectory() as tmp:
            td = pathlib.Path(tmp)
            words = [
                ("passiamo", 300.0), ("alla", 300.4), ("slide", 300.7),
                ("2", 300.9), ("poi", 301.4),
            ]
            cache = self._cache(
                td, words=[{"word": w, "start": t} for w, t in words]
            )
            out = self._review(cache, td / "video_finale.mp4")
        self.assertIn("slide 2 viene annunciata a 5m01s", out)
        self.assertIn("il video la mostra da 1m00s", out)
        self.assertIn("dalle parole in cache", out)

    def test_senza_contraddizioni_dichiara_il_confine_stimato(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = pathlib.Path(tmp)
            cache = self._cache(td, mentions_off=[])
            out = self._review(cache, td / "video_finale.mp4")
        self.assertIn("Confini stimati dal contenuto", out)
        self.assertIn("la slide 2", out)
        self.assertIn("niente da rivedere", out)

    def test_video_assente_dubbi_senza_anteprima(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = pathlib.Path(tmp)
            cache = self._cache(
                td, mentions_off=[{"slide": 2, "mention": 1017.8, "declared": 1058.2}]
            )
            out = self._review(cache, td / "non_esiste.mp4")
        self.assertIn("Video assente", out)
        self.assertIn("slide 2 viene annunciata", out)

    def test_senza_report_lo_dice_e_non_esplode(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            td = pathlib.Path(tmp)
            cache = td / ".cache"
            cache.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(main, "CACHE_DIR", cache), self.assertLogs(
                "slide2video", level="ERROR"
            ) as logs:
                main._review_boundary_doubts(td / "video_finale.mp4")
        self.assertIn("Nessun report da rivedere", "\n".join(logs.output))


class TestWeakSignalSurvivesFrameCheck(unittest.TestCase):
    """Il dubbio del motore resta anche con l'artefatto verificato.

    Il frame check e la fiducia del motore rispondono a due domande diverse: il
    primo verifica che il video mostri la timeline, il secondo se la timeline
    sia giusta. Il frame viene preso a meta' segmento, quindi uno spostamento
    uniforme dei confini — il difetto reale, minuti di ritardo — passa senza
    essere visto. Per questo l'artefatto verificato non cancella il dubbio: lo
    dichiara come fatto accanto, e il dubbio resta.

    Qui sotto c'e' il test che dimostra il difetto: la run da cui viene questo
    file aveva 7 slide con confusability 100%, zero ancore, un confine
    sbagliato di due minuti, frame check 7/7 e riepilogo "nessuno, la
    sincronizzazione e' risultata solida".
    """

    def setUp(self):
        from semantic_sync import _set_weak_signal, reset_weak_signal_flag

        reset_weak_signal_flag()
        _set_weak_signal()

    def tearDown(self):
        from semantic_sync import reset_weak_signal_flag

        reset_weak_signal_flag()

    def _render(self, **kwargs):
        from main import _log_plain_summary

        with self.assertLogs("slide2video", level="INFO") as logs:
            _log_plain_summary([60.0, 60.0, 60.0], [1, 2, 3], 180.0, **kwargs)
        return "\n".join(logs.output)

    def test_weak_signal_is_a_doubt_when_artifact_not_verified(self):
        out = self._render(quality={"avg_sim": 0.86, "avg_z": 0.38, "min_avg_z": 0.45})
        self.assertIn("somiglianza tra parlato e slide", out)
        self.assertIn("Da controllare a mano", out)

    def test_artifact_verified_is_declared_as_a_fact(self):
        out = self._render(
            frame_check={"checked": 3, "coherent": 3, "mismatches": []},
            quality={"avg_sim": 0.86, "avg_z": 0.38, "min_avg_z": 0.45},
        )
        # L'artefatto è davvero verificato, e va detto: è un fatto utile.
        self.assertIn("video finito è stato verificato", out)
        self.assertIn("ogni segmento mostra la slide", out)

    def test_weak_signal_stays_a_doubt_when_artifact_verified(self):
        out = self._render(
            frame_check={"checked": 3, "coherent": 3, "mismatches": []},
            anchors={"anchored": 0, "transitions": 2, "unanchored_slides": [2, 3]},
            quality={"avg_sim": 0.86, "avg_z": 0.38, "min_avg_z": 0.45},
        )
        self.assertIn("somiglianza tra parlato e slide", out)
        # E non si può scrivere che è tutto a posto: sarebbe falso.
        self.assertNotIn("risultata solida", out)

    def test_il_dubbio_riguarda_solo_i_confini_non_ancorati(self):
        # Tutte le transizioni ancorate: la bassa fiducia viene dal deck
        # confondibile, non dai confini, che sono fatti pronunciati. Il dubbio
        # deve allora dire quello che c'è da fare (guardare i cambi di slide),
        # non accusare stime che non ci sono.
        out = self._render(
            frame_check={"checked": 3, "coherent": 3, "mismatches": []},
            anchors={"anchored": 2, "transitions": 2, "unanchored_slides": []},
            quality={"avg_sim": 0.86, "avg_z": 0.38, "min_avg_z": 0.45},
        )
        self.assertIn("somiglianza tra parlato e slide", out)
        self.assertIn("guarda a mano dove il parlato cambia tema", out)
        self.assertIn("ancore 'slide N' restano esatte", out)
        # Nessun riferimento a stime che in questo caso non esistono.
        self.assertNotIn("sono stime, non misure", out)


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

    def test_weak_signal_is_a_doubt(self):
        from semantic_sync import _set_weak_signal

        _set_weak_signal()
        out = self._render(
            quality={"avg_sim": 0.83, "avg_z": 0.20, "min_avg_z": 0.45},
        )
        self.assertIn("somiglianza tra parlato e slide è risultata debole", out)
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
        # Nessun pavimento applicato (senza ancore): nessuna riga in più.
        out = self._render(verdicts={3: "incerto"})
        self.assertNotIn("Durata garantita dall'anti-flicker", out)
        self.assertIn("slide 3 la durata è anomala", out)


    def test_annuncio_lontano_dal_confine_diventa_un_dubbio(self):
        # Caso reale del 14/10: annuncio a 16m56s, confine dichiarato a 17m38s.
        import main

        anchors = {
            "anchored": 8,
            "transitions": 9,
            "unanchored_slides": [10],
            "mentions_off": [{"slide": 3, "mention": 1016.5, "declared": 1058.2}],
        }
        with self.assertLogs("slide2video", level="INFO") as logs:
            main._log_plain_summary(
                [60.0, 60.0, 60.0],
                [1, 2, 3],
                180.0,
                anchors=anchors,
            )
        out = "\n".join(logs.output)
        self.assertIn("Da controllare a mano", out)
        self.assertIn("slide 3", out)
        self.assertIn("l'annuncio è un fatto", out)
        self.assertNotIn("risultata solida", out)

    def test_annunci_al_confine_niente_dubbio(self):
        # Nessuna contraddizione fra annunci e confini: nessun dubbio inventato.
        import main

        anchors = {
            "anchored": 2,
            "transitions": 2,
            "unanchored_slides": [3],
            "mentions_off": [],
        }
        with self.assertLogs("slide2video", level="INFO") as logs:
            main._log_plain_summary(
                [60.0, 60.0, 60.0],
                [1, 2, 3],
                180.0,
                anchors=anchors,
            )
        out = "\n".join(logs.output)
        self.assertIn("nessuno, la sincronizzazione è", out)
        self.assertNotIn("l'annuncio è un fatto", out)

    def test_dubbio_sui_confini_allega_l_anteprima(self):
        import main

        anchors = {
            "anchored": 1,
            "transitions": 2,
            "unanchored_slides": [3],
            "mentions_off": [
                {
                    "slide": 3,
                    "mention": 1016.5,
                    "declared": 1058.2,
                    "preview": ".cache/review/confine_slide03_00.png",
                }
            ],
        }
        with self.assertLogs("slide2video", level="INFO") as logs:
            main._log_plain_summary(
                [60.0, 60.0, 60.0],
                [1, 2, 3],
                180.0,
                anchors=anchors,
            )
        out = "\n".join(logs.output)
        self.assertIn("anteprima: .cache/review/confine_slide03_00.png", out)


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
        # più volte nella sequenza mostrata).
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
        # La stessa slide può comparire più volte nella
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

    def test_sync_report_carries_notes(self):
        # Le scelte della run (es. motore usato, segnale debole) devono restare
        # sull'artefatto: prima esistevano solo nei log e andavano perse.
        from main import _build_sync_report

        report = _build_sync_report(
            [60.0, 40.0],
            [1, 2],
            100.0,
            None,
            notes={"engine": "embedding_weak_signal"},
        )
        self.assertEqual(report["engine"], "embedding_weak_signal")


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

    def test_offsets_sparsi_non_distruggono_le_ancore(self):
        """Il caso reale del 04/10, con la combinazione che manca nei test.

        Dieci ancore, slides 2..11 dette nell'ordine giusto. Il contenuto ne
        contraddice alcune sparse, e la sotto-sequenza monotona ne scarta
        diverse: la timeline veniva ricostruita da zero e la slide 1 durava 4
        secondi.

        La combinazione e' voluta: quattro ancore hanno uno sliftamento
        CONSISTENTE (drift vero, la sotto-sequenza le tiene e vengono
        rimappate) e tre contraddicono in modo irregolare (la sotto-sequenza le
        scarta). Serve perche' con sole ancore contraddittorie il codice
        arrivava a "sospetto" anche senza la guardia, e il test passava per il
        motivo sbagliato: una guardia che non e' provata da niente.

        Qui senza la guardia vengono rimappate 4 ancore e perse 3; con la
        guardia non si corregge niente e si tengono tutte le ancore pronunciate.
        """
        n = 32
        slides = [f"tema{i} slide" for i in range(1, n + 1)]
        # Gli offset sono DISPERSI (mai due uguali consecutivi), altrimenti
        # entrerebbe il ramo 2 (una sola run sfasata) che corregge senza
        # passare dalla sotto-sequenza monotona: il test non proverebbe la
        # guardia. Alternati, il ramo 3 e' l'unico che puo' scartare ancore.
        punti = {
            2: 3, 6: 4, 10: 11, 14: 9, 18: 19, 22: 15, 26: 27, 30: 2,
        }
        parole = []
        for spoken, migliore in punti.items():
            t = 100.0 * spoken
            parole += [{"word": f"tema{migliore}", "start": t + i} for i in range(4)]
        ancore = {spoken: 100.0 * spoken for spoken in punti}

        report: dict = {}
        out = verify_anchor_mapping_embedding(
            slides,
            parole,
            ancore,
            total_slides=n,
            window_seconds=40.0,
            embed_fn=self._embed_fn(n),
            report=report,
        )
        # Nessuna correzione: il chiamante tiene tutte le ancore pronunciate.
        self.assertIsNone(
            out,
            "una correzione che perde ancore pronunciate non va applicata",
        )
        # E il caso resta ispezionabile: dichiarato sospetto, non liquidato.
        self.assertTrue(report.get("suspicious"))

    def test_perdita_di_una_sola_ancora_e_permessa(self):
        """La guardia tollera UNA perdita: oltre quella il contenuto non basta.

        Perdere un'ancora e' un giudizio legittimo (un numero pronunciato male):
        quel confine torna a essere stimato e il report lo dichiara. Perderne di
        piu' significa che la similarita' non dice niente, e allora il numero
        pronunciato vince.

        Qui la sotto-sequenza monotona scarta una sola ancora e ne lascia tre
        rimappabili: la correzione va applicata. Il test fissa il confine della
        guardia, che se fosse piu' stretta bloccherebbe anche i drift reali.
        """
        n = 32
        slides = [f"tema{i} slide" for i in range(1, n + 1)]
        punti = {2: 3, 6: 4, 10: 11, 14: 9, 18: 19}
        parole = []
        for spoken, migliore in punti.items():
            t = 100.0 * spoken
            parole += [{"word": f"tema{migliore}", "start": t + i} for i in range(4)]
        ancore = {spoken: 100.0 * spoken for spoken in punti}

        out = verify_anchor_mapping_embedding(
            slides,
            parole,
            ancore,
            total_slides=n,
            window_seconds=40.0,
            embed_fn=self._embed_fn(n),
        )
        self.assertIsNotNone(
            out, "una sola ancora persa e' un giudizio, non rumore"
        )
        # La misura della perdita e' il conteggio, non il confronto dei numeri:
        # un rimap cambia il numero di slide per definizione, quindi
        # `set(ancore) - set(out)` conta come perse anche le rimappate.
        self.assertEqual(len(out), len(ancore) - 1)
        # E il rimap c'e' stato: altrimenti questo test non coprirebbe nulla.
        self.assertNotEqual(set(out), set(ancore))

    def test_ancore_sulla_slide_1_non_vincolano_la_timeline(self):
        """Un rimappo che finisce sulla slide 1 non e' un confine di transizione.

        La slide 1 parte a 0.0s: un'ancora che la riguarda non puo' spostare
        nulla, quindi viene rimossa dal chiamante invece di far oscillare la
        timeline. Nota: non e' pero' il caso in cui la parola pronunciata sia
        "slide 1", che non dovrebbe mai accadere (la prima pagina non si annuncia).
        """
        n = 5
        slides = [f"tema{i} slide" for i in range(1, n + 1)]
        parole = []
        for spoken, migliore in ((2, 1), (3, 3), (4, 4)):
            t = 100.0 * spoken
            parole += [{"word": f"tema{migliore}", "start": t + i} for i in range(4)]
        ancore = {2: 200.0, 3: 300.0, 4: 400.0}

        out = verify_anchor_mapping_embedding(
            slides,
            parole,
            ancore,
            total_slides=n,
            window_seconds=40.0,
            embed_fn=self._embed_fn(n),
        )
        # Il chiamante (main) filtra via la slide 1; qui verifichiamo solo che
        # l'ancora non venga inventata sulla slide 1 se il mapping e' scartato.
        if out is not None:
            self.assertNotIn(1, out)

    def test_systematic_offset_detected_even_with_every_slide_anchored(self):
        # Il caso che il gating saltava: TUTTE le transizioni annunciate, quindi
        # nessuna slide senza ancora. È proprio lì che uno sfasamento di
        # numerazione resta invisibile (niente da completare), eppure sposta
        # l'intero video di una slide.
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
        # è SOSPETTO: il chiamante deve poter chiedere la verifica.
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
        # Corretto deterministicamente: nessun sospetto.
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
        # Segnale insufficiente: non sospetto.
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
        # in modo deterministico.
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
        # nessuna correzione, il mapping resta sospetto.
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


# =====================================================================
# ROBUSTEZZA DEL RAFFINAMENTO E DICHIARAZIONE DEI CONFINI STIMATI
# =====================================================================
class TestRefineRobustness(unittest.TestCase):
    """Il raffinamento sposta un confine solo se la misura lo sostiene.

    Caso reale del 14/10 (slide 10, unico confine stimato della run): nella
    finestra in cui il confine viene deciso la curva del punteggio ha deviazione
    standard 0.013 (±30s) / 0.019 (±45s), e le decisioni prese in due run
    consecutive valevano 0.021-0.023 (circa 1.8sigma sul MAD) e 0.054. Un margine
    assoluto di 0.01 non distingue un confine dal rumore della propria misura:
    qui si verifica che il margine venga confrontato col rumore MISURATO nella
    stessa finestra e che il confine non si muova quando ci sta dentro.
    """

    def setUp(self):
        import semantic_sync

        semantic_sync.reset_refine_diagnostics()
        self.addCleanup(semantic_sync.reset_refine_diagnostics)

    @staticmethod
    def _temi(temi, dim: int = 16, bias: float = 4.0):
        """Embedder finto RUMOROSO: tema nel testo + vettore pseudocasuale per testo.

        Il rumore per-testo è quello che rende il fixture realistico: con
        vettori one-hot i punteggi diventano identici dentro una zona omogenea,
        il MAD collassa a zero e la soglia sul rumore non ha più niente da
        misurare (proprio il caso che i vettori veri non producono).
        """

        def embed_fn(texts):
            vecs = []
            for text in texts:
                rng = np.random.default_rng(zlib.crc32(text.encode()))
                v = rng.normal(size=dim).astype(np.float32)
                for i, tema in enumerate(temi):
                    v[i] += bias * text.count(tema)
                vecs.append(v / max(float(np.linalg.norm(v)), 1e-9))
            return np.stack(vecs)

        return embed_fn

    @staticmethod
    def _due_sezioni(cambio: int, confine: int) -> tuple[list, list]:
        """Parlato di tema_a fino a ``cambio``, tema_b dopo; confine dichiarato."""
        words = _words(
            [("tema_a", float(t)) for t in range(0, cambio, 2)]
            + [("tema_b", float(t)) for t in range(cambio, 100, 2)]
        )
        segments = [
            {"slide": 1, "start": 0.0, "end": float(confine)},
            {"slide": 2, "start": float(confine), "end": 100.0},
        ]
        return words, segments

    def test_guadagno_dentro_il_rumore_non_sposta_e_lo_dichiara(self):
        # Il cambio reale di argomento sta 6s dopo il confine dichiarato, ma il
        # punteggio si muove da solo più di così (misurato su questo fixture:
        # margine 0.51 contro 2x0.30 di rumore, cioè 1.7sigma): il confine resta, e
        # la decisione dice perché (prima si sarebbe spostato di 6s senza dirlo).
        from semantic_sync import refine_diagnostics

        words, segments = self._due_sezioni(56, 50)
        out = refine_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b"],
            self._temi(["tema_a", "tema_b"]),
            window_seconds=30.0,
            min_segment_seconds=5.0,
            context_seconds=10.0,
        )
        self.assertAlmostEqual(out[1]["start"], 50.0)
        voce = next(d for d in refine_diagnostics() if d["slide"] == 2)
        self.assertEqual(voce["decision"], "tenuto: margine dentro il rumore")
        self.assertLess(float(voce["sigma"]), 2.0)
        self.assertGreater(float(voce["margin"]), 0.0)

    def test_guadagno_fuori_dal_rumore_sposta(self):
        # Cambio di argomento netto (40s) lontano dal confine dichiarato (30s):
        # il picco è molto più alto della dispersione, quindi si sposta.
        from semantic_sync import refine_diagnostics

        words, segments = self._due_sezioni(40, 30)
        out = refine_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b"],
            self._temi(["tema_a", "tema_b"]),
            window_seconds=30.0,
            min_segment_seconds=5.0,
            context_seconds=10.0,
        )
        # Il cambio è a 40s: il confine si sposta verso di lui (il fixture
        # arrotonda al timestamp di parola, quindi non si chiede il secondo).
        self.assertGreater(out[1]["start"], 30.0)
        self.assertAlmostEqual(out[0]["end"], out[1]["start"])
        voce = next(d for d in refine_diagnostics() if d["slide"] == 2)
        self.assertEqual(voce["decision"], "spostato")
        self.assertGreaterEqual(float(voce["sigma"]), 2.0)

    def test_decisione_instabile_non_sposta(self):
        # La prova di stabilità è un cancello: se il candidato vince solo con la
        # finestra stretta, il confine resta dov'è (qui la prova è forzata).
        from semantic_sync import refine_diagnostics

        words, segments = self._due_sezioni(40, 30)
        with mock.patch(
            "semantic_sync._refine_wins_under_perturbation", return_value=False
        ):
            out = refine_segment_boundaries(
                segments,
                words,
                ["tema_a", "tema_b"],
                self._temi(["tema_a", "tema_b"]),
                window_seconds=30.0,
                min_segment_seconds=5.0,
                context_seconds=10.0,
            )
        self.assertAlmostEqual(out[1]["start"], 30.0)
        voce = next(d for d in refine_diagnostics() if d["slide"] == 2)
        self.assertEqual(voce["decision"], "tenuto: vince solo a finestra stretta")

    def test_perturbazione_finestre_vuote_non_vince(self):
        # Ai bordi dell'audio la finestra allargata non contiene parlato: senza
        # poter verificare la stabilità non si sposta.
        import semantic_sync as ss

        words = _words([("tema_a", 1.0), ("tema_b", 1.5)])
        wt = [float(w["start"]) for w in words]
        self.assertFalse(
            ss._refine_wins_under_perturbation(
                words, wt, 0, 1, 1.5, 1.0, np.eye(2, dtype=np.float32), self._temi(["a", "b"]), 10.0
            )
        )

    def test_perturbazione_embedding_fallito_non_vince(self):
        import semantic_sync as ss

        words = _words([("tema_a", float(t)) for t in range(0, 40, 2)])
        wt = [float(w["start"]) for w in words]

        def exploding(texts):
            raise RuntimeError("modello non disponibile")

        self.assertFalse(
            ss._refine_wins_under_perturbation(
                words, wt, 0, 1, 30.0, 20.0, np.eye(2, dtype=np.float32), exploding, 10.0
            )
        )

    def test_riparazione_guidata_non_bloccata_dalle_soglie(self):
        # La riparazione guidata dai frame sa già che il confine è sbagliato (il
        # video mostrava un'altra slide): l'evidenza sostituisce il punteggio,
        # quindi le soglie di robustezza non la bloccano — mentre il raffinamento
        # libero, sulle stesse parole, non si muove.
        from semantic_sync import repair_segments_from_frame_mismatches

        words, segments = self._due_sezioni(40, 60)
        mismatches = [{"time": 55.0, "shown": 2, "similarity": 0.9}]
        with mock.patch(
            "semantic_sync._refine_wins_under_perturbation", return_value=False
        ):
            refined, applied = repair_segments_from_frame_mismatches(
                segments,
                mismatches,
                words,
                ["tema_a", "tema_b"],
                self._temi(["tema_a", "tema_b"]),
                min_segment_seconds=5.0,
                context_seconds=10.0,
            )
            libero = refine_segment_boundaries(
                segments,
                words,
                ["tema_a", "tema_b"],
                self._temi(["tema_a", "tema_b"]),
                window_seconds=30.0,
                min_segment_seconds=5.0,
                context_seconds=10.0,
            )
        self.assertEqual(len(applied), 1)
        self.assertLess(float(applied[0]["new_start"]), 60.0)
        self.assertLess(float(refined[1]["start"]), 60.0)
        self.assertAlmostEqual(float(libero[1]["start"]), 60.0)
        self.assertEqual(applied[0]["slide"], 2)

    def test_diagnostica_per_confine_registrata_con_i_numeri(self):
        # Ogni confine valutato lascia margine, rumore locale e sigma: è il dato
        # che permette di dire QUALE confine è debole invece di un solo numero
        # globale che non dice dove guardare.
        from semantic_sync import refine_diagnostics

        words, segments = self._due_sezioni(40, 30)
        refine_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b"],
            self._temi(["tema_a", "tema_b"]),
            window_seconds=30.0,
            min_segment_seconds=5.0,
            context_seconds=10.0,
        )
        voce = next(d for d in refine_diagnostics() if d["slide"] == 2)
        for campo in ("current", "best", "margin", "scatter", "sigma", "decision"):
            self.assertIn(campo, voce)
        self.assertGreater(float(voce["scatter"]), 0.0)


class TestRefineConEmbeddingInCache(unittest.TestCase):
    """Le regole nuove girano anche quando gli embedding arrivano dalla cache.

    È la classe di errori che nessuno vedrebbe: una regola messa per sbaglio
    dietro un ``if not from_cache`` non si applicherebbe mai alle run già fatte
    (proprio quelle che si rivedono per recuperare un'ancora), e la differenza
    non comparirebbe da nessuna parte.
    """

    def setUp(self):
        import semantic_sync

        self._ss = semantic_sync
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_dir = semantic_sync._EMBED_CACHE_DIR
        semantic_sync._EMBED_CACHE_DIR = pathlib.Path(self._tmp.name)
        semantic_sync.reset_embed_cache()
        semantic_sync.reset_refine_diagnostics()
        self.addCleanup(self._restore)

    def _restore(self):
        self._ss._EMBED_CACHE_DIR = self._orig_dir
        self._ss.set_embed_cache_enabled(True)
        self._ss.reset_embed_cache()
        self._ss.reset_refine_diagnostics()

    @staticmethod
    def _counting_embed():
        """Embedder finto rumoroso (stessa forma dei test di robustezza) + conteggio."""
        calls = []

        def _embed(texts):
            calls.append(list(texts))
            out = []
            for text in texts:
                rng = np.random.default_rng(zlib.crc32(text.encode()))
                v = rng.normal(size=16).astype(np.float32)
                for i, tema in enumerate(("tema_a", "tema_b")):
                    v[i] += 4.0 * text.count(tema)
                out.append(v / max(float(np.linalg.norm(v)), 1e-9))
            return np.stack(out)

        _embed.embed_id = "modello-di-prova"
        _embed.calls = calls
        return _embed

    def test_secondo_giro_dagli_stessi_embedding_stesse_decisioni(self):
        from semantic_sync import refine_diagnostics

        words = _words(
            [("tema_a", float(t)) for t in range(0, 40, 2)]
            + [("tema_b", float(t)) for t in range(40, 100, 2)]
        )
        segments = [
            {"slide": 1, "start": 0.0, "end": 30.0},
            {"slide": 2, "start": 30.0, "end": 100.0},
        ]
        embed = self._counting_embed()
        primo = refine_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b"],
            embed,
            window_seconds=30.0,
            min_segment_seconds=5.0,
            context_seconds=10.0,
        )
        diagnosi_prima = refine_diagnostics()
        self._ss.reset_embed_cache()  # nuova run nello stesso processo, disco pieno
        secondo = refine_segment_boundaries(
            segments,
            words,
            ["tema_a", "tema_b"],
            embed,
            window_seconds=30.0,
            min_segment_seconds=5.0,
            context_seconds=10.0,
        )
        diagnosi_dopo = refine_diagnostics()
        self.assertEqual(primo, secondo)
        self.assertEqual(diagnosi_prima, diagnosi_dopo)
        self.assertEqual(diagnosi_dopo[0]["decision"], "spostato")
        # Il primo giro fa tre chiamate (slide, finestre dei candidati, prova di
        # stabilità); il secondo ne fa due in meno perché gli embedding delle
        # SLIDE arrivano dalla cache (content-addressed). Le finestre di parlato
        # si ricalcolano: sono il lavoro per-confine, non una cache. Il test
        # tiene ferme entrambe le cose, così un "salto" introdotto per sbaglio
        # nella cache calda si vede subito.
        self.assertEqual(len(embed.calls), 5)


class TestConfiniStimatiNonSostenuti(unittest.TestCase):
    """Il riepilogo dichiara i confini stimati che il contenuto non sostiene.

    Caso reale del 14/10: la slide 10 era l'unico confine stimato e il video la
    mostrava 40s dopo l'annuncio. Il riepilogo la elencava come tutte le altre,
    quindi una stima non sostenuta era indistinguibile da una misurata.
    """

    def test_durata_molto_sotto_la_mediana_su_confine_stimato(self):
        from main import _estimated_boundary_facts

        # Durate reali della run del 14/10 (slide 10 = 63.1s su mediana 110.6s).
        durations = [135.0, 121.25, 108.4, 112.72, 110.39, 101.22, 110.84, 100.42, 157.94, 63.07]
        slide_ids = list(range(1, 11))
        fatti = _estimated_boundary_facts(durations, slide_ids, {10}, [])
        self.assertEqual([f["slide"] for f in fatti], [10])
        self.assertEqual(fatti[0]["duration"], 63.1)
        self.assertAlmostEqual(float(fatti[0]["median"]), 110.62, places=1)
        self.assertLess(float(fatti[0]["ratio"]), 0.6)

    def test_confine_ancorato_non_entra(self):
        from main import _estimated_boundary_facts

        durations = [100.0, 60.0, 100.0]
        # La slide 2 è corta ma il suo inizio è un annuncio pronunciato: non è
        # una stima da dubitare, è un fatto.
        self.assertEqual(_estimated_boundary_facts(durations, [1, 2, 3], set(), []), [])

    def test_confine_sostenuto_e_durata_normale_non_entrano(self):
        from main import _estimated_boundary_facts

        durations = [100.0, 100.0, 100.0]
        refine = [
            {"slide": 2, "margin": 0.4, "scatter": 0.013, "sigma": 30.0, "decision": "spostato"}
        ]
        self.assertEqual(_estimated_boundary_facts(durations, [1, 2, 3], {2}, refine), [])

    def test_confine_non_sostenuto_entra_anche_se_la_durata_e_normale(self):
        from main import _estimated_boundary_facts

        durations = [100.0, 100.0, 100.0]
        refine = [
            {
                "slide": 2,
                "margin": 0.007,
                "scatter": 0.013,
                "sigma": 0.5,
                "decision": "tenuto: margine dentro il rumore",
            }
        ]
        fatti = _estimated_boundary_facts(durations, [1, 2, 3], {2}, refine)
        self.assertEqual([f["slide"] for f in fatti], [2])
        self.assertEqual(fatti[0]["sigma"], 0.5)

    def test_confine_non_misurabile_entra_col_motivo(self):
        from main import _estimated_boundary_facts, _weak_estimate_text

        durations = [100.0, 100.0]
        refine = [
            {"slide": 2, "decision": "non valutabile: finestre di parlato vuote (silenzi)"}
        ]
        fatti = _estimated_boundary_facts(durations, [1, 2], {2}, refine)
        self.assertEqual([f["slide"] for f in fatti], [2])
        testo = _weak_estimate_text(fatti[0])
        self.assertIn("non si è potuto misurare", testo)
        self.assertIn("finestre di parlato vuote", testo)
        self.assertIn("non è un annuncio 'slide N'", testo)

    def test_durata_garantita_dal_pavimento_esclusa(self):
        from main import _estimated_boundary_facts

        # La durata della slide 2 è una scelta di leggibilità (il pavimento
        # anti-flicker l'ha allungata): non è una misura da confrontare con la
        # mediana, e viene già dichiarata altrove.
        durations = [100.0, 8.0, 100.0]
        floor_report = {
            "min_seconds": 8.0,
            "guaranteed": [{"slide": 2, "before": 2.0, "duration": 8.0}],
            "shortened": [],
            "unguaranteed": [],
        }
        self.assertEqual(
            _estimated_boundary_facts(durations, [1, 2, 3], {2}, [], floor_report), []
        )

    def test_il_riepilogo_stampa_il_confine_stimato_non_sostenuto(self):
        import main

        refine = [
            {
                "slide": 3,
                "margin": 0.008,
                "scatter": 0.013,
                "sigma": 0.6,
                "decision": "tenuto: margine dentro il rumore",
            }
        ]
        anchors = {"unanchored_slides": [3], "anchored": 1, "transitions": 2}
        with self.assertLogs("slide2video", level="INFO") as logs:
            main._log_plain_summary(
                [100.0, 100.0, 60.0],
                [1, 2, 3],
                260.0,
                anchors=anchors,
                refine=refine,
            )
        out = "\n".join(logs.output)
        self.assertIn("la slide 3", out)
        self.assertIn("0.6 volte il rumore locale", out)
        self.assertIn("contro una mediana di", out)

    def test_senza_diagnostica_il_riepilogo_non_inventa_dubbi(self):
        import main

        anchors = {"unanchored_slides": [3], "anchored": 1, "transitions": 2}
        with self.assertLogs("slide2video", level="INFO") as logs:
            main._log_plain_summary([100.0, 100.0, 100.0], [1, 2, 3], 300.0, anchors=anchors)
        out = "\n".join(logs.output)
        self.assertNotIn("volte il rumore locale", out)
        self.assertIn("nessuno, la sincronizzazione è risultata solida", out)
