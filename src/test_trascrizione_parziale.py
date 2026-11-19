#!/usr/bin/env python3
"""Salvataggio a pezzi e RIPRESA della trascrizione.

Whisper non si mette in pausa: se la run muore a meta', le parole gia'
prodotte devono restare su disco. E alla run dopo si deve decodificare solo la
coda, non tutto: qui si verifica il punto di ripresa (`coperto_fino`), lo
spostamento dei timestamp (`offset`) e le due reti di sicurezza (parziale
vecchio formato, taglio fallito) che ricadono sulla trascrizione intera.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import transcription
from chunks import Word
from transcription import (
    _CHECKPOINT_OGNI_SEGMENTI,
    _collect_words,
    _prepara_ripresa,
    _salva_parziale,
    percorso_parziale,
)


class _Segmento:
    """Il minimo che `_collect_words` legge da un segmento di faster-whisper."""

    def __init__(self, testo: str, inizio: float, fine: float | None = None) -> None:
        self.text = testo
        self.start = inizio
        self.end = inizio + 1.0 if fine is None else fine
        self.words: list[object] = []


class TestSalvataggioAPezzi(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.audio = self.base / "podcast.m4a"
        self.audio.write_bytes(b"audio fittizio")
        self._patch = mock.patch("config.CACHE_DIR", self.base / ".cache")
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_il_percorso_dipende_dall_audio(self) -> None:
        primo = percorso_parziale(self.audio)
        altro = self.base / "altro.m4a"
        altro.write_bytes(b"audio diverso")
        self.assertNotEqual(primo, percorso_parziale(altro))

    def test_il_file_salvato_e_leggibile(self) -> None:
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.5}], coperto_fino=3.0)
        dati = json.loads(percorso_parziale(self.audio).read_text(encoding="utf-8"))
        self.assertEqual(dati["parole"], 1)
        self.assertEqual(dati["words"][0]["word"], "ciao")
        self.assertEqual(dati["coperto_fino"], 3.0)

    def test_si_salva_ogni_n_segmenti(self) -> None:
        segmenti = [
            _Segmento(f"parola {i}", float(i)) for i in range(_CHECKPOINT_OGNI_SEGMENTI * 2)
        ]
        with mock.patch.object(transcription, "_salva_parziale") as salva:
            parole = _collect_words(iter(segmenti), self.audio)
        # Due parole per segmento (il testo si divide in token).
        self.assertEqual(len(parole), _CHECKPOINT_OGNI_SEGMENTI * 2 * 2)
        self.assertEqual(salva.call_count, 2, "un salvataggio ogni N segmenti")
        # Il secondo salvataggio porta anche il punto di ripresa: fine del
        # segmento N, che e' dove l'audio e' stato decodificato.
        coperto = salva.call_args_list[1].args[2]
        self.assertAlmostEqual(coperto, float(_CHECKPOINT_OGNI_SEGMENTI * 2 - 1) + 1.0, delta=0.01)

    def test_a_trascrizione_finita_il_parziale_sparisce(self) -> None:
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.0}], coperto_fino=1.0)
        self.assertTrue(percorso_parziale(self.audio).exists())
        _collect_words(iter([_Segmento("ciao", 0.0)]), self.audio)
        self.assertFalse(percorso_parziale(self.audio).exists())

    def test_senza_audio_nessun_file(self) -> None:
        _collect_words(iter([_Segmento("ciao", 0.0)]))
        self.assertFalse((self.base / ".cache" / "parziali").exists())


class TestRipresa(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.audio = self.base / "podcast.m4a"
        self.audio.write_bytes(b"audio fittizio")
        self._patch = mock.patch("config.CACHE_DIR", self.base / ".cache")
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _scrivi_parziale(self, **dati: object) -> None:
        path = percorso_parziale(self.audio)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"audio": str(self.audio), **dati}), encoding="utf-8")

    def test_riprende_dalla_coda_con_il_taglio(self) -> None:
        self._scrivi_parziale(
            parole=2,
            coperto_fino=120.0,
            words=[{"word": "ciao", "start": 1.0}, {"word": "mondo", "start": 2.0}],
        )
        taglio = self.base / "resto.m4a"
        taglio.write_bytes(b"x")
        with mock.patch.object(transcription, "_taglia_audio", return_value=taglio) as t:
            iniziali, sorgente, offset = _prepara_ripresa(self.audio)
        self.assertEqual([w["word"] for w in iniziali], ["ciao", "mondo"])
        self.assertEqual(sorgente, taglio)
        self.assertEqual(offset, 120.0)
        t.assert_called_once_with(self.audio, 120.0)

    def test_parziale_senza_punto_di_ripresa_ricomincia(self) -> None:
        # Formato vecchio (solo "words"): il testo si tiene, ma non si riprende.
        self._scrivi_parziale(parole=1, words=[{"word": "ciao", "start": 1.0}])
        iniziali, sorgente, offset = _prepara_ripresa(self.audio)
        self.assertEqual(iniziali, [])
        self.assertEqual(sorgente, self.audio)
        self.assertEqual(offset, 0.0)

    def test_taglio_fallito_ricomincia_da_capo(self) -> None:
        self._scrivi_parziale(
            parole=1, coperto_fino=60.0, words=[{"word": "ciao", "start": 1.0}]
        )
        with mock.patch.object(transcription, "_taglia_audio", return_value=None):
            iniziali, sorgente, offset = _prepara_ripresa(self.audio)
        self.assertEqual(iniziali, [])
        self.assertEqual((sorgente, offset), (self.audio, 0.0))

    def test_un_parziale_illeggibile_non_ferma_la_run(self) -> None:
        path = percorso_parziale(self.audio)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(_prepara_ripresa(self.audio), ([], self.audio, 0.0))

    def test_il_parziale_salva_i_parametri(self) -> None:
        params = {"model": "small", "beam": 5}
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.0}], 3.0, params)
        dati = json.loads(percorso_parziale(self.audio).read_text(encoding="utf-8"))
        self.assertEqual(dati["params"], params)

    def test_parametri_diversi_non_riprendono(self) -> None:
        # Un transcript mezzo `small` e mezzo `large` sarebbe sbagliato senza
        # dirlo: se i parametri non coincidono si ricomincia.
        _salva_parziale(
            self.audio, [{"word": "ciao", "start": 0.0}], 60.0, {"model": "small"}
        )
        iniziali, sorgente, offset = _prepara_ripresa(self.audio, {"model": "large"})
        self.assertEqual((iniziali, sorgente, offset), ([], self.audio, 0.0))

    def test_parametri_uguali_riprendono(self) -> None:
        params = {"model": "small", "beam": 5}
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.0}], 60.0, params)
        taglio = self.base / "resto.m4a"
        taglio.write_bytes(b"x")
        with mock.patch.object(transcription, "_taglia_audio", return_value=taglio):
            iniziali, sorgente, offset = _prepara_ripresa(self.audio, dict(params))
        self.assertEqual(len(iniziali), 1)
        self.assertEqual((sorgente, offset), (taglio, 60.0))

    def test_parziale_senza_parametri_non_riprende(self) -> None:
        # Formato di una versione precedente: non si sa con che modello e'
        # stato fatto, quindi non si mescola.
        self._scrivi_parziale(
            parole=1, coperto_fino=60.0, words=[{"word": "ciao", "start": 0.0}]
        )
        self.assertEqual(_prepara_ripresa(self.audio, {"model": "small"})[2], 0.0)

    def test_il_percorso_non_cambia_col_touch(self) -> None:
        # Copiare o toccare l'audio non deve buttare via una trascrizione buona:
        # il nome del parziale dipende dal CONTENUTO.
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.0}], 1.0)
        prima = percorso_parziale(self.audio)
        os.utime(self.audio, (time.time() + 60, time.time() + 60))
        self.assertEqual(percorso_parziale(self.audio), prima)

    def test_la_firma_segue_il_contenuto(self) -> None:
        prima = percorso_parziale(self.audio)
        self.audio.write_bytes(b"audio DIVERSO e piu' lungo")
        self.assertNotEqual(percorso_parziale(self.audio), prima)

    def test_ultima_ripresa_riporta_il_punto(self) -> None:
        _salva_parziale(self.audio, [{"word": "ciao", "start": 0.0}], 42.0, {"m": 1})
        taglio = self.base / "r.m4a"
        taglio.write_bytes(b"x")
        with mock.patch.object(transcription, "_taglia_audio", return_value=taglio):
            _prepara_ripresa(self.audio, {"m": 1})
        self.assertEqual(transcription.ultima_ripresa(), 42.0)

    def test_lo_spostamento_dei_timestamp_e_assoluto(self) -> None:
        segmenti = [_Segmento("ciao", 0.0, 1.0), _Segmento("mondo", 1.0, 2.0)]
        parole = _collect_words(iter(segmenti), offset=100.0)
        self.assertEqual([w["start"] for w in parole], [100.0, 101.0])

    def test_il_nuovo_parziale_contiene_anche_le_parole_gia_fatte(self) -> None:
        # Se la run muore ANCHE nella coda, il parziale non deve rimpicciolirsi:
        # le parole della ripresa restano dentro.
        precedenti: list[Word] = [{"word": "prima", "start": 0.5}]
        segmenti = [_Segmento(f"x{i}", float(i), float(i) + 1.0) for i in range(_CHECKPOINT_OGNI_SEGMENTI)]
        _collect_words(iter(segmenti), self.audio, offset=100.0, precedenti=precedenti)
        # A fine trascrizione il parziale viene rimosso: qui interessa il
        # contenuto salvato a meta', quindi si guarda durante il percorso.
        with mock.patch.object(transcription, "_salva_parziale") as salva:
            _collect_words(
                iter([_Segmento(f"x{i}", float(i), float(i) + 1.0) for i in range(_CHECKPOINT_OGNI_SEGMENTI)]),
                self.audio,
                offset=100.0,
                precedenti=precedenti,
            )
        salvate = salva.call_args.args[1]
        self.assertEqual(salvate[0]["word"], "prima")
        self.assertGreater(len(salvate), _CHECKPOINT_OGNI_SEGMENTI)


if __name__ == "__main__":
    unittest.main()
