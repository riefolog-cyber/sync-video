#!/usr/bin/env python3
"""Raccoglie un bundle diagnostico da inviare a chi assiste (P3).

Sostituisce gli screenshot tagliati: versione, referto verifica, report
dell'ultima run e conteggi — mai i contenuti (niente audio, slide, video,
transcript intere, .env). Scrive solo il proprio zip nella cartella.

Non installa e non modifica il progetto: gira anche su un PC rotto. Niente
import pesanti in cima (stesso vincolo di verifica_pc).

Esegui con: python src/diagnostica.py (o diagnostica.bat)
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import verifica_pc
from config import versione_programma
from hardware import project_root
from verifica_pc import Relazione

# Pacchetti di cui interessa la versione (ognuno protetto: assente = "n/d").
_PACCHETTI = (
    "pillow",
    "moviepy",
    "fastembed",
    "faster-whisper",
    "numpy",
    "tqdm",
    "pytesseract",
    "pymupdf",
    "packaging",
    "av",
)

# Mai nel bundle: segreti e trascrizioni intere. I NOMI dei materiali
# (podcast.m4a, presentazione.pdf) si possono elencare con le dimensioni:
# servono a vedere se il file e' al posto giusto, e non sono contenuti.
# La raccolta elenca comunque solo nomi+byte, mai bytes dei file.
_ESCLUSI_NOMI = frozenset({"Audio", ".env", "transcript_raw.txt"})


def _sicuro(nome: str) -> bool:
    """True se il NOME del file puo' finire nel bundle (mai contenuti)."""
    return nome not in _ESCLUSI_NOMI


def _righe_verifica(radice: Path) -> list[str]:
    """Il referto verifica_pc come righe di testo (mai un'eccezione)."""
    rep = Relazione()
    verifica_pc._esegui_prove(rep, verifica_pc._prove(radice))
    return verifica_pc._righe(rep)


def _versioni() -> dict[str, object]:
    """Python, app e pacchetti chiave (assenti = "n/d", mai un errore)."""
    try:
        python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    except Exception:  # noqa: BLE001 - mai un traceback dal diagnostico
        python = "n/d"
    pacchetti: dict[str, str] = {}
    try:
        import importlib.metadata

        for nome in _PACCHETTI:
            try:
                pacchetti[nome] = importlib.metadata.version(nome)
            except Exception:  # noqa: BLE001 - pacchetto assente o metadata rotta
                pacchetti[nome] = "n/d"
    except Exception:  # noqa: BLE001 - idem
        pass
    try:
        app = versione_programma()
    except Exception:  # noqa: BLE001 - mai un traceback dal diagnostico
        app = "n/d"
    return {"python": python, "app": app, "pacchetti": pacchetti}


def _conta_parole(cache: Path) -> dict[str, object]:
    """Quante parole nelle cache di trascrizione (conteggi, mai il testo)."""
    try:
        files = sorted(cache.glob("transcript_*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return {"file": None, "parole": None, "nota": "cache non leggibile"}
    if not files:
        return {"file": None, "parole": None, "nota": "nessuna trascrizione in cache"}
    try:
        dati = json.loads(files[-1].read_text(encoding="utf-8"))
        parole = dati.get("words_raw") or []
        return {"file": files[-1].name, "parole": len(parole)}
    except (OSError, ValueError):
        return {"file": files[-1].name, "parole": None, "nota": "file illeggibile"}


def _stato_run(radice: Path) -> dict[str, object]:
    """Ultima run in numeri: materiali (nomi+byte), video, slide, parole."""
    cache = radice / ".cache"
    stato: dict[str, object] = {}
    try:
        materiali = [
            {"nome": p.name, "byte": p.stat().st_size}
            for p in sorted(radice.iterdir())
            if p.is_file() and not p.name.startswith(".") and _sicuro(p.name)
        ]
    except OSError:
        materiali = []
    stato["file_in_cartella"] = materiali
    try:
        video = radice / "video_finale.mp4"
        stato["video"] = (
            {"byte": video.stat().st_size} if video.is_file() else None
        )
    except OSError:
        stato["video"] = None
    try:
        report_raw = (cache / "sync_report.json").read_text(encoding="utf-8")
        report = json.loads(report_raw)
        stato["report"] = report
    except (OSError, ValueError):
        stato["report"] = None
    try:
        righe = (cache / "timing_history.jsonl").read_text(encoding="utf-8").splitlines()
        stato["timing_ultime"] = righe[-5:]
    except OSError:
        stato["timing_ultime"] = []
    stato["trascrizione"] = _conta_parole(cache)
    try:
        slide = list((radice / "temp_slides").glob("slide_*.png")) if (radice / "temp_slides").exists() else []
        stato["slide_renderizzate"] = len(slide)
    except OSError:
        stato["slide_renderizzate"] = None
    try:
        stato["tentativo_gpu_incompiuto"] = (cache / "openvino_attempt.json").exists()
    except OSError:
        stato["tentativo_gpu_incompiuto"] = None
    return stato


def scrivi_bundle(radice: Path) -> Path:
    """Scrive diagnostica-<data>.zip nella cartella e ne restituisce il percorso.

    Ogni sezione e' protetta: se qualcosa non si legge, nel bundle finisce una
    nota invece di far saltare tutto.
    """
    marca = time.strftime("%Y%m%d-%H%M%S")
    cartella = radice / f"diagnostica-{marca}"
    cartella.mkdir(parents=True, exist_ok=True)
    try:
        try:
            (cartella / "verifica.txt").write_text(
                "\n".join(_righe_verifica(radice)) + "\n", encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001 - il bundle non deve mai fallire qui
            (cartella / "verifica.txt").write_text(f"non disponibile: {e}\n", encoding="utf-8")
        try:
            (cartella / "versioni.json").write_text(
                json.dumps(_versioni(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:  # noqa: BLE001 - idem
            (cartella / "versioni.json").write_text(
                json.dumps({"errore": str(e)}, ensure_ascii=False), encoding="utf-8"
            )
        try:
            (cartella / "run.json").write_text(
                json.dumps(_stato_run(radice), indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001 - idem
            (cartella / "run.json").write_text(
                json.dumps({"errore": str(e)}, ensure_ascii=False), encoding="utf-8"
            )
        base_zip = str(radice / f"diagnostica-{marca}")
        return Path(shutil.make_archive(base_zip, "zip", root_dir=str(radice), base_dir=cartella.name))
    finally:
        shutil.rmtree(cartella, ignore_errors=True)


def main() -> int:
    radice = project_root()
    try:
        percorso = scrivi_bundle(radice)
    except Exception as e:  # noqa: BLE001 - ultima rete: mai un traceback all'utente
        print(f"\n   [Diagnostica] impossibile scrivere il bundle: {e}")
        return 1
    print()
    print("=" * 72)
    print("   Diagnostica pronta: invia questo file a chi ti assiste.")
    print(f"   {percorso}")
    print("   Contiene versioni, referto e numeri dell'ultima run.")
    print("   NON contiene audio, slide, video ne' trascrizioni.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
