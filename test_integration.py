#!/usr/bin/env python3
"""
Test di integrazione end-to-end per la pipeline Slide2Video.
Verifica che le fasi principali funzionino insieme (senza encoding video).

Esegui con: python -m unittest test_integration -v
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock

from timeline import (
    extract_timeline_from_transcript,
    reconcile_timeline,
)
from video import build_video


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
