#!/usr/bin/env python3
"""
Test unitari per la pipeline di sincronizzazione.
Esegui con: python -m unittest test_sync -v
Coprono la logica di "precisione assoluta": niente distribuzioni uniformi,
interruzione con avviso se la sincronizzazione è impossibile.
"""

import unittest
from itertools import pairwise

import numpy as np

from semantic_sync import (
    SemanticOptions,
    make_anchor_remap_filter,
    merge_short_segments,
    refine_llm_segment_boundaries,
    refine_ordered_llm_timeline,
    semantic_timeline_from_texts,
    verify_anchor_mapping_embedding,
)
from timeline import (
    detect_flow_from_words,
    extract_timeline_from_transcript,
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


class TestSemanticSync(unittest.TestCase):
    """Sincronizzazione semantica (embeddings): DP monotona senza LLM."""

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

    def test_quality_guard_rejects_noise(self):
        themes = ["alfa", "beta", "gamma", "delta"]
        # Blocchi di rumore: nessuna parola tema -> similarità zero
        blocks = [{"time": i * 5.0, "text": "zappa qwerty nullo"} for i in range(8)]
        tl = semantic_timeline_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=40.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0, min_slide_duration=2.0),
        )
        self.assertIsNone(tl)

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


class TestAnomalousDurations(unittest.TestCase):
    """Guard-rail durate anomale del riepilogo finale (main._find_anomalous_durations)."""

    @staticmethod
    def _find(durations, slide_ids):
        from main import _find_anomalous_durations

        return _find_anomalous_durations(durations, slide_ids)

    def test_long_slide_flagged(self):
        self.assertEqual(self._find([100.0, 100.0, 100.0, 400.0], [1, 2, 3, 4]), [(4, 400.0)])

    def test_short_slide_flagged(self):
        self.assertEqual(self._find([100.0, 100.0, 100.0, 20.0], [1, 2, 3, 4]), [(4, 20.0)])

    def test_balanced_no_warning(self):
        self.assertEqual(self._find([120.0, 130.0, 110.0, 140.0], [1, 2, 3, 4]), [])

    def test_too_few_slides_ignored(self):
        self.assertEqual(self._find([100.0, 400.0], [1, 2]), [])



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

    def test_quality_guard_rejects_noise(self):
        # Blocchi di rumore (nessun tema): similarità zero -> None
        from semantic_sync import free_order_segments_from_texts

        themes = ["alfa", "beta", "gamma", "delta"]
        blocks = [{"time": i * 5.0, "first_time": i * 5.0 + 0.5, "text": "zappa qwerty nullo"} for i in range(10)]
        segs = free_order_segments_from_texts(
            [f"{t} slide" for t in themes],
            blocks,
            total_slides=4,
            total_duration=50.0,
            embed_fn=self._fake_embed(themes),
            options=SemanticOptions(window_seconds=5.0),
        )
        self.assertIsNone(segs)


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

        def side_effect(model_name, cache_dir):
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

        def side_effect(model_name, cache_dir):
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

        def side_effect(model_name, cache_dir):
            called.append(model_name)
            return FakeModel()

        with self._patch_text_embedding(side_effect):
            _load_embed_model("only-model", ".cache", alternate_name="only-model")
        self.assertEqual(called, ["only-model"])

    def test_both_fail_returns_none(self):
        def side_effect(model_name, cache_dir):
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
