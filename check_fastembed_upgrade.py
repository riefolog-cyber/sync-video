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
         slide, z-score per colonna, argmax per blocco -> frazione di blocchi
         in cui la slide "best" e' identica;
  5. emette un verdetto: EQUIVALENTE (aggiornabile) o DIVERGENTE (tieni il pin).

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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
WORKER = BASE_DIR / "_embed_candidate_worker.py"

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
def _newest(pattern: str) -> Path | None:
    files = sorted(CACHE_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def collect_real_texts(
    slide_cache: Path | None = None,
    transcript_cache: Path | None = None,
    max_slides: int = 100,
) -> dict[str, list[str]] | None:
    """Restituisce {"slides": [...], "blocks": [...]} dai cache reali."""
    slides_path = slide_cache or _newest("slides_*.json")
    transcript_path = transcript_cache or _newest("transcript_*.json")
    if not slides_path or not transcript_path:
        return None

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
    cache_dir: str | None = None,
) -> np.ndarray | None:
    """Embedding di baseline con la fastembed installata (via semantic_sync)."""
    cache_dir = cache_dir or str(CACHE_DIR / "embedding_model")
    from semantic_sync import _load_embed_model, _make_embed_fn

    model_obj = _load_embed_model(model, cache_dir, alternate_name=ALTERNATE_MODEL)
    if model_obj is None:
        return None
    embed_fn = _make_embed_fn(model_obj)
    emb = embed_fn(texts)
    return np.asarray(emb, dtype=np.float32) if emb is not None else None


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
        subprocess.check_call([sys.executable, "-m", "venv", str(venv_dir)])
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
    cache_dir: str | None = None,
    venv_dir: Path | None = None,
) -> tuple[np.ndarray | None, Path | None]:
    """Embedding della candidata tramite venv isolata. Restituisce (emb, venv_dir)."""
    own_venv = venv_dir is None
    venv_dir = venv_dir or Path(tempfile.mkdtemp(prefix="fastembed_ab_"))
    cache_dir = cache_dir or str(CACHE_DIR / "embedding_model")

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


# =====================================================================
# Confronto e verdetto
# =====================================================================
def compare(
    base: np.ndarray,
    cand: np.ndarray,
    n_slides: int,
    sim_mean_threshold: float = DEFAULT_SIM_MEAN_THRESHOLD,
    decision_match_threshold: float = DEFAULT_DECISION_MATCH_THRESHOLD,
) -> dict[str, float | str]:
    """Confronta baseline vs candidata sugli stessi testi.

    - cosine per vettore (le righe sono gli stessi testi in ordine);
    - stabilita' della decisione di sync: con i primi `n_slides` come slide e
      il resto come blocchi, calcola la matrice (B,N), lo z-score per colonna
      e la frazione di blocchi in cui l'argmax (slide best) coincide.
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

    def real_starts(sim: np.ndarray) -> np.ndarray:
        znorm = zscore_matrix(sim)
        comp = competition_matrix(znorm)
        starts = monotonic_alignment(comp, candidates, min_gap)
        if starts is None:
            return np.full(n_slides, -1)
        return np.asarray(starts, dtype=int)

    base_start = real_starts(base_sim)
    cand_start = real_starts(cand_sim)
    match = float((base_start == cand_start).mean()) if base_start.size else 1.0

    def zscore_best(sim: np.ndarray) -> np.ndarray:
        std = sim.std(axis=0)
        std[std < 1e-9] = 1e-9
        z = (sim - sim.mean(axis=0)) / std
        return cast(np.ndarray, np.argmax(z, axis=1) + 1)  # 1-based slide

    base_best = zscore_best(base_sim)
    cand_best = zscore_best(cand_sim)
    argmax_match = float((base_best == cand_best).mean()) if base_best.size else 1.0

    verdict = "EQUIVALENTE" if (
        cosine.mean() >= sim_mean_threshold and match >= decision_match_threshold
    ) else "DIVERGENTE"

    return {
        "n_vectors": float(n),
        "n_slides": float(n_slides),
        "n_blocks": float(base_best.size),
        "cosine_mean": float(cosine.mean()),
        "cosine_min": float(cosine.min()),
        "decision_match": match,
        "argmax_match": argmax_match,
        "verdict": str(verdict),
        "threshold_sim_mean": float(sim_mean_threshold),
        "threshold_decision_match": float(decision_match_threshold),
    }


# =====================================================================
# CLI
# =====================================================================
def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test A/B isolato fastembed")
    parser.add_argument("--candidate", default=None, help="Versione candidata (default: ultima PyPI)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--report", default=str(CACHE_DIR / "fastembed_ab.json"))
    parser.add_argument("--keep-venv", action="store_true", help="Non cancellare la venv temporanea")
    parser.add_argument("--sim-mean", type=float, default=DEFAULT_SIM_MEAN_THRESHOLD)
    parser.add_argument("--decision-match", type=float, default=DEFAULT_DECISION_MATCH_THRESHOLD)
    args = parser.parse_args(argv)

    if not WORKER.exists():
        print(f"ERRORE: worker non trovato: {WORKER}", file=sys.stderr)
        return 2

    if args.candidate is None:
        import urllib.request

        try:
            with urllib.request.urlopen("https://pypi.org/pypi/fastembed/json", timeout=15) as r:
                args.candidate = json.load(r)["info"]["version"]
        except Exception as e:
            print(f"ERRORE: non riesco a determinare l'ultima versione: {e}", file=sys.stderr)
            return 3

    texts = collect_real_texts()
    if texts is None or not texts["slides"] or not texts["blocks"]:
        print("ERRORE: nessun dato reale in .cache (slides_*.json / transcript_*.json).", file=sys.stderr)
        return 4

    print(f"[Test A/B fastembed] baseline installata vs candidata {args.candidate}")
    print(f"  Testi reali: {len(texts['slides'])} slide, {len(texts['blocks'])} blocchi")

    all_texts = texts["slides"] + texts["blocks"]
    base = baseline_embeddings(all_texts, model=args.model)
    if base is None:
        print("ERRORE: baseline non calcolata.", file=sys.stderr)
        return 5
    print(f"  Baseline ({base.shape[0]}x{base.shape[1]}) OK")

    cand, venv_dir = candidate_embeddings(all_texts, args.candidate, model=args.model)
    if cand is None:
        print("ERRORE: candidata non calcolata (venv/install o embedding falliti).", file=sys.stderr)
        return 6
    print(f"  Candidata {args.candidate} ({cand.shape[0]}x{cand.shape[1]}) OK")

    result = compare(base, cand, n_slides=len(texts["slides"]), sim_mean_threshold=args.sim_mean,
                     decision_match_threshold=args.decision_match)
    result.update({"candidate": args.candidate, "model": args.model})

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n  Riepilogo: {len(texts['slides'])} slide, {len(texts['blocks'])} blocchi")
    print(f"  coseno medio  : {result['cosine_mean']:.4f} (min {result['cosine_min']:.4f})")
    print(f"  decision match: {result['decision_match']:.3f} ({result['n_blocks']:.0f} blocchi)")
    print(f"  argmax match  : {result['argmax_match']:.3f} (segnale grezzo per blocco)")
    print(f"\n  VERDETTO: {result['verdict']}")
    print(f"  Report: {report}")

    if venv_dir is not None and not args.keep_venv:
        shutil.rmtree(venv_dir, ignore_errors=True)

    return 0 if result["verdict"] == "EQUIVALENTE" else 1


if __name__ == "__main__":
    sys.exit(main())
