#!/usr/bin/env python3
"""Le zone cieche non si ritrascrivono due volte.

Il beam veloce mangia intere frasi nelle zone difficili ("la c' la c'..."
invece di "Passiamo alla slide 3"): la zona viene ritrascritta col beam
accurato per cercare l'annuncio. Ma se dentro la zona non c'e' nessun
annuncio â€” il caso normale, non un errore â€” la run successiva ritrascrive
la STESSA zona per trovare, di nuovo, niente: ~25s l'una, a ogni run.

La rimedio e' un ricordo delle zone tentate, con l'impronta del loro
CONTENUTO: una zona il cui contenuto non e' cambiato ha gia' dato il suo
risultato, quindi non si ritrascrive. Se una parola cambia, l'impronta
cambia e la zona viene ritrascritta davvero.

Esegui con: python -m unittest test_zone_cieche -v
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import main
from chunks import Word


class TestZoneCiecheNonRifatte(unittest.TestCase):
    """Una zona gia' tentata non si ritrascrive se il contenuto non e' cambiato."""

    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(whisper_model="small", lang="ita")

    def _chiamate(self) -> tuple[list[tuple[float, float]], Callable[..., list[Word]]]:
        registrate: list[tuple[float, float]] = []

        def _fake(audio_path: Path, lo: float, hi: float, **kwargs: Any) -> list[Word]:
            registrate.append((lo, hi))
            return [{"word": "passiamo", "start": lo + 1.0}]

        return registrate, _fake

    def _zone(self, parole: list[Word]) -> list[Word]:
        """Le parole della trascrizione: la zona senza ancora e' fra 10s e 50s."""
        return parole

    def test_la_zona_viene_ritrascritta_una_volta_solo(self) -> None:
        parole: list[Word] = [
            {"word": "ciao", "start": 5.0},
            {"word": "mondo", "start": 20.0},
            {"word": "bene", "start": 45.0},
        ]
        registrate, fake = self._chiamate()
        with (
            mock.patch.object(main, "retranscribe_zone", side_effect=fake),
            mock.patch.object(main, "_write_raw_transcript", return_value=None),
        ):
            primo = main._recover_anchor_zones(
                self._zone(parole),
                {2: 10.0, 4: 50.0},
                4,
                300.0,
                Path("audio.m4a"),
                self._args(),
                "slide-audio",
            )
            # Seconda run: la zona e' gia' stata tentata, contenuto uguale.
            secondo = main._recover_anchor_zones(
                self._zone(parole),
                {2: 10.0, 4: 50.0},
                4,
                300.0,
                Path("audio.m4a"),
                self._args(),
                "slide-audio",
                zones_tentate=primo.tried,
            )

        self.assertEqual(len(registrate), 1, "la zona non deve essere ritrascritta due volte")
        self.assertTrue(primo.tried, "la prima run deve ricordare la zona tentata")
        self.assertEqual(secondo.tried, primo.tried)
        self.assertEqual(secondo.seconds, 0.0)

    def test_contenuto_cambiato_la_zona_viene_ritrascritta(self) -> None:
        # Una parola cambiata dentro la zona: l'impronta cambia, e la zona va
        # ritrascritta davvero (il risultato sarebbe diverso).
        prima_parole: list[Word] = [
            {"word": "ciao", "start": 5.0},
            {"word": "mondo", "start": 20.0},
        ]
        dopo_parole: list[Word] = [
            {"word": "ciao", "start": 5.0},
            {"word": "universo", "start": 20.0},
        ]
        registrate, fake = self._chiamate()
        with (
            mock.patch.object(main, "retranscribe_zone", side_effect=fake),
            mock.patch.object(main, "_write_raw_transcript", return_value=None),
        ):
            primo = main._recover_anchor_zones(
                self._zone(prima_parole),
                {2: 10.0, 4: 50.0},
                4,
                300.0,
                Path("audio.m4a"),
                self._args(),
                "slide-audio",
            )
            main._recover_anchor_zones(
                self._zone(dopo_parole),
                {2: 10.0, 4: 50.0},
                4,
                300.0,
                Path("audio.m4a"),
                self._args(),
                "slide-audio",
                zones_tentate=primo.tried,
            )

        self.assertEqual(len(registrate), 2, "il contenuto cambiato va ritrascritto")

    def test_l_impronta_cambia_con_il_contenuto(self) -> None:
        parole_a: list[Word] = [{"word": "ciao", "start": 20.0}]
        parole_b: list[Word] = [{"word": "ciao", "start": 20.1}]
        self.assertNotEqual(
            main._zone_hash(parole_a, 10.0, 30.0),
            main._zone_hash(parole_b, 10.0, 30.0),
        )
        # Stessi limiti, stesse parole: la stessa zona.
        self.assertEqual(
            main._zone_hash(parole_a, 10.0, 30.0),
            main._zone_hash(parole_a, 10.0, 30.0),
        )


if __name__ == "__main__":
    unittest.main()

