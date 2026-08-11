#!/usr/bin/env python3
"""
Analisi approfondita di sincronizzazione del video finale.

Verifica a 360 gradi la riuscita della sincronizzazione:

  1. Timeline: durate per slide, segmenti corti/anomali, monotonicita.
  2. Allineamento audio <-> slide: similarita embedding (MiniLM in cache,
     media su finestre da 4s) e F1 lessicale per segmento; confronto slide
     mostrata vs "best" (argmax su tutte le slide).
  3. Confini: taglio a meta parola (parola a cavallo del confine), pausa
     prima/dopo il confine (taglio naturale vs a meta frase).
  4. Ancore: delta tra timestamp ancora dichiarato e inizio segmento reale.
   5. Frame estratti: per ogni segmento estrae un frame a meta segmento dal
      video e lo confronta con le slide renderizzate (temp_slides) tramite
      similarita di immagine (coseno su grayscale downscaled) per confermare
      cosa e davvero a schermo in ogni momento.
   6. Confini: per ogni confine estrae un frame subito dopo il taglio e
      verifica che a schermo sia apparsa la slide successiva (N+1).

Non modifica nulla: legge solo cache/video e scrive i frame estratti in
`.analysis_frames/`.

Strumento standalone di verifica post-run: NON fa parte della pipeline
(main.py non lo importa). Eseguito manualmente dopo una generazione per
controllare la qualità della sincronizzazione.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(r"C:/Users/Gianni/Desktop/sync video")
CACHE = BASE / ".cache"
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import numpy as np

from chunks import build_windows
from config import (
    DEFAULT_EMBEDDING_CACHE_DIR,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_MODEL_ALTERNATE,
    STOPWORDS_ITA,
)
from semantic_sync import _clean_slide_text, _load_embed_model, _make_embed_fn

TIMELINE_FILE = None
ANCHORS_FILE = None
SLIDES_FILE = None
TRANSCRIPT_FILE = None

# Auto-rilevamento dei file piu recenti della run corrente.
# Il file "timeline" ha voci con la chiave "end"; il file "ancore" ha voci
# con solo "slide" e "start" (senza "end").
#
# PREFERENZA timeline: se esiste "llm_timeline_finale.json" e' la timeline
# FINALE validata da main.py (start/end usati davvero per il video), salvata
# da ogni run anche nel flusso semantico MiniLM. Va preferita alle cache
# llm_*.json GREZZE: quelle contengono la timeline LLM pre-raffinamento e, in
# assenza del flusso LLM (semantico puro), sarebbero stale di una run
# precedente -> falsi mismatch. Il file "ancore" resta auto-rilevato dai
# llm_*.json senza chiave "end".
#
# NOTA: i file llm_*.json in cache contengono la timeline GREZZA prodotta
# dall'LLM. main.py però ri-raffina a ogni run i confini delle slide senza
# ancora esplicita (refine_llm_timeline_from_words), quindi il video finale è
# stato costruito con la timeline RAFFINATA, che può differire da quella in
# cache (es. confine spostato di qualche secondo). Le discrepanze segnalate
# qui possono quindi essere attese e NON indicare un video desincronizzato.
def _newest(pattern: str) -> Path:
    files = sorted(CACHE.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"ERRORE: nessun file {pattern} in {CACHE}")
        sys.exit(1)
    return files[0]


# 1) Timeline finale validata da main.py (se presente)
FINAL_TIMELINE = CACHE / "llm_timeline_finale.json"
if FINAL_TIMELINE.exists():
    TIMELINE_FILE = FINAL_TIMELINE
    print(f"[Verifica] Uso timeline finale validata: {FINAL_TIMELINE.name}")

# 2) Timeline LLM / ancore: auto-rilevamento (solo se non gia' impostata)
for f in sorted(CACHE.glob("llm_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
    if f == FINAL_TIMELINE:
        continue
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        continue
    if not isinstance(data, list) or not data:
        continue
    if all(isinstance(e, dict) and "end" in e for e in data) and TIMELINE_FILE is None:
        TIMELINE_FILE = f
    elif (
        all(isinstance(e, dict) and "start" in e and "end" not in e for e in data)
        and ANCHORS_FILE is None
    ):
        ANCHORS_FILE = f

TIMELINE_FILE = TIMELINE_FILE or _newest("llm_*.json")
SLIDES_FILE = _newest("slides_*.json")
TRANSCRIPT_FILE = _newest("transcript_*.json")
VIDEO = BASE / "video_finale.mp4"
FRAMES_DIR = BASE / ".analysis_frames"

# Durata audio: dal file m4a se disponibile, altrimenti dall'ultima parola.
try:
    from pydub import AudioSegment

    AUDIO_DURATION = len(AudioSegment.from_file(BASE / "podcast.m4a")) / 1000.0
except Exception:
    tmp_tc = json.loads(TRANSCRIPT_FILE.read_text(encoding="utf-8"))
    AUDIO_DURATION = max(float(w["end"]) for w in tmp_tc["words_raw"]) + 5.0
WORD_GAP_CUT = 0.4  # secondi: gap < soglia => taglio "a meta frase"
IMAGE_SIZE = (96, 54)

# ----------------------------------------------------------------------
# Caricamento dati
# ----------------------------------------------------------------------
timeline = json.loads(TIMELINE_FILE.read_text(encoding="utf-8"))
anchors_list = json.loads(ANCHORS_FILE.read_text(encoding="utf-8")) if ANCHORS_FILE else []
slides = json.loads(SLIDES_FILE.read_text(encoding="utf-8"))
tc = json.loads(TRANSCRIPT_FILE.read_text(encoding="utf-8"))

words = tc["words_raw"]
slide_texts = slides["slide_texts"]
slide_files = [Path(p) for p in slides["slide_files"]]
total_slides = len(slide_texts)

anchors = {int(a["slide"]): float(a["start"]) for a in anchors_list} if anchors_list else {}

# Segmenti reali: end = start della slide successiva, ultimo = fine audio
starts = [float(s["start"]) for s in timeline]
segs: list[dict] = []
for i, s in enumerate(starts):
    end = starts[i + 1] if i + 1 < len(starts) else AUDIO_DURATION
    segs.append({"slide": int(timeline[i]["slide"]), "start": s, "end": end})

# ----------------------------------------------------------------------
# Embedding
# ----------------------------------------------------------------------
model = _load_embed_model(
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_CACHE_DIR,
    alternate_name=DEFAULT_EMBEDDING_MODEL_ALTERNATE,
)
if model is None:
    print("ERRORE: modello embedding non caricabile.")
    sys.exit(1)
embed = _make_embed_fn(model)

slide_clean = [_clean_slide_text(t) for t in slide_texts]
slide_emb = embed(slide_clean)

# Finestre globali da 4s (come il pipeline)
windows = build_windows(words, AUDIO_DURATION, 4.0)
win_texts = [w["text"] for w in windows if w["words"]]
win_times = [w["start"] for w in windows if w["words"]]
win_emb = embed(win_texts)  # (W, D)


def seg_embedding(start: float, end: float) -> np.ndarray:
    idxs = [i for i, t in enumerate(win_times) if start <= t < end]
    if not idxs:
        vec = embed([_clean_slide_text(" ".join(w["word"] for w in words
                                                if start <= float(w["start"]) < end))])[0]
        return np.asarray(vec, dtype=np.float32)
    return np.asarray(win_emb[idxs].mean(axis=0), dtype=np.float32)


def seg_text(start: float, end: float) -> str:
    return " ".join(w["word"] for w in words if start <= float(w["start"]) < end)


def word_end(w: dict, idx: int) -> float:
    """Fine parola: inizio della successiva, o inizio + stima durata media."""
    end = w.get("end")
    if end is not None:
        return float(end)
    if idx + 1 < len(words):
        return float(words[idx + 1]["start"])
    return float(w["start"]) + 0.4


def keywords(text: str) -> set[str]:
    return set(re.findall(r"[a-zàèéìòù']+", text.lower())) - STOPWORDS_ITA


def lex_f1(a: str, b: str) -> float:
    A, B = keywords(a), keywords(b)
    if not A or not B:
        return 0.0
    inter = A & B
    p = len(inter) / len(A)
    r = len(inter) / len(B)
    return 2 * p * r / (p + r) if p + r else 0.0


# ----------------------------------------------------------------------
# 1+2. Tabella segmenti: durata, sim, best, F1
# ----------------------------------------------------------------------
print("=" * 100)
print("1. TIMELINE + ALLINEAMENTO AUDIO <-> SLIDE (embedding MiniLM + F1 lessicale)")
print("=" * 100)
print(f"{'sl':>3} {'inizio':>8} {'fine':>8} {'dur':>7} | {'sim':>6} {'best':>4} "
      f"{'b-sim':>6} {'rank':>4} {'F1':>5} | giudizio")
print("-" * 100)

n_best = 0
n_weak = 0
low_sim: list[tuple[int, float, float]] = []
shown_sims: list[float] = []
for seg in segs:
    s = seg["slide"]
    st, en = seg["start"], seg["end"]
    dur = en - st
    emb = seg_embedding(st, en)
    sims = emb @ slide_emb.T
    shown = float(sims[s - 1])
    shown_sims.append(shown)
    best_slide = int(np.argmax(sims)) + 1
    best_sim = float(sims.max())
    order = int((sims > shown).sum()) + 1  # rank della mostrata (1 = migliore)
    text = seg_text(st, en)
    f1 = lex_f1(text, slide_texts[s - 1])

    is_best = best_slide == s
    if is_best:
        n_best += 1
    if shown < 0.10:
        n_weak += 1
        low_sim.append((s, shown, best_sim))

    if is_best:
        verdict = "OK (best)"
    elif order <= 3:
        verdict = f"OK~ (best={best_slide})"
    else:
        verdict = f"<-- slide {s} vs best {best_slide}"
    print(
        f"{s:>3} {st:>8.1f} {en:>8.1f} {dur:>7.1f} | {shown:>6.3f} {best_slide:>4} "
        f"{best_sim:>6.3f} {order:>4} {f1:>5.3f} | {verdict}"
    )

print("-" * 100)
print(f"Segmenti in cui la slide mostrata E' la migliore per contenuto: {n_best}/{len(segs)}")
print(f"Segmenti con similarita bassa (< 0.10): {n_weak} {low_sim}")

# ----------------------------------------------------------------------
# 3. Confini: taglio a meta parola / a meta frase
# ----------------------------------------------------------------------
print()
print("=" * 100)
print("2. CONFINI: tagli a meta parola o a meta frase")
print("=" * 100)
cuts_word = 0
cuts_phrase = 0
cuts_pause = 0
# Tollera il rounding delle timeline salvate (main.py arrotonda a 3 decimali,
# le parole Whisper hanno ~16us di precisione): un confine a <50ms dall'inizio
# di una parola e' di fatto un taglio su confine di parola, non META-PAROLA.
BOUNDARY_SNAP_TOL = 0.05
boundaries = starts[1:]
for b in boundaries:
    # parola a cavallo del confine? (con tolleranza di snap su confine di parola)
    straddle = [
        w
        for i, w in enumerate(words)
        if float(w["start"]) + BOUNDARY_SNAP_TOL < b < word_end(w, i) - BOUNDARY_SNAP_TOL
    ]
    before = [w for i, w in enumerate(words) if word_end(w, i) <= b]
    after = [w for i, w in enumerate(words) if float(w["start"]) >= b]
    gap_before = b - word_end(before[-1], words.index(before[-1])) if before else 999.0
    gap_after = float(after[0]["start"]) - b if after else 999.0
    kind = "META-PAROLA" if straddle else ("pausa" if min(gap_before, gap_after) >= WORD_GAP_CUT else "a meta frase")
    if straddle:
        cuts_word += 1
    elif min(gap_before, gap_after) >= WORD_GAP_CUT:
        cuts_pause += 1
    else:
        cuts_phrase += 1
    prev_txt = " ".join(w["word"] for w in before[-8:])
    next_txt = " ".join(w["word"] for w in after[:8])
    print(f"  confine {b:>8.1f}s : {kind:11s} | ...{prev_txt} | {next_txt}...")

print(f"\nTagli a META PAROLA: {cuts_word} | a meta frase: {cuts_phrase} | su pausa naturale: {cuts_pause}")

# ----------------------------------------------------------------------
# 4. Ancore
# ----------------------------------------------------------------------
print()
print("=" * 100)
print("3. ANCORE: delta ancora dichiarata vs inizio segmento")
print("=" * 100)
for a_slide, a_time in sorted(anchors.items()):
    actual = next((s["start"] for s in segs if s["slide"] == a_slide), None)
    delta = (actual - a_time) if actual is not None else float("nan")
    print(f"  slide {a_slide:>2}: ancora {a_time:>8.1f}s | inizio segmento {actual:>8.1f}s | delta {delta:>+6.2f}s")

# ----------------------------------------------------------------------
# 5. Frame estratti: cosa c'e davvero a schermo
# ----------------------------------------------------------------------
print()
print("=" * 100)
print("4. FRAME ESTRATTI (a meta segmento) vs SLIDE RENDERIZZATE (temp_slides)")
print("=" * 100)

FRAMES_DIR.mkdir(exist_ok=True)

from PIL import Image


def img_sim(a: Path, b: Path) -> float:
    arr_a = np.asarray(Image.open(a).convert("L").resize(IMAGE_SIZE), dtype=np.float32).ravel()
    arr_b = np.asarray(Image.open(b).convert("L").resize(IMAGE_SIZE), dtype=np.float32).ravel()
    arr_a = arr_a - arr_a.mean()
    arr_b = arr_b - arr_b.mean()
    na, nb = np.linalg.norm(arr_a), np.linalg.norm(arr_b)
    if na == 0 or nb == 0:
        return 0.0
    return float((arr_a @ arr_b) / (na * nb))


frame_ok = 0
mismatches: list[tuple[int, int, float]] = []
for i, seg in enumerate(segs):
    s = seg["slide"]
    t = (seg["start"] + seg["end"]) / 2
    out = FRAMES_DIR / f"seg{i:02d}_t{t:07.1f}_slide{s:02d}.png"
    if not out.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{t:.3f}",
                "-i",
                str(VIDEO),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out),
            ],
            check=False,
        )
    sims_img = [img_sim(out, sf) for sf in slide_files]
    best_img = int(np.argmax(sims_img)) + 1
    best_sim_img = max(sims_img)
    ok = best_img == s and best_sim_img >= 0.85
    if ok:
        frame_ok += 1
    else:
        mismatches.append((s, best_img, best_sim_img))
    flag = "OK" if best_img == s else f"<-- VIDEO mostra slide {best_img}?"
    print(f"  seg {i:>2} (slide {s:>2}, t={t:>7.1f}s): frame vs slide {best_img:>2} (sim {best_sim_img:.3f}) {flag}")

print(f"\nFrame coerenti con la timeline: {frame_ok}/{len(segs)}")
if mismatches:
    print("DISCREPANZE:", mismatches)

# ----------------------------------------------------------------------
# 6. Confini: frame subito dopo ogni taglio -> slide successiva
# ----------------------------------------------------------------------
print()
print("=" * 100)
print("5. CONFINI: frame subito dopo ogni taglio (attesa slide N+1)")
print("=" * 100)
# NB: i confini qui provengono dalla timeline in cache (GREZZA); main.py
# ri-raffina a ogni run i confini delle slide senza ancora esplicita, quindi
# un confine segnalato come disallineato puo' essere atteso (video corretto).
boundary_ok = 0
for i, seg in enumerate(segs[1:], start=1):
    s = seg["slide"]
    t = seg["start"] + 1.0  # 1s dopo il taglio
    if t >= AUDIO_DURATION:
        continue
    out = FRAMES_DIR / f"bnd{i:02d}_t{t:07.1f}_slide{s:02d}.png"
    if not out.exists():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{t:.3f}",
                "-i",
                str(VIDEO),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out),
            ],
            check=False,
        )
    sims_img = [img_sim(out, sf) for sf in slide_files]
    best_img = int(np.argmax(sims_img)) + 1
    best_sim_img = max(sims_img)
    ok = best_img == s and best_sim_img >= 0.85
    if ok:
        boundary_ok += 1
    flag = "OK" if ok else f"<-- mostra slide {best_img}?"
    print(
        f"  confine {i}->{s} a {seg['start']:>7.1f}s: frame @{t:>7.1f}s "
        f"-> slide {best_img:>2} (sim {best_sim_img:.3f}) {flag}"
    )

print(f"\nConfini coerenti con la timeline: {boundary_ok}/{len(segs) - 1}")

# ----------------------------------------------------------------------
# Riepilogo
# ----------------------------------------------------------------------
print()
print("=" * 100)
print("RIEPILOGO")
print("=" * 100)
durs = [s["end"] - s["start"] for s in segs]
print(
    f"Slide totali: {total_slides} | segmenti: {len(segs)} | "
    f"tutti mostrati: {len(set(s['slide'] for s in segs)) == total_slides}"
)
print(
    f"Durata media {np.mean(durs):.1f}s | min {min(durs):.1f}s "
    f"(slide {int(segs[int(np.argmin(durs))]['slide'])}) | max {max(durs):.1f}s"
)
short = [(int(s["slide"]), round(s["end"] - s["start"], 1)) for s in segs if s["end"] - s["start"] < 15.0]
print(f"Segmenti corti (<15s): {short if short else 'nessuno'}")
print(f"Similarita media slide mostrata: {np.mean(shown_sims):.3f}")
