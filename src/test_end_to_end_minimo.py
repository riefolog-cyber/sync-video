#!/usr/bin/env python3
"""La pipeline INTERA, con materiale vero (lento).

Tutti gli altri test sono finti: l'OCR di un PDF finto, la trascrizione di un
audio finto, un video costruito al volo. Qui c'e' il percorso reale:

    PDF vero -> Tesseract -> voce vera (fixture) -> faster-whisper
    -> ancore "slide N" -> timeline -> ffmpeg -> MP4

E' l'unico test che si accorge di un collegamento rotto: un argomento di
ffmpeg sbagliato, un nome di file, l'ordine degli import, un default che
cambia. Costa ~20 secondi per test, quindi gira solo con
SYNC_VIDEO_RUN_SLOW=1 (job notturno); in locale:

    SYNC_VIDEO_RUN_SLOW=1 python -m unittest test_end_to_end_minimo -v

Il PDF si genera qui (pymupdf), l'audio e' ``test_fixtures/minimo.m4a``: 33
secondi di voce italiana che annuncia "passiamo alla slide due/tre/quattro/
cinque".

Due test: la run normale, e la RIPRESA (mezzo audio gia' trascritto, la run
deve decodificare solo la coda e dire da dove riparte).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiuti_test import eseguibile_tesseract
from test_slow import slow

SRC = Path(__file__).resolve().parent
AUDIO = SRC / "test_fixtures" / "minimo.m4a"
PAGINE = 5
TIMEOUT = 900


def _strumenti_mancanti() -> list[str]:
    """Cosa serve perche' il test abbia senso (senza, si salta)."""
    mancanti: list[str] = []
    if shutil.which("ffmpeg") is None:
        mancanti.append("ffmpeg")
    if eseguibile_tesseract() is None:
        mancanti.append("tesseract")
    for modulo in ("pymupdf", "faster_whisper"):
        try:
            __import__(modulo)
        except ImportError:
            mancanti.append(modulo)
    if not AUDIO.exists():
        mancanti.append(str(AUDIO))
    return mancanti


def _pdf_con_numeri(path: Path, pagine: int = PAGINE) -> None:
    """Un PDF vero: numero grande al centro, che l'OCR deve leggere."""
    import pymupdf

    doc = pymupdf.open()
    for n in range(1, pagine + 1):
        pagina = doc.new_page(width=1280, height=720)
        pagina.insert_text((600, 430), str(n), fontsize=240)
        pagina.insert_text((90, 90), f"Lezione di prova - pagina {n}", fontsize=28)
    doc.save(str(path))
    doc.close()


def _cartella_embedding_reale() -> str:
    """Il modello embedding della macchina (2 GB): non va riscaricato nel test.

    La cache della run punta a una cartella temporanea, ma i VETTORI del
    modello no: si passa quella vera con `--semantic-cache-dir`.
    """
    from config import DEFAULT_EMBEDDING_CACHE_DIR

    return DEFAULT_EMBEDDING_CACHE_DIR


def _durata_audio(path: Path) -> float:
    """Durata vera della fixture, letta da ffmpeg: niente numeri scritti a mano.

    Si usa `ffmpeg -i` e non `ffprobe`: la CI ha solo il binario statico di
    imageio-ffmpeg, che ffprobe non lo porta.
    """
    esito = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", (esito.stderr or "") + (esito.stdout or ""))
    if not m:
        raise AssertionError(f"ffmpeg non ha dato la durata di {path}: {esito.stderr}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _comando(pdf: Path, video: Path, slides: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        str(SRC / "main.py"),
        "--pdf",
        str(pdf),
        "--audio",
        str(AUDIO),
        "--output",
        str(video),
        "--slides-dir",
        str(slides),
        "--semantic-cache-dir",
        _cartella_embedding_reale(),
        "--transcriber",
        "whisper",
        "--whisper-model",
        "small",
        "--no-update-check",
        "--no-auto-setup",
        "--no-confirm",
        *extra,
    ]


class TestPipelineCompleta(unittest.TestCase):
    def _prepara(self, tmp: str) -> tuple[Path, Path, Path]:
        base = Path(tmp)
        pdf = base / "presentazione.pdf"
        _pdf_con_numeri(pdf)
        return base, pdf, base / ".cache"

    def _salta_se_manca_qualcosa(self) -> None:
        mancanti = _strumenti_mancanti()
        if mancanti:
            self.skipTest("manca: " + ", ".join(mancanti))
        # Il modello embedding (2 GB) NON si scarica di nascosto in un job
        # notturno: o c'e' in cache (la CI la mantiene) o si e' detto di
        # scaricarlo. La variabile rende la cosa una scelta, non una sorpresa.
        if not Path(_cartella_embedding_reale()).exists() and os.environ.get(
            "SYNC_VIDEO_E2E_SCARICA"
        ) != "1":
            self.skipTest(
                "modello embedding non in cache ("
                + _cartella_embedding_reale()
                + "): con SYNC_VIDEO_E2E_SCARICA=1 il test lo scarica (~2 GB)"
            )

    def _esegui(self, base: Path, cache: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], str, Path]:
        video = base / "video_finale.mp4"
        ambiente = {
            **os.environ,
            "SYNC_VIDEO_CACHE_DIR": str(cache),
            "PYTHONIOENCODING": "utf-8",
        }
        esito = subprocess.run(
            _comando(base / "presentazione.pdf", video, base / "slides", *extra),
            cwd=str(base),
            env=ambiente,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
        )
        return esito, (esito.stdout or "") + (esito.stderr or ""), video

    @slow("pipeline intera su materiale vero: OCR, Whisper, embeddings, ffmpeg")
    def test_cinque_slide_con_annunci(self) -> None:
        self._salta_se_manca_qualcosa()

        with tempfile.TemporaryDirectory(prefix="e2e_") as tmp:
            base, _pdf, cache = self._prepara(tmp)
            esito, output, video = self._esegui(base, cache)
            self.assertEqual(esito.returncode, 0, output[-4000:])

            # 1. Il video esiste davvero.
            self.assertTrue(video.exists(), "nessun video prodotto")
            self.assertGreater(video.stat().st_size, 50_000, "video troppo piccolo")

            # 2. La timeline ha una pagina per slide, in ordine.
            timeline = json.loads((cache / "timeline_finale.json").read_text(encoding="utf-8"))
            self.assertEqual([s["slide"] for s in timeline], list(range(1, PAGINE + 1)))
            inizi = [float(s["start"]) for s in timeline]
            self.assertEqual(inizi, sorted(inizi))

            # 3. Il confine della slide 2 e' quello ANNUNCIATO, non zero e non
            #    alla fine: la voce dice "passiamo alla slide due" dopo
            #    l'introduzione (circa al secondo 7).
            self.assertGreater(inizi[1], 2.0, f"slide 2 a {inizi[1]}s: ancora persa")
            self.assertLess(inizi[1], 20.0, f"slide 2 a {inizi[1]}s: ancora sbagliata")

            # 4. L'ultima slide copre la coda dell'audio. La durata si legge
            #    dal file: rigenerare la fixture non deve rompere il test.
            self.assertAlmostEqual(
                float(timeline[-1]["end"]), _durata_audio(AUDIO), delta=6.0
            )

            # 5. Il riepilogo leggibile e' accanto al video.
            self.assertTrue((base / "riepilogo.md").exists(), "manca riepilogo.md")

            # 6. Il video precedente non esisteva: nessun backup da creare.
            self.assertFalse((base / "video_finale (precedente).mp4").exists())

    @slow("pipeline intera con una trascrizione da riprendere")
    def test_riprende_da_meta_audio(self) -> None:
        """Meta' audio gia' trascritto: la run decodifica la coda e lo dice."""
        self._salta_se_manca_qualcosa()

        from config import DEFAULT_WHISPER_BATCH, DEFAULT_WHISPER_BEAM
        from transcription import _salva_parziale

        # Gli STESSI parametri che main passa di default: la ripresa e'
        # ammessa solo a parita' di modello/beam/lingua/batch.
        parametri = {
            "engine": "faster-whisper",
            "model": "small",
            # Stessi valori della run reale: il parziale ora include device e
            # compute type e uno scritto con i vecchi parametri verrebbe
            # scartato come "altra decodifica".
            "device": "cpu",
            "compute_type": "int8",
            "beam": int(DEFAULT_WHISPER_BEAM),
            "language": "it",
            "vad": True,
            "batch": int(DEFAULT_WHISPER_BATCH),
        }
        with tempfile.TemporaryDirectory(prefix="e2e_ripresa_") as tmp:
            base, _pdf, cache = self._prepara(tmp)
            with mock.patch("config.CACHE_DIR", cache):
                _salva_parziale(
                    AUDIO, [{"word": "ciao", "start": 0.5}], coperto_fino=6.0, params=parametri
                )

            esito, output, video = self._esegui(base, cache)
            self.assertEqual(esito.returncode, 0, output[-4000:])
            self.assertIn("Riprendo da", output, output[-3000:])
            self.assertIn("parole recuperate dal parziale", output, output[-3000:])
            self.assertTrue(video.exists())

            timeline = json.loads((cache / "timeline_finale.json").read_text(encoding="utf-8"))
            self.assertEqual([s["slide"] for s in timeline], list(range(1, PAGINE + 1)))
            # Il riepilogo lo dice: il testo viene da due passate.
            riepilogo = (base / "riepilogo.md").read_text(encoding="utf-8")
            self.assertIn("Trascrizione ripresa da", riepilogo)

    @slow("pipeline intera con --no-ripresa")
    def test_no_ripresa_decodifica_tutto(self) -> None:
        """Con --no-ripresa il parziale viene ignorato e poi cancellato."""
        self._salta_se_manca_qualcosa()

        from config import DEFAULT_WHISPER_BATCH, DEFAULT_WHISPER_BEAM
        from transcription import _salva_parziale, percorso_parziale

        parametri = {
            "engine": "faster-whisper",
            "model": "small",
            # Stessi valori della run reale: il parziale ora include device e
            # compute type e uno scritto con i vecchi parametri verrebbe
            # scartato come "altra decodifica".
            "device": "cpu",
            "compute_type": "int8",
            "beam": int(DEFAULT_WHISPER_BEAM),
            "language": "it",
            "vad": True,
            "batch": int(DEFAULT_WHISPER_BATCH),
        }
        with tempfile.TemporaryDirectory(prefix="e2e_noripresa_") as tmp:
            base, _pdf, cache = self._prepara(tmp)
            with mock.patch("config.CACHE_DIR", cache):
                _salva_parziale(
                    AUDIO, [{"word": "ciao", "start": 0.5}], coperto_fino=6.0, params=parametri
                )
                parziale = percorso_parziale(AUDIO)

            esito, output, _video = self._esegui(base, cache, "--no-ripresa")
            self.assertEqual(esito.returncode, 0, output[-4000:])
            self.assertIn("--no-ripresa: ignoro il parziale", output, output[-3000:])
            self.assertNotIn("Riprendo da", output)
            # Finita la decodifica intera il parziale viene comunque rimosso.
            self.assertFalse(parziale.exists())


if __name__ == "__main__":
    unittest.main()
