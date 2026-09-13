#!/usr/bin/env python3
"""
Test unitari per la correzione dei nomi propri nella trascrizione
(transcription.correct_transcript_names).
Esegui con: python -m unittest test_transcription -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from transcription import correct_transcript_names, openvino_usable


def _words(items):
    """Converte [(word, start)] in lista di dict Whisper con end/conf."""
    return [{"word": w, "start": float(t), "end": float(t) + 0.3, "conf": 1.0} for w, t in items]


class TestCorrectTranscriptNames(unittest.TestCase):
    """Dizionario dei nomi propri che Whisper storpi sistematicamente."""

    def test_single_word_correction(self):
        words = _words([("sigmond", 10.0), ("freud", 10.4)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["sigmund", "freud"])

    def test_multi_word_phrase_collapses(self):
        # "thomas mur" -> "thomas more": il gruppo diventa una parola sola,
        # le successive vengono scartate e l'intervallo temporale è conservato.
        words = _words([("thomas", 5.0), ("mur", 5.4), ("invento", 6.0)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["thomas more", "invento"])
        self.assertAlmostEqual(out[0]["end"], 5.7)  # fine dell'ultima parola del gruppo

    def test_case_and_punctuation_insensitive(self):
        words = _words([("Mark", 1.0), ("chiuse!", 1.5)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["marcuse"])

    def test_phrase_split_by_other_words_not_corrected(self):
        # "kep curo" deve comparire come coppia consecutiva per essere corretto
        words = _words([("kep", 0.0), ("non", 0.5), ("curo", 1.0)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["kep", "non", "curo"])

    def test_unchanged_words_left_intact(self):
        words = _words([("ciao", 0.0), ("mondo", 1.0)])
        out = correct_transcript_names(words)
        self.assertEqual([w["word"] for w in out], ["ciao", "mondo"])

    def test_original_list_not_mutated(self):
        words = _words([("on", 0.0), ("lock", 0.5)])
        correct_transcript_names(words)
        self.assertEqual([w["word"] for w in words], ["on", "lock"])

    def test_empty_input(self):
        self.assertEqual(correct_transcript_names([]), [])


class TestOpenvinoUsable(unittest.TestCase):
    """openvino_usable: l'avviso OpenVINO va mostrato solo se percorribile."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_path = Path(self._tmp.name) / "machine_setup.json"
        patcher = mock.patch("transcription.MACHINE_CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(self, transcriber: str) -> None:
        self.config_path.write_text(
            json.dumps({"transcriber": transcriber}), encoding="utf-8"
        )

    def _patch_runtime(self, genai_ok: bool, devices: list[str] | None = None):
        """Simula la presenza/assenza del runtime OpenVINO in sys.modules."""
        modules = {"openvino_genai": mock.MagicMock() if genai_ok else None}
        if genai_ok:
            core = mock.MagicMock(return_value=mock.MagicMock(available_devices=devices or []))
            modules["openvino"] = mock.MagicMock(Core=core)
        else:
            modules["openvino"] = None
        return mock.patch.dict(sys.modules, modules)

    def test_setup_says_openvino(self):
        self._write_config("openvino")
        self.assertTrue(openvino_usable())

    def test_setup_says_whisper(self):
        self._write_config("whisper")
        self.assertFalse(openvino_usable())

    def test_setup_whisper_wins_over_runtime(self):
        # Anche col runtime installato, la decisione di machine_setup prevale.
        self._write_config("whisper")
        with self._patch_runtime(genai_ok=True, devices=["CPU"]):
            self.assertFalse(openvino_usable())

    def test_no_setup_and_no_runtime(self):
        # Nessun machine_setup.json e openvino non installato -> avviso soppresso.
        with self._patch_runtime(genai_ok=False):
            self.assertFalse(openvino_usable())

    def test_no_setup_with_igpu(self):
        # Runtime installato con device GPU (iGPU Intel): consiglio sensato.
        with self._patch_runtime(genai_ok=True, devices=["GPU"]):
            self.assertTrue(openvino_usable())

    def test_no_setup_cpu_only(self):
        # openvino installato ma solo CPU (es. ARM/AMD): senza iGPU non c'è
        # guadagno di velocità -> avviso soppresso (caso Snapdragon).
        with self._patch_runtime(genai_ok=True, devices=["CPU"]):
            self.assertFalse(openvino_usable())

    def test_no_setup_runtime_without_devices(self):
        # Runtime installato ma nessun device disponibile -> non percorribile.
        with self._patch_runtime(genai_ok=True, devices=[]):
            self.assertFalse(openvino_usable())

    def test_corrupt_setup_falls_back_to_runtime(self):
        self.config_path.write_text("{non-json", encoding="utf-8")
        with self._patch_runtime(genai_ok=True, devices=["GPU"]):
            self.assertTrue(openvino_usable())


class TestWhisperBeamConfig(unittest.TestCase):
    """Beam size di faster-whisper configurabile (env/CLI) e inoltrato al motore."""

    def test_default_beam_matches_config(self):
        import inspect

        from config import DEFAULT_WHISPER_BEAM
        from transcription import transcribe_with_whisper

        sig = inspect.signature(transcribe_with_whisper)
        self.assertEqual(sig.parameters["beam_size"].default, DEFAULT_WHISPER_BEAM)
        self.assertGreaterEqual(DEFAULT_WHISPER_BEAM, 1)

    def test_transcribe_audio_forwards_beam_size(self):
        from transcription import transcribe_audio

        with mock.patch("transcription.transcribe_with_whisper", return_value=("", [])) as m:
            transcribe_audio(Path("audio.mp3"), transcriber="whisper", whisper_beam=2)
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs.get("beam_size"), 2)

    def test_transcribe_audio_forwards_batch_size(self):
        from transcription import transcribe_audio

        with mock.patch("transcription.transcribe_with_whisper", return_value=("", [])) as m:
            transcribe_audio(Path("audio.mp3"), transcriber="whisper", whisper_batch=4)
        self.assertEqual(m.call_args.kwargs.get("batch_size"), 4)


def _fake_segments(items):
    """Segmenti minimi compatibili con _collect_words (testo + parole)."""
    segs = []
    for words in items:
        seg = mock.MagicMock()
        seg.text = " ".join(w for w, _t in words)
        seg.words = [mock.MagicMock(word=w, start=t) for w, t in words]
        segs.append(seg)
    return segs


class TestBatchedDecoding(unittest.TestCase):
    """Decoding a batch: stesso modello, solo più throughput, mai fatale."""

    def test_default_batch_matches_config(self):
        import inspect

        from config import DEFAULT_WHISPER_BATCH
        from transcription import transcribe_with_whisper

        sig = inspect.signature(transcribe_with_whisper)
        self.assertEqual(sig.parameters["batch_size"].default, DEFAULT_WHISPER_BATCH)
        self.assertGreaterEqual(DEFAULT_WHISPER_BATCH, 0)

    def _run(self, batch_size, pipe_side_effect=None, pipe_exists=True):
        from transcription import _transcribe_with_fallback

        model = mock.MagicMock()
        model.transcribe.return_value = (_fake_segments([[("ciao", 0.0)]]), mock.MagicMock())
        pipe_cls = mock.MagicMock()
        if pipe_side_effect is not None:
            pipe_cls.return_value.transcribe.side_effect = pipe_side_effect
        else:
            pipe_cls.return_value.transcribe.return_value = (
                _fake_segments([[("ciao", 0.0)], [("mondo", 1.0)]]),
                mock.MagicMock(),
            )
        modules = {"faster_whisper": mock.MagicMock(BatchedInferencePipeline=pipe_cls)}
        if not pipe_exists:
            modules = {"faster_whisper": mock.MagicMock(spec=[])}
        with mock.patch.dict(sys.modules, modules):
            words, _info = _transcribe_with_fallback(
                model,
                Path("audio.mp3"),
                language="it",
                beam_size=1,
                vad_filter=True,
                vad_parameters={},
                batch_size=batch_size,
            )
        return words, model, pipe_cls

    def test_batch_used_when_enabled(self):
        words, model, pipe_cls = self._run(batch_size=8)
        pipe_cls.return_value.transcribe.assert_called_once()
        self.assertEqual(pipe_cls.return_value.transcribe.call_args.kwargs.get("batch_size"), 8)
        model.transcribe.assert_not_called()
        self.assertEqual([w["word"] for w in words], ["ciao", "mondo"])

    def test_sequential_when_batch_disabled(self):
        _words, model, pipe_cls = self._run(batch_size=1)
        model.transcribe.assert_called_once()
        pipe_cls.return_value.transcribe.assert_not_called()

    def test_falls_back_to_sequential_on_failure(self):
        # Un guasto del decoding a batch non deve far fallire la trascrizione.
        _words, model, _pipe = self._run(batch_size=8, pipe_side_effect=RuntimeError("boom"))
        model.transcribe.assert_called_once()

    def test_falls_back_when_pipeline_missing(self):
        # Versione di faster-whisper senza BatchedInferencePipeline.
        _words, model, _pipe = self._run(batch_size=8, pipe_exists=False)
        model.transcribe.assert_called_once()

    def test_words_still_extracted_without_word_timestamps(self):
        # Ramo di riserva: nessun timestamp per parola, si usa l'inizio del segmento.
        from transcription import _collect_words

        seg = mock.MagicMock(text="uno due", words=None)
        seg.start = 3.5
        words = _collect_words([seg])
        self.assertEqual([w["word"] for w in words], ["uno", "due"])
        self.assertEqual({w["start"] for w in words}, {3.5})


class TestResolvedTranscriber(unittest.TestCase):
    """Motore RISOLTO: è ciò che finisce nella chiave di cache della trascrizione."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.model_dir = Path(self._tmp.name) / "whisper_openvino_small"

    def _runtime(self, installed: bool):
        return mock.patch.dict(sys.modules, {"openvino_genai": mock.MagicMock() if installed else None})

    def test_explicit_whisper(self):
        from transcription import resolved_transcriber

        with self._runtime(installed=True):
            self.assertEqual(resolved_transcriber("whisper", self.model_dir), "whisper")

    def test_auto_without_runtime(self):
        from transcription import resolved_transcriber

        with self._runtime(installed=False):
            self.assertEqual(resolved_transcriber("auto", self.model_dir), "whisper")

    def test_auto_without_model(self):
        from transcription import resolved_transcriber

        with self._runtime(installed=True):
            self.assertEqual(resolved_transcriber("auto", self.model_dir), "whisper")

    def test_auto_with_model(self):
        from transcription import resolved_transcriber

        self.model_dir.mkdir(parents=True)
        with self._runtime(installed=True):
            self.assertEqual(resolved_transcriber("auto", self.model_dir), "openvino")

    def test_openvino_explicit_without_model_stays_openvino(self):
        # Il modello verrà scaricato da transcribe_audio: la scelta è comunque
        # OpenVINO, e la cache deve saperlo prima del download.
        from transcription import resolved_transcriber

        with self._runtime(installed=True):
            self.assertEqual(resolved_transcriber("openvino", self.model_dir), "openvino")

    def test_auto_warns_only_with_runtime(self):
        """L'avviso OpenVINO compare solo se il runtime c'è ma il modello manca."""
        from transcription import transcribe_audio

        with (
            self._runtime(installed=True),
            mock.patch("transcription.transcribe_with_whisper", return_value=("", [])),
            mock.patch("transcription.log.warning") as warn,
        ):
            transcribe_audio(Path("a.mp3"), transcriber="auto", openvino_model_dir=self.model_dir)
        self.assertTrue(any("OpenVINO" in str(c.args[0]) for c in warn.call_args_list))

        with (
            self._runtime(installed=False),
            mock.patch("transcription.transcribe_with_whisper", return_value=("", [])),
            mock.patch("transcription.log.warning") as warn,
        ):
            transcribe_audio(Path("a.mp3"), transcriber="auto", openvino_model_dir=self.model_dir)
        self.assertFalse(any("OpenVINO" in str(c.args[0]) for c in warn.call_args_list))

    def test_explicit_openvino_without_runtime_raises(self):
        from transcription import transcribe_audio

        with self._runtime(installed=False), self.assertRaises(RuntimeError):
            transcribe_audio(Path("a.mp3"), transcriber="openvino", openvino_model_dir=self.model_dir)


class TestTranscriptCacheKey(unittest.TestCase):
    """La chiave di cache deve descrivere tutte le scelte che cambiano il testo."""

    def _args(self, **overrides):
        base = {
            "lang": "ita",
            "whisper_model": "small",
            "transcriber": "whisper",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "whisper_beam": 1,
            "whisper_batch": 8,
            "openvino_device": "GPU",
            "openvino_model_dir": str(Path(tempfile.gettempdir()) / "non_esiste_ov"),
        }
        base.update(overrides)
        return mock.MagicMock(**base)

    def test_key_changes_with_beam_batch_and_compute(self):
        from main import _transcript_cache_key

        base = _transcript_cache_key("a" * 32, self._args())
        self.assertNotEqual(base, _transcript_cache_key("a" * 32, self._args(whisper_beam=5)))
        self.assertNotEqual(base, _transcript_cache_key("a" * 32, self._args(whisper_batch=1)))
        self.assertNotEqual(base, _transcript_cache_key("a" * 32, self._args(whisper_compute_type="float32")))
        self.assertNotEqual(base, _transcript_cache_key("a" * 32, self._args(whisper_device="cuda")))
        self.assertNotEqual(base, _transcript_cache_key("a" * 32, self._args(whisper_model="base")))
        self.assertNotEqual(base, _transcript_cache_key("b" * 32, self._args()))

    def test_auto_and_whisper_agree_when_engine_resolves_to_whisper(self):
        # 'auto' senza modello OpenVINO È faster-whisper: stessa cache, nessuna
        # trascrizione rifatta inutilmente al cambio di flag.
        from main import _transcript_cache_key

        with mock.patch.dict(sys.modules, {"openvino_genai": None}):
            auto = _transcript_cache_key("a" * 32, self._args(transcriber="auto"))
            whisper = _transcript_cache_key("a" * 32, self._args(transcriber="whisper"))
        self.assertEqual(auto, whisper)

    def test_openvino_engine_gets_its_own_key(self):
        # Se il modello IR esiste, 'auto' passa a OpenVINO: la chiave DEVE
        # cambiare, altrimenti si riuserebbe un testo prodotto dall'altro motore.
        from main import _transcript_cache_key

        with tempfile.TemporaryDirectory() as tmp:
            model_dir = Path(tmp) / "ov"
            model_dir.mkdir()
            with mock.patch.dict(sys.modules, {"openvino_genai": mock.MagicMock()}):
                auto = _transcript_cache_key(
                    "a" * 32, self._args(transcriber="auto", openvino_model_dir=str(model_dir))
                )
                whisper = _transcript_cache_key("a" * 32, self._args(transcriber="whisper"))
        self.assertNotEqual(auto, whisper)
        self.assertIn("openvino", auto)


class TestAutoBeamPolicy(unittest.TestCase):
    """Beam automatico: veloce quando le ancore vincolano, accurato quando no."""

    def _enabled(self, beam, auto_beam=True):
        import main as m

        return m._beam_auto_enabled(mock.MagicMock(whisper_beam=beam, auto_beam=auto_beam))

    def test_active_only_with_greedy_beam(self):
        self.assertTrue(self._enabled(1))
        # beam 2-5 = l'utente ha già scelto l'accuratezza: non c'è nulla da rifare.
        self.assertFalse(self._enabled(2))
        self.assertFalse(self._enabled(5))

    def test_disabled_by_flag(self):
        self.assertFalse(self._enabled(1, auto_beam=False))

    def test_accurate_beam_needed_when_few_anchors(self):
        import main as m

        self.assertFalse(m._needs_accurate_beam(10, 11))  # 10/10 vincolate: fissata
        self.assertFalse(m._needs_accurate_beam(5, 11))  # 5/10 = 0.5 (soglia)
        self.assertTrue(m._needs_accurate_beam(4, 11))  # 4/10 = 0.4
        self.assertTrue(m._needs_accurate_beam(0, 11))  # nessuna ancora
        self.assertTrue(m._needs_accurate_beam(0, 15))  # flusso libero

    def test_single_slide_deck_never_escalates(self):
        import main as m

        self.assertFalse(m._needs_accurate_beam(0, 1))

    def test_threshold_is_configurable(self):
        # Con AUTO_BEAM_PINNED_RATIO=1.1 ricade nel percorso accurato anche un
        # deck completamente ancorato: serve per provarlo sui dati reali.
        import main as m

        with mock.patch.object(m, "AUTO_BEAM_PINNED_RATIO", 1.1):
            self.assertTrue(m._needs_accurate_beam(10, 11))


class TestAccurateBeamEscalation(unittest.TestCase):
    """Seconda trascrizione: solo quando il contenuto decide, e mai due volte."""

    def setUp(self):
        import main

        self._main = main
        self._old_cache_dir = main.CACHE_DIR
        main.CACHE_DIR = self._tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(self._restore_cache_dir)

    def _restore_cache_dir(self):
        self._main.CACHE_DIR = self._old_cache_dir

    def _args(self, **overrides):
        base = {
            "lang": "ita",
            "whisper_model": "small",
            "transcriber": "whisper",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "whisper_beam": 1,
            "whisper_batch": 8,
            "auto_beam": True,
            "no_cache": False,
            "openvino_device": "GPU",
            "openvino_model_dir": str(Path(tempfile.gettempdir()) / "non_esiste_ov"),
        }
        base.update(overrides)
        return mock.MagicMock(**base)

    def test_transcribes_with_accurate_beam_and_keeps_both_caches(self):
        m = self._main
        args = self._args()
        key = m._transcript_cache_key("a" * 32, args, beam=m.DEFAULT_WHISPER_BEAM_ACCURATE)
        words = [{"word": "slide", "start": 1.0}]
        with mock.patch.object(m, "transcribe_audio", return_value=("testo", words)) as tr:
            transcript, out_words, note = m._transcribe_with_accurate_beam(Path("a.m4a"), args, key)
        self.assertEqual(transcript, "testo")
        self.assertEqual(out_words, words)
        self.assertEqual(note["accurate_beam"], m.DEFAULT_WHISPER_BEAM_ACCURATE)
        self.assertEqual(tr.call_args.kwargs.get("whisper_beam"), m.DEFAULT_WHISPER_BEAM_ACCURATE)
        self.assertEqual(tr.call_args.kwargs.get("whisper_batch"), 8)
        # La chiave non è quella della decodifica veloce: le due trascrizioni
        # convivono in cache invece di sovrascriversi.
        self.assertNotEqual(key, m._transcript_cache_key("a" * 32, args))
        cached = cast("dict", m._load_cache(key))
        self.assertEqual(cached["words_raw"], words)

    def test_second_call_reuses_cache_without_retranscribing(self):
        m = self._main
        args = self._args()
        key = m._transcript_cache_key("a" * 32, args, beam=m.DEFAULT_WHISPER_BEAM_ACCURATE)
        with mock.patch.object(
            m, "transcribe_audio", return_value=("testo", [{"word": "x", "start": 0.0}])
        ):
            m._transcribe_with_accurate_beam(Path("a.m4a"), args, key)
        with mock.patch.object(m, "transcribe_audio") as tr:
            transcript, _words, note = m._transcribe_with_accurate_beam(Path("a.m4a"), args, key)
        tr.assert_not_called()
        self.assertEqual(transcript, "testo")
        self.assertTrue(note.get("accurate_from_cache"))

    def test_both_transcripts_survive_orphan_cleanup(self):
        m = self._main
        args = self._args()
        greedy = m._transcript_cache_key("a" * 32, args)
        accurate = m._transcript_cache_key("a" * 32, args, beam=m.DEFAULT_WHISPER_BEAM_ACCURATE)
        m._save_cache(greedy, {"transcript": "veloce"})
        m._save_cache(accurate, {"transcript": "accurata"})
        # Entrambe sono cache ATTIVE: cancellare l'accurata a fine run farebbe
        # ripagare una seconda trascrizione a ogni esecuzione.
        self.assertEqual(m._clean_orphan_cache({"slides_x", greedy, accurate}), 0)
        self.assertIsNotNone(m._load_cache(accurate))


class TestBeamAbReport(unittest.TestCase):
    """Il confronto fra le due trascrizioni: la misura registrata e riusata."""

    def setUp(self):
        import main

        self._main = main
        self._old_cache_dir = main.CACHE_DIR
        main.CACHE_DIR = self._tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(self._restore_cache_dir)

    def _restore_cache_dir(self):
        self._main.CACHE_DIR = self._old_cache_dir

    def _args(self, **overrides):
        base = {
            "semantic_model": "intfloat/multilingual-e5-large",
            "semantic_cache_dir": str(Path(tempfile.gettempdir()) / "emb"),
            "semantic_window": 4.0,
            "semantic_min_duration": 3.0,
            "semantic_min_sim": 0.10,
            "semantic_min_z": 0.45,
            "semantic_temperature": 0.15,
            "no_cache": False,
        }
        base.update(overrides)
        return mock.MagicMock(**base)

    def _compare(self, greedy_z, accurate_z):
        import main as m

        def fake(slide_texts, words, total_slides, total_duration, options=None, anchors=None, embed_fn=None):
            z = greedy_z if words == [{"word": "veloce"}] else accurate_z
            if z is None:
                return None
            return {"avg_sim": 0.83, "avg_z": z, "concordance": 0.62, "confusability": 1.0, "blocks": 263.0}

        with mock.patch.object(m, "alignment_quality_from_words", side_effect=fake):
            return m._compare_transcript_alignment(
                self._args(),
                ["slide uno", "slide due"],
                2,
                120.0,
                [{"word": "veloce"}],
                [{"word": "accurata"}],
            )

    def test_accurate_wins_when_better(self):
        note = self._compare(greedy_z=0.70, accurate_z=0.75)
        self.assertEqual(note["would_have_won"], "accurate")
        self.assertAlmostEqual(cast(float, note["delta_avg_z"]), 0.05, places=4)

    def test_greedy_win_is_measured(self):
        # Caso reale (13/09): la decodifica veloce dava un picco PIÙ alto.
        note = self._compare(greedy_z=0.753, accurate_z=0.733)
        self.assertEqual(note["would_have_won"], "greedy")
        self.assertLess(cast(float, note["delta_avg_z"]), 0)

    def test_tie_is_reported_as_tie(self):
        self.assertEqual(self._compare(greedy_z=0.70, accurate_z=0.70)["would_have_won"], "pari")

    def test_unmeasurable_comparison_is_declared(self):
        # Senza segnale non si inventa un vincitore: si dichiara che il
        # confronto non è calcolabile.
        note = self._compare(greedy_z=0.70, accurate_z=None)
        self.assertIn("error", note)
        self.assertNotIn("would_have_won", note)

    def test_report_keeps_the_context_of_the_comparison(self):
        # concordance/confusability restano nel report: su un deck confondibile
        # il confronto è rumore e va riconosciuto come tale.
        note = self._compare(greedy_z=0.70, accurate_z=0.75)
        self.assertEqual(cast(dict, note["greedy"])["confusability"], 1.0)
        self.assertEqual(cast(dict, note["accurate"])["concordance"], 0.62)

    def test_measure_is_cached_and_not_recomputed(self):
        # Due embedding completi per una decisione che non cambia: la misura si
        # fa una volta per audio+slide+modello, poi si legge.
        m = self._main
        args = self._args()
        key = m._beam_ab_cache_key("slides_abc_300_ita", "a" * 32, args)
        m._save_cache(key, {"ab": {"delta_avg_z": -0.02, "would_have_won": "greedy"}})
        with mock.patch.object(m, "alignment_quality_from_words") as q:
            note = m._compare_transcript_alignment(
                args, ["slide"], 2, 100.0, [{"word": "a"}], [{"word": "b"}], key
            )
        q.assert_not_called()
        self.assertTrue(note["from_cache"])
        self.assertEqual(note["delta_avg_z"], -0.02)

    def test_measure_is_written_to_cache(self):
        m = self._main
        args = self._args()
        key = m._beam_ab_cache_key("slides_abc_300_ita", "a" * 32, args)
        self._compare(greedy_z=0.70, accurate_z=0.75)  # scalda la funzione finta
        with mock.patch.object(m, "alignment_quality_from_words", side_effect=self._fake_quality):
            m._compare_transcript_alignment(
                args, ["slide"], 2, 100.0, [{"word": "veloce"}], [{"word": "accurata"}], key
            )
        cached = cast("dict", m._load_cache(key))
        self.assertIn("ab", cached)
        self.assertEqual(cached["ab"]["would_have_won"], "accurate")

    @staticmethod
    def _fake_quality(slide_texts, words, total_slides, total_duration, options=None, anchors=None, embed_fn=None):
        z = 0.70 if words == [{"word": "veloce"}] else 0.75
        return {"avg_sim": 0.83, "avg_z": z, "concordance": 0.62, "confusability": 1.0, "blocks": 263.0}


class TestBeamChoice(unittest.TestCase):
    """La misura decide quale trascrizione usare, senza pagarne una nuova."""

    def _decide(self, ab, added_anchors=False):
        import main as m

        return m._use_accurate_transcript(ab, added_anchors)

    def test_accurate_wins(self):
        use_accurate, reason = self._decide({"delta_avg_z": 0.05})
        self.assertTrue(use_accurate)
        self.assertIn("accurata ha il segnale migliore", reason)

    def test_fast_wins_so_the_accurate_is_not_used(self):
        # È la richiesta esplicita: se il segnale migliore ce l'ha la veloce,
        # non si paga (né si usa) la decodifica accurata.
        use_accurate, reason = self._decide({"delta_avg_z": -0.0203})
        self.assertFalse(use_accurate)
        self.assertIn("veloce ha il segnale migliore", reason)

    def test_tie_keeps_the_prudent_choice(self):
        use_accurate, _ = self._decide({"delta_avg_z": 0.0})
        self.assertTrue(use_accurate)

    def test_margin_makes_the_choice_more_prudent(self):
        import main as m

        ab = {"delta_avg_z": 0.02}
        self.assertTrue(self._decide(ab)[0])
        with mock.patch.object(m, "AUTO_BEAM_AB_MARGIN", 0.05):
            self.assertFalse(self._decide(ab)[0])

    def test_new_anchors_trump_the_measurement(self):
        # Le ancore sono riferimenti espliciti: se le trova solo la decodifica
        # accurata, resta quella anche se il proxy di somiglianza preferirebbe
        # la veloce.
        use_accurate, reason = self._decide({"delta_avg_z": -0.30}, added_anchors=True)
        self.assertTrue(use_accurate)
        self.assertIn("ancore", reason)

    def test_unmeasurable_comparison_keeps_the_accurate(self):
        use_accurate, reason = self._decide({"error": "qualità non calcolabile"})
        self.assertTrue(use_accurate)
        self.assertIn("non calcolabile", reason)

    def test_cache_key_ignores_the_margin_but_not_the_engine(self):
        import main as m

        args = mock.MagicMock(
            semantic_model="intfloat/multilingual-e5-large",
            semantic_window=4.0,
            semantic_min_duration=3.0,
            semantic_temperature=0.15,
        )
        base = m._beam_ab_cache_key("slides_abc_300_ita", "a" * 32, args)
        # Cambiare la soglia di scelta non invalida la misura: è un dato, la
        # decisione si riapplica a ogni run.
        with mock.patch.object(m, "AUTO_BEAM_AB_MARGIN", 0.9):
            self.assertEqual(base, m._beam_ab_cache_key("slides_abc_300_ita", "a" * 32, args))
        for changed in (
            {"semantic_model": "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"},
            {"semantic_window": 8.0},
            {"semantic_min_duration": 6.0},
            {"semantic_temperature": 0.30},
        ):
            other = m._beam_ab_cache_key("slides_abc_300_ita", "a" * 32, mock.MagicMock(**{**args.__dict__, **changed}))
            self.assertNotEqual(base, other)
        self.assertNotEqual(base, m._beam_ab_cache_key("slides_xyz_300_ita", "a" * 32, args))
        self.assertNotEqual(base, m._beam_ab_cache_key("slides_abc_300_ita", "b" * 32, args))


if __name__ == "__main__":
    unittest.main()
