#!/usr/bin/env python3
"""
Test A/B isolato per decidere se aggiornare il pacchetto pinnato fastembed.

Perche' esiste il pin (vedi updates.py): fastembed 0.5.1 usa pooling CLS;
le versioni successive (>=0.6) usano mean pooling per e5-large, cambiando
gli embedding rispetto alla baseline validata A/B.

Questo test verifica EMPIRICAMENTE se la versione candidata produce embedding
realmente diversi su testi reali del progetto:

  1. raccoglie testi veri: slide (da .cache/slides_*.json) e blocchi
     (ricostruiti dalle parole di .cache/transcript_*.json);
  2. calcola gli embedding di baseline con la fastembed ATTUALMENTE
     installata (0.5.1) via semantic_sync;
  3. crea una venv TEMPORANEA isolata, installa la versione candidata e
     ricalcola gli embedding sugli STESSI testi (riusando la cache del
     modello: nessun nuovo download);
  4. confronta:
       - coseno-similarita' media/min per vettore tra baseline e candidata;
       - stabilita' della decisione di sync: matrice di similarita' blocchi x
         slide, z-score per colonna, allineamento monotono -> frazione di
         blocchi in cui l'inizio della slide "best" e' identico;
  5. emette un verdetto: EQUIVALENTE (aggiornabile) o DIVERGENTE (tieni il pin).
     Se anche solo uno dei due allineamenti fallisce il verdetto e'
     DIVERGENTE con match=0: -1 == -1 non e' una prova di equivalenza.

Baseline e candidata usano lo STESSO batch (config.DEFAULT_EMBED_BATCH, passato
al worker): il padding e' per-batch, quindi batch diversi darebbero vettori non
confrontabili. Il batch usato finisce nel report. Le cache sono quelle di
config (SYNC_VIDEO_CACHE_DIR / EMBEDDING_CACHE_DIR), non un .cache hardcoded.

Non modifica l'ambiente di lavoro: la candidata vive solo nella venv
temporanea (cancellata al termine a meno di --keep-venv).

Uso:
  python check_fastembed_upgrade.py                      # candidata = ultima PyPI
  python check_fastembed_upgrade.py --candidate 0.7.4    # versione specifica
  python check_fastembed_upgrade.py --report .cache/fastembed_ab.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import numpy as np

# Deve stare PRIMO: mette la radice nel sys.path, senza di lei i moduli core
# (hardware, fastembed...) non sono importabili da scripts/.
from _bootstrap import RADICE

# La radice la dà il bootstrap: `__file__` sta in scripts/, quindi `parent`
# qui sarebbe la cartella sbagliata.
BASE_DIR = RADICE


def _config_value(name: str, fallback: Any) -> Any:
    """Legge una costante di config, con fallback se config non e' importabile."""
    try:
        import config
    except ImportError:
        return fallback
    return getattr(config, name, fallback)


# config.CACHE_DIR rispetta SYNC_VIDEO_CACHE_DIR: il vecchio `.cache` hardcoded
# faceva cercare/leggere i cache nel posto sbagliato quando la cache e' altrove.
CACHE_DIR = Path(_config_value("CACHE_DIR", BASE_DIR / ".cache"))
# Stesso batch della baseline: `_make_embed_fn` usa config.DEFAULT_EMBED_BATCH e
# il padding e' per-batch, quindi la candidata deve ricevere questo valore.
DEFAULT_EMBED_BATCH = int(_config_value("DEFAULT_EMBED_BATCH", 64))

# Il worker e' suo, sta nella stessa cartella di questo script.
WORKER = Path(__file__).resolve().parent / "_embed_candidate_worker.py"

DEFAULT_MODEL = "intfloat/multilingual-e5-large"
ALTERNATE_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

# Soglie per il verdetto. Parametrizzate per permettere test dei sogliatori.
# Se la similarita' media per vettore supera SIM_MEAN e la frazione di
# decisioni identiche supera DECISION_MATCH, gli embedding sono considerati
# equivalenti e l'aggiornamento e' sicuro.
DEFAULT_SIM_MEAN_THRESHOLD = 0.90
DEFAULT_DECISION_MATCH_THRESHOLD = 0.95


# =====================================================================
# Raccolta testi reali
# =====================================================================
def _newest(pattern: str, cache_dir: Path | None = None) -> Path | None:
    d = cache_dir if cache_dir is not None else CACHE_DIR
    files = sorted(d.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _embedding_cache_dir(cache_dir: str | Path | None = None) -> str:
    """Cache dei modelli: quella usata dal flusso automatico (config.py)."""
    if cache_dir is not None:
        return str(cache_dir)
    return str(_config_value("DEFAULT_EMBEDDING_CACHE_DIR", CACHE_DIR / "embedding_model"))


# Token di run: prima il run_id nel JSON, altrimenti un timestamp/run tag nel
# nome file. Senza token (vecchi cache) si mantiene il comportamento storico.
_TIME_TOKEN_RE = re.compile(
    r"(?<!\d)(20\d{2}[-_]\d{2}[-_]\d{2}(?:[T_\- ]?\d{2}[:\-_]?\d{2}[:\-_]?\d{2})?|\d{8}[T_\-]?\d{6})(?!\d)"
)
_RUN_TOKEN_RE = re.compile(r"(?i)run[-_]([a-z0-9]+)")


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _run_token(path: Path, data: dict | None) -> str | None:
    """Identificatore della run di un cache, o None se non dichiarato."""
    if data:
        rid = data.get("run_id") or data.get("runId")
        if rid is not None and str(rid).strip():
            return f"id:{rid}"
    match = _RUN_TOKEN_RE.search(path.stem)
    if match:
        return f"run:{match.group(1)}"
    match = _TIME_TOKEN_RE.search(path.stem)
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        return f"ts:{digits}" if digits else None
    return None


def _find_with_token(tokens: dict[Path, str | None], token: str) -> Path | None:
    """File piu' recente con quel token di run, se esiste."""
    files = sorted(tokens, key=lambda p: p.stat().st_mtime, reverse=True)
    return next((p for p in files if tokens[p] == token), None)


def _pair_cache_files(
    cache_dir: Path,
    slide_cache: Path | None,
    transcript_cache: Path | None,
) -> tuple[Path, Path] | None:
    """Sceglie slide e trascrizione della STESSA run; None se non coerenti."""
    slides_path: Path | None
    transcript_path: Path | None
    if slide_cache is not None and transcript_cache is not None:
        slides_path, transcript_path = slide_cache, transcript_cache
        ts = _run_token(slides_path, _read_json(slides_path))
        tt = _run_token(transcript_path, _read_json(transcript_path))
        if ts and tt and ts != tt:
            print(
                f"ERRORE: cache di run diverse ({slides_path.name}: {ts}, "
                f"{transcript_path.name}: {tt}).",
                file=sys.stderr,
            )
            return None
        return slides_path, transcript_path

    slides_path = _newest("slides_*.json", cache_dir)
    transcript_path = _newest("transcript_*.json", cache_dir)
    if slides_path is None or transcript_path is None:
        return None

    # I due file PIU' recenti possono venire da run diverse: cerca prima una
    # coppia con lo stesso token, poi rifiuta l'accoppiamento invece di
    # confrontare testi che non sono mai appartenuti alla stessa run.
    slide_tokens = {p: _run_token(p, _read_json(p)) for p in cache_dir.glob("slides_*.json")}
    trans_tokens = {p: _run_token(p, _read_json(p)) for p in cache_dir.glob("transcript_*.json")}
    ts, tt = slide_tokens.get(slides_path), trans_tokens.get(transcript_path)
    if ts is not None and tt is not None and ts != tt:
        alt_tr = _find_with_token(trans_tokens, ts)
        alt_sl = _find_with_token(slide_tokens, tt)
        if alt_tr is not None:
            transcript_path = alt_tr
        elif alt_sl is not None:
            slides_path = alt_sl
        else:
            print(
                "ERRORE: cache slides/transcript di run diverse "
                f"({slides_path.name}: {ts}, {transcript_path.name}: {tt}). "
                "Riesegui la run o rimuovi i cache inattuali.",
                file=sys.stderr,
            )
            return None
    elif ts is not None and tt is None:
        alt_tr = _find_with_token(trans_tokens, ts)
        if alt_tr is not None:
            transcript_path = alt_tr
    elif tt is not None and ts is None:
        alt_sl = _find_with_token(slide_tokens, tt)
        if alt_sl is not None:
            slides_path = alt_sl
    return slides_path, transcript_path


def collect_real_texts(
    slide_cache: Path | None = None,
    transcript_cache: Path | None = None,
    max_slides: int = 100,
    cache_dir: Path | None = None,
) -> dict[str, list[str]] | None:
    """Restituisce {"slides": [...], "blocks": [...]} dai cache reali.

    I percorsi espliciti hanno precedenza; altrimenti si cerca una coppia
    coerente per token/timestamp di run (o run_id nel JSON) e si rifiutano le
    coppie di run diverse.
    """
    d = cache_dir if cache_dir is not None else CACHE_DIR
    pair = _pair_cache_files(d, slide_cache, transcript_cache)
    if pair is None:
        return None
    slides_path, transcript_path = pair

    slides: list[str] = []
    try:
        sd = json.loads(slides_path.read_text(encoding="utf-8"))
        slides = [str(t) for t in sd.get("slide_texts", [])][:max_slides]
    except (OSError, json.JSONDecodeError):
        return None

    words: list[dict] = []
    try:
        td = json.loads(transcript_path.read_text(encoding="utf-8"))
        words = [w for w in td.get("words_raw", []) if isinstance(w, dict) and w.get("word")]
    except (OSError, json.JSONDecodeError):
        return None
    if not words:
        return None

    from chunks import Word
    from semantic_sync import build_semantic_blocks

    total_duration = float(words[-1].get("start", 0)) + 3.0
    word_list = [
        cast(Word, {"word": str(w["word"]), "start": float(w["start"])})
        for w in words
    ]
    blocks = build_semantic_blocks(word_list, total_duration)
    block_texts = [str(b["text"]) for b in blocks]

    return {"slides": slides, "blocks": block_texts}


# =====================================================================
# Baseline con fastembed installata (0.5.1)
# =====================================================================
def baseline_embeddings(
    texts: list[str],
    model: str = DEFAULT_MODEL,
    cache_dir: str | Path | None = None,
) -> np.ndarray | None:
    """Embedding di baseline con la fastembed installata (via interfaccia embedder).

    Il batch e' quello di config.DEFAULT_EMBED_BATCH, usato da `_make_embed_fn`
    dentro `load_embedder`: e' lo stesso valore passato al worker della
    candidata, quindi i vettori sono confrontabili.
    """
    from embedder import load_embedder

    emb = load_embedder(model, _embedding_cache_dir(cache_dir), alternate_name=ALTERNATE_MODEL)
    if emb is None:
        return None
    return np.asarray(emb.embed(texts), dtype=np.float32)


# =====================================================================
# Candidata in venv isolata
# =====================================================================
def _venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def setup_candidate_venv(candidate: str, venv_dir: Path) -> Path | None:
    """Crea venv e installa fastembed==candidate. Restituisce il python, o None."""
    if not (_venv_python(venv_dir)).exists():
        # Anche la creazione della venv puo' fallire (ensurepip mancante, disco
        # pieno, ACL) o pendere: senza controllo l'eccezione usciva prima che
        # il chiamante potesse ripulire la cartella temporanea.
        try:
            subprocess.check_call(
                [sys.executable, "-m", "venv", str(venv_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
    py = _venv_python(venv_dir)
    try:
        subprocess.check_call(
            [str(py), "-m", "pip", "install", "-q", f"fastembed=={candidate}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1800,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return py


def candidate_embeddings(
    texts: list[str],
    candidate: str,
    model: str = DEFAULT_MODEL,
    cache_dir: str | Path | None = None,
    venv_dir: Path | None = None,
    batch_size: int | None = None,
) -> tuple[np.ndarray | None, Path | None]:
    """Embedding della candidata tramite venv isolata. Restituisce (emb, venv_dir).

    `batch_size` deve essere quello della baseline (config.DEFAULT_EMBED_BATCH):
    il worker lo riceve esplicitamente per evitare padding per-batch diverso.
    """
    own_venv = venv_dir is None
    venv_dir = venv_dir or Path(tempfile.mkdtemp(prefix="fastembed_ab_"))
    try:
        cache_dir = _embedding_cache_dir(cache_dir)
        batch = DEFAULT_EMBED_BATCH if batch_size is None else batch_size

        texts_path = venv_dir / "texts.json"
        texts_path.write_text(json.dumps({"slides": texts}), encoding="utf-8")
        out = venv_dir / "emb.npy"

        py = setup_candidate_venv(candidate, venv_dir)
        if py is None:
            return None, (venv_dir if own_venv else None)

        try:
            subprocess.check_call(
                [
                    str(py),
                    str(WORKER),
                    str(texts_path),
                    model,
                    cache_dir,
                    str(out),
                    str(batch),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
            )
            emb = np.load(out)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None, (venv_dir if own_venv else None)

        if not own_venv:
            return emb, None
        return emb, venv_dir
    except BaseException:
        # Qualsiasi eccezione NON gestita qui (un .npy corrotto, un errore nel
        # write dei testi, KeyboardInterrupt) non deve lasciare una venv da
        # centinaia di MB sul disco. Il percorso di errore "gestito" restituisce
        # la cartella al chiamante, che la rimuove a sua volta.
        if own_venv:
            shutil.rmtree(venv_dir, ignore_errors=True)
        raise


# =====================================================================
# Confronto e verdetto
# =====================================================================
def compare(
    base: np.ndarray,
    cand: np.ndarray,
    n_slides: int,
    sim_mean_threshold: float = DEFAULT_SIM_MEAN_THRESHOLD,
    decision_match_threshold: float = DEFAULT_DECISION_MATCH_THRESHOLD,
) -> dict[str, float | str | bool]:
    """Confronta baseline vs candidata sugli stessi testi.

    - cosine per vettore (le righe sono gli stessi testi in ordine);
    - stabilita' della decisione di sync: con i primi `n_slides` come slide e
      il resto come blocchi, calcola la matrice (B,N), lo z-score per colonna
      e la frazione di blocchi in cui l'inizio della slide best coincide.

    Se `monotonic_alignment` non produce un allineamento valido per una delle
    due sequenze, `decision_match` e' 0 e il verdetto e' DIVERGENTE: il -1
    usato come segnaposto non puo' essere interpretato come "stesse decisioni".
    """
    n = min(base.shape[0], cand.shape[0])
    base, cand = base[:n], cand[:n]
    norms = np.linalg.norm(base, axis=1) * np.linalg.norm(cand, axis=1)
    norms[norms == 0] = 1.0
    cosine = (base * cand).sum(axis=1) / norms

    n_slides = min(n_slides, n - 1)
    # Matrice blocchi x slide: blocchi = righe [n_slides:], slide = colonne [0:n_slides]
    base_b = base[n_slides:]
    cand_b = cand[n_slides:]
    base_sl = base[:n_slides]
    cand_sl = cand[:n_slides]

    base_sim = base_b @ base_sl.T  # (B, N)
    cand_sim = cand_b @ cand_sl.T

    # Decisione REALE del pipeline: stessa normalizzazione (z-score per-slide),
    # stessa competizione softmax e stessa DP monotona di semantic_sync. Il
    # semplice argmax grezzo è instabile (il test A/B con cosine 0.93 dava solo
    # il 63% di decisioni identiche): ciò che conta è se la TIMELINE finale
    # cambia, non il singolo blocco.
    from semantic_sync import (
        build_candidates,
        competition_matrix,
        monotonic_alignment,
        zscore_matrix,
    )

    min_gap = 1
    candidates = build_candidates(len(base_b), n_slides, min_gap)
    if candidates is None:
        raise ValueError("candidates non costruibili: blocchi insufficienti")

    def real_starts(sim: np.ndarray) -> np.ndarray | None:
        znorm = zscore_matrix(sim)
        comp = competition_matrix(znorm)
        starts = monotonic_alignment(comp, candidates, min_gap)
        if starts is None:
            return None
        return np.asarray(starts, dtype=int)

    base_start = real_starts(base_sim)
    cand_start = real_starts(cand_sim)
    # Allineamento fallito = esito non confrontabile, NON "tutti -1 uguali":
    # forzare match=0 evita il falso EQUIVALENTE (task "verdetto falso").
    if base_start is not None and cand_start is not None:
        alignment_valid = True
        match = float((base_start == cand_start).mean()) if base_start.size else 1.0
    else:
        alignment_valid = False
        match = 0.0

    def zscore_best(sim: np.ndarray) -> np.ndarray:
        std = sim.std(axis=0)
        std[std < 1e-9] = 1e-9
        z = (sim - sim.mean(axis=0)) / std
        return cast(np.ndarray, np.argmax(z, axis=1) + 1)  # 1-based slide

    base_best = zscore_best(base_sim)
    cand_best = zscore_best(cand_sim)
    argmax_match = float((base_best == cand_best).mean()) if base_best.size else 1.0

    verdict = "EQUIVALENTE" if (
        alignment_valid
        and cosine.mean() >= sim_mean_threshold
        and match >= decision_match_threshold
    ) else "DIVERGENTE"

    return {
        "n_vectors": float(n),
        "n_slides": float(n_slides),
        "n_blocks": float(base_best.size),
        "cosine_mean": float(cosine.mean()),
        "cosine_min": float(cosine.min()),
        "decision_match": match,
        "argmax_match": argmax_match,
        "alignment_valid": alignment_valid,
        "verdict": str(verdict),
        "threshold_sim_mean": float(sim_mean_threshold),
        "threshold_decision_match": float(decision_match_threshold),
    }


# =====================================================================
# Versione candidata (stessa regola del flusso automatico)
# =====================================================================
def _fallback_latest_compatible(data: dict) -> str | None:
    """Fallback equivalente a `updates._latest_compatible` se updates non importa.

    Esclude release con tutti i file yanked, pre-release e quelle il cui
    `requires_python` non contiene il Python corrente: la vecchia lettura di
    `info.version` poteva proporre una versione che pip non installerebbe.
    """
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    best: tuple[Any, str] | None = None
    for version_str, files in data.get("releases", {}).items():
        validi = [f for f in files if not f.get("yanked")] if files else []
        if not validi:
            continue
        try:
            version = Version(version_str)
        except InvalidVersion:
            continue
        if version.is_prerelease:
            continue
        requires_python = next((f.get("requires_python") for f in validi if f.get("requires_python")), None)
        if requires_python and not SpecifierSet(requires_python).contains(
            f"{sys.version_info.major}.{sys.version_info.minor}"
        ):
            continue
        if best is None or version > best[0]:
            best = (version, version_str)
    return best[1] if best else None


def _candidate_version_from_pypi() -> str | None:
    """Ultima versione INSTALLABILE di fastembed (regola condivisa con updates.py)."""
    try:
        # Import locale: updates importa questo script solo dentro una funzione,
        # quindi non c'e' dipendenza circolare al livello dei moduli.
        from updates import _fetch_pypi_json, _latest_compatible
    except ImportError:
        import urllib.request

        try:
            req = urllib.request.Request(
                "https://pypi.org/pypi/fastembed/json",
                headers={"User-Agent": "slide2video-update-check/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return _fallback_latest_compatible(data)
        except Exception:
            return None

    data = _fetch_pypi_json("fastembed")
    if data is None:
        return None
    try:
        return _latest_compatible(data)
    except Exception:
        return None


# =====================================================================
# CLI
# =====================================================================
def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test A/B isolato fastembed")
    parser.add_argument("--candidate", default=None, help="Versione candidata (default: ultima installabile su PyPI)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Cartella cache della run (default: config.CACHE_DIR); i modelli stanno in <dir>/embedding_model",
    )
    parser.add_argument("--report", default=str(CACHE_DIR / "fastembed_ab.json"))
    parser.add_argument("--keep-venv", action="store_true", help="Non cancellare la venv temporanea")
    parser.add_argument("--sim-mean", type=float, default=DEFAULT_SIM_MEAN_THRESHOLD)
    parser.add_argument("--decision-match", type=float, default=DEFAULT_DECISION_MATCH_THRESHOLD)
    args = parser.parse_args(argv)

    if not WORKER.exists():
        print(f"ERRORE: worker non trovato: {WORKER}", file=sys.stderr)
        return 2

    if args.candidate is None:
        args.candidate = _candidate_version_from_pypi()
        if args.candidate is None:
            print(
                "ERRORE: non riesco a determinare l'ultima versione INSTALLABILE di fastembed "
                "(rete assente o nessuna release compatibile).",
                file=sys.stderr,
            )
            return 3

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    texts = collect_real_texts(cache_dir=cache_dir)
    if texts is None or not texts["slides"] or not texts["blocks"]:
        print("ERRORE: nessun dato reale coerente in cache (slides_*.json / transcript_*.json).", file=sys.stderr)
        return 4

    batch = DEFAULT_EMBED_BATCH
    print(f"[Test A/B fastembed] baseline installata vs candidata {args.candidate} (batch={batch})")
    print(f"  Testi reali: {len(texts['slides'])} slide, {len(texts['blocks'])} blocchi")

    # La cache dei modelli e' derivata da quella della run, come in config.py
    # (a meno di EMBEDDING_CACHE_DIR); updates.py la passa esplicita alla candidata.
    embedding_cache = _embedding_cache_dir(cache_dir / "embedding_model" if cache_dir else None)

    all_texts = texts["slides"] + texts["blocks"]
    base = baseline_embeddings(all_texts, model=args.model, cache_dir=embedding_cache)
    if base is None:
        print("ERRORE: baseline non calcolata.", file=sys.stderr)
        return 5
    print(f"  Baseline ({base.shape[0]}x{base.shape[1]}) OK")

    venv_dir: Path | None = None
    try:
        cand, venv_dir = candidate_embeddings(
            all_texts, args.candidate, model=args.model, cache_dir=embedding_cache, batch_size=batch
        )
        if cand is None:
            print("ERRORE: candidata non calcolata (venv/install o embedding falliti).", file=sys.stderr)
            return 6
        print(f"  Candidata {args.candidate} ({cand.shape[0]}x{cand.shape[1]}) OK")

        result = compare(base, cand, n_slides=len(texts["slides"]), sim_mean_threshold=args.sim_mean,
                         decision_match_threshold=args.decision_match)
        result.update({
            "candidate": args.candidate,
            "model": args.model,
            "batch_size": batch,
            "cache_dir": embedding_cache,
        })

        report = Path(args.report)
        report.parent.mkdir(parents=True, exist_ok=True)
        # Scrittura atomica come in updates.py: un taglio durante write_text
        # lasciava un JSON troncato al nome canonico, e il controllo A/B
        # veniva ripetuto (venv temporanea + 200 embedding) a ogni avvio.
        tmp_report = report.with_name(report.name + ".tmp")
        tmp_report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_report, report)

        print(f"\n  Riepilogo: {len(texts['slides'])} slide, {len(texts['blocks'])} blocchi")
        print(f"  coseno medio  : {result['cosine_mean']:.4f} (min {result['cosine_min']:.4f})")
        print(f"  decision match: {result['decision_match']:.3f} ({result['n_blocks']:.0f} blocchi)")
        print(f"  argmax match  : {result['argmax_match']:.3f} (segnale grezzo per blocco)")
        if not result["alignment_valid"]:
            print("  allineamento  : FALLITO -> DIVERGENTE (match forzato a 0)")
        print(f"\n  VERDETTO: {result['verdict']}")
        print(f"  Report: {report}")

        return 0 if result["verdict"] == "EQUIVALENTE" else 1
    finally:
        # La venv temporanea va rimossa anche se `compare` o la scrittura del
        # report sollevano: prima era l'ultima riga del solo percorso felice.
        if venv_dir is not None and not args.keep_venv:
            shutil.rmtree(venv_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
