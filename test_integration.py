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
        """Un thread non-daemon vivo -> log + os._exit(0) dopo flush."""
        import logging

        import main

        zombie = self._fake_thread("ffmpeg-reader", daemon=False)
        with (
            patch("main.os._exit") as mock_exit,
            patch("main.logging.shutdown") as mock_shutdown,
            patch.object(logging.Logger, "info") as mock_log,
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


def _words(items):
    """Converte [(word, start)] in lista di dict Whisper."""
    return [{"word": w, "start": t} for w, t in items]



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
