#!/usr/bin/env python3
"""La pipeline INTERA, con materiale vero (lento).

Tutti gli altri test sono finti: l'OCR di un PDF finto, la trascrizione di un
audio finto, un video costruito al volo. Qui c'e' il percorso reale:

    PDF vero -> Tesseract -> voce vera (fixture) -> faster-whisper
    -> ancore "slide N" -> timeline -> ffmpeg -> MP4

E' l'unico test che si accorge di un collegamento rotto: un argomento di
ffmpeg sbagliato, un nome di file, l'ordine degli import, un default che
cambia. Costa ~1 minuto, quindi gira solo con SYNC_VIDEO_RUN_SLOW=1 (job
notturno); in locale:

    SYNC_VIDEO_RUN_SLOW=1 python -m unittest test_end_to_end_minimo -v

Il PDF si genera qui (pymupdf), l'audio e' ``test_fixtures/minimo.m4a``: 33
secondi di voce italiana che annuncia "passiamo alla slide due/tre/quattro/
cinque".
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_ambiente import eseguibile_tesseract
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


class TestPipelineCompleta(unittest.TestCase):
    @slow("pipeline intera su materiale vero: OCR, Whisper, embeddings, ffmpeg")
    def test_cinque_slide_con_annunci(self) -> None:
        mancanti = _strumenti_mancanti()
        if mancanti:
            self.skipTest("manca: " + ", ".join(mancanti))

        with tempfile.TemporaryDirectory(prefix="e2e_") as tmp:
            base = Path(tmp)
            pdf = base / "presentazione.pdf"
            _pdf_con_numeri(pdf)
            cache = base / ".cache"
            video = base / "video_finale.mp4"

            comando = [
                sys.executable,
                str(SRC / "main.py"),
                "--pdf",
                str(pdf),
                "--audio",
                str(AUDIO),
                "--output",
                str(video),
                "--slides-dir",
                str(base / "slides"),
                "--semantic-cache-dir",
                _cartella_embedding_reale(),
                "--transcriber",
                "whisper",
                "--whisper-model",
                "small",
                "--no-update-check",
                "--no-auto-setup",
                "--no-confirm",
            ]
            ambiente = {
                **os.environ,
                "SYNC_VIDEO_CACHE_DIR": str(cache),
                "PYTHONIOENCODING": "utf-8",
            }
            esito = subprocess.run(
                comando,
                cwd=str(base),
                env=ambiente,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT,
            )
            output = (esito.stdout or "") + (esito.stderr or "")
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

            # 4. L'ultima slide copre la coda dell'audio (33.5s).
            self.assertAlmostEqual(float(timeline[-1]["end"]), 33.5, delta=6.0)

            # 5. Il riepilogo leggibile e' accanto al video.
            self.assertTrue((base / "riepilogo.md").exists(), "manca riepilogo.md")

            # 6. Il video precedente non esisteva: nessun backup da creare.
            self.assertFalse((base / "video_finale (precedente).mp4").exists())


if __name__ == "__main__":
    unittest.main()
