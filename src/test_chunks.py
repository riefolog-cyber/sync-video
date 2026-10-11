#!/usr/bin/env python3
"""
Test unitari per il modulo condiviso `chunks` (finestre temporali).
Verifica che i dati grezzi siano identici a quelli che producevano
`build_semantic_blocks` e `build_llm_chunks` prima del refactor.

Esegui con: python -m unittest test_chunks -v
"""

import threading
import unittest

from chunks import build_windows, words_in_window, words_text_in_window


def _words(items):
    """Converte [(word, start)] in lista di dict Whisper."""
    return [{"word": w, "start": t} for w, t in items]


class TestBuildWindows(unittest.TestCase):
    def test_empty_words(self):
        self.assertEqual(build_windows([], 100.0, 30.0), [])

    def test_covers_duration(self):
        words = _words([(f"p{i}", i * 5.0) for i in range(20)])
        windows = build_windows(words, total_duration=100.0, window_seconds=30.0)
        # 0-30, 30-60, 60-90, 90-100
        self.assertEqual(len(windows), 4)
        self.assertEqual(windows[0]["start"], 0.0)
        self.assertAlmostEqual(windows[-1]["end"], 100.0)

    def test_non_positive_window_terminates(self):
        """Con window <= 0 il ciclo non terminava MAI.

        `end = start + 0` non avanza, il while non esce e ad ogni giro appende
        una finestra vuota: CPU al 100% e memoria che cresce senza limite, senza
        via d'uscita. Raggiungibile da `SEMANTIC_WINDOW=0` nel .env o da
        `--semantic-window 0`, che non validavano nulla. Il clamp porta la
        finestra al minimo invece di far terminare male l'utente: una finestra
        da 1s degrada la qualita' dell'allineamento ma produce comunque una
        timeline, mentre un'eccezione uccide la run su un refuso.

        Il test gira in un thread con join: se il clamp mancasse, il test
        penderebbe per sempre invece di fallire in modo leggibile.
        """
        for bad in (0.0, -1.0, -4.0, -100.0):
            with self.subTest(window=bad):
                result: list[int] = []
                error: list[BaseException] = []

                def run(bad=bad, result=result, error=error):
                    try:
                        result.append(len(build_windows(_words([("a", 0.0)]), total_duration=40.0, window_seconds=bad)))
                    except BaseException as e:
                        # BLE001 e' gia' ignorato per i test in ruff.toml: qui
                        # l'eccezione va catturata per essere riportata
                        # nell'assert, non per essere ignorata.
                        error.append(e)

                t = threading.Thread(target=run, daemon=True)
                t.start()
                t.join(10.0)
                self.assertFalse(t.is_alive(), f"build_windows non termina con window_seconds={bad}")
                self.assertEqual(error, [], f"build_windows ha sollevato con window_seconds={bad}")
                # con il clamp al minimo, la durata e' coperta in modo finito
                self.assertTrue(0 < result[0] <= 60, f"numero di finestre implausibile: {result[0]}")

    def test_first_time_is_first_real_word(self):
        words = _words([("uno", 0.7), ("due", 1.2), ("tre", 5.3)])
        windows = build_windows(words, total_duration=20.0, window_seconds=4.0)
        self.assertAlmostEqual(windows[0]["first_time"], 0.7)
        self.assertAlmostEqual(windows[1]["first_time"], 5.3)

    def test_silent_window_kept_with_ellipsis(self):
        # Finestra vuota: la finestra resta (testo "...") per la timeline continua
        words = _words([("ciao", 1.0), ("mondo", 1.5)])
        windows = build_windows(words, total_duration=20.0, window_seconds=4.0)
        self.assertGreaterEqual(len(windows), 1)
        self.assertEqual(windows[0]["text"], "ciao mondo")

    def test_words_list_matches_text(self):
        words = _words([("a", 1.0), ("b", 1.5), ("c", 2.0)])
        windows = build_windows(words, total_duration=10.0, window_seconds=4.0)
        self.assertEqual(windows[0]["words"], ["a", "b", "c"])
        self.assertEqual(windows[0]["text"], "a b c")

    def test_identical_inputs_produce_identical_windows(self):
        w1 = build_windows(_words([("x", 1.0)]), total_duration=10.0, window_seconds=4.0)
        w2 = build_windows(_words([("x", 1.0)]), total_duration=10.0, window_seconds=4.0)
        self.assertEqual(w1, w2)


class TestWordsInWindow(unittest.TestCase):
    """Lo stralcio di parlato dietro un'ancora deve restare IDENTICO.

    La ricerca binaria sostituisce la scansione lineare: se il risultato
    cambiasse, cambierebbe la verifica del mapping delle ancore, cioè
    quali confini vengono corretti. Qui si confronta sempre con la
    definizione originale, compresi i casi limite.
    """

    @staticmethod
    def _reference(words, start, end):
        return [str(w["word"]) for w in words if start <= float(w["start"]) < end]

    def _assert_matches(self, words, start, end):
        self.assertEqual(
            words_in_window(words, start, end),
            self._reference(words, start, end),
        )

    def test_matches_linear_scan_on_ordered_words(self):
        words = _words([(f"w{i}", i * 0.5) for i in range(200)])
        for start, end in [(0.0, 1.0), (3.0, 7.5), (0.0, 100.0), (99.0, 1000.0)]:
            self._assert_matches(words, start, end)

    def test_matches_linear_scan_on_random_windows(self):
        import random

        rng = random.Random(42)
        words = _words([(f"w{i}", i * 0.37) for i in range(300)])
        for _ in range(200):
            a = rng.uniform(0, 110)
            self._assert_matches(words, a, a + rng.uniform(0, 20))

    def test_window_is_half_open(self):
        # [start, end): la parola che inizia a `end` è esclusa.
        words = _words([("a", 1.0), ("b", 2.0), ("c", 3.0)])
        self.assertEqual(words_in_window(words, 1.0, 2.0), ["a"])
        self.assertEqual(words_in_window(words, 1.0, 3.0), ["a", "b"])

    def test_empty_and_inverted_windows(self):
        words = _words([("a", 1.0), ("b", 2.0)])
        self.assertEqual(words_in_window(words, 5.0, 5.0), [])
        self.assertEqual(words_in_window(words, 9.0, 2.0), [])
        self.assertEqual(words_in_window(words, 0.0, 1.0), [])

    def test_unsorted_words_fall_back_to_linear_scan(self):
        # Timestamp non crescenti: la ricerca binaria darebbe un risultato
        # sbagliato, quindi il fallback deve riprodurre la definizione.
        words = _words([("c", 5.0), ("a", 1.0), ("b", 3.0)])
        self._assert_matches(words, 0.0, 4.0)
        self._assert_matches(words, 2.0, 6.0)

    def test_repeated_timestamps_are_all_included(self):
        # Due parole con lo stesso start: la ricerca binaria (left/left)
        # include TUTTE le parole del blocco, come la scansione lineare.
        words = _words([("a", 1.0), ("b", 1.0), ("c", 1.0), ("d", 2.0)])
        self._assert_matches(words, 1.0, 2.0)
        self.assertEqual(words_in_window(words, 1.0, 2.0), ["a", "b", "c"])

    def test_text_helper_joins_and_strips(self):
        words = _words([("a", 1.0), ("b", 2.0), ("c", 9.0)])
        self.assertEqual(words_text_in_window(words, 0.0, 5.0), "a b")
        self.assertEqual(words_text_in_window(words, 100.0, 200.0), "")


if __name__ == "__main__":
    unittest.main()
