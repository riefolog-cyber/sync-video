#!/usr/bin/env python3
"""Regressioni del controllo "audio cambiato durante la run".

Il 26/11 il podcast e' stato rigenerato mentre la pipeline lavorava: la
trascrizione (e quindi la timeline) apparteneva alla versione precedente e il
video e' usato fuori sincrono. Questi test coprono i due controlli aggiunti.

Esegui con: python -m unittest test_audio_race -v
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import main
import pipeline_cache
import pipeline_review
import riepilogo
import transcription
from chunks import Word


class TestAudioCambiato(unittest.TestCase):
    def test_file_invariato(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio originale")
            hash_audio = main._file_hash(audio)
            self.assertFalse(main._audio_cambiato_durante_la_run(audio, hash_audio))

    def test_file_sostituito(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"versione vecchia")
            vecchia = main._file_hash(audio)
            # Stesso nome, stessa estensione, contenuto diverso.
            audio.write_bytes(b"versione nuova piu' lunga")
            self.assertTrue(main._audio_cambiato_durante_la_run(audio, vecchia))

    def test_file_scomparso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            audio.unlink()  # il file NON c'e' piu': conta come cambiamento
            self.assertTrue(main._audio_cambiato_durante_la_run(audio, "hash"))

    def test_file_illeggibile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            with mock.patch.object(main, "_file_hash", side_effect=OSError("lock")):
                self.assertTrue(main._audio_cambiato_durante_la_run(audio, "hash"))


class TestCoperturaTrascrizione(unittest.TestCase):
    @staticmethod
    def _parole(*secondi: float) -> list[Word]:
        return [cast("Word", {"word": "ciao", "start": t}) for t in secondi]

    def test_trascrizione_completa(self) -> None:
        self.assertEqual(main._coda_audio_senza_parole(self._parole(0, 60, 119.5), 120.0), 0.5)

    def test_trascrizione_troncata(self) -> None:
        # Il caso reale: testo fino a 860s, audio di 1198s.
        self.assertAlmostEqual(
            main._coda_audio_senza_parole(self._parole(0, 400, 860.0), 1198.34), 338.34, places=2
        )

    def test_trascrizione_vuota(self) -> None:
        self.assertEqual(main._coda_audio_senza_parole([], 120.0), 120.0)

    def test_parole_sporche_non_crashano(self) -> None:
        parole = [cast("Word", {"word": "x", "start": "n/d"}), cast("Word", {"word": "y"})]
        self.assertEqual(main._coda_audio_senza_parole(parole, 100.0), 100.0)

    def test_timestamp_non_finito_non_disattiva_il_controllo(self) -> None:
        for non_finito in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(valore=non_finito):
                parole = [
                    cast("Word", {"word": "x", "start": non_finito}),
                    cast("Word", {"word": "y", "start": 100.0}),
                ]
                self.assertEqual(main._coda_audio_senza_parole(parole, 120.0), 20.0)


class TestHashUnaSolaVolta(unittest.TestCase):
    def test_salva_parziale_calcola_la_firma_una_volta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            with (
                mock.patch.object(transcription, "CACHE_DIR", Path(tmp)),
                mock.patch.object(
                    transcription, "_firma_audio", wraps=transcription._firma_audio
                ) as firma,
            ):
                transcription._salva_parziale(audio, [], coperto_fino=10.0, params=None)
            self.assertEqual(firma.call_count, 1)


class TestParzialeContenuto(unittest.TestCase):
    def test_parziale_di_altra_versione_stesso_nome_dimensione_e_mtime(self) -> None:
        import config

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            audio = cache / "podcast.m4a"
            # >2 MB: le vecchie finestre da 256 KB non vedevano il mezzo.
            originale = bytearray(b"A" * (3 * 1024 * 1024))
            audio.write_bytes(originale)
            stat = audio.stat()
            with (
                mock.patch.object(config, "CACHE_DIR", cache),
                mock.patch.object(transcription, "CACHE_DIR", cache),
                mock.patch.object(transcription, "_taglia_audio", return_value=cache / "coda.flac"),
                mock.patch.object(
                    transcription, "_firma_audio", wraps=transcription._firma_audio
                ) as firma,
            ):
                transcription._salva_parziale(
                    audio, [{"word": "vecchia", "start": 1.0}], coperto_fino=60.0, params=None
                )
                vecchia_firma = firma.call_args_list[-1].args[0] and str(
                    transcription._firma_audio(audio)
                )
                # Stesso percorso, dimensione e mtime: cambia SOLO il mezzo.
                originale[len(originale) // 2] = ord("B")
                audio.write_bytes(originale)
                os.utime(audio, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                self.assertNotEqual(transcription._firma_audio(audio), vecchia_firma)
                # Il parziale e' scritto con la firma VECCHIA ma nel file della
                # firma NUOVA: cosi' si esercita il controllo sul campo "firma".
                percorso = transcription.percorso_parziale(audio)
                percorso.write_text(
                    json.dumps(
                        {
                            "audio": str(audio),
                            "firma": vecchia_firma,
                            "words": [{"word": "estranea", "start": 1.0}],
                            "coperto_fino": 60.0,
                        }
                    ),
                    encoding="utf-8",
                )
                iniziali, _sorgente, _offset = transcription._prepara_ripresa(audio, params=None)
            self.assertEqual(iniziali, [])
            # Il parziale con la firma vecchia resta come artefatto inerte:
            # la protezione e' che NON venga usato per la ripresa.
            dati_parziale = json.loads(percorso.read_text(encoding="utf-8"))
            self.assertEqual(dati_parziale["firma"], vecchia_firma)


class TestMotivoInterruzione(unittest.TestCase):
    def test_abort_registra_il_motivo(self) -> None:
        report: dict[str, object] = {"segments": []}
        with (
            mock.patch.object(main, "_save_sync_report") as salvataggio,
            self.assertRaises(SystemExit),
        ):
            main._abort_senza_video(report, "test", motivo="audio_sostituito_durante_la_run")
        self.assertIs(report["senza_video"], True)
        self.assertEqual(report["motivo_interruzione"], "audio_sostituito_durante_la_run")
        salvataggio.assert_called_once_with(report)

    def test_abort_aggiorna_la_pagina_vecchia(self) -> None:
        report: dict[str, object] = {
            "segments": [{"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0}],
        }
        output = Path("video_finale.mp4")
        with (
            mock.patch.object(main, "_save_sync_report"),
            mock.patch("riepilogo.scrivi_riepilogo") as pagina,
            self.assertRaises(SystemExit),
        ):
            main._abort_senza_video(
                report, "test", motivo="audio_sostituito_durante_la_run", output_path=output
            )
        pagina.assert_called_once()
        self.assertEqual(pagina.call_args.args[1], output)

    def test_riepilogo_non_dice_dry_run_dopo_un_abort(self) -> None:
        report: dict[str, object] = {
            "segments": [{"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0}],
            "senza_video": True,
            "motivo_interruzione": "audio_sostituito_durante_la_run",
        }
        testo = riepilogo.riepilogo_markdown(report, "lezione.mp4", "26/11/2026", dry_run=True)
        self.assertNotIn("dry-run", testo)
        self.assertIn("podcast sostituito", testo)

    def test_abort_dopo_rendering_conserva_la_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Path(tmp) / "timeline_finale.json"
            timeline.write_text("[]", encoding="utf-8")
            video = Path(tmp) / "video_finale.mp4"
            video.write_bytes(b"video")
            with (
                mock.patch.object(main, "CACHE_DIR", Path(tmp)),
                mock.patch.object(main, "_save_sync_report"),
                mock.patch("riepilogo.scrivi_riepilogo") as pagina,
                self.assertRaises(SystemExit),
            ):
                main._abort_senza_video(
                    {"segments": []},
                    "test",
                    motivo="audio_sostituito_durante_la_run",
                    output_path=video,
                    video_generato=True,
                )
            self.assertTrue(timeline.exists())
            self.assertIs(pagina.call_args.kwargs["video_presente"], True)

    def test_pagina_di_abort_usa_la_data_di_oggi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video_finale.mp4"
            video.write_bytes(b"video")
            with (
                mock.patch.object(main, "_save_sync_report"),
                mock.patch("riepilogo.scrivi_riepilogo") as pagina,
                self.assertRaises(SystemExit),
            ):
                main._abort_senza_video(
                    {"segments": []}, "test", motivo="audio_sostituito_durante_la_run", output_path=video,
                )
            self.assertEqual(pagina.call_args.kwargs["data"], time.strftime("%d/%m/%Y"))

    def test_abort_rimuove_la_timeline_non_piu_valida(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Path(tmp) / "timeline_finale.json"
            timeline.write_text("[]", encoding="utf-8")
            with (
                mock.patch.object(main, "CACHE_DIR", Path(tmp)),
                mock.patch.object(main, "_save_sync_report"),
                mock.patch("riepilogo.scrivi_riepilogo"),
                self.assertRaises(SystemExit),
            ):
                main._abort_senza_video({"segments": []}, "test", motivo="audio_sostituito_durante_la_run")
            self.assertFalse(timeline.exists())

    def test_motivi_stabili_riconosciuti_da_review_e_riepilogo(self) -> None:
        for motivo in ("allineamento_sospetto", "prima_sezione_disallineata"):
            with self.subTest(motivo=motivo):
                testo = riepilogo.riepilogo_markdown(
                    {"segments": [], "motivo_interruzione": motivo}, "lezione.mp4", "26/11/2026"
                )
                self.assertIn("interrotto", testo)
                self.assertIn(motivo.replace("_", " "), testo)

    def test_motivi_stabili_spiegati_da_review(self) -> None:
        for motivo, spiegazione in (
            ("allineamento_sospetto", "allineamento delle slide era sospetto"),
            ("prima_sezione_disallineata", "prima sezione non parlava della slide 1"),
        ):
            with self.subTest(motivo=motivo), tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                video = cache / "video.mp4"
                video.write_bytes(b"video")
                pipeline_cache.save_sync_report(
                    {
                        "segments": [{"slide": 1, "start": 0.0, "end": 60.0}],
                        "senza_video": True,
                        "motivo_interruzione": motivo,
                    },
                    cache_dir=cache,
                )
                with self.assertLogs("slide2video", level="ERROR") as log:
                    codice = pipeline_review._review_boundary_doubts(video, cache_dir=cache)
                self.assertEqual(codice, 3)
                self.assertIn(spiegazione, "\n".join(log.output))

    def test_riepilogo_avvisa_la_coda_senza_parole(self) -> None:
        report: dict[str, object] = {
            "segments": [
                {"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0},
                {"slide": 2, "start": 60.0, "end": 120.0, "duration": 60.0},
            ],
            "anchors": {"transitions": 1, "anchored": 1},
            "coda_senza_parole": 338.3,
        }
        testo = riepilogo.riepilogo_markdown(report, "lezione.mp4", "26/11/2026")
        self.assertIn("prima della fine dell'audio", testo)
        self.assertNotIn("Niente: tutti i confini", testo)

    def test_coda_al_di_la_di_sessanta_non_arrotondata_via(self) -> None:
        report: dict[str, object] = {
            "segments": [{"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0}],
            "coda_senza_parole": 60.4,
        }
        testo = riepilogo.riepilogo_markdown(report, "lezione.mp4", "26/11/2026")
        self.assertIn("prima della fine dell'audio", testo)


if __name__ == "__main__":
    unittest.main()
