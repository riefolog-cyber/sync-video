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
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

BASE = Path(__file__).resolve().parent
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
from llm_sync import slide_di_altro_deck
from semantic_sync import _clean_slide_text, _load_embed_model, _make_embed_fn, segment_verdict
from video import _letterbox_references, _reference_matrix, _similarities

TIMELINE_FILE = None
ANCHORS_FILE = None

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
# Il confronto frame-vs-slide non usa una dimensione propria: riusa
# `video._reference_matrix` / `_similarities`, così ha lo stesso metro (e lo
# stesso letterbox) di `video.frame_consistency_check`. Due implementazioni
# divergenti dello stesso confronto sono gia' state la causa di un falso mismatch
# sui deck con rapporti misti.

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


# La timeline e le ancore vanno confrontate con il deck PRIMA di usarle: e'
# l'unico dato che distingue "cache di un altro podcast" da "misura di questo".
# Il caso e' reale: le cache `llm_<hash>.json` sono hash del contenuto e non
# hanno nel nome nulla che le leghi al materiale corrente.
_fuori_timeline = slide_di_altro_deck(timeline, total_slides)
if _fuori_timeline:
    print(
        f"[Verifica] ATTENZIONE: {TIMELINE_FILE.name} contiene slide fuori dal deck "
        f"corrente ({sorted(set(_fuori_timeline))} su 1..{total_slides}): e' una "
        "cache di un altro podcast, la sua sezione 3 sarebbe falsa."
    )

_fuori_ancore = slide_di_altro_deck(anchors_list, total_slides)
if _fuori_ancore:
    print(
        f"[Verifica] ATTENZIONE: ignoro le ancore di {ANCHORS_FILE.name}: parlano di "
        f"slide fuori dal deck corrente ({sorted(set(_fuori_ancore))} su "
        f"1..{total_slides}). Il confronto ancorra/segmenti della sezione 3 verrebbe "
        "riportato con scarti inventati."
    )
    anchors_list = []

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
print("1. TIMELINE + ALLINEAMENTO AUDIO <-> SLIDE (embedding + z-score + F1 lessicale)")
print("=" * 100)
print(f"{'sl':>3} {'inizio':>8} {'fine':>8} {'dur':>7} | {'sim':>6} {'best':>4} "
      f"{'b-z':>6} {'rank':>4} {'F1':>5} | giudizio")
print("-" * 100)
# Nota: 'best' e' calcolato con lo z-score per-slide (come il pipeline):
# la similarita' coseno grezza (colonna 'sim') non e' un giudizio di sync.

n_best = 0
n_weak = 0
low_sim: list[tuple[int, float, float]] = []
shown_sims: list[float] = []

# Cosine grezza per ogni segmento (per le metriche informative), poi giudizio
# del "best" con la STESSA normalizzazione del pipeline (z-score per-slide):
# senza z-score la slide-riepilogo (similarita' uniformemente alta) vincerebbe
# quasi ovunque sulla cosine grezza, producendo falsi disallineamenti.
raw_rows: list[np.ndarray] = []
tbl: list[dict] = []
for seg in segs:
    s = seg["slide"]
    st, en = seg["start"], seg["end"]
    emb = seg_embedding(st, en)
    sims = emb @ slide_emb.T
    raw_rows.append(sims)
    tbl.append(
        {
            "slide": s,
            "start": st,
            "end": en,
            "dur": en - st,
            "shown": float(sims[s - 1]),
            "f1": lex_f1(seg_text(st, en), slide_texts[s - 1]),
        }
    )
    shown_sims.append(float(sims[s - 1]))

seg_sims = np.stack(raw_rows) if raw_rows else np.zeros((0, len(slide_texts)))
verdicts = segment_verdict(seg_sims, shown=[t["slide"] for t in tbl]) if len(tbl) else []

for i, (row, v) in enumerate(zip(tbl, verdicts, strict=True)):
    s, shown = row["slide"], row["shown"]
    best_slide = int(v["best"])
    best_z = float(v["best_z"])
    order = int(v["rank"])
    if shown < 0.10:
        n_weak += 1
        low_sim.append((s, shown, float(seg_sims[i].max() if len(tbl) else 0.0)))
    is_best = best_slide == s
    if is_best:
        n_best += 1
        verdict = "OK (best)"
    elif order <= 3:
        verdict = f"OK~ (best={best_slide})"
    else:
        verdict = f"<-- slide {s} vs best {best_slide}"
    print(
        f"{s:>3} {row['start']:>8.1f} {row['end']:>8.1f} {row['dur']:>7.1f} | "
        f"{shown:>6.3f} {best_slide:>4} {best_z:>6.3f} {order:>4} {row['f1']:>5.3f} | {verdict}"
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
# Indici e timestamp pre-calcolati: il ciclo sotto interpola TUTTE le parole a
# ogni confine, quindi rifare gli start/fine a ogni iterazione costava un giro
# completo del vocabolario per confine (con 20k parole e 60 confini: 1.2M di
# conversioni float inutili). Inoltre `words.index(before[-1])` era un doppio
# difetto: cercare il dict nell'intera lista è O(n), e `list.index` restituisce
# il PRIMO elemento uguale, non quello trovato — con due parole identiche
# (whisper ripete spesso la stessa parola, e il fallback `_parse_transcript_raw`
# non scrive il campo "end") l'indice restituito è quello sbagliato e `word_end`
# legge la parola successiva del posto sbagliato, gonfiando `gap_before`.
# Misurato: un confine a 2.2s dentro una parola veniva riportato come "pausa"
# (gap 2.20s) invece di "a meta frase" (gap 0.20s), cioè i tagli difettosi
# sparivano dal referto. Si lavora quindi con gli indici veri, non con la
# ricerca del valore.
word_indices = list(range(len(words)))
word_starts = [float(w["start"]) for w in words]
word_ends = [word_end(w, i) for i, w in enumerate(words)]
for b in boundaries:
    # parola a cavallo del confine? (con tolleranza di snap su confine di parola)
    straddle = [
        w
        for i, w in enumerate(words)
        if word_starts[i] + BOUNDARY_SNAP_TOL < b < word_ends[i] - BOUNDARY_SNAP_TOL
    ]
    before_idx = [i for i in word_indices if word_ends[i] <= b]
    after_idx = [i for i in word_indices if word_starts[i] >= b]
    gap_before = b - word_ends[before_idx[-1]] if before_idx else 999.0
    gap_after = word_starts[after_idx[0]] - b if after_idx else 999.0
    kind = "META-PAROLA" if straddle else ("pausa" if min(gap_before, gap_after) >= WORD_GAP_CUT else "a meta frase")
    if straddle:
        cuts_word += 1
    elif min(gap_before, gap_after) >= WORD_GAP_CUT:
        cuts_pause += 1
    else:
        cuts_phrase += 1
    prev_txt = " ".join(words[i]["word"] for i in before_idx[-8:])
    next_txt = " ".join(words[i]["word"] for i in after_idx[:8])
    print(f"  confine {b:>8.1f}s : {kind:11s} | ...{prev_txt} | {next_txt}...")

print(f"\nTagli a META PAROLA: {cuts_word} | a meta frase: {cuts_phrase} | su pausa naturale: {cuts_pause}")

# ----------------------------------------------------------------------
# 4. Ancore
# ----------------------------------------------------------------------
print()
print("=" * 100)
print("3. ANCORE: delta ancora dichiarata vs inizio segmento")
print("=" * 100)
if not anchors:
    # Meglio dichiararlo che lasciare una sezione vuota: una sezione muta sembra
    # uno strumento rotto, e il lettore non puo' distinguere "nessuna ancora
    # salvata" da "nessuna ancora perche' le ho scartate".
    print("  (nessuna ancora utilizzabile: non e' stato salvato alcun riferimento "
          "'slide N' di questa run)")
for a_slide, a_time in sorted(anchors.items()):
    actual = next((s["start"] for s in segs if s["slide"] == a_slide), None)
    delta = (actual - a_time) if actual is not None else float("nan")
    # Una ancora puo' non avere un segmento corrispondente (slide mai mostrata,
    # o rimappata): `delta` e' gia' protetto, ma la riga va formattata senza
    # crashare, altrimenti lo strumento muore PRIMA delle sezioni 4 e 5, cioe'
    # proprio di quelle che servono a capire il video. Il caso e' reale: con un
    # rimap di ancore la slide dichiarata puo' non comparire nella timeline.
    if actual is None:
        print(
            f"  slide {a_slide:>2}: ancora {a_time:>8.1f}s | "
            f"nessun segmento con questa slide (slide non mostrata o rimappata)"
        )
        continue
    print(f"  slide {a_slide:>2}: ancora {a_time:>8.1f}s | inizio segmento {actual:>8.1f}s | delta {delta:>+6.2f}s")

# ----------------------------------------------------------------------
# 5. Frame estratti: cosa c'e davvero a schermo
# ----------------------------------------------------------------------
print()
print("=" * 100)
print("4. FRAME ESTRATTI (a meta segmento) vs SLIDE RENDERIZZATE (temp_slides)")
print("=" * 100)

FRAMES_DIR.mkdir(exist_ok=True)


def _extract_frame(out: Path, t: float) -> None:
    """Estrae un frame dal video AL VOLO se assente o se il video e' piu'
    nuovo del frame (run precedente): la cache dei frame puo' contenere
    immagini di un video precedente con lo STESSO nome (stesso timestamp
    di confine e stessa slide), che produrrebbero falsi mismatch.
    """
    if out.exists() and out.stat().st_mtime >= VIDEO.stat().st_mtime:
        return
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


def _build_slide_paths() -> list[Path]:
    """Reference delle slide COSI' COME SONO NEL VIDEO (con letterbox).

    Stessa normalizzazione di `video.frame_consistency_check`, riusata dalla
    funzione condivisa invece di reimplementata: i due strumenti devono dare lo
    stesso verdetto sugli stessi frame, o il riepilogo di `analysis_sync.py`
    contraddirrebbe l'esito che la pipeline ha gia' scritto nel report.

    Le reference stanno in una directory fuori da `FRAMES_DIR`, che deve
    contenere solo i frame estratti dal video. Restituisce una lista VUOTA se una
    slide non e' leggibile: il chiamante salta il confronto invece di produrre
    un referto inventato.
    """
    with tempfile.TemporaryDirectory(prefix="analysis_refs_") as tmp:
        built = _letterbox_references([str(sf) for sf in slide_files], Path(tmp))
        if built is None:
            return []
        # La temporanea muore con il `with`: i PNG servono ancora, quindi si
        # copiano in una directory che sopravvive allo script.
        dest = FRAMES_DIR.parent / ".analysis_refs"
        if dest.exists():
            for stale in dest.glob("ref_*.png"):
                with suppress(OSError):
                    stale.unlink()
        dest.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for src in built:
            target = dest / src.name
            shutil.copyfile(src, target)
            out.append(target)
        return out


def _slide_matrix() -> np.ndarray:
    """Matrice (slide, 64*64) dei vettori delle slide, gia' normalizzati.

    Stessa ottimizzazione di `video._reference_matrix`: si legge ogni slide una
    volta sola e ogni frame si confronta con un prodotto matriciale, invece di
    riaprire e riconvertire ogni slide a ogni frame.

    Le reference sono le slide COSI' COME SONO NEL VIDEO, cioe' con lo stesso
    letterbox che applica l'encoder: `_letterbox_references` e' la funzione
    condivisa che lo garantisce. Prima questo script confrontava il frame (con
    le barre nere) contro il PNG originale (senza): su un deck con rapporti
    diversi — una 4:3, una pagina verticale, una tabella larga — la similarita'
    crollava sotto soglia e ogni segmento finiva fra i falsi mismatch. Misurato
    su una slide 4:3 in un deck 16:9: 0.537 (falso mismatch) contro 1.000 di
    `video.frame_consistency_check`, che applica il letterbox. Non e' solo un
    falso allarme: con la riparazione automatica attiva il confine viene
    spostato su rumore, cioe' una timeline buona viene corrotta.

    Una slide illeggibile o uniforme diventa una riga di zeri -> 0.0.
    """
    return _reference_matrix(slide_paths)


def _frame_sims(out: Path, slide_matrix: np.ndarray) -> np.ndarray:
    """Coseno di un frame contro tutte le slide normalizzate (stesso metro di
    `video.frame_consistency_check`, così i due strumenti non divergono)."""
    return _similarities(out, slide_matrix)


# Le slide vengono normalizzate con lo stesso letterbox dell'encoder: senza,
# un deck con rapporti misti produce falsi mismatch (vedi _build_slide_paths).
# Lista vuota = slide non leggibili: si salta il confronto, senza inventare un
# referto. E` anche la forma che mypy riesce a restringere.
slide_paths = _build_slide_paths()
if not slide_paths:
    print("AVVISO: slide non leggibili: confronto frame-vs-slide saltato.")
SLIDE_MATRIX = _slide_matrix()

frame_ok = 0
mismatches: list[tuple[int, int, float]] = []
for i, seg in enumerate(segs if slide_paths else []):
    s = seg["slide"]
    t = (seg["start"] + seg["end"]) / 2
    out = FRAMES_DIR / f"seg{i:02d}_t{t:07.1f}_slide{s:02d}.png"
    _extract_frame(out, t)
    sims_img = _frame_sims(out, SLIDE_MATRIX)
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
for i, seg in enumerate(segs[1:] if slide_paths else [], start=1):
    s = seg["slide"]
    t = seg["start"] + 1.0  # 1s dopo il taglio
    if t >= AUDIO_DURATION:
        continue
    out = FRAMES_DIR / f"bnd{i:02d}_t{t:07.1f}_slide{s:02d}.png"
    _extract_frame(out, t)
    sims_img = _frame_sims(out, SLIDE_MATRIX)
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
