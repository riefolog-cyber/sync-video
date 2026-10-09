#!/usr/bin/env python3
"""
Test di integrazione end-to-end per la pipeline Slide2Video.
Verifica che le fasi principali funzionino insieme (senza encoding video).

Esegui con: python -m unittest test_integration -v
"""

import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from timeline import (
    extract_timeline_from_transcript,
    reconcile_timeline,
)
from video import build_video


class TestForceCleanExit(unittest.TestCase):
    """Protezione anti-zombie: uscita forzata solo con thread residui non-daemon."""

    @staticmethod
    def _fake_thread(name: str, daemon: bool, alive: bool = True):
        t = MagicMock(spec=threading.Thread)
        t.name = name
        t.daemon = daemon
        t.is_alive.return_value = alive
        return t

    def test_no_lingering_threads_exits_normally(self):
        """Solo il thread corrente: nessuna uscita forzata."""
        import main

        with (
            patch("main.os._exit") as mock_exit,
            patch("main.threading.enumerate", return_value=[threading.current_thread()]),
        ):
            main._force_clean_exit()
        mock_exit.assert_not_called()

    def test_daemon_threads_do_not_force_exit(self):
        """I thread daemon non bloccano l'uscita: nessuna uscita forzata."""
        import main

        daemon = self._fake_thread("daemon-worker", daemon=True)
        with (
            patch("main.os._exit") as mock_exit,
            patch(
                "main.threading.enumerate",
                return_value=[threading.current_thread(), daemon],
            ),
        ):
            main._force_clean_exit()
        mock_exit.assert_not_called()

    def test_lingering_non_daemon_forces_exit_zero(self):
        """Un thread non-daemon vivo -> warning + os._exit(0) dopo flush."""
        import logging

        import main

        zombie = self._fake_thread("ffmpeg-reader", daemon=False)
        with (
            patch("main.os._exit") as mock_exit,
            patch("main.logging.shutdown") as mock_shutdown,
            patch.object(logging.Logger, "warning") as mock_log,
            patch(
                "main.threading.enumerate",
                return_value=[threading.current_thread(), zombie],
            ),
        ):
            main._force_clean_exit()
        mock_shutdown.assert_called_once()
        mock_log.assert_called_once()
        self.assertIn("ffmpeg-reader", str(mock_log.call_args))
        mock_exit.assert_called_once_with(0)

    def test_dead_threads_ignored(self):
        """Thread is_alive()=False (in chiusura): non forzano l'uscita."""
        import main

        dying = self._fake_thread("almost-done", daemon=False, alive=False)
        with (
            patch("main.os._exit") as mock_exit,
            patch(
                "main.threading.enumerate",
                return_value=[threading.current_thread(), dying],
            ),
        ):
            main._force_clean_exit()
        mock_exit.assert_not_called()

    def test_worker_di_un_pool_non_forza_uscita(self):
        """Un worker fermo sulla coda di un pool vero NON forza l'uscita.

        Usa un ThreadPoolExecutor reale, non un mock: è l'unico modo per avere
        un thread davvero fermo dentro concurrent.futures.thread._worker, cioè
        lo stato in cui `import openvino` lascia a fine run il sender della
        telemetria (openvino/__init__.py:106 -> openvino.tools.ovc ->
        init_ovc_telemetry -> TelemetrySender, un pool che force_shutdown non
        chiude mai).

        Il worker è non-daemon ma non blocca l'uscita: concurrent.futures
        registra threading._register_atexit, che a fine run mette None in coda
        e fa join. Senza il filtro, os._exit(0) saltava gli atexit a ogni run
        e forzava l'exit code a 0 senza che ci fosse mai un hang.
        """
        from concurrent.futures import ThreadPoolExecutor

        import main

        with ThreadPoolExecutor(max_workers=1) as pool:
            # il task crea il worker, che poi si riaccoda su work_queue.get
            pool.submit(lambda: None).result()
            with (
                patch("main.os._exit") as mock_exit,
                patch("main.logging.shutdown") as mock_shutdown,
            ):
                main._force_clean_exit()
        mock_exit.assert_not_called()
        mock_shutdown.assert_not_called()

    def test_thread_che_non_e_un_worker_viene_ancora_segnalato(self):
        """Il filtro deve colpire i soli worker: un thread normale forza ancora.

        Stesso nome convenzionale di un worker ("ThreadPoolExecutor-0_0"), ma
        il frame in cima alla pila è un'altra funzione: il nome da solo non
        basta, altrimenti si nasconderebbe proprio il thread impiantato che la
        protezione deve catturare.
        """
        import main

        finto = self._fake_thread("ThreadPoolExecutor-0_0", daemon=False)
        with (
            patch("main.os._exit") as mock_exit,
            patch("main.logging.shutdown"),
            patch(
                "main.threading.enumerate",
                return_value=[threading.current_thread(), finto],
            ),
            patch("main._in_coda_su_un_pool", return_value=False),
        ):
            main._force_clean_exit()
        mock_exit.assert_called_once_with(0)

    def test_worker_e_thread_bloccante_insieme_forzano_uscita(self):
        """Se c'e' anche un vero thread bloccante, l'uscita forzata scatta.

        Il filtro non deve far dimenticare i colpevoli veri: ignorare i worker
        senza guardare il resto della lista annullerebbe la protezione.
        """
        import logging
        from concurrent.futures import ThreadPoolExecutor

        import main

        bloccante = self._fake_thread("ffmpeg-reader", daemon=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: None).result()
            with (
                patch("main.os._exit") as mock_exit,
                patch("main.logging.shutdown") as mock_shutdown,
                patch.object(logging.Logger, "warning") as mock_log,
                patch(
                    "main.threading.enumerate",
                    return_value=[threading.current_thread(), bloccante],
                ),
            ):
                main._force_clean_exit()
        mock_shutdown.assert_called_once()
        self.assertIn("ffmpeg-reader", str(mock_log.call_args))
        mock_exit.assert_called_once_with(0)

    def test_senza_riferimento_al_file_nessun_filtro(self):
        """Se __file__ manca (interprete frozen) il filtro non se ne accorge.

        _WORKER_POOL_FILE vale None quando concurrent.futures non espone il
        percorso del proprio file, e li' non si puo' distinguere un worker da un
        thread impiantato. Il default e' NON filtrare: la protezione deve
        scattare, perche' il rischio di ignorare un colpevole vero e' peggiore
        del fastidio di un falso positivo che si e' gia' visto una volta sola.
        """
        import main

        zombie = self._fake_thread("ThreadPoolExecutor-0_0", daemon=False)
        with (
            patch("main._WORKER_POOL_FILE", None),
            patch("main.os._exit") as mock_exit,
            patch("main.logging.shutdown"),
            patch(
                "main.threading.enumerate",
                return_value=[threading.current_thread(), zombie],
            ),
        ):
            main._force_clean_exit()
        mock_exit.assert_called_once_with(0)


def _words(items):
    """Converte [(word, start)] in lista di dict Whisper."""
    return [{"word": w, "start": t} for w, t in items]



class TestVideoBatchScript(unittest.TestCase):
    """Il controllo del video finito è una scelta dell'utente, non un dettaglio:
    deve restare attivo nel bat con cui si genera il video, disattivabile solo
    esplicitamente (set VERIFY_VIDEO=0)."""

    def test_verify_video_enabled_by_default(self):
        script = (Path(__file__).parent / "genera_video.bat").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn('if not defined VERIFY_VIDEO set "VERIFY_VIDEO=1"', script)
        self.assertIn("--verify-video", script)


class TestPipelineIntegration(unittest.TestCase):
    """Test che le fasi del pipeline funzionino insieme."""

    def test_full_timeline_pipeline(self):
        """Fase 1→2→3: trascrizione → deterministica → riconciliazione."""
        # Simula parole Whisper con segnali "slide N"
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

        # Fase 3a: estrazione deterministica
        timeline = extract_timeline_from_transcript(words, total_slides=4, total_duration=200.0, flow="slide-audio")
        self.assertEqual(timeline, {1: 0.0, 2: 30.3, 3: 80.3, 4: 130.3})

        # Fase 3c: riconciliazione
        durations = reconcile_timeline(timeline, 4, 200.0)
        self.assertEqual(len(durations), 4)
        self.assertAlmostEqual(sum(durations), 200.0)

    def test_build_video_validation(self):
        """build_video rifiuta lunghezze non corrispondenti."""
        with self.assertRaises(ValueError):
            build_video(
                ["slide_001.png", "slide_002.png"],  # 2 slide
                [10.0],  # 1 durata
                MagicMock(),  # audio_clip mock
                Path("output.mp4"),
            )

    def test_audio_slide_flow_pipeline(self):
        """Flusso audio-slide: 'passiamo al blocco successivo'."""
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
        timeline = extract_timeline_from_transcript(words, total_slides=3, total_duration=200.0, flow="audio-slide")
        self.assertEqual(timeline, {1: 0.0, 2: 30.0, 3: 100.0})

        durations = reconcile_timeline(timeline, 3, 200.0)
        self.assertEqual(len(durations), 3)
        self.assertAlmostEqual(sum(durations), 200.0)


if __name__ == "__main__":
    unittest.main()
