#!/usr/bin/env python3
"""Regression del Batch 37 (timeline, chunk, OCR, CLI).

I difetti coperti sono stati verificati nella review: parole perse da un NaN,
ancore rubate da conteggi con preposizione articolata, cache di fallback
degradato, flag che non cambiano niente. Nessun test richiede rete.

Esegui con: python -m unittest discover -s src -p 'test_review_batch37.py' -v
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np

import config
import embedder
import main
import ocr
import timeline
from chunks import TimeIndex, Word, build_windows, words_in_window
from timeline import (
    announcements_off_timeline,
    extract_slide_anchors,
    out_of_range_tail_anchor,
    reconcile_timeline,
)

SCRIPTS = Path(__file__).resolve().parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import check_embedding_models as cem  # noqa: E402


def _parole(coppie: list[tuple[str, float]]) -> list[Word]:
    return [{"word": w, "start": t} for w, t in coppie]


class TestFinestre(unittest.TestCase):
    def test_nan_e_parole_oltre_la_durata_non_vengono_perse(self) -> None:
        parole = _parole(
            [
                ("prima", 1.0),
                ("nan", float("nan")),
                ("dopo", 2.0),
                ("finale", 12.7),
            ]
        )
        finestre = build_windows(parole, 10.0, 4.0)
        testo = " ".join(" ".join(f["words"]) for f in finestre)
        for atteso in ("prima", "dopo", "finale"):
            self.assertIn(atteso, testo)

    def test_finestra_con_bound_nan_restituisce_vuoto(self) -> None:
        parole = _parole([("a", 1.0), ("b", 2.0), ("c", 3.0)])
        indice = TimeIndex(parole)
        self.assertEqual(words_in_window(parole, 0.0, float("nan"), indice), [])
        self.assertEqual(words_in_window(parole, float("nan"), 5.0, indice), [])


class TestEmbedder(unittest.TestCase):
    def test_vettore_non_finito_non_diventa_nan(self) -> None:
        def fn(_testi: Any) -> np.ndarray:
            return np.array([[np.inf, 0.0], [3.0, 4.0]], dtype=np.float32)

        out = embedder._FastEmbedEmbedder(object(), fn).embed(["a", "b"])
        self.assertTrue(np.isfinite(out).all())
        np.testing.assert_allclose(out[0], np.zeros(2))
        self.assertAlmostEqual(float(np.linalg.norm(out[1])), 1.0, places=5)


class TestEnvCoverage(unittest.TestCase):
    def test_frazione_out_of_range_torna_al_default(self) -> None:
        with mock.patch.dict(os.environ, {"MIN_ANCHOR_COVERAGE": "2"}):
            self.assertEqual(config._env_frazione("MIN_ANCHOR_COVERAGE", 0.5), 0.5)
        with mock.patch.dict(os.environ, {"MIN_ANCHOR_COVERAGE": "0"}):
            self.assertEqual(config._env_frazione("MIN_ANCHOR_COVERAGE", 0.5), 0.0)

    def test_valore_cli_non_valido_viene_rifiutato(self) -> None:
        with mock.patch.dict(os.environ, {"WHISPER_MODEL": "smal", "TRANSCRIBER": "openvin"}):
            args = config.parse_args([])
        self.assertEqual(args.whisper_model, "small")
        self.assertIn(args.transcriber, ("auto", "whisper"))

    def test_clean_cache_con_comando_di_uscita_viene_rifiutato(self) -> None:
        for coppia in (["--review", "--clean-cache"], ["--solo-riepilogo", "--clean-cache"]):
            with self.subTest(coppia=coppia), self.assertRaises(SystemExit) as ctx:
                config.parse_args(coppia)
            self.assertEqual(ctx.exception.code, 2)


class TestMonitorModelli(unittest.TestCase):
    def test_429_sul_modello_preferito_non_distrugge_la_baseline(self) -> None:
        class HfHubHTTPError(Exception):
            response = SimpleNamespace(status_code=429)

        class Api:
            def model_info(self, mid: str, **_k: Any) -> Any:
                if mid == cem.PREFERRED_MODEL:
                    raise HfHubHTTPError("rate limit")
                return SimpleNamespace(lastModified=datetime(2026, 1, 1), downloads=1)

            def list_models(self, **_k: Any) -> list[Any]:
                return []

        with self.assertRaises(cem.NetworkError):
            cem._gather(Api(), known=set())


class TestAncoreTimeline(unittest.TestCase):
    def test_conteggio_con_preposizione_articolata(self) -> None:
        parole = _parole(
            [
                ("nelle", 5.0),
                ("13", 5.2),
                ("slide", 5.4),
                ("del", 5.6),
                ("documento", 5.8),
                ("passiamo", 101.0),
                ("slide", 101.2),
                ("2", 101.4),
            ]
        )
        self.assertTrue(timeline._preceded_by_determiner(parole, 1))
        ancore = extract_slide_anchors(parole, 14, "slide-audio")
        self.assertIn(2, ancore)
        self.assertNotIn(13, ancore)

    def test_richiamo_finale_non_diventa_ancora(self) -> None:
        base = _parole([("come", 900.0), ("dicevo", 900.1), ("nella", 900.2)])
        richiamo = base + _parole([("slide", 900.3), ("15", 900.4)])
        self.assertEqual(out_of_range_tail_anchor(richiamo, 14, {3: 11.0}), {})
        transizione = _parole(
            [("passiamo", 900.0), ("alla", 900.2), ("slide", 900.4), ("15", 900.6)]
        )
        self.assertIn(14, out_of_range_tail_anchor(transizione, 14, {3: 11.0}))

    def test_due_numeri_prima_della_stessa_slide_non_danno_lo_stesso_tempo(self) -> None:
        parole = _parole(
            [("cinque", 100.0), ("tre", 100.2), ("slide", 100.4),
             ("e", 100.6), ("poi", 100.8), ("via", 101.0)]
        )
        ancore = extract_slide_anchors(parole, 10, "slide-audio")
        self.assertEqual(len(set(ancore.values())), len(ancore))

    def test_tempo_non_finito_non_diventa_ancora_nan(self) -> None:
        parole = _parole(
            [("passiamo", 10.0), ("slide", 10.5), ("2", float("nan")),
             ("passiamo", 50.0), ("slide", 50.5), ("3", 51.0)]
        )
        ancore = extract_slide_anchors(parole, 10, "slide-audio")
        self.assertTrue(all(math.isfinite(t) for t in ancore.values()))
        with self.assertRaises(ValueError) as ctx:
            reconcile_timeline({1: 0.0, 2: float("nan")}, 2, 100.0)
        self.assertIn("non finito", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))

    def test_verbo_distante_dalla_slide_viene_comunque_segnalato(self) -> None:
        parole = _parole(
            [
                ("passiamo", 0.0), ("e", 0.1), ("come", 0.2), ("abbiamo", 0.3),
                ("detto", 0.4), ("prima", 0.5), ("quattro", 0.6),
                ("alla", 0.7), ("slide", 0.8),
            ]
        )
        dubbi = announcements_off_timeline(parole, {4: 500.0}, 10)
        self.assertEqual(len(dubbi), 1)
        self.assertEqual(dubbi[0][0], 4)


class TestCacheFallbackPDF(unittest.TestCase):
    def test_marker_fallback_non_riusato_dopo_installazione_converter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ppt = base / "deck.pptx"
            ppt.write_bytes(b"ppt")
            pdf = base / "deck.pdf"
            pdf.write_bytes(b"%PDF-1.4 finto")

            def falso_fallback(_ppt: Path, destinazione: Path) -> bool:
                destinazione.write_bytes(b"slide ricostruite")
                return True

            with (
                mock.patch.object(ocr, "_find_soffice", return_value=None),
                mock.patch.object(ocr, "_find_genoffice", return_value=None),
                mock.patch.object(ocr, "_find_onlyoffice", return_value=None),
                mock.patch.object(ocr, "_find_powerpoint", return_value=None),
                mock.patch.object(ocr, "_pptx_fallback_to_pdf", side_effect=falso_fallback),
                mock.patch.object(ocr, "_file_md5", return_value="abc"),
            ):
                ocr.convert_presentation_to_pdf(ppt, base, fallback_senza_grafica=True)
            marker = base / "deck.src_md5"
            self.assertIn("fallback", marker.read_text(encoding="ascii"))

            # Ora il converter c'e': il marker "fallback" non deve poter
            # dichiarare valido il PDF degradato senza riconvertire.
            with (
                mock.patch.object(ocr, "_find_soffice", return_value="soffice"),
                mock.patch.object(ocr, "_find_genoffice", return_value=None),
                mock.patch.object(ocr, "_find_onlyoffice", return_value=None),
                mock.patch.object(ocr, "_find_powerpoint", return_value=None),
                mock.patch.object(ocr, "_esegui_converter") as converter,
                mock.patch.object(ocr, "_pdf_valido", return_value=True),
                mock.patch.object(ocr, "_file_md5", return_value="abc"),
            ):
                converter.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
                # Anche se la riconversione non riesce, il PDF degradato NON
                # deve essere riusato: deve esserci almeno un tentativo.
                with self.assertRaises(RuntimeError):
                    ocr.convert_presentation_to_pdf(ppt, base)
            converter.assert_called()

    def test_comando_di_fallback_virgoletta_il_percorso_con_spazi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ppt = Path(tmp) / "deck.pptx"
            ppt.write_bytes(b"ppt")
            with (
                mock.patch.object(ocr, "_find_soffice", return_value=None),
                mock.patch.object(ocr, "_find_genoffice", return_value=None),
                mock.patch.object(ocr, "_find_onlyoffice", return_value=None),
                mock.patch.object(ocr, "_find_powerpoint", return_value=None),
                self.assertRaises(RuntimeError) as ctx,
            ):
                ocr.convert_presentation_to_pdf(ppt, Path(tmp))
        comando_atteso = 'py -3.11 "' + str(ocr.config_module_path())
        self.assertIn(comando_atteso, str(ctx.exception))


class TestTestoNativo(unittest.TestCase):
    @staticmethod
    def _pdf(base: Path, pagine: list[tuple[str, bytes | None]]) -> Path:
        import fitz

        pdf = base / "deck.pdf"
        doc = fitz.open()
        for testo, immagine in pagine:
            pagina = doc.new_page(width=300, height=400)
            pagina.insert_text((20, 40), testo)
            if immagine is not None:
                pagina.insert_image(fitz.Rect(20, 60, 280, 350), stream=immagine)
        doc.save(str(pdf))
        doc.close()
        return pdf

    def test_titolo_breve_sopravvive_a_ocr_vuoto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pdf = self._pdf(base, [("Domande?", None)])
            falso = mock.Mock()
            falso.image_to_string.return_value = "   "
            with (
                mock.patch.object(ocr, "_tesseract", return_value=falso),
                mock.patch.object(ocr, "_lingue_disponibili", return_value=["ita"]),
            ):
                _, testi = ocr.extract_slides_text_ocr(pdf, base / "out", dpi=72, workers=1)
        self.assertIn("Domande?", testi[0])

    def test_didascalia_e_testo_dalla_immagine_coessistono(self) -> None:
        import fitz

        png = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
        png.clear_with(255)
        stream = png.tobytes("png")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pdf = self._pdf(
                base,
                [("Figure 1: andamento della curva nel tempo", stream)],
            )
            with (
                mock.patch.object(ocr, "_ocr_single_slide", return_value="TESTO DALL IMMAGINE"),
                mock.patch.object(ocr, "_lingue_disponibili", return_value=["ita"]),
            ):
                _, testi = ocr.extract_slides_text_ocr(pdf, base / "out", dpi=72, workers=1)
        self.assertIn("Figure 1", testi[0])
        self.assertIn("TESTO DALL IMMAGINE", testi[0])


class TestAbortCoerente(unittest.TestCase):
    def test_abort_gate_invalida_report_e_timeline_vecchi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            rapporto = base / "sync_report.json"
            rapporto.write_text(json.dumps({"segments": [{"slide": 1}]}), encoding="utf-8")
            vecchia = base / "timeline_finale.json"
            vecchia.write_text("[]", encoding="utf-8")
            with mock.patch.object(main, "CACHE_DIR", base), self.assertRaises(SystemExit):
                main._abort("gate non superato")
            dati = json.loads(rapporto.read_text(encoding="utf-8"))
            self.assertTrue(dati["senza_video"])
            self.assertEqual(dati["motivo_interruzione"], "gate non superato")
            self.assertFalse(vecchia.exists())

    def test_encoding_fallito_rimuove_timeline_nuova(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            timeline_file = base / "timeline_finale.json"
            timeline_file.write_text("[]", encoding="utf-8")
            report: dict[str, object] = {}
            with (
                mock.patch.object(main, "CACHE_DIR", base),
                mock.patch.object(main, "build_video", side_effect=OSError("encoding")),
                self.assertRaises(SystemExit),
            ):
                main._build_video_con_gestione(
                    report, [], [], Path("a.wav"), Path("v.mp4"),
                    engine="ffmpeg", encoder="libx264", transitions=0.0, reuse=False,
                    encoder_out=[], reused_out=[], video_generato=True,
                )
            self.assertFalse(timeline_file.exists())
            self.assertEqual(report.get("motivo_interruzione"), "encoding_fallito")


if __name__ == "__main__":
    unittest.main()
