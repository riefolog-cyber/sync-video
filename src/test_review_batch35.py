#!/usr/bin/env python3
"""Regression del Batch 35 (review: trascrizione, cache, OCR, report).

Ogni test nasce da un difetto verificato: input sporco che faceva crashare,
cache accettata con identita' sbagliata, vettori non finiti letti come segnale
perfetto, slide ripetute confrontate con un confine che non era il loro.

Esegui con: python -m unittest test_review_batch35 -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import numpy as np

import chunks
import config
import ocr
import pipeline_inputs
import pipeline_report
import riepilogo
import semantic_sync
import subtitles
import transcription
from pipeline_review import _review_boundary_doubts
from timeline import announcements_off_timeline, repetition_loops
from transcription import _collect_words, _prepara_ripresa


class FakeSegment:
    """Il minimo di faster-whisper che `_collect_words` legge."""

    def __init__(self, parole: list[tuple[str, float]], testo: str, inizio: float, fine: float) -> None:
        self.words = [SimpleNamespace(word=w, start=t) for w, t in parole]
        self.text = testo
        self.start = inizio
        self.end = fine


class TestRipresaTrascrizione(unittest.TestCase):
    def test_parola_persa_nella_sovrapposizione_viene_recuperata(self) -> None:
        # Il parziale arrivava a 10s, la ripresa riascolta da 7s. Una parola
        # saltata dal decoder veloce a 8.5s va tenuta: il vecchio limite
        # "ultima parola salvata" la cancellava per sempre.
        seg = FakeSegment([("recuperata", 1.5), ("nuova", 3.0)], "recuperata nuova", 1.5, 3.5)
        out = _collect_words(
            [seg], offset=7.0, precedenti=[{"word": "fine", "start": 10.0}]
        )
        self.assertEqual([w["word"] for w in out], ["recuperata", "nuova"])
        self.assertEqual([w["start"] for w in out], [8.5, 10.0])

    def test_doppione_del_giunto_resta_scartato(self) -> None:
        seg = FakeSegment([("fine", 3.0)], "fine", 3.0, 3.3)
        out = _collect_words([seg], offset=7.0, precedenti=[{"word": "fine", "start": 10.0}])
        self.assertEqual(out, [])

    def test_parola_attaccata_al_taglio_non_e_un_doppione(self) -> None:
        # Il decoder della coda assegna la sua prima parola esattamente a
        # `offset` (7.89s), anche quando e' un doppione spostato di "La" a
        # 9.69s: il confronto col testo non lo becca perche' dista 1.8s.
        # Con il limite esclusivo la ripresa restituiva 58 parole invece di
        # 57 (test lento test_ripresa_equivalente, nightly del 27/11/2026).
        seg = FakeSegment([("La", 0.0), ("ha", 3.02)], "La ha", 0.0, 3.3)
        out = _collect_words(
            [seg],
            offset=7.89,
            precedenti=[{"word": "La", "start": 9.69}, {"word": "libertà", "start": 9.91}],
        )
        self.assertEqual([w["word"] for w in out], ["ha"])

    def test_senza_ripresa_la_parola_a_zero_non_si_perde(self) -> None:
        seg = FakeSegment([("ciao", 0.0)], "ciao", 0.0, 0.3)
        out = _collect_words([seg])
        self.assertEqual([w["word"] for w in out], ["ciao"])

    def test_parziale_con_tempo_stringa_viene_normalizzato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            dati = {
                "audio": str(audio),
                "firma": "f",
                "params": None,
                "words": [{"word": "ciao", "start": "50.0"}],
                "coperto_fino": 52.0,
            }
            with (
                mock.patch.object(transcription, "_firma_audio", return_value="f"),
                mock.patch.object(transcription, "_leggi_parziale", return_value=dati),
                mock.patch.object(transcription, "_taglia_audio", return_value=audio),
            ):
                parole, _, taglio = _prepara_ripresa(audio)
            self.assertEqual(parole, [{"word": "ciao", "start": 50.0}])
            self.assertGreater(taglio, 0.0)

    def test_parziale_con_parola_non_stringa_riparte_da_capo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            dati = {
                "audio": str(audio),
                "firma": "f",
                "words": [{"word": 123, "start": 50.0}],
                "coperto_fino": 52.0,
            }
            with (
                mock.patch.object(transcription, "_firma_audio", return_value="f"),
                mock.patch.object(transcription, "_leggi_parziale", return_value=dati),
            ):
                parole, ripreso, taglio = _prepara_ripresa(audio)
            self.assertEqual(parole, [])
            self.assertEqual(ripreso, audio)
            self.assertEqual(taglio, 0.0)


class TestElencoMateriali(unittest.TestCase):
    def test_un_file_bloccato_non_svuota_lelenco(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a.mp3").write_bytes(b"a")
            (base / "b.pdf").write_bytes(b"b")
            (base / "c.pptx").write_bytes(b"c")
            reale = Path.is_file

            def is_file_selettivo(self: Path, *a: Any, **k: Any) -> Any:
                if self.name == "b.pdf":
                    raise OSError("bloccato")
                return reale(self, *a, **k)

            with mock.patch.object(Path, "is_file", is_file_selettivo):
                nomi, altri = pipeline_inputs._nomi_file_visti(base)
        self.assertEqual(nomi, ["a.mp3", "c.pptx"])
        self.assertEqual(altri, 0)


class TestTempiSporchiTimeline(unittest.TestCase):
    def test_repetition_loops_non_esplode_su_tempo_non_numerico(self) -> None:
        parole: list[dict[str, object]] = [
            {"word": "la", "start": "boh"},
            {"word": "la", "start": float("nan")},
        ] * 6
        zone = repetition_loops(cast("Any", parole), min_repeats=6, max_cycle=1)
        self.assertEqual(len(zone), 1)
        self.assertEqual(zone[0], (0.0, 0.0))


class TestCacheSlideOCR(unittest.TestCase):
    def test_png_troncata_non_e_valida(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            buona = base / "buona.png"
            Image.new("RGB", (8, 8), "red").save(buona)
            troncata = base / "troncata.png"
            troncata.write_bytes(buona.read_bytes()[:40])
            self.assertTrue(ocr.slide_png_valida(buona))
            self.assertFalse(ocr.slide_png_valida(troncata))
            self.assertFalse(ocr.slide_png_valida(base / "assente.png"))

    def test_tetto_pixel_non_aggirato_dal_pavimento_72(self) -> None:
        class PixFinta:
            def __init__(self, dpi: int) -> None:
                self.dpi = dpi
                self.w = max(1, int(20000 * dpi / 72))
                self.h = max(1, int(20000 * dpi / 72))

            def save(self, path: str) -> None:
                Path(path).write_bytes(b"png")

        class PaginaFinta:
            rect = SimpleNamespace(width=20000.0, height=20000.0)

            def get_pixmap(self, dpi: int) -> PixFinta:
                return PixFinta(dpi)

        class DocFake:
            def load_page(self, n: int) -> PaginaFinta:
                return PaginaFinta()

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "poster.pdf"
            pdf.write_bytes(b"pdf")
            out = Path(tmp) / "slide_000.png"
            with mock.patch.object(ocr.fitz, "open", return_value=DocFake()):
                _, dpi = ocr._render_page((str(pdf), 0, str(out), 300))
        self.assertLess(dpi, 72)
        self.assertLessEqual(
            int(20000 * dpi / 72) ** 2, ocr.MAX_RENDER_PIXELS
        )

    def test_una_pagina_illeggibile_non_butta_via_le_altre(self) -> None:
        class DocFake:
            def __len__(self) -> int:
                return 3

            def load_page(self, i: int) -> Any:
                if i == 1:
                    raise RuntimeError("pagina strana")
                return SimpleNamespace(get_text=lambda _mode: f"testo {i}")

        testi = ocr._testo_nativo(DocFake())
        self.assertEqual(len(testi), 3)
        self.assertIn("testo 0", testi[0])
        self.assertEqual(testi[1], "")
        self.assertIn("testo 2", testi[2])


class TestCacheEmbedding(unittest.TestCase):
    def _fn(self, nome: str) -> Any:
        def embed(texts: Any) -> np.ndarray:
            return np.eye(len(texts), dtype=np.float32)

        embed.embed_id = nome  # type: ignore[attr-defined]
        return embed

    def _scrivi_npz(self, cartella: Path, testi: list[str], modello: str) -> str:
        chiave = semantic_sync._embed_cache_key(modello, testi)
        np.savez(
            cartella / f"emb_{chiave}.npz",
            emb=np.eye(len(testi), dtype=np.float32),
            seconds=np.float32(1.0),
            keys=np.array([semantic_sync._chiave_testo(t) for t in testi], dtype="U16"),
            model=np.array(modello),
        )
        return chiave

    def test_hit_esatto_con_modello_diverso_viene_ricalcolato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp)
            self._scrivi_npz(cartella, ["alfa", "beta"], "MODELLO_SBAGLIATO")
            with (
                mock.patch.object(semantic_sync, "_EMBED_CACHE_DIR", cartella),
                mock.patch.object(semantic_sync, "_EMBED_MEMO", {}),
                mock.patch.object(semantic_sync, "_EMBED_CACHE_ENABLED", True),
            ):
                self.assertIsNone(
                    semantic_sync._embed_list_cached(
                        self._fn("GIUSTO"), ["alfa", "beta"], "probe"
                    )
                )

    def test_hit_esatto_con_stesse_chiavi_viene_riusato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp)
            self._scrivi_npz(cartella, ["alfa", "beta"], "GIUSTO")
            with (
                mock.patch.object(semantic_sync, "_EMBED_CACHE_DIR", cartella),
                mock.patch.object(semantic_sync, "_EMBED_MEMO", {}),
                mock.patch.object(semantic_sync, "_EMBED_CACHE_ENABLED", True),
            ):
                arr = semantic_sync._embed_list_cached(
                    self._fn("GIUSTO"), ["alfa", "beta"], "probe"
                )
            self.assertIsNotNone(arr)
            np.testing.assert_array_equal(arr, np.eye(2, dtype=np.float32))

    def test_vettori_non_finiti_non_sono_un_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp)
            chiave = semantic_sync._embed_cache_key("GIUSTO", ["alfa", "beta"])
            np.savez(
                cartella / f"emb_{chiave}.npz",
                emb=np.array([[np.nan, 0.0], [0.0, 1.0]], dtype=np.float32),
                seconds=np.float32(1.0),
                keys=np.array(
                    [semantic_sync._chiave_testo(t) for t in ("alfa", "beta")], dtype="U16"
                ),
                model=np.array("GIUSTO"),
            )
            with (
                mock.patch.object(semantic_sync, "_EMBED_CACHE_DIR", cartella),
                mock.patch.object(semantic_sync, "_EMBED_MEMO", {}),
                mock.patch.object(semantic_sync, "_EMBED_CACHE_ENABLED", True),
            ):
                self.assertIsNone(
                    semantic_sync._embed_list_cached(
                        self._fn("GIUSTO"), ["alfa", "beta"], "probe"
                    )
                )


class TestSegnaleNonFinito(unittest.TestCase):
    def test_nan_non_diventa_allineamento_perfetto(self) -> None:
        sim = np.array([[np.nan, 0.2], [0.3, 0.4]])
        report = semantic_sync.signal_quality_report(sim)
        self.assertEqual(report["concordance"], 0.0)
        self.assertEqual(report["confusability"], 1.0)

    def test_embedding_non_finito_non_da_duplicati_zero(self) -> None:
        self.assertEqual(
            semantic_sync.confusability_slides(np.array([[np.nan, np.nan]])), 1.0
        )

    def test_embed_and_report_rifiuta_sim_non_finita(self) -> None:
        arr = np.array([[np.nan, 0.0], [0.0, 1.0]], dtype=np.float32)
        with mock.patch.object(semantic_sync, "_embed_list", side_effect=[arr, arr]):
            esito = semantic_sync._embed_and_report(
                ["slide 1", "slide 2"], [{"text": "blocco"}], 2, self._fn(), "probe"
            )
        self.assertIsNone(esito)

    @staticmethod
    def _fn() -> Any:
        def embed(texts: Any) -> np.ndarray:
            return np.eye(len(texts), dtype=np.float32)

        embed.embed_id = "probe"  # type: ignore[attr-defined]
        return embed


class TestFinestreConDurataSporca(unittest.TestCase):
    def test_durata_infinita_non_imprigiona_la_run(self) -> None:
        parole: list[Any] = [{"word": "a", "start": 1.0}]
        for durata in (float("inf"), float("nan"), -1.0):
            with self.subTest(durata=durata):
                finestre = chunks.build_windows(parole, durata, 4.0)
                self.assertLessEqual(len(finestre), 2)
                self.assertTrue(all(f["end"] <= 5.0 + 1e-6 for f in finestre))


class TestEnvSemanticCoerenteConCli(unittest.TestCase):
    def test_valori_negativi_tornano_al_default(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "SEMANTIC_MIN_DURATION": "-5",
                "SEMANTIC_MIN_Z": "-5",
                "SEMANTIC_TEMPERATURE": "-5",
            },
        ):
            self.assertEqual(config._env_float_con_minimo("SEMANTIC_MIN_DURATION", 3.0, 0.0), 3.0)
            self.assertEqual(config._env_float_con_minimo("SEMANTIC_MIN_Z", 0.45, 0.0), 0.45)
            self.assertEqual(
                config._env_float_con_minimo("SEMANTIC_TEMPERATURE", 0.15, 0.0, incluso=False),
                0.15,
            )


class TestDiagnosticaRefineIndicizzata(unittest.TestCase):
    def test_seconda_occorrenza_non_cancella_la_prima(self) -> None:
        semantic_sync.reset_refine_diagnostics()
        try:
            semantic_sync._record_refine(1, 2, by_index=True, decision="A")
            semantic_sync._record_refine(2, 2, by_index=True, decision="B")
            semantic_sync._record_refine(1, 2, by_index=True, decision="C")
            voci = semantic_sync.refine_diagnostics()
            self.assertEqual([v["decision"] for v in voci], ["C", "B"])
        finally:
            semantic_sync.reset_refine_diagnostics()


class TestSottotitoliRobusti(unittest.TestCase):
    def test_tempo_non_finito_non_fa_perdere_srt_e_capitoli(self) -> None:
        parole: list[dict[str, object]] = [
            {"word": "ciao", "start": 0.0},
            {"word": "mondo", "start": float("nan")},
            {"word": "fine", "start": float("inf")},
        ]
        srt = subtitles.build_srt(cast("Any", parole), durata=10.0)
        self.assertIn("ciao", srt)
        self.assertEqual(subtitles.timestamp_srt(float("nan")), "00:00:00,000")

    def test_errore_nei_capitoli_non_lascia_un_srt_dichiarato_non_scritto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video_finale.mp4"
            parole = [{"word": "ciao", "start": 0.0}]
            with mock.patch.object(subtitles, "build_capitoli", side_effect=ValueError("BAD")):
                srt, cap = subtitles.scrivi_sottotitoli(
                    video, parole, {1: 0.0}, ["slide 1"], durata=10.0
                )
            self.assertIsNone(srt)
            self.assertIsNone(cap)
            self.assertFalse((Path(tmp) / "video_finale.srt").exists())


class TestRiepilogoAbort(unittest.TestCase):
    def test_abort_prima_del_video_non_dice_assente(self) -> None:
        report: dict[str, object] = {
            "segments": [{"slide": 1, "start": 0.0, "end": 10.0}],
            "motivo_interruzione": "audio sostituito durante la run",
        }
        testo = riepilogo.riepilogo_markdown(
            report, "video_finale.mp4", video_presente=False
        )
        self.assertNotIn("assente dalla cartella", testo)
        self.assertIn("nessun video generato per questa timeline", testo)

    def test_assenza_verificata_resta_assente(self) -> None:
        report: dict[str, object] = {
            "segments": [{"slide": 1, "start": 0.0, "end": 10.0}],
        }
        testo = riepilogo.riepilogo_markdown(
            report, "video_finale.mp4", video_presente=False
        )
        self.assertIn("assente dalla cartella", testo)


class TestFormatTime(unittest.TestCase):
    def test_ore_e_negativi(self) -> None:
        self.assertEqual(pipeline_report.format_time(59.9), "59s")
        self.assertEqual(pipeline_report.format_time(3600), "1h00m00s")
        self.assertEqual(pipeline_report.format_time(7261), "2h01m01s")
        self.assertEqual(pipeline_report.format_time(-1.2), "0s")


class TestSlideRipetuteConfrontoAnnunci(unittest.TestCase):
    def test_doppia_occorrenza_non_genera_dubbio_falso(self) -> None:
        # Con --skip-slides la slide 3 compare due volte (40s e 60s) ma e'
        # annunciata una volta sola: il confronto con l'ultima occorrenza
        # diceva "video in ritardo" su un confine mai annunciato.
        parole: list[dict[str, object]] = [
            {"word": "passiamo", "start": 40.0},
            {"word": "alla", "start": 40.2},
            {"word": "slide", "start": 40.4},
            {"word": "3", "start": 40.6},
        ]
        starts = {1: 0.0, 2: 20.0, 3: 60.0, 5: 80.0}
        alternative = {3: [40.0, 60.0]}
        self.assertNotEqual(
            announcements_off_timeline(cast("Any", parole), starts, 5), []
        )
        self.assertEqual(
            announcements_off_timeline(
                cast("Any", parole), starts, 5, starts_alternativi=alternative
            ),
            [],
        )

    def test_review_esclude_le_slide_ripetute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            report: dict[str, object] = {
                "segments": [
                    {"slide": 1, "start": 0.0, "end": 20.0},
                    {"slide": 2, "start": 20.0, "end": 40.0},
                    {"slide": 3, "start": 40.0, "end": 60.0},
                    {"slide": 3, "start": 60.0, "end": 80.0},
                    {"slide": 5, "start": 80.0, "end": 100.0},
                ],
                "transcript_cache_key": "transcript_probe",
            }
            (cache / "sync_report.json").write_text(json.dumps(report), encoding="utf-8")
            (cache / "transcript_probe.json").write_text(
                json.dumps(
                    {
                        "words_raw": [
                            {"word": "passiamo", "start": 40.0},
                            {"word": "alla", "start": 40.2},
                            {"word": "slide", "start": 40.4},
                            {"word": "3", "start": 40.6},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertLogs("slide2video", level="INFO") as logs:
                _review_boundary_doubts(cache / "video_assente.mp4", cache_dir=cache)
            out = "\n".join(logs.output)
        self.assertNotIn("slide 3 viene annunciata", out)
        self.assertIn("dalle parole in cache", out)


if __name__ == "__main__":
    unittest.main()
