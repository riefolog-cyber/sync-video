#!/usr/bin/env python3
"""
Test unitari per la correzione dei nomi propri nella trascrizione
(transcription.correct_transcript_names).
Esegui con: python -m unittest test_transcription -v
"""

import unittest

from transcription import correct_transcript_names


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


if __name__ == "__main__":
    unittest.main()
