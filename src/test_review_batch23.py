#!/usr/bin/env python3
"""Regressioni della revisione batch 23.

Quinta revisione: capitoli leggibili dall'audio, regressioni del batch 22,
mapping ancore e qualita' dei test stessi.

Esegui con: python -m unittest test_review_batch23 -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import diagnostica
import main
import pipeline_review
import subtitles
import transcription
from chunks import Word
from timeline import extract_slide_anchors

ROOT = Path(__file__).resolve().parent.parent


class _Args:
    def __init__(self, **valori: object) -> None:
        self.clean_cache = False
        self.include_models = False
        self.yes = True
        self.pdf = "presentazione.pptx"
        self.pdf_path = ROOT / "presentazione.pptx"
        self.audio = "podcast.m4a"
        self.dpi = 300
        self.lang = "ita"
        self.fallback_senza_grafica = False
        self.prefetch_models = False
        for chiave, valore in valori.items():
            setattr(self, chiave, valore)


class TestCapitoliLeggibili(unittest.TestCase):
    def test_ocr_rumoroso_non_diventa_titolo_di_capitolo(self) -> None:
        ocr = [
            "L'inggtanno del piacere senza fine",
            "L'outoerotismo blocca lu F cunosità e la pazienza",
        ]
        parlato = [
            {"word": "L'inganno", "start": 0.0},
            {"word": "del", "start": 1.0},
            {"word": "piacere", "start": 2.0},
            {"word": "L'autoerotismo", "start": 30.0},
            {"word": "blocca", "start": 31.0},
            {"word": "la", "start": 32.0},
            {"word": "curiosità", "start": 33.0},
        ]
        testo = subtitles.build_capitoli(
            {1: 0.0, 2: 30.0}, ocr, words=parlato
        )
        self.assertNotIn("inggtanno", testo)
        self.assertNotIn("cunosità", testo)
        self.assertIn("inganno", testo)
        self.assertIn("curiosità", testo)

    def test_ocr_buono_non_viene_sostituito(self) -> None:
        ocr = ["Bisogni Primari: Fame, Sete, Sonno."]
        parlato = [
            {"word": "ciao", "start": 0.0},
            {"word": "a", "start": 1.0},
            {"word": "tutti", "start": 2.0},
        ]
        testo = subtitles.build_capitoli({1: 0.0}, ocr, words=parlato)
        self.assertIn("Bisogni Primari", testo)

    def test_tuoni_maiuscoli_hanno_qualita_bassa(self) -> None:
        self.assertLess(
            subtitles._qualita_titolo("IEEARSI DIRLA SFFSITE"),
            subtitles._qualita_titolo("Bisogni primari e desideri"),
        )


class TestRegressioniBatch22(unittest.TestCase):
    def test_clean_cache_pptx_senza_converter_non_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pptx = base / "presentazione.pptx"
            pptx.write_bytes(b"pptx")
            audio = base / "podcast.m4a"
            audio.write_bytes(b"audio")
            with (
                mock.patch.object(main, "bootstrap"),
                mock.patch.object(
                    main,
                    "convert_presentation_to_pdf",
                    side_effect=RuntimeError("nessun converter"),
                ) as converti,
                mock.patch("cache_maintenance.clean_cache", return_value={}) as pulizia,
                self.assertLogs("slide2video", level="WARNING") as log,
            ):
                main.main(["--clean-cache", "--yes", "--pdf", str(pptx), "--audio", str(audio)])
            self.assertEqual(converti.call_count, 1)
            self.assertEqual(pulizia.call_count, 1)
            self.assertEqual(pulizia.call_args.kwargs["active_keys"], set())
            self.assertIn("Pulizia piena", "\n".join(log.output))

    def test_openvino_esplicito_preserva_segnale_di_altro_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = base / "a.m4a"
            b = base / "b.m4a"
            a.write_bytes(b"a")
            b.write_bytes(b"b")
            with (
                mock.patch.object(transcription, "CACHE_DIR", base),
                mock.patch.object(transcription, "resolved_transcriber", return_value="openvino"),
                mock.patch.object(transcription, "openvino_model_complete", return_value=True),
                mock.patch.object(
                    transcription, "transcribe_with_openvino", return_value=("t", [])
                ),
            ):
                transcription._write_attempt(a)
                transcription.transcribe_audio(
                    b, transcriber="openvino", openvino_model_dir=base / "ir"
                )
                segnale = transcription._read_attempt()
            self.assertTrue(transcription._same_audio(segnale, a))

    def test_fallback_cpu_fallito_mantiene_segnale_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            audio = base / "podcast.m4a"
            audio.write_bytes(b"audio")
            with (
                mock.patch.object(transcription, "CACHE_DIR", base),
                mock.patch.object(transcription, "resolved_transcriber", return_value="openvino"),
                mock.patch.object(transcription, "openvino_model_complete", return_value=True),
                mock.patch.object(
                    transcription, "decide_transcriber", return_value=("whisper", "crash")
                ),
                mock.patch.object(
                    transcription,
                    "transcribe_with_whisper",
                    side_effect=RuntimeError("modello mancante"),
                ),
            ):
                transcription._write_attempt(audio)
                with self.assertRaises(RuntimeError):
                    transcription.transcribe_audio(audio, transcriber="auto")
                self.assertTrue(transcription._openvino_attempt_path().exists())

    def test_id_openvino_diverso_con_ir_completo_riscarica(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ir = Path(tmp) / "ir"
            ir.mkdir()
            (ir / "config.json").write_text("{}", encoding="utf-8")
            (ir / "m.xml").write_bytes(b"x")
            (ir / "m.bin").write_bytes(b"y")
            (ir / "openvino_model_id.json").write_text(
                json.dumps({"model_id": "OLD"}), encoding="utf-8"
            )
            with (
                mock.patch.object(transcription, "resolved_transcriber", return_value="openvino"),
                mock.patch.object(transcription, "openvino_model_complete", return_value=True),
                mock.patch.object(transcription, "decide_transcriber", return_value=("openvino", None)),
                mock.patch.object(transcription, "transcribe_with_openvino", return_value=("t", [])),
                mock.patch.object(
                    transcription, "download_openvino_model", return_value=ir
                ) as scarica,
            ):
                transcription.transcribe_audio(
                    Path(tmp) / "podcast.m4a", transcriber="auto", openvino_model_dir=ir
                )
            self.assertEqual(scarica.call_count, 1)

    def test_report_preview_rifiutato_da_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "sync_report.json").write_text(
                json.dumps({"anteprima": True, "segments": []}), encoding="utf-8"
            )
            with self.assertLogs("slide2video", level="ERROR") as log:
                esito = pipeline_review._review_boundary_doubts(
                    Path("video.mp4"), cache_dir=cache
                )
            self.assertEqual(esito, 3)
            self.assertIn("--preview", "\n".join(log.output))

    def test_review_non_dice_trascrizione_assente_con_mapping_vecchio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "sync_report.json").write_text(
                json.dumps(
                    {
                        "segments": [{"slide": 1, "start": 0.0}],
                        "anchors": {"mapping_corrected": True},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "pipeline_review.latest_transcript_words",
                    return_value=[{"word": "slide", "start": 1.0}],
                ),
                self.assertLogs("slide2video", level="WARNING") as log,
            ):
                esito = pipeline_review._review_boundary_doubts(
                    Path("video.mp4"), cache_dir=cache
                )
            testo = "\n".join(log.output)
            self.assertEqual(esito, 3)
            self.assertNotIn("nessuna trascrizione leggibile", testo)


class TestScriptEDiagnostica(unittest.TestCase):
    def test_ab_report_ha_i_campi_che_updates_valida(self) -> None:
        testo = (ROOT / "src" / "scripts" / "check_fastembed_upgrade.py").read_text(
            encoding="utf-8"
        )
        for campo in ('"installed"', '"texts_hash"', '"ts"', '"threshold_sim_mean"'):
            self.assertIn(campo, testo)
        self.assertIn("tempfile.mkstemp", testo)

    def test_debug_hardware_pulisce_le_temporanee(self) -> None:
        testo = (ROOT / "src" / "_debug_hardware.py").read_text(encoding="utf-8")
        self.assertIn("TemporaryDirectory(prefix=\"dbg_hw_\")", testo)
        self.assertIn("TemporaryDirectory(prefix=\"dbg_fb_\")", testo)
        self.assertEqual(testo.count("tmp_ctx.cleanup()"), 1)
        self.assertEqual(testo.count("tmp2_ctx.cleanup()"), 1)

    def test_bundle_diagnostico_trova_output_non_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            radice = Path(tmp)
            (radice / "lezione.mp4").write_bytes(b"video")
            stato = diagnostica._stato_run(radice)
            self.assertIsNotNone(stato["video"])
            assert stato["video"] is not None
            self.assertIsInstance(stato["video"], dict)
            self.assertEqual(stato["video"]["byte"], 5)  # type: ignore[index]


class TestAncore(unittest.TestCase):
    def test_numero_fuso_fuori_deck_non_ruba_il_seguente(self) -> None:
        parole = cast("list[Word]", [
            {"word": "slaidiciannove", "start": 10.0},
            {"word": "il", "start": 11.0},
            {"word": "3", "start": 12.0},
            {"word": "passiamo", "start": 13.0},
            {"word": "alla", "start": 14.0},
            {"word": "slide", "start": 15.0},
            {"word": "3", "start": 16.0},
        ])
        ancore = extract_slide_anchors(parole, 14)
        self.assertNotIn(13.0, ancore.values())

    def test_implicito_a_fine_trascrizione_non_inventa_slide(self) -> None:
        parole = cast("list[Word]", [
            {"word": "passiamo", "start": 11.0},
            {"word": "alla", "start": 11.2},
            {"word": "slide", "start": 11.4},
            {"word": "3", "start": 11.6},
            {"word": "passiamo", "start": 32.0},
            {"word": "alla", "start": 32.2},
            {"word": "prossima", "start": 32.4},
            {"word": "slide", "start": 32.6},
            {"word": "4", "start": 32.8},
        ])
        ancore = extract_slide_anchors(parole, 6)
        self.assertNotIn(5, ancore)


if __name__ == "__main__":
    unittest.main()
