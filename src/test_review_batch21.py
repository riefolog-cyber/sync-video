#!/usr/bin/env python3
"""Regressioni della revisione batch 21.

Dopo due batch di correzioni, questa revisione ha coperto l'orchestrazione di
main.py, la trascrizione/gap, la diagnostica hardware e gli script/launcher.

Esegui con: python -m unittest test_review_batch21 -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import config
import machine_setup
import main
import pipeline_inputs
import pipeline_review
import transcription
import verifica_pc

ROOT = Path(__file__).resolve().parent.parent


class _Args:
    def __init__(self, **valori: object) -> None:
        self.transcriber = "auto"
        self.whisper_device = "cpu"
        self.whisper_compute_type = "int8"
        self.openvino_device = "CPU"
        self.openvino_model_dir = "x"
        self.video_encoder = "auto"
        self.whisper_model = "small"
        for chiave, valore in valori.items():
            setattr(self, chiave, valore)


class TestInputEAnteprime(unittest.TestCase):
    def test_find_audio_file_ignora_un_candidato_non_leggibile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cartella = Path(tmp)
            buono = cartella / "buono.m4a"
            rotto = cartella / "rotto.mp3"
            buono.write_bytes(b"audio")
            rotto.write_bytes(b"audio")
            stat_originale = Path.stat

            def stat_selettivo(self: Any, *a: Any, **k: Any) -> Any:
                if self.name == "rotto.mp3":
                    raise OSError("file bloccato")
                return stat_originale(self, *a, **k)

            with mock.patch.object(Path, "stat", stat_selettivo):
                trovato = pipeline_inputs.find_audio_file(cartella)
            self.assertEqual(trovato, buono)

    def test_zero_dubbi_rimuove_anteprime_vecchie(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review"
            review.mkdir()
            vecchia = review / "confine_slide03_00.png"
            vecchia.write_bytes(b"vecchia")
            pipeline_review._write_boundary_previews(Path("video.mp4"), [], review)
            self.assertFalse(vecchia.exists())

    def test_prefetch_fallito_esce_uno(self) -> None:
        with (
            mock.patch.object(main, "bootstrap"),
            mock.patch.object(main, "machine_setup"),
            mock.patch.object(main, "run_update_check"),
            mock.patch.object(main, "_risolvi_encoder_video", return_value="libx264"),
            mock.patch.object(main, "_prefetch_models", return_value=False),
        ):
            try:
                main.main(["--prefetch-models", "--no-update-check"])
            except SystemExit as eccezione:
                self.assertEqual(eccezione.code, 1)
            else:
                self.fail("un prefetch fallito deve fermare il programma")


class TestTrascrizione(unittest.TestCase):
    def test_gap_usa_flac_lossless(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("shutil.which", return_value="ffmpeg"),
            mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run,
        ):
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            transcription.retranscribe_zone(audio, 10.0, 20.0)
        comando = run.call_args.args[0]
        self.assertIn("-c:a", comando)
        self.assertIn("flac", comando)
        self.assertTrue(str(comando[-1]).endswith(".flac"))

    def test_marker_gpu_cancellato_dopo_trascrizione_cpu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "podcast.m4a"
            audio.write_bytes(b"audio")
            with mock.patch.object(transcription, "CACHE_DIR", Path(tmp)):
                transcription._write_attempt(audio)
                marker = transcription._openvino_attempt_path()
                self.assertTrue(marker.exists())
                with mock.patch.object(
                    transcription, "transcribe_with_whisper", return_value=("testo", [])
                ):
                    transcription.transcribe_audio(audio, transcriber="whisper")
                self.assertFalse(marker.exists())

    def test_fallback_cuda_cpu_avvisa_la_lentezza(self) -> None:
        chiamate: list[dict[str, Any]] = []

        class ModelloFinto:
            def __init__(self, *_a: object, **kw: object) -> None:
                chiamate.append(kw)
                if kw.get("device") == "cuda":
                    raise RuntimeError("CUDA non disponibile")

            def transcribe(self, *_a: object, **_k: object) -> tuple[list, dict]:
                return [], {}

        with (
            mock.patch("faster_whisper.WhisperModel", ModelloFinto),
            mock.patch.object(
                transcription,
                "_transcribe_with_fallback",
                return_value=([], mock.Mock(language="it", language_probability=0.99)),
            ),
            mock.patch.object(transcription, "openvino_usable", return_value=False),
            mock.patch.object(transcription, "log") as log,
        ):
            transcription.transcribe_with_whisper(
                Path("podcast.m4a"), device="cuda", openvino_available=True
            )
        messaggi = " ".join(str(chiamata) for chiamata in log.warning.call_args_list)
        self.assertIn("CPU", messaggi)
        self.assertFalse(any("iGPU OpenVINO" in str(c) for c in log.info.call_args_list))


class TestSetupEDiagnostica(unittest.TestCase):
    def test_primo_avvio_cuda_senza_faster_whisper_ripiega_cpu(self) -> None:
        args = _Args(whisper_device="cpu", whisper_compute_type="int8")
        rec = {
            "transcriber": "whisper",
            "whisper_device": "cuda",
            "whisper_compute_type": "float16",
            "openvino_device": None,
            "reason": "test",
        }
        with (
            mock.patch.object(machine_setup, "_read_hardware", return_value=None),
            mock.patch.object(machine_setup, "detect_gpus", return_value=["NVIDIA RTX 4060"]),
            mock.patch.object(machine_setup, "_write_hardware"),
            mock.patch.object(machine_setup, "recommend", return_value=dict(rec)),
            mock.patch("importlib.util.find_spec", return_value=None),
        ):
            machine_setup.machine_setup(args, force=True)
        self.assertEqual(args.whisper_device, "cpu")
        self.assertEqual(args.whisper_compute_type, "int8")

    def test_openvino_device_ricalcolato_dopo_provisioning(self) -> None:
        args = _Args()
        stato = {"gpu": False}

        def disponibile() -> bool:
            return stato["gpu"]

        def provisione(*_a: object, **_k: object) -> bool:
            stato["gpu"] = True
            return True

        def recommend(*_a: object, **_k: object) -> dict[str, object]:
            return {
                "transcriber": "openvino",
                "whisper_device": "cpu",
                "whisper_compute_type": "int8",
                "openvino_device": "GPU" if disponibile() else "CPU",
                "reason": "test",
            }

        with (
            mock.patch.object(machine_setup, "_read_hardware", return_value=None),
            mock.patch.object(machine_setup, "detect_gpus", return_value=["Intel Iris Xe"]),
            mock.patch.object(machine_setup, "_write_hardware"),
            mock.patch.object(machine_setup, "recommend", side_effect=recommend),
            mock.patch.object(machine_setup, "_provision_openvino", side_effect=provisione),
            mock.patch.object(machine_setup, "openvino_device_available", side_effect=disponibile),
        ):
            machine_setup.machine_setup(args, force=True)
        self.assertEqual(args.openvino_device, "GPU")

    def test_rimedio_openvino_non_consigliato_senza_igpu(self) -> None:
        def importa_bloccato(nome: Any, *a: Any, **k: Any) -> Any:
            if nome == "openvino_genai":
                raise ImportError("manca")
            return importlib_import(nome, *a, **k)

        importlib_import = __import__
        with (
            mock.patch("config.cuda_available", return_value=False),
            mock.patch("builtins.__import__", side_effect=importa_bloccato),
            mock.patch("transcription.openvino_device_available", return_value=False),
        ):
            esito = verifica_pc._motore_senza_openvino("whisper")
        # Il rimedio resta disponibile per chi in futuro avesse una iGPU, ma
        # il dettaglio/avviso dice chiaramente che qui non gioverebbe.
        self.assertIn("nessuna iGPU Intel", esito.dettaglio)
        self.assertIn("non accelererebbe nulla", esito.rimedio)

    def test_prova_motore_usa_motore_configurato(self) -> None:
        relazione = verifica_pc.Relazione()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(config, "CACHE_DIR", Path(tmp)),
            mock.patch.object(config, "DEFAULT_TRANSCRIBER", "whisper"),
            mock.patch("transcription.resolved_transcriber", side_effect=lambda m, _d: m),
            mock.patch("transcription.openvino_usable", return_value=False),
        ):
            verifica_pc.prova_motore(relazione)
        testo = " ".join(e.dettaglio + " " + e.rimedio for e in relazione.esiti)
        self.assertNotIn("OpenVINO scelto", testo)
        self.assertNotIn("--transcriber openvino", testo)

    def test_un_solo_stato_per_motore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            segnale = Path(tmp) / "openvino_attempt.json"
            segnale.write_text(json.dumps({"audio": "podcast.m4a"}), encoding="utf-8")
            relazione = verifica_pc.Relazione()
            with (
                mock.patch.object(config, "CACHE_DIR", Path(tmp)),
                mock.patch("transcription.resolved_transcriber", return_value="openvino"),
                mock.patch("transcription.openvino_usable", return_value=True),
            ):
                verifica_pc.prova_motore(relazione)
            motori = [e for e in relazione.esiti if e.nome == "Motore"]
            self.assertEqual(len(motori), 1)


class TestScriptELauncher(unittest.TestCase):
    def test_hf_401_e_non_verificabile(self) -> None:
        sys.path.insert(0, str(ROOT / "src" / "scripts"))
        import check_embedding_models as cm

        class Errore401(Exception):
            pass

        Errore401.__name__ = "HfHubHTTPError"
        exc = Errore401("401 Unauthorized")
        exc.response = mock.Mock(status_code=401)  # type: ignore[attr-defined]
        self.assertTrue(cm._is_network_error(exc))

    def test_chiamate_hf_hanno_timeout(self) -> None:
        sys.path.insert(0, str(ROOT / "src" / "scripts"))
        import check_embedding_models as cm

        testo = (ROOT / "src" / "scripts" / "check_embedding_models.py").read_text(encoding="utf-8")
        self.assertIn("model_info(mid, files_metadata=False, timeout=_HF_TIMEOUT)", testo)
        self.assertLessEqual(cm._HF_TIMEOUT, 30)
        # `list_models` NON ha il parametro timeout: passarglielo faceva
        # fallire ogni controllo con uscita 3.
        blocco_lista = testo.split("api.list_models(", 1)[1].split(")", 1)[0]
        self.assertNotIn("timeout", blocco_lista)

    def test_gather_con_api_reale_non_usa_kwarg_inesistente(self) -> None:
        sys.path.insert(0, str(ROOT / "src" / "scripts"))
        import check_embedding_models as cm

        class ApiFirmaReale:
            def model_info(
                self, mid: str, files_metadata: bool = False, timeout: Any = None
            ) -> Any:
                return mock.Mock(lastModified=None, downloads=1)

            def list_models(self, search: str, sort: str, direction: int, limit: int) -> list:
                return []

        dati = cm._gather(ApiFirmaReale())
        self.assertIn("tracked", dati)
        self.assertIn("new_candidates", dati)

    def test_timeline_assente_esce_non_verificabile(self) -> None:
        sys.path.insert(0, str(ROOT / "src" / "scripts"))
        import prova_materiale_reale as prova

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(prova, "CACHE", Path(tmp)),
            mock.patch.object(prova, "_materiale", return_value=(Path("deck.pdf"), Path("podcast.m4a"))),
            mock.patch.object(prova, "_pipeline"),
        ):
            try:
                prova.main([])
            except SystemExit as eccezione:
                self.assertEqual(eccezione.code, prova.ESITO_NON_VERIFICABILE)
            else:
                self.fail("senza timeline l'esito deve essere 'non verificabile'")

    def test_launcher_propagano_errorelevel(self) -> None:
        genera = (ROOT / "genera_video.bat").read_text(encoding="utf-8", errors="replace")
        prova = (ROOT / "prova.bat").read_text(encoding="utf-8", errors="replace")
        pulisci = (ROOT / "pulisci.bat").read_text(encoding="utf-8", errors="replace")
        self.assertIn("endlocal & exit /b %EXIT_CODE%", genera)
        self.assertIn("endlocal & exit /b %EXIT_CODE%", prova)
        self.assertIn("endlocal & exit /b %ERRORI%", pulisci)
        self.assertIn("if errorlevel 1", pulisci)

    def test_confronta_embedding_legge_somiglianza_media(self) -> None:
        testo = (ROOT / "confronta-embedding.ps1").read_text(encoding="utf-8", errors="replace")
        self.assertIn("somiglianza media", testo)
        self.assertIn("punteggio", testo)
        self.assertIn("--no-update-check", testo)
        # Il consumatore e' inutile se il produttore cambia frase: si verifica
        # che main.py emetta ANCORA la riga che la regex del .ps1 sa leggere.
        import re

        produttore = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("somiglianza media ", produttore)
        riga = (
            "ALTO — il parlato somiglia chiaramente alla sua slide "
            "(punteggio 1.477 contro una soglia di 0.450; somiglianza media 0.836)"
        )
        trovato = re.search(r"somiglianza media\s+([\d]+\.[\d]+)", riga)
        self.assertIsNotNone(trovato)
        assert trovato is not None
        self.assertEqual(trovato.group(1), "0.836")


if __name__ == "__main__":
    unittest.main()
