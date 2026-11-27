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
import math
import shutil
import sys
import tempfile
import time
from pathlib import Path

import verifica_pc
from config import CACHE_DIR, versione_programma
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


def _mtime_sicuro(path: Path) -> float:
    """mtime, o -inf se il file e' sparito/irraggiungibile.

    Antivirus e OneDrive possono togliere un file fra l'elenco e lo ``stat``:
    un solo candidato illeggibile non deve far sparire TUTTI gli altri (ne'
    far credere che il video non esista). Stessa regola di
    ``pipeline_inputs._mtime_legibile``.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


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


def _solo_numeri(valore: object, profondita: int = 0) -> object | None:
    """Sottoinsieme di un report con SOLI numeri/flag, mai testo libero.

    Il bundle e' fatto per essere inviato a chi assiste: il report grezzo
    conteneva `correzioni_nomi` (parole dell'audio), `sorgenti` e i percorsi
    assoluti delle anteprime, mentre la console prometteva "solo numeri".
    Qui si conservano conteggi, tempi e esiti; tutto il resto si perde.
    """
    if isinstance(valore, bool):
        return valore
    if isinstance(valore, (int, float)):
        return valore if math.isfinite(float(valore)) else None
    # Il guard va DOPO i numeri e a profondita' 3: i segmenti del report
    # sono dict di numeri a profondita' 2 e con il vecchio limite 2
    # sparivano tutti dal bundle, proprio i dati per diagnosticare il video.
    if profondita >= 3:
        return None
    if isinstance(valore, dict):
        pulito = {
            str(k): v
            for k, v in ((k, _solo_numeri(v, profondita + 1)) for k, v in valore.items())
            if v is not None
        }
        return pulito or None
    if isinstance(valore, list):
        voci = [v for v in (_solo_numeri(v, profondita + 1) for v in valore[:50]) if v is not None]
        return voci or None
    return None


def _conta_parole(cache: Path) -> dict[str, object]:
    """Quante parole nelle cache di trascrizione (conteggi, mai il testo)."""
    try:
        files = sorted(cache.glob("transcript_*.json"), key=_mtime_sicuro)
    except OSError:
        return {"file": None, "parole": None, "nota": "cache non leggibile"}
    if not files:
        return {"file": None, "parole": None, "nota": "nessuna trascrizione in cache"}
    try:
        dati = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"file": files[-1].name, "parole": None, "nota": "file illeggibile"}
    # Un JSON valido ma con la forma sbagliata (lista, oppure "words_raw" non
    # lista) non deve sollevare AttributeError/TypeError dentro il bundle.
    if not isinstance(dati, dict):
        return {"file": files[-1].name, "parole": None, "nota": "formato inatteso"}
    parole = dati.get("words_raw")
    if not isinstance(parole, list):
        return {"file": files[-1].name, "parole": None, "nota": "formato inatteso"}
    return {"file": files[-1].name, "parole": len(parole)}


def _stato_run(radice: Path) -> dict[str, object]:
    """Ultima run in numeri: materiali (nomi+byte), video, slide, parole.

    La cache e' QUELLA DELLA RUN, non radice/'.cache' scritto a mano:
    `SYNC_VIDEO_CACHE_DIR` la sposta (test, dischi diversi), e il bundle
    serviva proprio a spiegare una run che non si trovava piu'.
    """
    cache = CACHE_DIR
    stato: dict[str, object] = {}
    try:
        voci = sorted(radice.iterdir())
    except OSError:
        voci = []
    materiali: list[dict[str, object]] = []
    for p in voci:
        if p.name.startswith(".") or not _sicuro(p.name):
            continue
        try:
            if p.is_file():
                materiali.append({"nome": p.name, "byte": p.stat().st_size})
        except OSError:
            # Un file sparito non deve cancellare l'elenco degli altri.
            continue
    stato["file_in_cartella"] = materiali
    try:
        # `--output lezione.mp4` e' una scelta supportata: il bundle non puo'
        # cercare solo il nome di default e dire "video assente" quando il
        # video prodotto e' li' accanto. Si prende l'MP4 piu' recente, senza
        # le copie "(precedente)" del backup.
        video = max(
            (p for p in radice.glob("*.mp4") if "precedente" not in p.name),
            key=_mtime_sicuro,
            default=None,
        )
        if video is not None and _mtime_sicuro(video) == float("-inf"):
            video = None
        stato["video"] = {"byte": video.stat().st_size} if video is not None else None
    except OSError:
        stato["video"] = None
    try:
        report_raw = (cache / "sync_report.json").read_text(encoding="utf-8")
        report = json.loads(report_raw)
    except (OSError, ValueError):
        stato["report"] = None
    else:
        # Whitelist negativa: solo numeri/flag, mai stringhe libere, nomi
        # corretti dall'audio o percorsi delle anteprime.
        if isinstance(report, dict):
            pulito = {
                chiave: valore
                for chiave, valore in (
                    (k, _solo_numeri(v)) for k, v in report.items()
                )
                if valore is not None
            }
            stato["report"] = pulito or None
        else:
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
    # Nome UNIVOCO: `mkdir(exist_ok=True)` su un nome prevedibile riadottava
    # una cartella lasciata da un avvio/interruzione precedente, e lo zip
    # archiviava (e poi cancellava!) file arbitrari dell'utente.
    cartella = Path(tempfile.mkdtemp(prefix=f"diagnostica-{marca}-", dir=str(radice)))
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
        # La cartella temporanea ha un suffisso unico: metterlo anche nello
        # zip evita che due bundle nello stesso secondo si sovrascrivano.
        suffisso = cartella.name.rsplit("-", 1)[-1]
        base_zip = str(radice / f"diagnostica-{marca}-{suffisso}")
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
