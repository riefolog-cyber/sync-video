#!/usr/bin/env python3
"""Regression del Batch 38 (script, avvio, trascrizione, report).

Difetti verificati: baseline del monitor modelli cancellata da un 404,
opzioni CLI senza valore che verificano il video sbagliato, apostrofi nei
percorsi che rompono i .bat, numeri ASR punteggiati buttati via, segmenti
sparsi dal bundle diagnostico.

Esegui con: python -m unittest discover -s src -p 'test_review_batch38.py' -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import config
import diagnostica
import pipeline_review
import riepilogo
import subtitles
import transcription
from chunks import Word
from transcription import _collect_words, _compress_words, ultima_ripresa

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "src" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import check_embedding_models as cem  # noqa: E402
import check_fastembed_upgrade as cfu  # noqa: E402
import prova_materiale_reale as pmr  # noqa: E402


class TestScriptAusiliari(unittest.TestCase):
    def test_404_non_cancella_la_baseline_del_modello_preferito(self) -> None:
        vecchio = {
            "tracked": {
                cem.PREFERRED_MODEL: {"lastModified": "2026-01-01T00:00:00", "downloads": 5}
            }
        }
        nuovo = {
            "tracked": {
                cem.PREFERRED_MODEL: {"lastModified": None, "downloads": None, "error": "404"},
                "altro/modello": {"lastModified": "2026-02-01", "downloads": 1},
            }
        }
        scritto: dict[str, Any] = {}

        def falso_write(path: Path, testo: str) -> None:
            scritto["testo"] = testo

        with (
            mock.patch.object(cem, "_load_state", return_value=vecchio),
            mock.patch.object(cem, "_atomic_write", side_effect=falso_write),
            mock.patch.object(cem, "_utcnow", return_value="ora"),
        ):
            cem._save_state(nuovo)
        dati = json.loads(scritto["testo"])
        self.assertEqual(
            dati["tracked"][cem.PREFERRED_MODEL]["lastModified"], "2026-01-01T00:00:00"
        )

    def test_cache_con_forma_sbagliata_non_fa_schattare_la_revisione(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            slides = base / "slides_a.json"
            slides.write_text("[]", encoding="utf-8")
            transcript = base / "transcript_a.json"
            transcript.write_text(
                json.dumps({"words_raw": [{"word": "ciao", "start": 1.0}]}), encoding="utf-8"
            )
            esito = cfu.collect_real_texts(slide_cache=slides, transcript_cache=transcript)
        self.assertIsNone(esito)

    def test_aggiorna_rifiuta_riferimento_vuoto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            riferimento = Path(tmp) / "rif.json"
            with (
                mock.patch.object(pmr, "_timeline", return_value=[]),
                mock.patch.object(pmr, "RIFERIMENTO", riferimento),
            ):
                esito = pmr.main(["--aggiorna"])
            self.assertEqual(esito, pmr.ESITO_NON_VERIFICABILE)
            self.assertFalse(riferimento.exists())

    def test_video_senza_valore_viene_rifiutato(self) -> None:
        sorgente = (SCRIPTS / "analysis_sync.py").read_text(encoding="utf-8")
        self.assertIn("_valore.startswith(\"-\")", sorgente)
        self.assertIn("sys.exit(2)", sorgente)


class TestBatAvvio(unittest.TestCase):
    def test_percorso_sorgente_non_interpolato_nella_sonda(self) -> None:
        sorgente = (ROOT / "_python.bat").read_text(encoding="utf-8")
        self.assertNotIn("r'%~dp0src'", sorgente)
        self.assertIn("os.environ.get('SYNC_VIDEO_SRC'", sorgente)

    def test_venv_sondata_prima_dell_uso(self) -> None:
        sorgente = (ROOT / "_python.bat").read_text(encoding="utf-8")
        ramo = sorgente.split('if exist "%~dp0.venv\\Scripts\\python.exe" (', 1)[1].split(")", 1)[0]
        self.assertIn("-c \"%_SONDA%\"", ramo)

    def test_download_powershell_usa_la_variabile_dambiente(self) -> None:
        sorgente = (ROOT / "_python.bat").read_text(encoding="utf-8")
        riga = next(r for r in sorgente.splitlines() if "Invoke-WebRequest" in r)
        self.assertNotIn("%_SETUP_EXE%", riga)
        self.assertIn("$env:SYNC_SETUP_EXE", riga)

    def test_pulisci_controlla_tutti_i_del(self) -> None:
        righe = (ROOT / "pulisci.bat").read_text(encoding="utf-8").splitlines()
        for i, riga in enumerate(righe):
            if not riga.strip().lower().startswith("del "):
                continue
            seguenti = "\n".join(righe[i + 1 : i + 3])
            self.assertIn("errorlevel", seguenti, f"del senza controllo: {riga}")

    def test_guardia_python_prima_del_pip(self) -> None:
        sorgente = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
        funzione = sorgente.split("def installa_dipendenze_se_mancano()", 1)[1].split("\n\n\n", 1)[0]
        self.assertLess(funzione.index("_rifiuta_python_troppo_recente()"), funzione.index("_ensure_pip_packages()"))

    def test_fallback_turbo_non_e_quello_inesistente(self) -> None:
        modulo_finto = SimpleNamespace()
        with mock.patch.dict(sys.modules, {"faster_whisper.utils": modulo_finto}):
            repo = config.whisper_repo("turbo")
        self.assertIsNotNone(repo)
        self.assertIn("mobiuslabsgmbh", repo or "")


class TestTrascrizioneNumeri(unittest.TestCase):
    def test_numeri_punteggiati_sopravvivono_all_compressione(self) -> None:
        parole: list[Word] = [
            {"word": "slide", "start": 0.0},
            {"word": "3.", "start": 0.2},
            {"word": "4,", "start": 0.4},
        ]
        compatta = _compress_words(parole, frozenset(), min_word_len=4)
        self.assertIn("3.", compatta)
        self.assertIn("4,", compatta)

    def test_ripresa_usa_il_thread_corrente(self) -> None:
        osservati: dict[str, float] = {}

        def lavora(nome: str, valore: float) -> None:
            transcription.azzera_ripresa()
            transcription._STATO_RIPRESA.coperto = valore
            osservati[nome] = ultima_ripresa()

        t1 = threading.Thread(target=lavora, args=("a", 120.0))
        t2 = threading.Thread(target=lavora, args=("b", 45.0))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(osservati["a"], 120.0)
        self.assertEqual(osservati["b"], 45.0)

    def test_coperto_fino_non_regredisce_dopo_riascolto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            transcription.azzera_ripresa()
            transcription._STATO_RIPRESA.coperto_iniziale = 120.0
            seg = SimpleNamespace(
                words=[SimpleNamespace(word="x", start=1.2)],
                text="x",
                start=1.2,
                end=1.2,
            )
            with mock.patch.object(transcription, "_salva_parziale") as salva:
                _collect_words(
                    [seg] * 50,
                    audio,
                    offset=117.0,
                    precedenti=[{"word": "gia", "start": 119.0}],
                )
        self.assertGreaterEqual(salva.call_args.args[2], 120.0)

    def test_marker_gpu_usa_lock_per_il_read_modify_write(self) -> None:
        sorgente = (ROOT / "src" / "transcription.py").read_text(encoding="utf-8")
        self.assertIn("_ATTEMPT_LOCK = threading.RLock()", sorgente)
        blocco = sorgente.split("def _write_attempt(", 1)[1].split("def _clear_attempt(", 1)[0]
        self.assertIn("with _ATTEMPT_LOCK", blocco)

    def test_no_ripresa_non_scrive_checkpoint(self) -> None:
        sorgente = (ROOT / "src" / "transcription.py").read_text(encoding="utf-8")
        self.assertIn("checkpoint_path=audio_path if riprendi else None", sorgente)


class TestReportDiagnostica(unittest.TestCase):
    def test_bundle_conserva_i_segmenti_numerici(self) -> None:
        report = {
            "segments": [
                {"slide": 2, "start": 10.0, "end": 40.0, "duration": 30.0, "verdict": "coerente"}
            ]
        }
        pulito = diagnostica._solo_numeri(report)
        assert isinstance(pulito, dict)
        segmenti = pulito["segments"]
        assert isinstance(segmenti, list)
        self.assertEqual(segmenti[0]["duration"], 30.0)
        self.assertNotIn("verdict", segmenti[0])

    def test_review_non_verificabile_rimuove_anteprime_vecchie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "sync_report.json").write_text(
                json.dumps({"segments": [{"slide": 1, "start": 0.0, "end": 10.0}], "anchors": {}}),
                encoding="utf-8",
            )
            review = cache / "review"
            review.mkdir()
            vecchia = review / "confine_slide02_00.png"
            vecchia.write_bytes(b"vecchia")
            with self.assertLogs("slide2video", level="WARNING"):
                esito = pipeline_review._review_boundary_doubts(
                    cache / "video_assente.mp4", cache_dir=cache
                )
            self.assertEqual(esito, 3)
            self.assertFalse(vecchia.exists())

    def test_due_bundle_nello_stesso_secondo_hanno_nomi_diversi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            radice = Path(tmp)
            with (
                mock.patch.object(diagnostica.time, "strftime", return_value="20261127-999999"),
                mock.patch.object(diagnostica, "_righe_verifica", return_value=["riga"]),
            ):
                primo = diagnostica.scrivi_bundle(radice)
                secondo = diagnostica.scrivi_bundle(radice)
            self.assertNotEqual(primo, secondo)
            self.assertTrue(primo.exists())
            self.assertTrue(secondo.exists())

    def test_zero_pagine_non_e_una_sola_pagina(self) -> None:
        testo = riepilogo.riepilogo_markdown(
            {"segments": [], "motivo_interruzione": "encoding_fallito"}
        )
        self.assertNotIn("Una sola pagina", testo)
        self.assertIn("Nessuna pagina", testo)

    def test_riepilogo_non_punta_a_riga_inesistente(self) -> None:
        testo = riepilogo.riepilogo_markdown(
            {
                "segments": [
                    {"slide": 1, "start": 0.0, "end": 10.0},
                    {"slide": 2, "start": 10.0, "end": 20.0},
                ]
            }
        )
        self.assertNotIn("pagine senza annuncio", testo)


class TestTimestampCapitolo(unittest.TestCase):
    def test_tempo_non_finito_non_esplode(self) -> None:
        self.assertEqual(subtitles.timestamp_capitolo(float("nan")), "0:00")
        self.assertEqual(subtitles.timestamp_capitolo(float("inf")), "0:00")
        self.assertEqual(subtitles.timestamp_capitolo(cast("float", "boh")), "0:00")


if __name__ == "__main__":
    unittest.main()
