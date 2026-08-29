#!/usr/bin/env python3
"""
Test unitari per la selezione slide via LLM (llm_sync.py).
Esegui con: python -m unittest test_llm_sync -v

Verificano parsing JSON, costruzione dei chunk e la cascata di fallback
fra i modelli di 9Router (unico provider online) -> None, con endpoint
mockati (nessuna rete).
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from llm_sync import (
    _build_ordered_timeline,
    _chunk_slides_from_segments,
    _conflicts_with_anchors,
    build_anchor_verify_prompt,
    build_llm_chunks,
    build_ordered_prompt,
    build_prompt,
    build_review_prompt,
    clean_slide_text_for_llm,
    llm_ordered_timeline,
    llm_timeline_segments,
    llm_verify_anchor_mapping,
    parse_anchor_verification,
    parse_llm_response,
    review_llm_timeline,
    router_alive,
    wait_for_router,
)


class TestBuildChunks(unittest.TestCase):
    """Costruzione chunk temporali dalla trascrizione."""

    def _words(self):
        # parole ogni 5s per 100s
        return [{"word": f"parola{i}", "start": i * 5.0} for i in range(20)]

    def test_chunks_cover_duration(self):
        chunks = build_llm_chunks(self._words(), total_duration=100.0, chunk_seconds=30.0)
        self.assertEqual(len(chunks), 4)  # 0-30, 30-60, 60-90, 90-100
        self.assertEqual(chunks[0]["num"], 1)
        self.assertAlmostEqual(chunks[0]["start"], 0.0)
        self.assertAlmostEqual(chunks[-1]["end"], 100.0)
        # primo timestamp reale di parola conservato
        self.assertAlmostEqual(chunks[0]["first_time"], 0.0)

    def test_empty_words(self):
        self.assertEqual(build_llm_chunks([], 100.0), [])

    def test_chunk_has_text(self):
        chunks = build_llm_chunks(self._words(), total_duration=100.0, chunk_seconds=30.0)
        self.assertIn("parola0", chunks[0]["text"])
        self.assertIn("parola2", chunks[0]["text"])


class TestPrompt(unittest.TestCase):
    """Costruzione del prompt."""

    def test_prompt_contains_slides_and_chunks(self):
        slides = ["Slide uno", "Slide due"]
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 2.0}],
            total_duration=30.0,
            chunk_seconds=30.0,
        )
        system, user = build_prompt(slides, chunks)
        self.assertIn("1. Slide uno", user)
        self.assertIn("2. Slide due", user)
        self.assertIn("chunk 1", user)
        self.assertIn("JSON", system)

    def test_prompt_has_anti_summary_and_transition_rules(self):
        # Le regole anti-errore devono comparire nel prompt di sistema
        system, _ = build_prompt(
            ["Slide a", "Slide b"],
            build_llm_chunks([{"word": "ciao", "start": 1.0}], total_duration=30.0, chunk_seconds=30.0),
        )
        self.assertIn("RIASSUNTO", system)
        self.assertIn("transizione", system.lower())
        self.assertIn("RIPETI", system)

    def test_prompt_has_conclusion_and_dominant_topic_rules(self):
        # Le nuove regole anti-errore (niente slide di sintesi anticipata sul
        # solo tema guida, chunk con doppio argomento assegnato al dominante)
        # devono comparire nel prompt di sistema.
        system, _ = build_prompt(
            ["Slide a", "Slide b"],
            build_llm_chunks([{"word": "ciao", "start": 1.0}], total_duration=30.0, chunk_seconds=30.0),
        )
        self.assertIn("tema guida", system)
        self.assertIn("cassetta degli attrezzi", system)
        self.assertIn("MAGGIORE parte", system)

    def test_prompt_cleans_slide_watermark(self):
        # Il rumore OCR (watermark NotebookLM) non deve arrivare all'LLM
        slides = ["Contenuto utile fù NotebookLM"]
        chunks = build_llm_chunks([{"word": "ciao", "start": 1.0}], total_duration=30.0, chunk_seconds=30.0)
        _, user = build_prompt(slides, chunks)
        self.assertIn("Contenuto utile", user)
        self.assertNotIn("NotebookLM", user)


class TestCleanSlideText(unittest.TestCase):
    """Pulizia del testo OCR prima dell'invio all'LLM."""

    def test_removes_watermark(self):
        self.assertEqual(
            clean_slide_text_for_llm("Il tema fù NotebookLM"),
            "Il tema",
        )

    def test_collapses_whitespace(self):
        self.assertEqual(
            clean_slide_text_for_llm("  parola   uno\n   parola due "),
            "parola uno parola due",
        )

    def test_drops_garbage_lines(self):
        # Righe di rumore OCR (diagrammi/icone) con bassa densità alfanumerica
        t = "Testo valido\n||| --)) PLE )| (( *\ntesto due"
        out = clean_slide_text_for_llm(t)
        self.assertIn("Testo valido", out)
        self.assertIn("testo due", out)
        self.assertNotIn("PLE", out)

    def test_truncates_to_max_chars(self):
        out = clean_slide_text_for_llm("x" * 2000, max_chars=50)
        self.assertEqual(len(out), 50)


class TestReviewPrompt(unittest.TestCase):
    """Prompt del secondo passaggio di revisione."""

    def test_contains_mapping_and_format(self):
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 31.0}],
            total_duration=60.0,
            chunk_seconds=30.0,
        )
        system, user = build_review_prompt(
            ["Slide a", "Slide b"],
            chunks,
            [1, 2],
        )
        self.assertIn("chunk 1: slide 1", user)
        self.assertIn("chunk 2: slide 2", user)
        self.assertIn("Proposta attuale", user)
        self.assertIn("JSON", system)


class TestChunkSlidesFromSegments(unittest.TestCase):
    """Ricostruzione mappa chunk->slide dai segmenti (diff del review)."""

    def test_reconstructs_assignment(self):
        chunks = build_llm_chunks(
            [{"word": "w", "start": 5.0}, {"word": "x", "start": 35.0}],
            total_duration=60.0,
            chunk_seconds=30.0,
        )
        segments = [
            {"slide": 1, "start": 0.0, "end": 30.0},
            {"slide": 4, "start": 30.0, "end": 60.0},
        ]
        self.assertEqual(
            _chunk_slides_from_segments(chunks, segments),
            [1, 4],
        )

    def test_uses_first_time_not_window_start(self):
        # Regressione: i segmenti sono costruiti sul confine "first_time"
        # (primo parlato del chunk), non su "start" (inizio finestra). Un
        # chunk con il primo parlato tardo nella finestra non deve finire
        # attribuito al segmento precedente.
        chunks = [
            {"num": 1, "start": 0.0, "end": 30.0, "first_time": 5.0, "text": "a"},
            {"num": 2, "start": 30.0, "end": 60.0, "first_time": 55.0, "text": "b"},
            {"num": 3, "start": 60.0, "end": 90.0, "first_time": 65.0, "text": "c"},
        ]
        segments = [
            {"slide": 1, "start": 0.0, "end": 55.0},
            {"slide": 2, "start": 55.0, "end": 65.0},
            {"slide": 3, "start": 65.0, "end": 90.0},
        ]
        # start=30.0 cadrebbe nel segmento 1 (sbagliato); first_time=55.0
        # cade correttamente nel segmento 2.
        self.assertEqual(
            _chunk_slides_from_segments(chunks, segments),
            [1, 2, 3],
        )


class TestReview(unittest.TestCase):
    """Secondo passaggio: ri-verifica della mappa (endpoint mockati)."""

    @staticmethod
    def _ep(name, url):
        return {"name": name, "url": url, "model": f"model-{name}", "api_key": "", "timeout": 5}

    def _no_cache(self):
        return (
            patch("llm_sync._load_llm_cache", return_value=None),
            patch("llm_sync._save_llm_cache"),
            patch("llm_sync.router_alive", return_value=True),
        )

    def test_returns_diffs_when_disagree(self):
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 31.0}],
            total_duration=60.0,
            chunk_seconds=30.0,
        )
        # Proposta: chunk 1->1, chunk 2->2 ; il reviewer corregge il chunk 2 a 3
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"chunk": 1, "slide": 1}, {"chunk": 2, "slide": 3}]'),
        ):
            diffs = review_llm_timeline(
                ["slide a", "slide b", "slide c"],
                chunks,
                [1, 2],
                total_slides=3,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNotNone(diffs)
        self.assertEqual(diffs, [{"chunk": 2, "slide": 3}])

    def test_empty_diffs_when_agree(self):
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 31.0}],
            total_duration=60.0,
            chunk_seconds=30.0,
        )
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"chunk": 1, "slide": 1}, {"chunk": 2, "slide": 2}]'),
        ):
            diffs = review_llm_timeline(
                ["slide a", "slide b"],
                chunks,
                [1, 2],
                total_slides=2,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertEqual(diffs, [])

    def test_no_endpoints_returns_none(self):
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}],
            total_duration=30.0,
            chunk_seconds=30.0,
        )
        diffs = review_llm_timeline(
            ["slide a"],
            chunks,
            [1],
            total_slides=1,
            endpoints=[],
        )
        self.assertIsNone(diffs)

    def test_unreachable_returns_none(self):
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}],
            total_duration=30.0,
            chunk_seconds=30.0,
        )
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value=None),
        ):
            diffs = review_llm_timeline(
                ["slide a"],
                chunks,
                [1],
                total_slides=1,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNone(diffs)

    def test_review_invoked_by_timeline_when_flag_set(self):
        # Con review=True la timeline deve chiamare il secondo passaggio.
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"chunk": 1, "slide": 1}]'),
            patch("llm_sync.review_llm_timeline", return_value=[]) as mock_review,
        ):
            llm_timeline_segments(
                ["slide a", "slide b"],
                [{"word": "ciao", "start": 0.5}],
                total_slides=2,
                total_duration=30.0,
                endpoints=[self._ep("9router", "http://x")],
                review=True,
            )
        mock_review.assert_called_once()

    def test_fresh_path_passes_reconstructed_slides_to_review(self):
        # Regressione cache: nel ramo fresh il review deve ricevere la mappa
        # chunk->slide RICOSTRUITA dai segmenti merged (come nel ramo cache-hit),
        # non le slide grezze del modello. Un chunk "null" lascia un vuoto nei
        # segmenti e deve ricostruirsi come None in entrambi i rami: così la
        # chiave cache della revisione coincide tra primo e secondo run.
        words = [
            {"word": "a", "start": 1.0},
            {"word": "b", "start": 31.0},
            {"word": "c", "start": 61.0},
        ]
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch(
                "llm_sync._call_endpoint",
                return_value=('[{"chunk": 1, "slide": 1}, {"chunk": 2, "slide": null}, {"chunk": 3, "slide": 3}]'),
            ),
            patch("llm_sync.review_llm_timeline", return_value=[]) as mock_review,
        ):
            llm_timeline_segments(
                ["slide a", "slide b", "slide c"],
                words,
                total_slides=3,
                total_duration=90.0,
                chunk_seconds=30.0,
                endpoints=[self._ep("9router", "http://x")],
                review=True,
            )
        mock_review.assert_called_once()
        slides_arg = mock_review.call_args.args[2]
        self.assertEqual(slides_arg, [1, None, 3])

    def test_cache_hit_path_passes_reconstructed_slides_to_review(self):
        # Regressione cache: nel ramo cache-hit il review riceve la mappa
        # RICOSTRUITA dai segmenti cachati (stessa logica del ramo fresh), così
        # la chiave cache della revisione coincide tra primo e secondo run.
        words = [
            {"word": "a", "start": 1.0},
            {"word": "b", "start": 31.0},
            {"word": "c", "start": 61.0},
        ]
        cached = [
            {"slide": 1, "start": 0.0, "end": 31.0},
            {"slide": 3, "start": 61.0, "end": 90.0},
        ]
        with (
            patch("llm_sync._load_llm_cache", return_value=cached),
            patch("llm_sync._save_llm_cache"),
            patch("llm_sync.review_llm_timeline", return_value=[]) as mock_review,
        ):
            llm_timeline_segments(
                ["slide a", "slide b", "slide c"],
                words,
                total_slides=3,
                total_duration=90.0,
                chunk_seconds=30.0,
                endpoints=[self._ep("9router", "http://x")],
                review=True,
            )
        mock_review.assert_called_once()
        slides_arg = mock_review.call_args.args[2]
        self.assertEqual(slides_arg, [1, None, 3])


class TestParseResponse(unittest.TestCase):
    """Parsing tollerante della risposta LLM."""

    def test_plain_array(self):
        slides = parse_llm_response(
            '[{"chunk": 1, "slide": 3}, {"chunk": 2, "slide": null}]',
            2,
        )
        self.assertEqual(slides, [3, None])

    def test_fenced_code_block(self):
        slides = parse_llm_response(
            '```json\n[{"chunk": 1, "slide": 4}]\n```',
            1,
        )
        self.assertEqual(slides, [4])

    def test_extra_text_around(self):
        slides = parse_llm_response(
            'Ecco la risposta:\n[{"chunk": 1, "slide": 2}]\nSpero sia utile.',
            1,
        )
        self.assertEqual(slides, [2])

    def test_single_quotes_repair(self):
        slides = parse_llm_response(
            "[{'chunk': 1, 'slide': 5}]",
            1,
        )
        self.assertEqual(slides, [5])

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_llm_response("non è JSON", 2))
        self.assertIsNone(parse_llm_response("", 2))

    def test_out_of_range_slide_null(self):
        # slide fuori intervallo -> None
        slides = parse_llm_response('[{"chunk": 1, "slide": 99}]', 1, total_slides=4)
        self.assertEqual(slides, [None])

    def test_string_and_float_slide(self):
        # I modelli locali a volte emettono stringhe o float: tollerato
        slides = parse_llm_response(
            '[{"chunk": 1, "slide": "4"}, {"chunk": 2, "slide": 6.0}]',
            2,
        )
        self.assertEqual(slides, [4, 6])


class TestEndpointConfig(unittest.TestCase):
    """Configurazione endpoint: SOLO 9Router online (niente LM Studio/OpenAI)."""

    def _patch_endpoints(self, side_effect):
        return patch("llm_sync._endpoints", side_effect=side_effect)

    def test_auto_and_9router_use_only_9router(self):
        # Provider 'auto' e '9router' devono restituire SOLO endpoint 9router
        fake = [
            {"name": "9router", "url": "http://x/v1/chat/completions", "model": "m1"},
            {"name": "9router", "url": "http://x/v1/chat/completions", "model": "m2"},
        ]
        with self._patch_endpoints(lambda: fake):
            from llm_sync import endpoints_for

            for provider in ("auto", "9router"):
                eps = endpoints_for(provider)
                self.assertEqual(len(eps), 2)
                self.assertTrue(all(e["name"] == "9router" for e in eps))

    def test_no_lmstudio_endpoint_in_default_config(self):
        # La configurazione di default non deve contenere LM Studio né OpenAI.
        # I test devono essere ermetici: azzera le variabili d'ambiente LLM.
        import os

        env_backup = {}
        for key in (
            "LLM_9ROUTER_URL",
            "LLM_9ROUTER_MODEL",
            "LLM_9ROUTER_BACKUP_MODEL",
            "LLM_9ROUTER_BACKUP_MODEL_2",
            "OPENAI_API_KEY",
            "LLM_LMSTUDIO_URL",
        ):
            env_backup[key] = os.environ.get(key)
            os.environ.pop(key, None)
        try:
            from llm_sync import _endpoints

            eps = _endpoints()
            names = [e["name"] for e in eps]
            self.assertTrue(names, "devono esserci endpoint configurati")
            self.assertNotIn("lmstudio", names)
            self.assertNotIn("openai", names)
            self.assertTrue(all(n == "9router" for n in names))
            # Cascata interna: almeno 2 modelli (principale + backup)
            self.assertGreaterEqual(len(eps), 2)
        finally:
            for key, value in env_backup.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class TestRouterHealthCheck(unittest.TestCase):
    """Health-check 9Router + pausa/ripresa quando serve l'LLM."""

    @staticmethod
    def _ep(name, url):
        return {"name": name, "url": url, "model": f"model-{name}", "api_key": "", "timeout": 5}

    def test_alive_when_models_respond_ok(self):
        class FakeResp:
            status_code = 200

        with patch("llm_sync.requests.get", return_value=FakeResp()):
            self.assertTrue(router_alive([self._ep("9router", "http://x/v1/chat/completions")]))

    def test_alive_when_models_respond_4xx(self):
        # 4xx (es. 401) = il router è SU (risponde), manca solo l'auth.
        class FakeResp:
            status_code = 401

        with patch("llm_sync.requests.get", return_value=FakeResp()):
            self.assertTrue(router_alive([self._ep("9router", "http://x/v1/chat/completions")]))

    def test_down_when_request_fails(self):
        with patch("llm_sync.requests.get", side_effect=OSError("connessione rifiutata")):
            self.assertFalse(router_alive([self._ep("9router", "http://x/v1/chat/completions")]))

    def test_down_without_requests_module(self):
        with patch("llm_sync._HAS_REQUESTS", False):
            self.assertFalse(router_alive([self._ep("9router", "http://x/v1/chat/completions")]))

    def test_down_without_endpoints(self):
        self.assertFalse(router_alive([]))

    def test_wait_returns_immediately_when_alive(self):
        with (
            patch("llm_sync.router_alive", return_value=True),
            patch("llm_sync.is_interactive", return_value=True) as mock_it,
        ):
            self.assertTrue(wait_for_router([self._ep("9router", "http://x/v1/chat/completions")]))
            mock_it.assert_not_called()  # nessuna attesa: router già su

    def test_noninteractive_falls_back_immediately(self):
        # stdin non è un terminale: senza 9Router si ripiega SUBITO (niente pausa)
        with (
            patch("llm_sync.router_alive", return_value=False),
            patch("llm_sync._launch_9router", return_value=False),
            patch("llm_sync.is_interactive", return_value=False),
        ):
            self.assertFalse(wait_for_router([self._ep("9router", "http://x/v1/chat/completions")]))

    def test_noninteractive_strict_raises(self):
        # Flusso libero + senza terminale: NIENTE fallback silenzioso, errore chiaro.
        with (
            patch("llm_sync.router_alive", return_value=False),
            patch("llm_sync._launch_9router", return_value=False),
            patch("llm_sync.is_interactive", return_value=False),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                wait_for_router(
                    [self._ep("9router", "http://x/v1/chat/completions")],
                    strict=True,
                )
            self.assertIn("9Router", str(ctx.exception))

    def test_auto_launch_brings_router_online(self):
        # 9Router spento: avvio automatico + polling -> riprende con l'LLM.
        calls = {"n": 0}

        def alive(*a, **k):
            calls["n"] += 1
            return calls["n"] >= 2

        with (
            patch("llm_sync.router_alive", side_effect=alive),
            patch("llm_sync._launch_9router", return_value=True),
            patch("llm_sync.is_interactive", return_value=False),
            patch("llm_sync.time.sleep"),
        ):
            self.assertTrue(
                wait_for_router(
                    [self._ep("9router", "http://x/v1/chat/completions")],
                    strict=True,
                )
            )

    def test_noninteractive_strict_raises_after_failed_auto_launch(self):
        # L'avvio automatico parte ma 9Router non arriva online entro la
        # finestra: NIENTE fallback silenzioso, il processo si ARRESTA.
        def fake_time():
            fake_time.n += 1
            return fake_time.n * 10  # +10s a iterazione: scatta dopo 90s

        fake_time.n = 0
        with (
            patch("llm_sync.router_alive", return_value=False),
            patch("llm_sync._launch_9router", return_value=True),
            patch("llm_sync.is_interactive", return_value=False),
            patch("llm_sync._skip_key_pressed", return_value=False),
            patch("llm_sync.time.sleep"),
            patch("llm_sync.time.time", side_effect=fake_time),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                wait_for_router(
                    [self._ep("9router", "http://x/v1/chat/completions")],
                    strict=True,
                )
            self.assertIn("9Router", str(ctx.exception))

    def test_interactive_strict_pauses_but_can_skip(self):
        # Con terminale, anche in strict l'utente può premere 'S' e procedere.
        with (
            patch("llm_sync.router_alive", return_value=False),
            patch("llm_sync._launch_9router", return_value=False),
            patch("llm_sync.is_interactive", return_value=True),
            patch("llm_sync._skip_key_pressed", return_value=True),
            patch("llm_sync.time.sleep"),
        ):
            self.assertFalse(
                wait_for_router(
                    [self._ep("9router", "http://x/v1/chat/completions")],
                    strict=True,
                )
            )

    def test_strict_with_alive_router_ok(self):
        with patch("llm_sync.router_alive", return_value=True):
            self.assertTrue(
                wait_for_router(
                    [self._ep("9router", "http://x/v1/chat/completions")],
                    strict=True,
                )
            )

    def test_interactive_resumes_when_router_comes_up(self):
        # Il router scende (pausa) poi torna su: il polling riprende da solo.
        calls = {"n": 0}

        def alive(*a, **k):
            calls["n"] += 1
            return calls["n"] >= 3

        with (
            patch("llm_sync.router_alive", side_effect=alive),
            patch("llm_sync._launch_9router", return_value=False),
            patch("llm_sync.is_interactive", return_value=True),
            patch("llm_sync.time.sleep"),
        ):
            self.assertTrue(wait_for_router([self._ep("9router", "http://x/v1/chat/completions")]))

    def test_interactive_skip_with_key(self):
        # Tasto 'S' durante la pausa: salta l'LLM e usa il MiniLM.
        with (
            patch("llm_sync.router_alive", return_value=False),
            patch("llm_sync._launch_9router", return_value=False),
            patch("llm_sync.is_interactive", return_value=True),
            patch("llm_sync._skip_key_pressed", return_value=True),
            patch("llm_sync.time.sleep"),
        ):
            self.assertFalse(wait_for_router([self._ep("9router", "http://x/v1/chat/completions")]))

    def test_interactive_timeout_falls_back(self):
        # Scadenza di --llm-wait-timeout: fallback MiniLM automatico.
        def fake_time():
            fake_time.n += 1
            return fake_time.n  # 1, 2, ... : dopo il 60° secondo scatta il timeout

        fake_time.n = 0
        with (
            patch("llm_sync.router_alive", return_value=False),
            patch("llm_sync._launch_9router", return_value=False),
            patch("llm_sync.is_interactive", return_value=True),
            patch("llm_sync._skip_key_pressed", return_value=False),
            patch("llm_sync.time.sleep"),
            patch("llm_sync.time.time", side_effect=fake_time),
        ):
            self.assertFalse(
                wait_for_router(
                    [self._ep("9router", "http://x/v1/chat/completions")],
                    wait_timeout=60.0,
                )
            )


class TestCascadeFallback(unittest.TestCase):
    """Cascata di fallback tra endpoint (mock, nessuna rete)."""

    @staticmethod
    def _ep(name, url):
        return {"name": name, "url": url, "model": f"model-{name}", "api_key": "", "timeout": 5}

    # Disabilita la cache nei test di cascata: ogni test deve chiamare davvero
    # gli endpoint mockati, non riusare i risultati del test precedente.
    # router_alive=True: la health-check 9Router è finta nei test.
    def _no_cache(self):
        return (
            patch("llm_sync._load_llm_cache", return_value=None),
            patch("llm_sync._save_llm_cache"),
            patch("llm_sync.router_alive", return_value=True),
        )

    def test_first_endpoint_success(self):
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"chunk": 1, "slide": 1}]'),
        ):
            segs = llm_timeline_segments(
                ["slide a", "slide b"],
                [{"word": "ciao", "start": 0.5}],
                total_slides=2,
                total_duration=30.0,
                endpoints=[self._ep("9router", "http://x"), self._ep("9router", "http://z")],
            )
        self.assertIsNotNone(segs)
        self.assertEqual(segs[0]["slide"], 1)

    def test_second_endpoint_fallback(self):
        # primo modello fallisce (None), il backup risponde
        def fake(endpoint, messages, temperature=0.0):
            if endpoint["url"] == "http://x":
                return None
            return '[{"chunk": 1, "slide": 2}]'

        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", side_effect=fake),
        ):
            segs = llm_timeline_segments(
                ["slide a", "slide b"],
                [{"word": "ciao", "start": 0.5}],
                total_slides=2,
                total_duration=30.0,
                endpoints=[
                    self._ep("9router", "http://x"),
                    self._ep("9router", "http://z"),
                ],
            )
        self.assertIsNotNone(segs)
        self.assertEqual(segs[0]["slide"], 2)

    def test_all_fail_returns_none(self):
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value=None),
        ):
            segs = llm_timeline_segments(
                ["slide a", "slide b"],
                [{"word": "ciao", "start": 0.5}],
                total_slides=2,
                total_duration=30.0,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNone(segs)

    def test_retry_on_429_then_success(self):
        # Rate-limit (429) sul primo tentativo: con i retry il secondo risponde.
        # Verifica che la risposta venga restituita senza fallire.
        good_body = '{"choices":[{"message":{"role":"assistant","content":"[{\\"chunk\\": 1, \\"slide\\": 3}]"}]}}'
        responses = [
            type(
                "FakeResp",
                (),
                {"status_code": 429, "text": "rate limited", "json": lambda self: (_ for _ in ()).throw(ValueError())},
            )(),
            type(
                "FakeResp",
                (),
                {"status_code": 200, "text": good_body, "json": lambda self: (_ for _ in ()).throw(ValueError())},
            )(),
        ]
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync.requests.post", side_effect=responses),
            patch("llm_sync.time.sleep"),
        ):  # niente attese reali nei test
            segs = llm_timeline_segments(
                ["slide a", "slide b", "slide c"],
                [{"word": "ciao", "start": 0.5}],
                total_slides=3,
                total_duration=30.0,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNotNone(segs)
        self.assertEqual(segs[0]["slide"], 3)

    def test_non_json_response_content_extracted(self):
        # Risposta del gateway con testo extra dopo il JSON (caso reale 9Router)
        raw_body = (
            '{"choices":[{"message":{"role":"assistant",'
            '"content":"[{\\"chunk\\": 1, \\"slide\\": 2}]"}]}} '
            "extra data qui"
        )
        fake_resp = type(
            "FakeResp",
            (),
            {"status_code": 200, "text": raw_body, "json": lambda self: (_ for _ in ()).throw(ValueError())},
        )()
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync.requests.post", return_value=fake_resp),
        ):
            segs = llm_timeline_segments(
                ["slide a", "slide b"],
                [{"word": "ciao", "start": 0.5}],
                total_slides=2,
                total_duration=30.0,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNotNone(segs)
        self.assertEqual(segs[0]["slide"], 2)

    def test_adjacent_same_slide_merged(self):
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"chunk": 1, "slide": 1}, {"chunk": 2, "slide": 1}]'),
        ):
            segs = llm_timeline_segments(
                ["slide a", "slide b"],
                [{"word": "w", "start": 1.0}, {"word": "x", "start": 40.0}],
                total_slides=2,
                total_duration=60.0,
                endpoints=[self._ep("9router", "http://y")],
            )
        self.assertIsNotNone(segs)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["slide"], 1)
        self.assertAlmostEqual(segs[0]["end"], 60.0)


class TestOrderedTimeline(unittest.TestCase):
    """Flusso ibrido ordinato: ancore deterministiche ESATTE + LLM per le
    slide senza ancora esplicita."""

    @staticmethod
    def _ep(name, url):
        return {"name": name, "url": url, "model": f"model-{name}", "api_key": "", "timeout": 5}

    def _no_cache(self):
        return (
            patch("llm_sync._load_llm_cache", return_value=None),
            patch("llm_sync._save_llm_cache"),
            patch("llm_sync.router_alive", return_value=True),
        )

    def test_prompt_mentions_anchors_and_order(self):
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 31.0}],
            total_duration=60.0,
            chunk_seconds=30.0,
        )
        system, user = build_ordered_prompt(
            ["Slide uno", "Slide due"],
            chunks,
            anchors={2: 25.0},
        )
        self.assertIn("ORDINE", system)
        self.assertIn("NON DECRESCENTE", system)
        self.assertIn("slide 2 a 25.0s", user)

    def test_anchors_preserved_exactly(self):
        # Le ancore esplicite NON devono essere spostate: slide 3 a 40.0s resta
        # tale anche se l'LLM la discuterebbe prima/dopo.
        words = [
            {"word": "a", "start": 5.0},
            {"word": "b", "start": 35.0},
            {"word": "c", "start": 65.0},
            {"word": "d", "start": 95.0},
        ]
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch(
                "llm_sync._call_endpoint",
                return_value=(
                    '[{"chunk": 1, "slide": 1}, '
                    '{"chunk": 2, "slide": 3}, '
                    '{"chunk": 3, "slide": 3}, '
                    '{"chunk": 4, "slide": 4}]'
                ),
            ),
        ):
            timeline = llm_ordered_timeline(
                ["s1", "s2", "s3", "s4"],
                words,
                total_slides=4,
                total_duration=120.0,
                anchors={3: 40.0},
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNotNone(timeline)
        self.assertAlmostEqual(timeline[3], 40.0)
        self.assertAlmostEqual(timeline[1], 0.0)

    def test_unanchored_slides_positioned_by_llm(self):
        # Slide senza ancora: posizionata al primo chunk dove l'LLM la discute.
        words = [
            {"word": "a", "start": 10.0},
            {"word": "b", "start": 40.0},
            {"word": "c", "start": 70.0},
            {"word": "d", "start": 100.0},
        ]
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch(
                "llm_sync._call_endpoint",
                return_value=(
                    '[{"chunk": 1, "slide": 1}, '
                    '{"chunk": 2, "slide": 3}, '
                    '{"chunk": 3, "slide": 3}, '
                    '{"chunk": 4, "slide": 4}]'
                ),
            ),
        ):
            timeline = llm_ordered_timeline(
                ["s1", "s2", "s3", "s4"],
                words,
                total_slides=4,
                total_duration=130.0,
                anchors={4: 100.0},
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNotNone(timeline)
        # slide 3 discussa dal chunk 2 (first_time ~40s)
        self.assertAlmostEqual(timeline[3], 40.0)
        self.assertAlmostEqual(timeline[4], 100.0)

    def test_unreachable_returns_none(self):
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value=None),
        ):
            timeline = llm_ordered_timeline(
                ["s1", "s2"],
                [{"word": "ciao", "start": 1.0}],
                total_slides=2,
                total_duration=30.0,
                anchors={2: 10.0},
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNone(timeline)

    def test_parse_garbage_returns_none(self):
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value="non è JSON"),
        ):
            timeline = llm_ordered_timeline(
                ["s1", "s2"],
                [{"word": "ciao", "start": 1.0}],
                total_slides=2,
                total_duration=30.0,
                anchors={2: 10.0},
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNone(timeline)

    def test_conflicts_with_anchors(self):
        # Slide senza ancora collocata in un punto che viola la monotonia
        # rispetto a un'ancora esatta -> conflitto.
        anchors = {6: 40.0}
        # slide 4 a 95s arriva DOPO l'ancora della slide 6 (40s): conflitto.
        self.assertTrue(_conflicts_with_anchors(4, 95.0, anchors))
        # slide 8 a 30s arriva PRIMA dell'ancora della slide 6: conflitto.
        self.assertTrue(_conflicts_with_anchors(8, 30.0, anchors))
        # slide 2 a 20s e prima della slide 6: nessun conflitto.
        self.assertFalse(_conflicts_with_anchors(2, 20.0, anchors))
        # slide 1 a 5s: nessun conflitto.
        self.assertFalse(_conflicts_with_anchors(1, 5.0, anchors))

    def test_build_ordered_timeline_forces_anchor_chunk(self):
        # Il chunk che contiene il timestamp dell'ancora deve essere FORZATO
        # alla slide dell'ancora (l'LLM può averlo assegnato ad altro), e le
        # posizioni delle slide senza ancora in conflitto vengono scartate.
        chunks = build_llm_chunks(
            [
                {"word": "a", "start": 5.0},
                {"word": "b", "start": 35.0},
                {"word": "c", "start": 65.0},
                {"word": "d", "start": 95.0},
            ],
            total_duration=130.0,
            chunk_seconds=30.0,
        )
        # chunk 2 (30-60s) contiene l'ancora slide 6 a 40s ma l'LLM lo assegna
        # alla slide 2; la slide 4 a 95s arriva dopo l'ancora della 6.
        slides = [1, 2, 2, 4, None]
        timeline = _build_ordered_timeline(
            slides,
            chunks,
            anchors={6: 40.0},
            total_slides=6,
            total_duration=130.0,
        )
        self.assertIsNotNone(timeline)
        # L'ancora resta esatta (slide 6 a 40s).
        self.assertAlmostEqual(timeline[6], 40.0)
        # La slide 2 non è stata accettata come prima discussione nel chunk 2
        # (forzato a 6): resta interpolata tra slide 1 e slide 6.
        self.assertGreater(timeline[2], timeline[1])
        self.assertLess(timeline[2], timeline[6])

    def test_force_anchors_prompt(self):
        chunks = build_llm_chunks(
            [{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 31.0}],
            total_duration=60.0,
            chunk_seconds=30.0,
        )
        _system, user = build_ordered_prompt(
            ["Slide uno", "Slide due"],
            chunks,
            anchors={2: 25.0},
            force_anchors=True,
        )
        # Il chunk 1 (0-30s) contiene l'ancora della slide 2 a 25s.
        self.assertIn("VINCOLI ASSOLUTI", user)
        self.assertIn("chunk 1 = slide 2", user)

    def test_ordered_retry_with_forced_anchors(self):
        # Primo tentativo: posizioni LLM in conflitto con le ancore -> retry
        # con le ancore forzate nei chunk. Il secondo tentativo produce una
        # timeline valida (verifico che venga usata).
        words = [
            {"word": "a", "start": 5.0},
            {"word": "b", "start": 35.0},
            {"word": "c", "start": 65.0},
            {"word": "d", "start": 95.0},
        ]
        resp1 = (
            '[{"chunk": 1, "slide": 1}, {"chunk": 2, "slide": 2}, '
            '{"chunk": 3, "slide": 2}, {"chunk": 4, "slide": 4}, '
            '{"chunk": 5, "slide": null}]'
        )
        resp2 = (
            '[{"chunk": 1, "slide": 1}, {"chunk": 2, "slide": 6}, '
            '{"chunk": 3, "slide": 6}, {"chunk": 4, "slide": 6}, '
            '{"chunk": 5, "slide": null}]'
        )
        # Timeline realistica da _build_ordered_timeline: l'ancora slide 6 a
        # 40s e' gia' esatta e le altre slide sono interpolate tra 0 e 40.
        final = {1: 0.0, 2: 8.0, 3: 16.0, 4: 24.0, 5: 32.0, 6: 40.0}
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_cascade", side_effect=[(resp1, "ep", "m"), (resp2, "ep", "m")]) as cascade,
            patch("llm_sync._build_ordered_timeline", side_effect=[None, final]),
        ):
            timeline = llm_ordered_timeline(
                ["s1", "s2", "s3", "s4", "s5", "s6"],
                words,
                total_slides=6,
                total_duration=130.0,
                anchors={6: 40.0},
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertEqual(cascade.call_count, 2)
        # Il secondo messaggio deve contenere i vincoli forzati.
        second_messages = cascade.call_args_list[1].args[1]
        self.assertIn("VINCOLI ASSOLUTI", " ".join(str(m) for m in second_messages))
        self.assertIsNotNone(timeline)
        # L'ancora slide 6 resta esatta anche dopo il retry.
        self.assertAlmostEqual(timeline[6], 40.0)


class TestAnchorVerification(unittest.TestCase):
    """Verifica mapping ancore: numero parlato -> slide reale del PDF.

    Copre il caso del podcast che NON segue le regole del prompt NotebookLM:
    la numerazione parlata può essere sfasata (es. lo speaker dice "quarta
    diapositiva" ma mostra la slide 5 del PDF). L'LLM corregge il numero in
    base al contenuto del parlato, mantenendo i tempi esatti.
    """

    @staticmethod
    def _ep(name, url):
        return {"name": name, "url": url, "model": f"model-{name}", "api_key": "", "timeout": 5}

    def _no_cache(self):
        return (
            patch("llm_sync._load_llm_cache", return_value=None),
            patch("llm_sync._save_llm_cache"),
            patch("llm_sync.router_alive", return_value=True),
        )

    def _words(self):
        # parlato con contenuti distinti a ogni intervallo
        return [
            {"word": "iceberg", "start": 30.0},
            {"word": "evoluzione", "start": 100.0},
            {"word": "attrezzi", "start": 180.0},
            {"word": "verita", "start": 260.0},
        ]

    def test_prompt_contains_anchors_and_content(self):
        system, user = build_anchor_verify_prompt(
            ["Slide a", "Slide b", "Slide c"],
            {2: 100.0},
            [{"word": "ciao", "start": 95.0}, {"word": "mondo", "start": 110.0}],
        )
        self.assertIn("PDF", system)
        self.assertIn("100.0s", user)
        self.assertIn("slide 2", user)
        self.assertIn("JSON", system)

    def test_parse_verification_response(self):
        mapping = parse_anchor_verification(
            '[{"timestamp": 100.0, "slide": 4}, {"timestamp": 180.0, "slide": 6}]',
        )
        self.assertEqual(mapping, {100.0: 4, 180.0: 6})

    def test_parse_verification_garbage(self):
        self.assertIsNone(parse_anchor_verification("non è JSON"))
        self.assertIsNone(parse_anchor_verification(""))
        self.assertIsNone(parse_anchor_verification("[not an array]"))

    def test_mapping_corrected_by_llm(self):
        # Lo speaker dice "slide 4" a 100s ma il contenuto è l'evoluzione
        # (slide 5 del PDF): l'LLM deve correggere il numero mantenendo il tempo.
        words = self._words()
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"timestamp": 100.0, "slide": 5}]'),
        ):
            verified = llm_verify_anchor_mapping(
                ["s1", "s2", "s3", "s4", "s5"],
                words,
                anchors={4: 100.0},
                total_slides=5,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNotNone(verified)
        self.assertEqual(verified, {5: 100.0})  # numero corretto, tempo identico

    def test_times_preserved_exactly(self):
        # I TEMPI delle ancore non devono mai cambiare, solo i numeri.
        words = self._words()
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch(
                "llm_sync._call_endpoint",
                return_value=(
                    '[{"timestamp": 100.0, "slide": 5}, '
                    '{"timestamp": 180.0, "slide": 6}, '
                    '{"timestamp": 260.0, "slide": 9}]'
                ),
            ),
        ):
            verified = llm_verify_anchor_mapping(
                [f"s{i}" for i in range(1, 10)],
                words,
                anchors={4: 100.0, 5: 180.0, 8: 260.0},
                total_slides=9,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNotNone(verified)
        self.assertAlmostEqual(verified[5], 100.0)
        self.assertAlmostEqual(verified[6], 180.0)
        self.assertAlmostEqual(verified[9], 260.0)

    def test_unreachable_returns_none(self):
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value=None),
        ):
            verified = llm_verify_anchor_mapping(
                ["s1", "s2"],
                self._words(),
                anchors={2: 100.0},
                total_slides=2,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNone(verified)

    def test_parse_garbage_returns_none(self):
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value="non è JSON"),
        ):
            verified = llm_verify_anchor_mapping(
                ["s1", "s2"],
                self._words(),
                anchors={2: 100.0},
                total_slides=2,
                endpoints=[self._ep("9router", "http://x")],
            )
        self.assertIsNone(verified)

    def test_no_anchors_returns_none(self):
        verified = llm_verify_anchor_mapping(
            ["s1", "s2"],
            self._words(),
            anchors={},
            total_slides=2,
        )
        self.assertIsNone(verified)

    def test_remap_rejected_by_filter_keeps_spoken_anchor(self):
        # Il filtro semantico non conferma il rimappo 4 -> 5: l'ancora resta
        # alla slide parlata 4, anche se l'LLM la propone.
        words = self._words()
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"timestamp": 100.0, "slide": 5}]'),
        ):
            verified = llm_verify_anchor_mapping(
                ["s1", "s2", "s3", "s4", "s5"],
                words,
                anchors={4: 100.0},
                total_slides=5,
                endpoints=[self._ep("9router", "http://x")],
                remap_filter=lambda _s, _t, _s2: False,
            )
        self.assertIsNone(verified)

    def test_remap_accepted_by_filter_when_content_supports(self):
        # Il filtro conferma il rimappo 4 -> 5: l'ancora corretta viene usata.
        words = self._words()
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"timestamp": 100.0, "slide": 5}]'),
        ):
            verified = llm_verify_anchor_mapping(
                ["s1", "s2", "s3", "s4", "s5"],
                words,
                anchors={4: 100.0},
                total_slides=5,
                endpoints=[self._ep("9router", "http://x")],
                remap_filter=lambda _s, _t, _s2: True,
            )
        self.assertIsNotNone(verified)
        self.assertEqual(verified, {5: 100.0})

    def test_filter_no_opinion_keeps_llm_remap(self):
        # Filtro senza opinione (None): comportamento storico, l'LLM fa fede.
        words = self._words()
        with (
            self._no_cache()[0],
            self._no_cache()[1],
            self._no_cache()[2],
            patch("llm_sync._call_endpoint", return_value='[{"timestamp": 100.0, "slide": 5}]'),
        ):
            verified = llm_verify_anchor_mapping(
                ["s1", "s2", "s3", "s4", "s5"],
                words,
                anchors={4: 100.0},
                total_slides=5,
                endpoints=[self._ep("9router", "http://x")],
                remap_filter=lambda _s, _t, _s2: None,
            )
        self.assertIsNotNone(verified)
        self.assertEqual(verified, {5: 100.0})


class TestCacheCleanup(unittest.TestCase):
    """Pulizia delle cache LLM/slides/transcript orfane (richiesta: a ogni
    nuovo avvio rimuovere la cache vecchia che non serve)."""

    def setUp(self):
        import tempfile

        import main

        self._main = main
        self._old_cache_dir = main.CACHE_DIR
        main.CACHE_DIR = self._tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(self._restore_cache_dir)

    def _restore_cache_dir(self):
        self._main.CACHE_DIR = self._old_cache_dir

    def _write(self, name):
        (self._tmpdir / name).write_text("[]")

    def test_clean_stale_llm_cache_keeps_only_current(self):
        self._write("llm_timeline_finale.json")
        for i in range(3):
            self._write(f"llm_orfano{i}.json")
        # chiavi della run corrente (alcune non esistono ancora: non devono
        # essere rimosse in fase di pulizia, devono solo essere conservate)
        keep = {"llm_timeline_finale", "llm_corrente_a", "llm_corrente_b"}
        removed = self._main._clean_stale_llm_cache(keep)
        self.assertEqual(removed, 3)
        remaining = sorted(p.name for p in self._tmpdir.glob("llm_*.json"))
        self.assertEqual(remaining, ["llm_timeline_finale.json"])

    def test_clean_stale_llm_cache_keeps_existing_current_files(self):
        self._write("llm_timeline_finale.json")
        self._write("llm_corrente.json")
        self._write("llm_orfano.json")
        removed = self._main._clean_stale_llm_cache({"llm_timeline_finale", "llm_corrente"})
        self.assertEqual(removed, 1)
        remaining = sorted(p.name for p in self._tmpdir.glob("llm_*.json"))
        self.assertEqual(remaining, ["llm_corrente.json", "llm_timeline_finale.json"])

    def test_clean_orphan_cache_at_startup(self):
        # cache di slide/trascrizione di PDF/audio precedenti
        self._write("slides_vecchiohash_300_ita.json")
        self._write("transcript_vecchiohash_ita_base_whisper.json")
        self._write("slides_hashattuale_300_ita.json")
        removed = self._main._clean_orphan_cache(
            {"slides_hashattuale_300_ita", "transcript_hashattuale_ita_base_whisper"}
        )
        self.assertEqual(removed, 2)
        remaining = sorted(p.name for p in self._tmpdir.glob("*.json"))
        self.assertEqual(remaining, ["slides_hashattuale_300_ita.json"])

    def test_clean_orphan_cache_keeps_machine_setup(self):
        # machine_setup.json è configurazione, non cache: deve sopravvivere
        # alla pulizia, altrimenti il rilevamento hardware riparte a ogni run
        # e la scelta del motore non viene mai riusata.
        self._write("machine_setup.json")
        self._write("slides_vecchiohash_300_ita.json")
        removed = self._main._clean_orphan_cache({"slides_hashattuale_300_ita"})
        self.assertEqual(removed, 1)
        self.assertTrue((self._tmpdir / "machine_setup.json").exists())

    def test_llm_cache_keys_for_variants(self):
        from llm_sync import llm_cache_keys_for

        words = [{"word": f"w{i}", "start": i * 5.0} for i in range(20)]
        slides = ["s1", "s2", "s3", "s4", "s5"]
        anchors_a = {2: 30.0, 5: 120.0}
        anchors_b = {3: 60.0}
        keys = llm_cache_keys_for(
            slides, words, total_slides=5, chunk_seconds=30.0,
            endpoints=[{"url": "http://x", "api_key": "k", "model": "m"}],
            anchors_variants=[anchors_a, anchors_b],
        )
        # 2 varianti di ancore x (verifica + ordinata) = 4 chiavi, tutte distinte
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(k.startswith("llm_") for k in keys))
        # nessun anchors vuoto -> nessuna chiave aggiunta
        empty = llm_cache_keys_for(
            slides, words, total_slides=5, chunk_seconds=30.0,
            endpoints=[], anchors_variants=[{}],
        )
        self.assertEqual(empty, set())


if __name__ == "__main__":
    unittest.main()
