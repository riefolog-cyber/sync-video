#!/usr/bin/env python3
"""La ripresa dà lo stesso testo di una decodifica intera (lento, audio vero).

Il checkpoint e il taglio audio possono sbagliare in silenzio: il testo esce
plausibile, i timestamp sembrano giusti, e nessuno se ne accorge. Qui si
confronta la trascrizione completa con quella ripresa a metà, e poi con quella
ripresa DUE volte (a 1/3 e a 2/3): stesso numero di parole, stesso ordine,
inizi entro 0,3s, parziale cancellato a fine decodifica.

L'unica differenza ammessa e' UNA parola al giunto (``MAX_PAROLE_DIVERSE``),
perche' la coda viene riascoltata con un contesto diverso e Whisper a volte
sceglie l'omofono: misurato sul materiale di prova, "è" -> "e" e "un" -> "uno",
sempre una parola sola su 57. Conteggio, ordine e tempi restano invece
identici: se la ripresa perdesse o duplicasse parole, o slittasse i tempi,
questo test diventa rosso.

Gira solo con SYNC_VIDEO_RUN_SLOW=1 (usa faster-whisper small vero):

    SYNC_VIDEO_RUN_SLOW=1 python -m unittest test_ripresa_equivalente -v

Nota: senza la sovrapposizione al taglio e senza il taglio lossless la coda
NON coincide — misurato: la ricodifica AAC cambiava una parola ("è" -> "e") e
la copia in `-c copy` slittava di un pacchetto, spostando il confine. E' il
motivo per cui esistono ``_SOVRAPPOSIZIONE_RIPRESA`` e il taglio FLAC.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from chunks import Word
from test_slow import slow

SRC = Path(__file__).resolve().parent
AUDIO = SRC / "test_fixtures" / "minimo.m4a"
# Tolleranza sugli inizi: la decodifica della coda puo' spostare un inizio di
# qualche centesimo (misurato: 0,09s al massimo). 0,3s e' il limite richiesto.
TOLLERANZA_INIZI = 0.3
# Al massimo UNA parola puo' cambiare (omofono al giunto): vedi docstring.
MAX_PAROLE_DIVERSE = 1


def _parametri() -> dict[str, Any]:
    """Gli stessi parametri che `transcribe_with_whisper` mette nel parziale."""
    from config import DEFAULT_WHISPER_BATCH, DEFAULT_WHISPER_BEAM

    return {
        "engine": "faster-whisper",
        "model": "small",
        # Il parziale ora identifica anche il decoder: device/compute devono
        # essere quelli della run reale, altrimenti viene scartato.
        "device": "cpu",
        "compute_type": "int8",
        "beam": int(DEFAULT_WHISPER_BEAM),
        "language": "it",
        "vad": True,
        "batch": int(DEFAULT_WHISPER_BATCH),
    }


class TestRipresaEquivalente(unittest.TestCase):
    def setUp(self) -> None:
        if not AUDIO.exists():
            self.skipTest(f"fixture assente: {AUDIO}")
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            self.skipTest("faster-whisper non installato")
        self._tmp = tempfile.TemporaryDirectory(prefix="ripresa_")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        # Mai la cache vera: il test non deve toccare il materiale dell'utente.
        patch = mock.patch("config.CACHE_DIR", self.base / "cache")
        patch.start()
        self.addCleanup(patch.stop)

    # --- aiuti ---------------------------------------------------------

    def _trascrivi(self) -> tuple[list[Word], float]:
        from transcription import transcribe_with_whisper

        t0 = time.time()
        _testo, parole = transcribe_with_whisper(AUDIO, model_size="small")
        return parole, time.time() - t0

    def _salva(self, parole: list[Word], fino: float) -> None:
        from transcription import _salva_parziale

        _salva_parziale(AUDIO, [w for w in parole if float(w["start"]) < fino], fino, _parametri())

    def _differenze(self, atteso: list[Word], ottenuto: list[Word]) -> tuple[list[str], list[str]]:
        """(differenze gravi, parole il cui testo cambia)."""
        gravi: list[str] = []
        testo: list[str] = []
        if len(atteso) != len(ottenuto):
            gravi.append(f"numero di parole: {len(atteso)} attese, {len(ottenuto)} ottenute")
        for i, (a, b) in enumerate(zip(atteso, ottenuto, strict=False)):
            if a["word"] != b["word"]:
                testo.append(
                    f"parola {i}: {a['word']!r} -> {b['word']!r} "
                    f"({float(a['start']):.2f}s -> {float(b['start']):.2f}s)"
                )
            elif abs(float(a["start"]) - float(b["start"])) > TOLLERANZA_INIZI:
                gravi.append(
                    f"parola {i} {a['word']!r}: inizio {float(a['start']):.2f}s "
                    f"contro {float(b['start']):.2f}s (oltre {TOLLERANZA_INIZI}s)"
                )
        return gravi, testo

    def _confronta(self, atteso: list[Word], ottenuto: list[Word], nota: str) -> int:
        """Asserisce l'equivalenza e ritorna quante parole differiscono."""
        gravi, testo = self._differenze(atteso, ottenuto)
        self.assertEqual(gravi, [], f"{nota}\n" + "\n".join(gravi))
        self.assertLessEqual(
            len(testo), MAX_PAROLE_DIVERSE, f"{nota}\n" + "\n".join(testo)
        )
        return len(testo)

    def _punto(self, parole: list[Word], dopo: float) -> float:
        """Il primo inizio di parola dopo ``dopo``: confine pulito, non a metà."""
        return next(float(w["start"]) for w in parole if float(w["start"]) > dopo)

    # --- i test --------------------------------------------------------

    @slow("due trascrizioni reali (faster-whisper small) sul materiale di prova")
    def test_ripresa_da_meta_da_lo_stesso_testo(self) -> None:
        import transcription

        atteso, t_completa = self._trascrivi()
        durata = float(atteso[-1]["start"])
        punto = self._punto(atteso, durata / 2)
        self._salva(atteso, punto)
        parziale = transcription.percorso_parziale(AUDIO)
        self.assertTrue(parziale.exists(), "il parziale seminato non e' stato scritto")

        ottenuto, t_ripresa = self._trascrivi()
        diverse = self._confronta(atteso, ottenuto, "ripresa a metà audio")
        print(
            f"[ripresa] completa: {len(atteso)} parole in {t_completa:.1f}s | "
            f"ripresa da {punto:.1f}s: {len(ottenuto)} parole in {t_ripresa:.1f}s | "
            f"parole diverse: {diverse}"
        )
        self.assertGreater(
            transcription.ultima_ripresa(), 0.0, "la ripresa non e' partita (parziale ignorato)"
        )
        self.assertFalse(parziale.exists(), "il parziale deve sparire a fine decodifica")

    @slow("tre trascrizioni reali (faster-whisper small) sul materiale di prova")
    def test_due_interruzioni_danno_lo_stesso_testo(self) -> None:
        import transcription

        atteso, _t_completa = self._trascrivi()
        durata = float(atteso[-1]["start"])
        primo = self._punto(atteso, durata / 3)
        secondo = self._punto(atteso, 2 * durata / 3)

        # Prima interruzione: parziale fino a 1/3.
        self._salva(atteso, primo)
        dopo_primo, t1 = self._trascrivi()
        self.assertGreater(transcription.ultima_ripresa(), 0.0, "la prima ripresa non e' partita")
        self._confronta(atteso, dopo_primo, "prima ripresa (1/3)")

        # Seconda interruzione: si riparte dal parziale RISCRITTO dalla ripresa
        # precedente (deve contenere anche le parole nuove).
        self._salva(dopo_primo, secondo)
        dopo_secondo, t2 = self._trascrivi()
        diverse = self._confronta(atteso, dopo_secondo, "seconda ripresa (2/3)")
        print(
            f"[ripresa] due interruzioni a {primo:.1f}s e {secondo:.1f}s: "
            f"{len(dopo_secondo)} parole in {t1:.1f}s + {t2:.1f}s | "
            f"parole diverse: {diverse}"
        )
        self.assertGreater(
            transcription.ultima_ripresa(), 0.0, "la seconda ripresa non e' partita"
        )
        self.assertFalse(
            transcription.percorso_parziale(AUDIO).exists(),
            "dopo la seconda ripresa il parziale deve essere cancellato",
        )


if __name__ == "__main__":
    unittest.main()
