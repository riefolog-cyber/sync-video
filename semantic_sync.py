#!/usr/bin/env python3
"""
Sincronizzazione semantica offline (sentence embeddings, e5-large) — motore
di allineamento del pipeline per il flusso ordinato.

Offline e deterministico: allinea le slide (OCR) con la trascrizione usando
la somiglianza semantica blocco-slide, senza chiamate di rete e senza modelli
LLM locali:

  1. raggruppa le parole Whisper/OpenVINO in blocchi temporali
  2. codifica slide (OCR) e blocchi con fastembed (ONNX, multilingue)
  3. normalizza la similarità per-slide (z-score sul baseline della singola
     slide: conta i picchi locali, non la distanza assoluta) così una slide
     riassuntiva con similarità uniformemente alta non cattura l'audio
  4. programmazione dinamica a segmenti monotoni → assegna a ogni slide il
     blocco di inizio, garantendo tempi strettamente crescenti
  5. valida con reconcile_timeline (precisione assoluta)

Stessa filosofia del progetto: se il segnale è insufficiente restituisce None
invece di inventare distribuzioni uniformi (il chiamante interrompe con avviso).

L'LLM (llm_sync.py) non sostituisce questo motore: nel flusso ordinato
posiziona SOLO le slide senza ancora esplicita (vincolate da qui), mentre nel
flusso libero guida la selezione chunk→slide.
"""

from __future__ import annotations

import bisect
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

import numpy as np

from chunks import Segment, Word, build_windows
from config import (
    DEFAULT_EMBED_THREADS,
    DEFAULT_EMBEDDING_CACHE_DIR,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_MODEL_ALTERNATE,
    DEFAULT_SEMANTIC_MIN_Z,
    DEFAULT_SEMANTIC_TEMPERATURE,
    log,
)
from timeline import reconcile_timeline

try:
    from fastembed import TextEmbedding

    _HAS_FASTEMBED = True
except ImportError:  # pragma: no cover
    _HAS_FASTEMBED = False

# Tempo cumulato di caricamento dei modelli embedding (download + ONNX in RAM).
# Esposto al chiamante per il riepilogo tempi di main.py.
_MODEL_LOAD_SECONDS = 0.0

# Cache a livello di modulo dei modelli embedding caricati: evita di ricaricare
# i pesi ONNX quando la verifica deterministica delle ancore e la sync semantica
# usano lo stesso modello nello stesso processo.
_EMBED_MODEL_CACHE: dict[tuple[str, str], TextEmbedding] = {}

# Segnale debole rilevato dall'ULTIMA sincronizzazione semantica (guard-rail).
# Esposto al chiamante perché il riepilogo finale di main.py avvisi l'utente che
# la timeline è "probabile, non garantita" (slide simili / parlato fuori ordine).
_WEAK_SIGNAL_SEEN = False

# Qualità dell'ULTIMO allineamento (guard-rail): media grezza e normalizzata
# dei blocchi assegnati, per il report e il riepilogo finale di main.py.
_LAST_QUALITY: dict[str, float] = {}


def model_load_seconds() -> float:
    """Restituisce i secondi cumulati di caricamento dei modelli embedding."""
    return _MODEL_LOAD_SECONDS


def weak_signal_seen() -> bool:
    """True se l'ultima sincronizzazione semantica ha rilevato un segnale debole."""
    return _WEAK_SIGNAL_SEEN


def last_quality() -> dict[str, float]:
    """Qualità dell'ultimo allineamento: ``avg_sim`` (cosine grezza), ``avg_z``
    (picco medio normalizzato) e la soglia usata ``min_avg_z``.

    Esposta al chiamante per il report (``sync_report.json``) e il riepilogo
    finale: così una run può essere riesaminata senza rifare l'embedding.
    """
    return dict(_LAST_QUALITY)


def reset_weak_signal_flag() -> None:
    """Azzera il flag di segnale debole (usato nei test tra chiamate diverse)."""
    global _WEAK_SIGNAL_SEEN
    _WEAK_SIGNAL_SEEN = False
    _LAST_QUALITY.clear()


def _set_weak_signal() -> None:
    """Segnala che l'ultima sincronizzazione ha prodotto un segnale debole."""
    global _WEAK_SIGNAL_SEEN
    _WEAK_SIGNAL_SEEN = True

EmbedFn = Callable[[Sequence[str]], np.ndarray]

# Concordanza dei picchi "moderata": soglia usata da weak_signal come aggravante
# quando le slide sono anche confondibili tra loro (concordanza < 70%).
MODERATE_CONCORDANCE_THRESHOLD = 0.7


@dataclass(frozen=True)
class SemanticOptions:
    """Parametri di tuning della sincronizzazione semantica.

    Raggruppa le soglie/tempi condivisi tra l'orchestratore (``*_from_words``)
    e le funzioni pure (``*_from_texts``): così i default e i valori effettivi
    non possono divergere tra i due livelli e i call-site passano un solo
    oggetto invece di filetti di parametri posizionali.
    """

    window_seconds: float = 4.0
    min_slide_duration: float = 3.0
    min_avg_similarity: float = 0.10
    # Guard-rail sulla scala normalizzata (z-score per slide): un picco medio
    # sotto questa soglia significa allineamento dubbio (vedi _mean_pair_scores).
    min_avg_z: float = DEFAULT_SEMANTIC_MIN_Z
    temperature: float = DEFAULT_SEMANTIC_TEMPERATURE
    min_segment_seconds: float = 8.0
    model_name: str | None = None
    alternate_model_name: str | None = None
    cache_dir: str | None = None


# =====================================================================
# COSTRUZIONE BLOCCHI TRASCRIZIONE (finestre temporali)
# =====================================================================
def build_semantic_blocks(
    words: list[Word],
    total_duration: float,
    window_seconds: float = 4.0,
    min_words: int = 3,
) -> list[dict[str, object]]:
    """
    Raggruppa le parole in finestre temporali fisse di `window_seconds` secondi.
    Le finestre con meno di `min_words` parole (silenzi) vengono scartate;
    ogni blocco conserva il timestamp di inizio della finestra.
    """
    blocks: list[dict[str, object]] = []
    for w in build_windows(words, total_duration, window_seconds):
        if len(w["words"]) < min_words:
            continue
        blocks.append(
            {
                "time": w["start"],
                "first_time": w["first_time"],
                "text": w["text"],
            }
        )
    return blocks


# =====================================================================
# PULIZIA TESTO SLIDE
# =====================================================================
def _clean_slide_text(text: str, max_chars: int = 2000) -> str:
    """Normalizza il testo OCR di una slide per l'embedding.

    max_chars alto (2000): slide ricche di testo non vengono troncate a metà,
    altrimenti l'embedding perderebbe i concetti della parte finale.
    """
    t = re.sub(r"\s+", " ", text or "").strip().lower()
    return t[:max_chars]


# =====================================================================
# EMBEDDING (fastembed / ONNX)
# =====================================================================
def _load_embed_model(
    model_name: str,
    cache_dir: str,
    alternate_name: str | None = None,
) -> TextEmbedding | None:
    """
    Carica il modello fastembed, con fallback automatico.

    Se il modello principale non si carica (es. download interrotto, modello
    non più disponibile), riprova con `alternate_name` prima di restituire
    None: e5-large è il default (vedi config.py), mpnet è l'alternativa.
    """
    global _MODEL_LOAD_SECONDS
    if not _HAS_FASTEMBED:
        log.warning("   [Semantico] fastembed non installato. Installa con: pip install fastembed")
        return None

    candidates: list[tuple[str, bool]] = [(model_name, False)]
    if alternate_name and alternate_name != model_name:
        candidates.append((alternate_name, True))

    for candidate, is_alt in candidates:
        try:
            key = (candidate, cache_dir)
            model = _EMBED_MODEL_CACHE.get(key)
            if model is None:
                _t0 = time.perf_counter()
                # threads: il default di fastembed è conservativo (spesso 1-4
                # thread ONNX); su CPU multi-core usare ~8 quasi raddoppia la
                # velocità di calcolo. Configurabile con EMBED_THREADS.
                model = TextEmbedding(
                    model_name=candidate,
                    cache_dir=str(cache_dir),
                    threads=DEFAULT_EMBED_THREADS,
                )
                log.debug("   [Semantico] Thread ONNX embedding: %d", DEFAULT_EMBED_THREADS)
                _MODEL_LOAD_SECONDS += time.perf_counter() - _t0
                _EMBED_MODEL_CACHE[key] = model
            if is_alt:
                log.warning(
                    "   [Semantico] Modello principale non disponibile: uso il fallback %s.",
                    candidate,
                )
            else:
                log.info("   [Semantico] Modello embedding: %s", candidate)
            return model
        except Exception as e:  # noqa: BLE001 - fastembed può lanciare molti tipi
            log.warning(
                "   [Semantico] Impossibile caricare il modello embedding (%s): %s",
                candidate,
                e,
            )
    return None


def _make_embed_fn(model: TextEmbedding, batch_size: int = 64) -> EmbedFn:
    """Avvolge fastembed: restituisce embeddings normalizzati (cosine).

    Per i modelli E5 (es. multilingual-e5-large) aggiunge il prefisso
    "passage: " previsto dal modello (ricerca simmetrica slide↔blocchi).
    """

    def _embed(texts: Sequence[str]) -> np.ndarray:
        prepared = list(texts)
        if getattr(model, "model_name", "") and "e5" in str(model.model_name).lower():
            prepared = ["passage: " + t for t in prepared]
        vecs = [np.asarray(v, dtype=np.float32) for v in model.embed(prepared, batch_size=batch_size)]
        arr = np.vstack(vecs)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return cast(np.ndarray, arr / norms)

    return _embed


# =====================================================================
# EMBEDDING + QUALITÀ SEGNALE (blocco condiviso tra i due flussi)
# =====================================================================
def _embed_and_report(
    slide_texts: Sequence[str],
    blocks: Sequence[dict[str, Any]],
    total_slides: int,
    embed_fn: EmbedFn,
    context: str,
) -> tuple[np.ndarray, dict[str, float]] | None:
    """Embedding slide+blocchi, cosine (B, N) e report di qualità del segnale.

    Blocco condiviso tra il flusso monotono e la selezione libera: embeddare
    i testi e calcolare il report è identico nei due casi, cambia solo il
    prefisso dei log. Restituisce ``(sim, report)`` oppure None in caso di
    errore (il chiamante ripiega).
    """
    slide_clean = [_clean_slide_text(t) for t in slide_texts[:total_slides]]
    block_texts = [str(b["text"]) for b in blocks]

    try:
        slide_emb = embed_fn(slide_clean)
        block_emb = embed_fn(block_texts)
    except Exception as e:  # noqa: BLE001 - embed_fn è iniettabile/esterno
        log.warning("   [%s] Errore durante l'embedding: %s", context, e)
        return None

    if slide_emb.shape[0] != total_slides or block_emb.shape[0] != len(blocks):
        log.warning("   [%s] Dimensioni embedding inattese.", context)
        return None

    sim = block_emb @ slide_emb.T  # (B, N) cosine
    return sim, signal_quality_report(sim, slide_emb)


# =====================================================================
# NORMALIZZAZIONE PER-SLIDE (z-score sul proprio baseline)
# =====================================================================
def zscore_matrix(
    sim: np.ndarray,
    eps: float = 1e-9,
) -> np.ndarray:
    """
    Standardizza ogni colonna (slide) rispetto al proprio andamento temporale.

    Trasforma la similarità assoluta in "quanto questo blocco è un picco per
    quella slide": una slide riassuntiva con similarità uniformemente alta su
    tutto l'audio (che "catturerebbe" metà narrazione) scende a ~0 e non
    domina più la DP. Colonna costante (std=0) → 0 (neutra).
    """
    m = np.array(sim, dtype=np.float64, copy=True)
    std = m.std(axis=0)
    std[std < eps] = eps
    return cast(np.ndarray, (m - m.mean(axis=0)) / std)


def _mean_pair_scores(
    sim: np.ndarray,
    sim_norm: np.ndarray,
    pairs: Sequence[tuple[int, int]],
) -> tuple[float, float]:
    """Media dei punteggi (cosine grezza, z-score) sui blocchi assegnati.

    Le due scale misurano cose diverse:

    - cosine grezza: ha una baseline altissima (~0.84 tra due testi italiani
      qualsiasi) e NON distingue un allineamento giusto da slide mescolate
      (misurato: 0.80-0.88 in entrambi i casi). Il valore assoluto non contiene
      informazione sull'allineamento.
    - z-score per-slide (la stessa matrice usata dal posizionamento): quanto
      ogni blocco è un PICCO per la slide a cui è stato assegnato. Sui dati
      reali separa 0.61-0.75 (ordine giusto) da <=0.43 (mescolate, invertite,
      non correlate) e 0.006 (slide quasi-duplicate).

    Returns:
        ``(avg_sim, avg_z)``; ``(0.0, 0.0)`` se non c'è alcuna coppia.
    """
    if not pairs:
        return 0.0, 0.0
    raw = sum(float(sim[blk, sld]) for blk, sld in pairs) / len(pairs)
    z = sum(float(sim_norm[blk, sld]) for blk, sld in pairs) / len(pairs)
    return raw, z


def segment_verdict(
    sim: np.ndarray,
    shown: Sequence[int] | None = None,
) -> list[dict[str, float | int]]:
    """Giudizio "best slide" per segmento, con la STESSA normalizzazione
    del pipeline (z-score per-slide), non la cosine grezza.

    Su una slide-riepilogo con similarita' uniformemente alta su tutto
    l'audio la cosine grezza la dichiarerebbe "best" per quasi ogni
    segmento (falso disallineamento), mentre lo z-score la rende neutra
    e lascia emergere i picchi locali delle slide reali.

    Args:
        sim: matrice (B, N) di similarita' coseno (righe = segmenti).
        shown: slide mostrata per ogni riga (1-based, opzionale): usata
            per calcolare `rank` della slide realmente a schermo.

    Returns:
        Lista (una per segmento) con:
            best:    slide piu' probabile per z-score
            best_z:  valore z del best
            shown_z: valore z della slide mostrata
            rank:    posizione della slide mostrata nella riga (1 = migliore)
    """
    z = zscore_matrix(sim)
    verdicts: list[dict[str, float | int]] = []
    for i, row in enumerate(z):
        best = int(np.argmax(row)) + 1
        best_z = float(row.max())
        shown_slide = int(shown[i]) if shown is not None else best
        shown_z = float(row[shown_slide - 1])
        rank = int((row > row[shown_slide - 1]).sum()) + 1
        verdicts.append({"best": best, "best_z": best_z, "shown_z": shown_z, "rank": rank})
    return verdicts


# =====================================================================
# COMPETIZIONE SOFTMAX TRA SLIDE (per blocco)
# =====================================================================
def competition_matrix(
    sim: np.ndarray,
    temperature: float = DEFAULT_SEMANTIC_TEMPERATURE,
) -> np.ndarray:
    """
    Normalizza la similarità riga per riga con una softmax sulle slide.

    Su un dato blocco, un valore pari a `sim[b, s]` conta quanto la slide `s`
    è migliore delle alternative in quel punto: così una slide-riepilogo con
    similarità uniformemente media su tutto l'audio non "cattura" metà della
    narrazione a scapito dei picchi locali delle altre slide.
    """
    m = np.array(sim, dtype=np.float64, copy=True)
    mx = m.max(axis=1, keepdims=True)
    mx[np.isnan(mx)] = 0.0
    e = np.exp((m - mx) / max(temperature, 1e-3))
    return cast(np.ndarray, e / e.sum(axis=1, keepdims=True))


# =====================================================================
# GUARD-RAILS: QUALITÀ DEL SEGNALE (rileva audio che non segue le slide)
# =====================================================================
def signal_quality_report(
    sim: np.ndarray,
    slide_emb: np.ndarray | None = None,
    duplicate_threshold: float = 0.6,
) -> dict[str, float]:
    """
    Misure di qualità del segnale per il guard-rail anti-disallineamento.

    - concordance: frazione di coppie di slide (i<j) il cui picco di
      similarità nel parlato arriva in ordine crescente. 1.0 = l'audio segue
      l'ordine delle slide (flusso slide-derivate); ~0.5 = ordine casuale
      (slide indipendenti dalla stessa fonte).
    - confusability: frazione di coppie di slide quasi-duplicati (cosine
      oltre `duplicate_threshold`), indice di quanto il tema unico rende
      debole la discriminazione.
    """
    _, N = sim.shape
    peaks = [int(np.argmax(sim[:, s])) for s in range(N)]
    concord = 0
    for i in range(N):
        for j in range(i + 1, N):
            if peaks[i] < peaks[j]:
                concord += 1
    total_pairs = N * (N - 1) / 2.0
    concordance = concord / total_pairs if total_pairs else 1.0

    confusability = 0.0
    if slide_emb is not None and slide_emb.shape[0] == N and N > 1:
        s2s = slide_emb @ slide_emb.T
        np.fill_diagonal(s2s, 0.0)
        confusability = float((s2s > duplicate_threshold).sum()) / (N * (N - 1))

    return {"concordance": concordance, "confusability": confusability}


def weak_signal(
    report: dict[str, float],
    min_concordance: float = 0.5,
    max_confusability: float = 0.5,
) -> bool:
    """True se il segnale è debole: l'audio non segue l'ordine delle slide.

    Gate primario: la concordanza dei picchi (l'audio segue l'ordine). La
    confondibilità tra slide è solo un aggravante: slide tematicamente simili
    sono normali nel flusso buono (derivate dal podcast), quindi da sole non
    devono far scattare l'avviso.
    """
    low_concordance = report["concordance"] < min_concordance
    confusable = report["confusability"] > max_confusability
    moderate_concordance = report["concordance"] < MODERATE_CONCORDANCE_THRESHOLD
    return low_concordance or (confusable and moderate_concordance)


# =====================================================================
# CANDIDATI DI INIZIO PER SLIDE
# =====================================================================
def build_candidates(
    num_blocks: int,
    total_slides: int,
    min_gap: int,
    blocks: Sequence[dict[str, Any]] | None = None,
    anchors: dict[int, float] | None = None,
) -> list[list[int]] | None:
    """
    Calcola per ogni slide gli indici di blocco di inizio ammessi.

    Default: tutti i blocchi in [lo, hi] con lo/hi che riservano abbastanza
    blocchi per le slide successive (`min_gap`). La Slide 1 è sempre il blocco 0.

    Se è disponibile un'ancona esatta per una slide (timestamp reale trovato
    deterministicamente), i candidati vengono ristretti al blocco più vicino
    (±1) per rispettare i segnali reali.
    """
    cands: list[list[int]] = []
    for s in range(1, total_slides + 1):
        lo = (s - 1) * min_gap
        hi = num_blocks - 1 - (total_slides - s) * min_gap
        if hi < lo:
            return None
        free = list(range(lo, hi + 1))
        chosen = free
        if blocks and anchors and s in anchors:
            times = np.array([float(b["time"]) for b in blocks], dtype=np.float64)
            k = int(np.argmin(np.abs(times - anchors[s])))
            constrained = [i for i in (k - 1, k, k + 1) if lo <= i <= hi]
            # Se nessuno dei tre blocchi è fattibile (l'ancora cade fuori dalla
            # finestra [lo, hi] imposta da `min_gap`) si usa il blocco fattibile
            # PIÙ VICINO all'ancora. Prima si rinunciava al vincolo
            # (`chosen = free`) e la DP poteva piazzare la slide ovunque, salvo
            # poi essere scavalcata dalla riga `timeline[s] = anchors[s]`:
            # ancora "rispettata" nel tempo ma segmento non monotono, cioè
            # violazione silenziosa.
            chosen = (
                sorted(set(constrained))
                if constrained
                else [min(max(k, lo), hi)]
            )
        cands.append(chosen)
    if cands:
        cands[0] = [0]
    return cands


# =====================================================================
# PROGRAMMAZIONE DINAMICA A SEGMENTI MONOTONI
# =====================================================================
def monotonic_alignment(
    sim: np.ndarray,
    candidates: list[list[int]],
    min_gap: int = 1,
) -> list[int] | None:
    """
    Segmenta i blocchi in `N` segmenti monotoni (uno per slide) massimizzando
    la similarità totale, con l'accelerazione del massimo cumulativo (running
    maximum) → O(N·B).

    Args:
        sim: matrice (B, N) di similarità coseno blocco x slide.
        candidates: candidates[s] = indici di blocco di inizio ammessi per la
                    slide s (0-based). candidates[0] deve essere [0].
        min_gap: numero minimo di blocchi che separa due inizio consecutivi.

    Returns:
        Lista `starts` lunga N (indice blocco di inizio per slide 1..N),
        oppure None se non esiste una segmentazione valida.
    """
    B, N = sim.shape
    if len(candidates) != N or not candidates[0] or candidates[0][0] != 0:
        return None

    # prefix sums per slide: pref[s][x] = sum_{b<x} sim[b][s]
    pref = np.zeros((N, B + 1), dtype=np.float64)
    for s in range(N):
        pref[s, 1:] = np.cumsum(sim[:, s])

    dp: list[np.ndarray] = [np.array([0.0])]  # dp[s] parallelo a candidates[s]
    parents: list[list[int | None]] = [[None]]  # parents[s][j] = indice in candidates[s-1]

    for s in range(1, N):
        cur = candidates[s]
        if not cur:
            return None
        prev_cands = candidates[s - 1]
        prev_dp = dp[s - 1]
        pref_s = pref[s - 1]  # segmento della slide s viene valutato qui
        cur_dp = np.full(len(cur), -np.inf)
        cur_parent: list[int | None] = [None] * len(cur)

        best = -np.inf
        best_idx = -1
        p = 0
        for j, b in enumerate(cur):
            limit = b - min_gap
            while p < len(prev_cands) and prev_cands[p] <= limit:
                c = prev_cands[p]
                val = prev_dp[p] - pref_s[c]
                if val > best:
                    best = val
                    best_idx = p
                p += 1
            if best_idx != -1:
                cur_dp[j] = pref_s[b] + best
                cur_parent[j] = best_idx

        if not np.isfinite(cur_dp).any():
            return None
        dp.append(cur_dp)
        parents.append(cur_parent)

    # Segmento finale della slide N: [c, B)
    prev_cands = candidates[N - 1]
    prev_dp = dp[N - 1]
    prefN = pref[N - 1]
    best = -np.inf
    best_idx = -1
    for j, c in enumerate(prev_cands):
        val = prev_dp[j] + prefN[B] - prefN[c]
        if val > best:
            best = val
            best_idx = j
    if best_idx == -1:
        return None

    starts = [0] * N
    starts[N - 1] = prev_cands[best_idx]
    for s in range(N - 1, 0, -1):
        j = candidates[s].index(starts[s])
        par = parents[s][j]
        if par is None:
            return None
        starts[s - 1] = candidates[s - 1][par]
    return starts


# =====================================================================
# TIMELINE SEMANTICA (pura: embedder iniettabile per i test)
# =====================================================================
def semantic_timeline_from_texts(
    slide_texts: Sequence[str],
    blocks: Sequence[dict[str, Any]],
    total_slides: int,
    total_duration: float,
    embed_fn: EmbedFn,
    options: SemanticOptions | None = None,
    anchors: dict[int, float] | None = None,
) -> dict[int, float] | None:
    """
    Costruisce la timeline semantica da slide e blocchi già pronti.

    Args:
        slide_texts: testi OCR delle slide.
        blocks: blocchi trascrizione con chiavi "time" e "text".
        total_slides: numero totale di slide.
        total_duration: durata audio in secondi.
        embed_fn: funzione testi -> matrice (N, dim) normalizzata.
        options: parametri di tuning (finestre, soglie, temperature); None = default.
        anchors: timestamp reali (slide -> sec) da rispettare.

    Returns:
        Timeline {slide: start_second} valida, oppure None.
    """
    opts = options or SemanticOptions()
    window_seconds = opts.window_seconds
    min_slide_duration = opts.min_slide_duration
    min_avg_similarity = opts.min_avg_similarity
    min_avg_z = opts.min_avg_z
    temperature = opts.temperature

    if total_slides < 2 or len(blocks) < total_slides:
        log.warning(
            "   [Semantico] %d blocchi < %d slide: segnale insufficiente.",
            len(blocks),
            total_slides,
        )
        return None

    embedded = _embed_and_report(slide_texts, blocks, total_slides, embed_fn, "Semantico")
    if embedded is None:
        return None
    sim, report = embedded

    # Guard-rail: segnale debole (es. slide generate dalla stessa fonte, non
    # dal podcast). Non blocca: avvisa che la sincronizzazione è inaffidabile
    # così l'utente può rigenerare la presentazione dal podcast.
    if weak_signal(report):
        _set_weak_signal()
        log.warning(
            "   [Semantico] AVVISO: segnale debole (concordanza picchi %.0f%%, "
            "slide confondibili %.0f%%). Il parlato potrebbe NON seguire "
            "l'ordine delle slide (es. slide generate dalla stessa fonte e non "
            "dal podcast): la sincronizzazione sarà inaffidabile. Rigenera la "
            "presentazione derivandola dal podcast per un allineamento 1:1.",
            report["concordance"] * 100,
            report["confusability"] * 100,
        )

    min_gap = max(1, int(min_slide_duration / window_seconds))

    candidates = build_candidates(
        len(blocks),
        total_slides,
        min_gap,
        blocks,
        anchors,
    )
    if candidates is None:
        log.warning("   [Semantico] Vincoli di segmentazione insoddisfacibili.")
        return None

    # Normalizzazione per-slide (z-score): la similarità assoluta di una slide
    # riassuntiva può essere uniformemente alta su tutto l'audio ("cattura"
    # metà narrazione). Lo z-score confronta ogni blocco col baseline della
    # propria slide: conta quanto il blocco è un PICCO per quella slide, non
    # la distanza assoluta. Le slide riassuntive uniformi scendono a ~0 e non
    # dominano più la DP.
    sim_norm = zscore_matrix(sim)

    # Competizione softmax per blocco: il posizionamento usa quanto ogni slide
    # è migliore delle alternative nel blocco, non la similarità assoluta.
    starts = monotonic_alignment(competition_matrix(sim_norm, temperature), candidates, min_gap)
    if starts is None:
        log.warning(
            "   [Semantico] Nessuna segmentazione valida trovata "
            "(sincronizzazione impossibile senza distribuzioni inventate)."
        )
        return None

    # --- Guardia di qualità: quanto i segmenti assegnati sono "picchi" ---
    # La cosine grezza non discrimina nulla (vedi _mean_pair_scores): si misura
    # la STESSA matrice normalizzata usata dal posizionamento. La vecchia
    # soglia sulla scala grezza resta nella sua semantica documentata, ma coi
    # valori reali (0.80+) non può mai scattare: è lo z-score a decidere.
    pairs = [
        (blk, s - 1) for s in range(1, total_slides) for blk in range(starts[s - 1], starts[s])
    ]
    avg_sim, avg_z = _mean_pair_scores(sim, sim_norm, pairs)
    _LAST_QUALITY.clear()
    _LAST_QUALITY.update({"avg_sim": avg_sim, "avg_z": avg_z, "min_avg_z": min_avg_z})

    if avg_sim < min_avg_similarity:
        log.warning(
            "   [Semantico] Similarità media troppo bassa (%.3f < %.2f): sincronizzazione impossibile.",
            avg_sim,
            min_avg_similarity,
        )
        return None

    if avg_z < min_avg_z:
        # Segnale, NON verdetto: un picco medio basso dice "allineamento
        # dubbio", non "allineamento assente". Fermarsi qui (come faceva la
        # guardia sulla scala grezza) scarterebbe timeline che ancore e LLM
        # possono ancora correggere; il valore alimenta invece l'escalation al
        # LLM (main._should_escalate_weak_signal), il gate --strict-sync e il
        # riepilogo finale.
        _set_weak_signal()
        log.warning(
            "   [Semantico] AVVISO: allineamento a bassa fiducia (picco medio "
            "normalizzato %.2f < %.2f). Il parlato di alcuni segmenti somiglia "
            "poco alla propria slide: la timeline viene usata comunque, ma va "
            "verificata (con l'LLM attivo le slide non ancorate vengono "
            "riposizionate).",
            avg_z,
            min_avg_z,
        )

    timeline: dict[int, float] = {1: 0.0}
    for s in range(2, total_slides + 1):
        block = blocks[starts[s - 1]]
        timeline[s] = float(block.get("first_time", block["time"]))

    # --- Refinamento a livello di parola ---
    # Il blocco di inizio è quantizzato a `window_seconds` secondi (es. 4s).
    # Senza ancora, la slide parte dal primo timestamp reale di parola del
    # blocco (non dall'inizio finestra). Se lo speaker ha detto esplicitamente
    # "slide N" al tempo T, la slide deve comparire esattamente in quel momento
    # (T è già verificato essere entro ±1 blocco dall'inizio scelto). La
    # monotonicità viene preservata con un piccolo margine di sicurezza.
    if anchors:
        for s in anchors:
            if s > 1:
                timeline[s] = float(anchors[s])
    anchor_slides = set(anchors) if anchors else set()
    prev_t = 0.0
    displaced: list[int] = []
    for s in range(2, total_slides + 1):
        clamped = max(timeline[s], prev_t + 0.5)
        # Un'ancora spostata in avanti dal clamp di monotonicità significa che
        # la sua posizione è incompatibile con quella delle slide precedenti:
        # il video non mostrerà la slide al timestamp pronunciato. Non è
        # silenzioso: va segnalato (analysis_sync.py ricontrolla la timeline).
        if s in anchor_slides and clamped > timeline[s] + 1e-6:
            displaced.append(s)
        timeline[s] = clamped
        prev_t = timeline[s]
    if displaced:
        log.warning(
            "   [Semantico] Ancora/e spostata/e dal vincolo di monotonicità "
            "(la slide non compare al timestamp pronunciato): %s. "
            "Verifica la timeline.",
            ", ".join(f"slide {s}" for s in displaced),
        )

    try:
        reconcile_timeline(timeline, total_slides, total_duration)
    except ValueError as e:
        log.warning("   [Semantico] Timeline non valida: %s", e)
        return None

    log.info(
        "   [Semantico] Timeline semantica generata (picco medio normalizzato "
        "%.3f, cosine media %.3f, blocchi: %d).",
        avg_z,
        avg_sim,
        len(blocks),
    )
    return timeline


# =====================================================================
# ORCHESTRATORE (da main.py): parole Whisper -> timeline
# =====================================================================
def semantic_timeline_from_words(
    slide_texts: list[str],
    words_raw: list[Word],
    total_slides: int,
    total_duration: float,
    options: SemanticOptions | None = None,
    anchors: dict[int, float] | None = None,
) -> dict[int, float] | None:
    """
    Pipeline completa della sincronizzazione semantica:

      1. blocchi temporali dalla trascrizione raw
      2. caricamento modello fastembed (con cache locale e fallback automatico
         sul modello alternativo se il principale non si carica)
      3. timeline semantica validata

    Se manca fastembed o nessun modello si carica (es. primo avvio senza rete),
    restituisce None senza interrompere: il chiamante interrompe con avviso.
    """
    opts = options or SemanticOptions()
    log.info("   Sincronizzazione semantica: embedding slide-trascrizione...")

    blocks = build_semantic_blocks(words_raw, total_duration, opts.window_seconds)
    if len(blocks) < total_slides:
        log.warning(
            "   [Semantico] Blocchi trascrizione insufficienti (%d < %d slide).",
            len(blocks),
            total_slides,
        )
        return None

    model = _load_embed_model(
        opts.model_name or DEFAULT_EMBEDDING_MODEL,
        opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
        alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
    )
    if model is None:
        return None

    embed_fn = _make_embed_fn(model)
    return semantic_timeline_from_texts(
        slide_texts,
        blocks,
        total_slides,
        total_duration,
        embed_fn,
        options=opts,
        anchors=anchors,
    )


def alignment_quality_from_words(
    slide_texts: list[str],
    words_raw: list[Word],
    total_slides: int,
    total_duration: float,
    options: SemanticOptions | None = None,
    anchors: dict[int, float] | None = None,
    embed_fn: EmbedFn | None = None,
) -> dict[str, float] | None:
    """Qualità del segnale di allineamento per UNA trascrizione, senza effetti.

    Serve a confrontare due trascrizioni dello STESSO audio (es. decodifica
    veloce contro accurata): la qualità della pipeline è misurata sui blocchi
    che la DP ha assegnato alle slide, quindi le due trascrizioni vanno
    allineate entrambe allo stesso modo, altrimenti il confronto misurerebbe
    anche le differenze di segmentazione. Esegue quindi la stessa catena
    (blocchi -> embedding -> z-score per slide -> competizione -> DP -> media
    dei picchi) e restituisce le stesse voci di ``last_quality()``, più il
    contesto del segnale (``concordance``/``confusability``).

    A differenza di ``semantic_timeline_from_words`` NON tocca la timeline,
    il flag di segnale debole né ``last_quality()``: è una misura, non una
    decisione. L'uguaglianza col metro della sincronizzazione vera è fissata da
    un test (`test_sync.TestBeamAbQuality`), così le due catene non possono
    divergere in silenzio.

    Returns:
        ``{"avg_sim", "avg_z", "concordance", "confusability", "blocks"}``
        oppure None se il segnale non basta (blocchi insufficienti, modello
        non caricabile, nessuna segmentazione valida).
    """
    opts = options or SemanticOptions()
    blocks = build_semantic_blocks(words_raw, total_duration, opts.window_seconds)
    if len(blocks) < total_slides:
        return None

    if embed_fn is None:
        model = _load_embed_model(
            opts.model_name or DEFAULT_EMBEDDING_MODEL,
            opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
            alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
        )
        if model is None:
            return None
        embed_fn = _make_embed_fn(model)

    embedded = _embed_and_report(slide_texts, blocks, total_slides, embed_fn, "Confronto trascrizioni")
    if embedded is None:
        return None
    sim, report = embedded

    min_gap = max(1, int(opts.min_slide_duration / opts.window_seconds))
    candidates = build_candidates(len(blocks), total_slides, min_gap, blocks, anchors)
    if candidates is None:
        return None
    sim_norm = zscore_matrix(sim)
    starts = monotonic_alignment(competition_matrix(sim_norm, opts.temperature), candidates, min_gap)
    if starts is None:
        return None

    pairs = [
        (blk, s - 1) for s in range(1, total_slides) for blk in range(starts[s - 1], starts[s])
    ]
    avg_sim, avg_z = _mean_pair_scores(sim, sim_norm, pairs)
    return {
        "avg_sim": avg_sim,
        "avg_z": avg_z,
        "concordance": float(report.get("concordance", 0.0)),
        "confusability": float(report.get("confusability", 0.0)),
        "blocks": float(len(blocks)),
    }


# =====================================================================
# VERIFICA DETERMINISTICA DEL MAPPING ANCORE (offset numerazione parlata)
# =====================================================================
def _lis_positions(values: Sequence[int]) -> list[int]:
    """Indici di una sotto-sequenza STRETTAMENTE crescente di ``values``
    (Longest Increasing Subsequence), in ordine di indice crescente.

    Usata per scartare gli outlier da una mappa "slide parlata -> slide del PDF"
    dedotta dal contenuto: un valore fuori sequenza (es. un'ancora di chiusura
    il cui parlato somiglia a più slide) rompe la monotonia e va escluso.
    """
    n = len(values)
    if n == 0:
        return []
    tails: list[int] = []
    tails_idx: list[int] = []
    prev = [-1] * n
    for i, v in enumerate(values):
        pos = bisect.bisect_left(tails, v)
        if pos > 0:
            prev[i] = tails_idx[pos - 1]
        if pos == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[pos] = v
            tails_idx[pos] = i
    order: list[int] = []
    k = tails_idx[-1]
    while k != -1:
        order.append(k)
        k = prev[k]
    order.reverse()
    return order


def verify_anchor_mapping_embedding(
    slide_texts: Sequence[str],
    words_raw: Sequence[Word],
    anchors: dict[int, float],
    total_slides: int,
    window_seconds: float = 40.0,
    options: SemanticOptions | None = None,
    embed_fn: EmbedFn | None = None,
    report: dict[str, Any] | None = None,
) -> dict[int, float] | None:
    """Corregge la numerazione parlata sistematicamente sfasata usando gli
    embeddings locali (nessuna chiamata LLM).

    ``report`` (opzionale): se fornito, riceve ``{"suspicious": bool}`` —
    True quando l'euristica ha visto almeno un'ancora il cui contenuto NON
    conferma il numero parlato ma non può correggere in modo affidabile
    (offset misti senza run correggibile, offset uniforme fuori range, ...).
    Il chiamante usa il segnale per decidere se vale la pena la verifica LLM
    anche quando di solito la salterebbe (una sola slide senza ancora).

    Se lo speaker numera le slide escludendo la copertina (dice "slide 1"
    mostrando la slide 2 del PDF), TUTTI i riferimenti sono sfasati dello
    stesso offset. Per ogni ancora "slide N" si legge il testo audio nella
    finestra successiva e si trova la slide del PDF semanticamente più vicina:
    se lo spostamento ``slide_dx - N`` è lo STESSO per tutte le ancore
    (offset coerente), lo si applica a tutte e si restituisce il mapping
    corretto. Se l'offset non è univoco o è zero, restituisce None (il
    chiamante ripiega sulle ancore originali o sulla verifica LLM).

    Gestisce anche l'offset PARZIALE (a gradino): se la numerazione è
    allineata per le prime ancore e poi slitta di una costante per le
    successive (es. il podcast salta una slide del PDF a metà narrazione e
    dice "slide 7" mostrando la slide 8), gli offset misti farebbero fallire
    la verifica uniforme: si corregge allora SOLO il tratto sfasato (run
    contigua di ancore con lo stesso offset non-zero, almeno 2 ancore),
    mantenendo intatte le ancore già allineate.

    Gestisce infine il DRIFT PROGRESSIVO: quando la numerazione parlata si
    allontana via via dal PDF (es. il podcast ha meno slide del PDF: "slide 3"
    mostra la 4, più avanti "slide 6" mostra la 9), gli offset non costanti
    formano più run e le regole precedenti rinunciano. Se ALMENO 2 ancore sono
    contraddette dal parlato si adotta allora la slide del PDF più simile
    ("best") di ogni ancora, tenendo la sola sotto-sequenza monotona crescente
    (LIS) per scartare gli outlier: correzione deterministica, senza LLM. Due
    ancore che il contenuto manda sulla STESSA slide senza salti all'indietro
    sono invece ambiguità reale, non drift: niente correzione.

    ``embed_fn`` è iniettabile (stessa convenzione di ``semantic_timeline_from_texts``):
    nei test si passa un embedder finto, in produzione viene caricato il
    modello fastembed locale.

    Returns:
        Ancora corretta {slide_pdf: tempo} oppure None se non c'è un offset
        sistematico rilevabile in modo affidabile.
    """
    def _done(value: dict[int, float] | None, suspicious: bool) -> dict[int, float] | None:
        """Imposta il report (se richiesto) e restituisce il valore."""
        if report is not None:
            report["suspicious"] = suspicious
        return value

    if not words_raw or len(anchors) < 2:
        return _done(None, False)

    opts = options or SemanticOptions()
    if embed_fn is None:
        model = _load_embed_model(
            opts.model_name or DEFAULT_EMBEDDING_MODEL,
            opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
            alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
        )
        if model is None:
            return _done(None, False)
        embed_fn = _make_embed_fn(model)

    slide_clean = [_clean_slide_text(t) for t in slide_texts[:total_slides]]
    try:
        slide_emb = embed_fn(slide_clean)
    except Exception as e:  # noqa: BLE001 - embedding può fallire per molti motivi
        log.warning("   [Ancore] Embedding slide non riuscito: %s", e)
        return _done(None, False)

    pairs: list[tuple[int, float, int]] = []  # (slide parlata, tempo, offset)
    for s, t in sorted(anchors.items(), key=lambda kv: kv[1]):
        excerpt = " ".join(w["word"] for w in words_raw if t <= w["start"] < t + window_seconds).strip()
        if not excerpt:
            continue
        try:
            excerpt_emb = embed_fn([excerpt])[0]
        except Exception as e:  # noqa: BLE001
            log.warning("   [Ancore] Embedding del parlato a %.1fs non riuscito: %s", t, e)
            continue
        sims = slide_emb @ excerpt_emb
        best = int(np.argmax(sims)) + 1  # 1-based: slide del PDF più simile
        pairs.append((s, t, best - s))

    offsets = [off for _, _, off in pairs]
    if len(offsets) < 2:
        # Troppo poco segnale per un giudizio: non sospetto (evita chiamate
        # LLM spurie quando la verifica non può valutare nulla).
        return _done(None, False)

    # 1) Offset UNIFORME su tutte le ancore (es. copertina esclusa): un
    #    segnale forte, correzione globale.
    if len(set(offsets)) == 1:
        offset = offsets[0]
        if offset == 0:
            # Tutte le ancore confermate dal contenuto: mapping coerente.
            return _done(None, False)

        # Prudenza massima: l'offset si applica SOLO se porta tutte le ancore a
        # slide valide del PDF. Un mapping parziale (qualche ancora fuori range)
        # significherebbe che l'offset non è coerente con l'intero set: niente
        # correzione, il chiamante ripiega sulle ancore originali. Il contenuto
        # però NON conferma la numerazione: resta sospetto.
        for s in anchors:
            if not 1 <= s + offset <= total_slides:
                return _done(None, True)

        corrected = {s + offset: t for s, t in anchors.items()}
        if not corrected:
            return _done(None, True)

        log.info(
            "   [Ancore] Offset sistematico %+d rilevato dagli embeddings: "
            "correggo la numerazione parlata su %d ancore.",
            offset,
            len(corrected),
        )
        return _done(corrected, False)

    # 2) Offset PARZIALE (a gradino): le ancore sono valutate in ordine
    #    temporale; si raggruppano le run contigue con lo stesso offset
    #    non-zero (le ancore allineate, offset 0, spezzano le run). Se esiste
    #    UN'UNICA run sfasata con almeno 2 ancore, lo sfasamento è sistematico
    #    nel suo tratto: si corregge solo quello, lasciando intatta la parte
    #    già allineata (es. "slide 7" che mostra la slide 8 perché il podcast
    #    salta una slide del PDF a metà narrazione).
    #
    #    Offsets misti = almeno un'ancora NON confermata dal contenuto: se non
    #    c'è una run correggibile (una sola ancora sfasata, drift a
    #    intermittenza) il mapping è AMBIGUO ma SOSPETTO: il chiamante può
    #    chiedere all'LLM di decidere leggendo il contenuto.
    runs: list[tuple[int, list[tuple[int, float]]]] = []  # (offset, ancore)
    for s, t, off in pairs:
        if off != 0 and runs and runs[-1][0] == off:
            runs[-1][1].append((s, t))
        elif off != 0:
            runs.append((off, [(s, t)]))
    shifted_runs = [r for r in runs if len(r[1]) >= 2]
    if len(shifted_runs) != 1:
        # 3) Mapping per CONTENUTO (drift progressivo): la numerazione parlata
        #    può allontanarsi via via dal PDF (es. il podcast ha meno slide del
        #    PDF: "slide 3" mostra la 4 e più avanti "slide 6" mostra la 9), per
        #    cui gli offset NON sono costanti e formano più run: le regole
        #    precedenti (offset uniforme / singola run) rinunciano e delegano la
        #    correzione all'LLM. Il contenuto però è spesso già univoco: se
        #    ALMENO 2 ancore sono contraddette dal parlato, si adotta la slide
        #    "best" di ogni ancora tenendo la sola sotto-sequenza monotona
        #    crescente (LIS), che scarta gli outlier (es. l'ancora di chiusura
        #    il cui parlato somiglia a più slide).
        #    Due ancore che il contenuto manda sulla STESSA slide senza alcun
        #    salto all'indietro sono invece ambiguità reale, non drift: niente
        #    correzione (resta il sospetto per la verifica LLM).
        ordered = sorted(pairs, key=lambda p: p[1])
        if sum(1 for _, _, off in ordered if off != 0) >= 2:
            best_seq = [s + off for s, _, off in ordered]
            descends = any(best_seq[i] < best_seq[i - 1] for i in range(1, len(best_seq)))
            has_duplicates = len(set(best_seq)) != len(best_seq)
            if not (has_duplicates and not descends):
                kept = _lis_positions(best_seq)
                kept_pairs = [ordered[i] for i in kept]
                remaps = [
                    (spoken, spoken + off) for spoken, _, off in kept_pairs if off != 0
                ]
                corrected = {spoken + off: time for spoken, time, off in kept_pairs}
                if (
                    len(remaps) >= 2
                    and len(corrected) == len(kept)
                    and set(corrected) != set(anchors)
                ):
                    log.info(
                        "   [Ancore] Drift progressivo corretto dagli embeddings: "
                        "%d ancore rimappate sul contenuto (%s).",
                        len(remaps),
                        ", ".join(f"slide {a} -> {b}" for a, b in remaps),
                    )
                    return _done(corrected, False)
        return _done(None, True)
    offset, run = shifted_runs[0]

    # Validità del rimappo: tutte le slide corrette dentro 1..total_slides,
    # nessuna collisione con le ancore non corrette e ordine temporale
    # crescente preservato (le ancore esplicite restano vincoli esatti).
    run_slides = {s for s, _ in run}
    corrected = {}
    for s, t in anchors.items():
        if s in run_slides:
            if not 1 <= s + offset <= total_slides:
                return _done(None, True)
            corrected[s + offset] = t
        else:
            corrected[s] = t
    if len(corrected) != len(anchors):
        return _done(None, True)
    seq = sorted(corrected.items(), key=lambda kv: kv[1])
    if any(seq[i][0] >= seq[i + 1][0] for i in range(len(seq) - 1)):
        return _done(None, True)

    log.info(
        "   [Ancore] Offset parziale %+d rilevato dagli embeddings su %d ancore "
        "(%s): correggo solo il tratto sfasato.",
        offset,
        len(run),
        ", ".join(str(s) for s, _ in run),
    )
    return _done(corrected, False)


def make_anchor_remap_filter(
    slide_texts: Sequence[str],
    words_raw: Sequence[Word],
    total_slides: int,
    window_seconds: float = 40.0,
    options: SemanticOptions | None = None,
    embed_fn: EmbedFn | None = None,
) -> Callable[[int, float, int], bool | None] | None:
    """Restituisce un validatore dei rimappi ancora proposti dall'LLM.

    La verifica LLM del mapping (``llm_verify_anchor_mapping``) corregge la
    numerazione parlata sfasata, ma un rimappo non supportato dal contenuto
    del parlato rischia di rompere la timeline: le ancore esplicite "slide N"
    sono vincoli ad alta precisione, e un rimappo ``s -> s'`` viene accettato
    SOLO se il parlato subito dopo il riferimento è semanticamente più vicino
    alla slide ``s'`` che alla slide parlata ``s`` (stesso modello embedding
    della verifica deterministica). Così l'LLM non può spostare un'ancora
    corretta (es. "slide 5" il cui contenuto è davvero la slide 5) e resta
    utile solo dove il contenuto conferma davvero lo sfasamento.

    La finestra di parlato per ogni ancora è cachata: ogni ancora viene
    codificata una sola volta per run.

    ``embed_fn`` è iniettabile (stessa convenzione di
    ``verify_anchor_mapping_embedding``): nei test si passa un embedder finto,
    in produzione viene caricato il modello fastembed locale.

    Returns:
        ``None`` se l'embedder non è disponibile (l'LLM fa fede, comportamento
        storico). Altrimenti una funzione ``(slide_parlata, tempo, slide_mappata)
        -> bool`` che restituisce ``True`` (rimappo supportato), ``False``
        (contraddetto dal contenuto) o ``None`` (finestra di parlato vuota o
        embedding fallito: nessuna opinione).
    """
    opts = options or SemanticOptions()
    if embed_fn is None:
        try:
            model = _load_embed_model(
                opts.model_name or DEFAULT_EMBEDDING_MODEL,
                opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
                alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
            )
            if model is None:
                return None
            embed_fn = _make_embed_fn(model)
        except Exception as e:  # noqa: BLE001
            log.warning("   [Ancore] Embedder non disponibile per la verifica dei rimappi: %s", e)
            return None

    slide_clean = [_clean_slide_text(t) for t in slide_texts[:total_slides]]
    try:
        slide_emb = embed_fn(slide_clean)
    except Exception as e:  # noqa: BLE001
        log.warning("   [Ancore] Embedding slide non riuscito: %s", e)
        return None

    _excerpt_emb: dict[float, np.ndarray] = {}

    def _validate(spoken_slide: int, t: float, new_slide: int) -> bool | None:
        exc = _excerpt_emb.get(round(t, 1))
        if exc is None:
            excerpt = " ".join(
                w["word"] for w in words_raw if t <= w["start"] < t + window_seconds
            ).strip()
            if not excerpt:
                return None
            try:
                exc = embed_fn([excerpt])[0]
            except Exception as e:  # noqa: BLE001
                log.warning("   [Ancore] Embedding del parlato a %.1fs non riuscito: %s", t, e)
                return None
            _excerpt_emb[round(t, 1)] = exc
        sims = slide_emb @ exc
        return bool(sims[new_slide - 1] > sims[spoken_slide - 1])

    return _validate


# =====================================================================
# SELEZIONE LIBERA (riordino): le slide possono apparire in qualsiasi
# ordine e ripetersi, seguendo il contenuto del podcast.
# =====================================================================
def _smooth_segments(
    best: np.ndarray,
    min_blocks: int,
) -> list[tuple[int, int, int]]:
    """
    Converte la sequenza di argmax per blocco in segmenti (slide, inizio,
    fine) con anti-flicker: i segmenti più corti di `min_blocks` blocchi
    vengono fusi nel vicino più lungo.

    Returns:
        Lista di tuple (slide 0-based, blocco_inizio, blocco_fine).
    """
    if len(best) == 0:
        return []
    runs: list[list[int]] = []
    for s in best.tolist():
        if runs and runs[-1][0] == s:
            runs[-1].append(s)
        else:
            runs.append([s])
    # rappresenta come (slide, start_block, end_block escluso)
    segs = []
    start = 0
    for r in runs:
        end = start + len(r)
        segs.append((r[0], start, end))
        start = end

    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i, (_s, a, b) in enumerate(segs):
            if b - a >= min_blocks:
                continue
            # fondi nel vicino più lungo
            if i == 0:
                n = 1
            elif i == len(segs) - 1:
                n = i - 1
            else:
                n = i - 1 if segs[i - 1][2] - segs[i - 1][1] >= segs[i + 1][2] - segs[i + 1][1] else i + 1
            lo, hi = min(i, n), max(i, n)
            # Il segmento corto viene ASSORBITO dal vicino più lungo: la slide
            # del risultato è quella del vicino (n), non quella corta (i).
            merged = (segs[n][0], segs[lo][1], segs[hi][2])
            segs = [*segs[:lo], merged, *segs[hi + 1 :]]
            changed = True
            break

    # Fusione finale: segmenti adiacenti con la STESSA slide diventano uno
    # solo (es. dopo una fusione corta possono restare due blocchi 8-8 o
    # quattro 14-14-14-14 consecutivi che vanno uniti).
    final: list[tuple[int, int, int]] = []
    for s, a, b in segs:
        if final and final[-1][0] == s:
            final[-1] = (s, final[-1][1], b)
        else:
            final.append((s, a, b))
    return [(s, a, b) for s, a, b in final if b - a >= min_blocks]


def free_order_segments_from_texts(
    slide_texts: Sequence[str],
    blocks: Sequence[dict[str, Any]],
    total_slides: int,
    total_duration: float,
    embed_fn: EmbedFn,
    options: SemanticOptions | None = None,
) -> list[Segment] | None:
    """
    Selezione libera delle slide: per ogni blocco audio prende la slide
    semanticamente più vicina, SENZA vincolo di ordine crescente. Le slide
    possono quindi apparire in qualsiasi ordine e ripetersi (es. si parla di
    overt/covert a 280s e di nuovo a 975s -> slide 4 mostrata due volte).

    Anti-flicker: i segmenti più corti di `options.min_segment_seconds` vengono
    fusi nel vicino più lungo, così la slide non cambia ogni 4 secondi.

    Returns:
        Lista di segmenti {"slide": n (1-based), "start": s, "end": e},
        oppure None se il segnale è insufficiente.
    """
    opts = options or SemanticOptions()
    if total_slides < 2 or len(blocks) < 2:
        log.warning(
            "   [Libero] Blocchi insufficienti (%d) per la selezione libera.",
            len(blocks),
        )
        return None
    embedded = _embed_and_report(slide_texts, blocks, total_slides, embed_fn, "Libero")
    if embedded is None:
        return None
    sim, report = embedded

    if weak_signal(report):
        _set_weak_signal()
        log.warning(
            "   [Libero] AVVISO: segnale debole (concordanza picchi %.0f%%, "
            "slide confondibili %.0f%%). La selezione libera segue comunque "
            "il contenuto, ma con slide simili le transizioni restano incerte.",
            report["concordance"] * 100,
            report["confusability"] * 100,
        )

    znorm = zscore_matrix(sim)
    best = znorm.argmax(axis=1)  # 0-based slide per blocco

    # ceil: rispetta sempre il minimo dichiarato (round può scendere sotto)
    min_blocks = max(1, int(np.ceil(opts.min_segment_seconds / opts.window_seconds)))
    segs = _smooth_segments(best, min_blocks)

    # Guardia di qualità: picco medio (normalizzato) dei segmenti scelti.
    # Come nel flusso ordinato la cosine grezza non discrimina: vale lo
    # z-score della matrice già usata per scegliere la slide di ogni blocco.
    pairs = [(blk, sld) for sld, a, b in segs for blk in range(a, b)]
    avg_sim, avg_z = _mean_pair_scores(sim, znorm, pairs)
    _LAST_QUALITY.clear()
    _LAST_QUALITY.update({"avg_sim": avg_sim, "avg_z": avg_z, "min_avg_z": opts.min_avg_z})

    if avg_sim < opts.min_avg_similarity:
        log.warning(
            "   [Libero] Similarità media troppo bassa (%.3f < %.2f): nessuna slide affidabile da mostrare.",
            avg_sim,
            opts.min_avg_similarity,
        )
        return None

    if avg_z < opts.min_avg_z:
        # Segnale, non verdetto (vedi il flusso ordinato): la selezione libera
        # segue il contenuto blocco per blocco, quindi una bassa fiducia non la
        # rende inutilizzabile, ma l'utente deve saperlo.
        _set_weak_signal()
        log.warning(
            "   [Libero] AVVISO: selezione a bassa fiducia (picco medio "
            "normalizzato %.2f < %.2f): le slide scelte possono non essere "
            "quelle giuste, verifica la timeline.",
            avg_z,
            opts.min_avg_z,
        )

    timeline_segments: list[Segment] = []
    for s, a, b in segs:
        start = float(blocks[a].get("first_time", blocks[a]["time"]))
        end = float(blocks[b].get("first_time", blocks[b]["time"])) if b < len(blocks) else total_duration
        if end <= start:
            end = min(total_duration, start + (b - a) * opts.window_seconds)
        timeline_segments.append({"slide": int(s) + 1, "start": start, "end": end})

    # Il primo segmento parte da 0.0 (come la slide 1 nel flusso classico):
    # con silenzio iniziale la prima parola sarebbe > 0 e il video perderebbe
    # i primi secondi di parlato (video più corto di audio+buffer).
    if timeline_segments:
        timeline_segments[0]["start"] = 0.0

    if not timeline_segments:
        log.warning(
            "   [Libero] Nessun segmento con durata minima: segnale insufficiente per la selezione libera.",
        )
        return None

    log.info(
        "   [Libero] %d segmenti generati (picco medio normalizzato %.3f, "
        "cosine media %.3f, %d blocchi).",
        len(timeline_segments),
        avg_z,
        avg_sim,
        len(blocks),
    )
    return timeline_segments


def free_order_segments_from_words(
    slide_texts: list[str],
    words_raw: list[Word],
    total_slides: int,
    total_duration: float,
    options: SemanticOptions | None = None,
) -> list[Segment] | None:
    """
    Pipeline completa della selezione libera: blocchi, modello embedding,
    segmenti non monotoni (stesso motore del flusso classico).
    """
    opts = options or SemanticOptions()
    log.info("   Sincronizzazione semantica (selezione libera, riordino slide)...")

    blocks = build_semantic_blocks(words_raw, total_duration, opts.window_seconds)
    if len(blocks) < 2:
        log.warning(
            "   [Libero] Blocchi trascrizione insufficienti (%d).",
            len(blocks),
        )
        return None

    model = _load_embed_model(
        opts.model_name or DEFAULT_EMBEDDING_MODEL,
        opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
        alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
    )
    if model is None:
        return None

    embed_fn = _make_embed_fn(model)
    return free_order_segments_from_texts(
        slide_texts,
        blocks,
        total_slides,
        total_duration,
        embed_fn,
        options=opts,
    )


# =====================================================================
# POST-ELABORAZIONE SEGMENTI LLM (flusso libero)
# =====================================================================
# I segmenti LLM sono quantizzati al chunk (30s): l'LLM sceglie la slide per
# ogni chunk ma non può esprimere confini più fini, quindi i cambi slide
# possono cadere a metà discorso, e l'ultimo chunk parziale può produrre un
# segmento finale molto corto. Queste due funzioni sono il refinamento
# deterministico a valle dell'LLM (nessuna chiamata extra):
#
#   - ``refine_llm_segment_boundaries``: sposta ogni confine entro
#     ``±window_seconds`` al punto di parola in cui la similarità locale "si
#     inverte" tra slide uscente ed entrante (confini a granularità di parola,
#     non di chunk);
#   - ``merge_short_segments``: assorbe i segmenti residui sotto soglia nel
#     vicino più lungo (stessa filosofia dell'anti-flicker del MiniLM).
#
# Vengono applicate SOLO al flusso libero e SOLO ai segmenti LLM (il MiniLM
# del flusso libero ha già il suo anti-flicker; il flusso ordinato ha ancore
# esatte che non vanno toccate).
DEFAULT_LLM_MIN_SEGMENT_SECONDS = 15.0  # soglia sotto cui un segmento LLM va assorbito


def merge_short_segments(
    segments: list[dict[str, Any]],
    min_seconds: float = DEFAULT_LLM_MIN_SEGMENT_SECONDS,
) -> list[dict[str, Any]]:
    """Assorbe i segmenti più corti di ``min_seconds`` nel vicino più lungo.

    Il segmento corto viene FUSO nel vicino più lungo: la slide del risultato
    è quella del vicino, l'inizio quello del segmento più a sinistra e la fine
    quello del più a destra. Il segmento finale corto (es. ultimo chunk
    parziale di pochi secondi) viene assorbito dal precedente. I segmenti
    adiacenti che finiscono con la stessa slide vengono uniti in un'unica
    occorrenza.

    Returns:
        Nuova lista di segmenti (mai più lunga dell'originale).
    """
    if not segments:
        return []
    out = [dict(s) for s in segments]
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i, seg in enumerate(out):
            if float(seg["end"]) - float(seg["start"]) >= min_seconds:
                continue
            # Scegli il vicino più lungo; il corto viene ASSORBITO da esso.
            if i == 0:
                n = 1
            elif i == len(out) - 1:
                n = i - 1
            else:
                left_len = float(out[i - 1]["end"]) - float(out[i - 1]["start"])
                right_len = float(out[i + 1]["end"]) - float(out[i + 1]["start"])
                n = i - 1 if left_len >= right_len else i + 1
            lo, hi = min(i, n), max(i, n)
            merged = {
                "slide": out[n]["slide"],
                "start": out[lo]["start"],
                "end": out[hi]["end"],
            }
            log.info(
                "   [LLM] Segmento corto (%.1fs) assorbito: slide %s -> %s (%.1fs-%.1fs).",
                float(seg["end"]) - float(seg["start"]),
                seg["slide"],
                out[n]["slide"],
                float(merged["start"]),
                float(merged["end"]),
            )
            out = [*out[:lo], merged, *out[hi + 1 :]]
            changed = True
            break

    # Unione finale: segmenti adiacenti con la STESSA slide (dopo gli
    # assorbimenti possono restarne due consecutivi).
    final: list[dict[str, Any]] = []
    for seg in out:
        if final and final[-1]["slide"] == seg["slide"]:
            final[-1]["end"] = seg["end"]
        else:
            final.append(seg)
    return final


def refine_llm_segment_boundaries(
    segments: list[dict[str, Any]],
    words: list[Word],
    slide_texts: Sequence[str],
    embed_fn: EmbedFn,
    window_seconds: float = 30.0,
    min_segment_seconds: float = 8.0,
    context_seconds: float = 10.0,
    margin: float = 0.01,
    refine_slides: Collection[int] | None = None,
    anchors: dict[int, float] | None = None,
    max_candidates: int = 48,
    search_window: Mapping[int, tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    """Rifinisce i confini dei segmenti LLM a granularità di parola.

    Ogni confine (inizio di un segmento) viene spostato entro
    ``±window_seconds`` al timestamp di parola che massimizza la coerenza
    locale: similarità della finestra di parlato appena PRIMA del confine con
    la slide uscente + similarità della finestra appena DOPO con la slide
    entrante. Se lo spostamento non migliora di almeno ``margin`` rispetto al
    confine corrente, il confine resta dov'è.

    ``max_candidates`` limita quanti timestamp-candidato vengono valutati per
    confine (campionamento uniforme con stride, confine corrente SEMPRE
    incluso): su finestre ampie evita di embeddere centinaia di finestre per
    confine, che è il collo principale del raffinamento. La risoluzione resta
    a livello di parola e la similarità è liscia attorno all'optimum, quindi
    la perdita di qualità è trascurabile.

    ``search_window`` (opzionale, ``{slide: (lo, hi)}``) SOSTITUISCE la
    finestra simmetrica ``±window_seconds`` per i confini indicati: serve alla
    riparazione guidata dalla verifica del video, dove la direzione dello
    spostamento è nota dall'evidenza (il video mostra la slide del segmento
    adiacente) e il confine può essere lontano più di ``window_seconds``. Il
    confine attuale è sempre incluso nella ricerca (base di confronto equa) e
    restano i limiti dei segmenti adiacenti.

    ``refine_slides`` (opzionale) limita il raffinamento ai SOLI confini di
    inizio delle slide indicate (numeri 1-based): i confini delle altre slide
    restano esattamente dove sono. Serve al flusso ordinato, dove le ancore
    ``"slide N"`` sono vincoli esatti e inviolabili e solo le slide senza
    ancora esplicita possono muoversi. Di default (None) tutti i confini sono
    candidati (flusso libero).

    Vincoli rispettati: monotonicità, durata minima ``min_segment_seconds``
    per i segmenti adiacenti e confini mai fuori dai limiti del segmento.

    Se l'embedding fallisce (modello iniettabile, errori di rete) i segmenti
    vengono restituiti invariati.

    Returns:
        Nuova lista di segmenti (l'originale è intatta).
    """
    if len(segments) < 2 or not words:
        return segments
    try:
        slide_emb = np.asarray(embed_fn([_clean_slide_text(t) for t in slide_texts]), dtype=np.float32)
    except Exception as e:  # noqa: BLE001 - embed_fn è iniettabile/esterno
        log.warning("   [LLM/Refine] Embedding slide non disponibile: confini invariati (%s).", e)
        return segments
    if slide_emb.ndim != 2 or slide_emb.shape[0] < 1:
        return segments
    slide_emb = (slide_emb / np.maximum(np.linalg.norm(slide_emb, axis=1, keepdims=True), 1e-9)).astype(np.float32)

    word_times = [float(w["start"]) for w in words]
    out = [dict(s) for s in segments]
    moved = 0
    for i in range(1, len(out)):
        prev_slide = int(out[i - 1]["slide"]) - 1
        next_slide = int(out[i]["slide"]) - 1
        if prev_slide == next_slide:
            continue
        # Flusso ordinato: i confini delle slide con ancora esplicita sono
        # vincoli esatti (timestamp reali del parlato) e non vanno toccati.
        if refine_slides is not None and int(out[i]["slide"]) not in refine_slides:
            continue
        if not (0 <= prev_slide < slide_emb.shape[0]) or not (0 <= next_slide < slide_emb.shape[0]):
            continue
        t_current = float(out[i]["start"])
        lo = max(float(out[i - 1]["start"]) + min_segment_seconds, t_current - window_seconds)
        hi = min(float(out[i]["end"]) - min_segment_seconds, t_current + window_seconds)
        if search_window is not None and int(out[i]["slide"]) in search_window:
            # Finestra imposta dall'esterno (verifica del video): il confine
            # può uscire dalla finestra simmetrica di default, ma mai dai
            # limiti dei segmenti adiacenti né dall'inclusione del confine
            # attuale (serve come termine di confronto).
            wlo, whi = search_window[int(out[i]["slide"])]
            lo = max(float(out[i - 1]["start"]) + min_segment_seconds, min(float(wlo), t_current))
            hi = min(float(out[i]["end"]) - min_segment_seconds, max(float(whi), t_current))
        if hi <= lo:
            continue
        candidates = sorted({t_current, *[t for t in word_times if lo <= t <= hi]})
        # Campionamento con stride: oltre max_candidates timestamp, valuta un
        # sottoinsieme uniforme (il confine corrente è sempre mantenuto). Su
        # finestre ampie questo evita centinaia di embedding per confine.
        if len(candidates) > max_candidates > 1:
            step = -(-len(candidates) // (max_candidates - 1))  # ceil-div
            sampled = set(candidates[::step])
            sampled.add(t_current)
            candidates = sorted(sampled)
        if len(candidates) < 2:
            continue

        # Finestre locali per ogni candidato. I candidati con una finestra
        # VUOTA (bordi dell'audio o silenzi) vengono scartati PRIMA
        # dell'embedding: una stringa "" può far fallire fastembed o produrre
        # vettori degeneri, e senza confronto col confine attuale il confine
        # verrebbe saltato del tutto.
        cand_pairs: list[tuple[float, str, str]] = []
        for t in candidates:
            before = _window_words(words, word_times, t - context_seconds, t)
            after = _window_words(words, word_times, t, t + context_seconds)
            if not before or not after:
                continue
            cand_pairs.append((t, before, after))
        if len(cand_pairs) < 2:
            continue
        cur_pair = next((p for p in cand_pairs if p[0] == t_current), None)
        if cur_pair is None:
            continue  # finestre vuote anche sul confine attuale: niente confronto equo

        # Testi deduplicati (un solo batch di embedding).
        texts: list[str] = []
        seen: dict[str, int] = {}
        indexed: list[tuple[float, int, int]] = []
        for t, before, after in cand_pairs:
            pair: list[int] = []
            for txt in (before, after):
                if txt not in seen:
                    seen[txt] = len(texts)
                    texts.append(txt)
                pair.append(seen[txt])
            indexed.append((t, pair[0], pair[1]))
        try:
            embs = np.asarray(embed_fn(texts), dtype=np.float32)
            if embs.ndim != 2 or embs.shape[0] != len(texts):
                continue
            embs = (embs / np.maximum(np.linalg.norm(embs, axis=1, keepdims=True), 1e-9)).astype(np.float32)
        except Exception as e:  # noqa: BLE001 - embed_fn è iniettabile/esterno
            log.warning("   [LLM/Refine] Embedding finestre non disponibile: confini invariati (%s).", e)
            continue

        scores = [
            float(embs[pair[1]] @ slide_emb[prev_slide]) + float(embs[pair[2]] @ slide_emb[next_slide])
            for pair in indexed
        ]
        best_idx = int(np.argmax(scores))
        cur_idx = next(i for i, p in enumerate(indexed) if p[0] == t_current)
        if best_idx == cur_idx or scores[best_idx] <= scores[cur_idx] + margin:
            continue
        best_t = indexed[best_idx][0]
        # Vincolo ancore: il nuovo confine non deve violare la monotonia
        # con le ancore esplicita (es. non posizionare slide 4 prima
        # dell'ancora slide 3 a 758.4s).
        if anchors:
            violates = False
            for a_slide, a_time in anchors.items():
                # Il confine (start di next_slide) deve essere DOPO l'ancora
                # se l'ancora e' la slide che termina o precede questo confine.
                if a_slide <= prev_slide + 1 and best_t < a_time - 0.1:
                    violates = True
                    break
                # Il confine deve essere PRIMA dell'ancora se l'ancora e' la
                # slide che inizia o segue questo confine.
                if a_slide >= next_slide + 1 and best_t > a_time + 0.1:
                    violates = True
                    break
            if violates:
                continue
        out[i]["start"] = best_t
        out[i - 1]["end"] = best_t
        moved += 1
        log.info(
            "   [LLM/Refine] Confine spostato a %.1fs (era %.1fs): slide %d -> %d (score %.3f vs %.3f).",
            best_t,
            t_current,
            prev_slide + 1,
            next_slide + 1,
            scores[best_idx],
            scores[cur_idx],
        )
    if moved:
        log.info("   [LLM/Refine] %d confine/i raffinato/i a livello di parola.", moved)
    return out


def _window_words(words: Sequence[Word], word_times: Sequence[float], start: float, end: float) -> str:
    """Testo parlato nell'intervallo [start, end), via bisect (le parole sono ordinate)."""
    lo_i = bisect.bisect_left(word_times, start)
    hi_i = bisect.bisect_left(word_times, end)
    return " ".join(words[k]["word"] for k in range(lo_i, hi_i))


def refine_llm_segments_from_words(
    segments: list[dict[str, Any]],
    words_raw: list[Word],
    slide_texts: list[str],
    options: SemanticOptions | None = None,
    window_seconds: float = 30.0,
) -> list[dict[str, Any]]:
    """Refinamento dei confini LLM con il modello embedding reale (fastembed).

    Come ``semantic_timeline_from_words``: carica il modello (con fallback sul
    modello alternativo), costruisce la ``EmbedFn`` e delega a
    ``refine_llm_segment_boundaries``. Se il modello non è disponibile i
    segmenti vengono restituiti invariati (nessuna interruzione).
    """
    opts = options or SemanticOptions()
    model = _load_embed_model(
        opts.model_name or DEFAULT_EMBEDDING_MODEL,
        opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
        alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
    )
    if model is None:
        return segments
    embed_fn = _make_embed_fn(model)
    return refine_llm_segment_boundaries(
        segments,
        words_raw,
        slide_texts,
        embed_fn,
        window_seconds=window_seconds,
        min_segment_seconds=max(5.0, opts.min_slide_duration),
    )


# =====================================================================
# POST-ELABORAZIONE TIMELINE LLM ORDINATA (flusso slide-audio/audio-slide)
# =====================================================================
# Nel flusso ordinato l'LLM posiziona SOLO le slide senza ancora esplicita
# (``llm_ordered_timeline``): i loro confini di inizio restano quantizzati al
# chunk (es. 30s) e possono cadere a metà parola o nel mezzo di un discorso
# ancora dedicato alla slide precedente. Queste due funzioni raffinano SOLO
# quei confini a livello di parola; le ancore esatte (``"slide N"``) non
# vengono MAI toccate.
def refine_ordered_llm_timeline(
    timeline: dict[int, float],
    anchors: dict[int, float],
    words: list[Word],
    slide_texts: Sequence[str],
    total_duration: float,
    embed_fn: EmbedFn,
    window_seconds: float = 30.0,
    min_segment_seconds: float = 8.0,
) -> dict[int, float]:
    """Rifinisce i confini della timeline LLM ordinata a granularità di parola.

    Converte la timeline ``{slide: start}`` in segmenti e delega a
    ``refine_llm_segment_boundaries`` limitando i candidati alle sole slide
    SENZA ancora (``refine_slides``): le ancore restano al timestamp esatto
    pronunciato nel parlato. La timeline in uscita rispetta la monotonicità
    (garantita dai vincoli del refine) ed è quindi riconciliabile.

    Se il modello embedding fallisce (embed_fn iniettabile/esterno) la
    timeline viene restituita invariata.

    Returns:
        Nuova timeline {slide: start}; quella in ingresso è intatta.
    """
    total_slides = len(slide_texts)
    refine_slides = {s for s in range(2, total_slides + 1) if s not in anchors}
    if not refine_slides or len(timeline) < 2:
        return timeline
    ordered = sorted(timeline)
    segments: list[dict[str, Any]] = []
    for i, s in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        segments.append(
            {
                "slide": int(s),
                "start": float(timeline[s]),
                "end": float(timeline[nxt]) if nxt is not None else float(total_duration),
            }
        )
    refined = refine_llm_segment_boundaries(
        segments,
        words,
        slide_texts,
        embed_fn,
        window_seconds=window_seconds,
        min_segment_seconds=min_segment_seconds,
        refine_slides=refine_slides,
        anchors=anchors,
    )
    return {int(seg["slide"]): float(seg["start"]) for seg in refined}


def refine_llm_timeline_from_words(
    timeline: dict[int, float],
    anchors: dict[int, float],
    words_raw: list[Word],
    slide_texts: list[str],
    total_duration: float,
    options: SemanticOptions | None = None,
    window_seconds: float = 30.0,
) -> dict[int, float]:
    """Refinamento della timeline LLM ordinata con il modello embedding reale.

    Come ``refine_ordered_llm_timeline`` ma carica il modello fastembed (con
    fallback sul modello alternativo) e delega a essa. Se il modello non è
    disponibile la timeline viene restituita invariata (nessuna interruzione).
    """
    opts = options or SemanticOptions()
    total_slides = len(slide_texts)
    # No-op veloce: senza slide da raffinare (tutte ancorate o timeline troppo
    # corta) la timeline torna invariata senza caricare il modello embedding.
    if len(timeline) < 2 or not {s for s in range(2, total_slides + 1) if s not in anchors}:
        return timeline
    model = _load_embed_model(
        opts.model_name or DEFAULT_EMBEDDING_MODEL,
        opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
        alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
    )
    if model is None:
        return timeline
    embed_fn = _make_embed_fn(model)
    return refine_ordered_llm_timeline(
        timeline,
        anchors,
        words_raw,
        slide_texts,
        total_duration,
        embed_fn,
        window_seconds=window_seconds,
        min_segment_seconds=max(5.0, opts.min_slide_duration),
    )


# =====================================================================
# RIPARAZIONE GUIDATA DALLA VERIFICA DEL VIDEO
# =====================================================================
def _segment_index_at(segments: Sequence[Mapping[str, Any]], time: float) -> int | None:
    """Indice del segmento che contiene ``time``, o None se fuori da tutti."""
    for idx, seg in enumerate(segments):
        if float(seg["start"]) <= time < float(seg["end"]):
            return idx
    return None


def repair_segments_from_frame_mismatches(
    segments: Sequence[Mapping[str, Any]],
    mismatches: Sequence[Mapping[str, Any]],
    words: Sequence[Word],
    slide_texts: Sequence[str],
    total_duration: float,
    embed_fn: EmbedFn,
    min_segment_seconds: float = 8.0,
    context_seconds: float = 12.0,
    max_shift_seconds: float = 180.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sposta i confini che la verifica del video ha trovato sbagliati.

    Ogni mismatch dice DOVE il video mostra una slide diversa da quella
    dichiarata dalla timeline. Se la slide mostrata è quella di un segmento
    ADIACENTE, il confine tra i due è fuori posto e la direzione dell'errore è
    nota: il video mostra la slide successiva → il confine è in ritardo e va
    anticipato (e viceversa). Il nuovo confine non viene indovinato: viene
    cercato dal motore embedding nella SOLA direzione indicata dall'evidenza,
    massimizzando la coerenza tra parlato prima/dopo e le due slide (stessa
    logica del raffinamento dei confini).

    Non cambia la sequenza delle slide mostrate, solo QUANDO ognuna entra in
    scena: la riparazione è quindi sempre applicabile a un video già montato,
    basta rigenerarlo con le nuove durate.

    Un mismatch NON riparabile (slide mostrata non adiacente = probabile
    problema di rendering, o frame poco riconoscibile perché la slide è quella
    dichiarata) viene segnalato e ignorato: meglio nessuna correzione che una
    inventata.

    Returns:
        ``(segmenti, spostamenti)``: i segmenti aggiornati (l'input è intatto)
        e l'elenco degli spostamenti applicati, ognuno con ``slide``, ``shown``,
        ``frame_time``, ``old_start``, ``new_start``. Senza nulla da spostare
        torna la lista originale con spostamenti vuoti (nessun re-render).
    """
    out = [dict(s) for s in segments]
    if len(out) < 2 or not words or not mismatches:
        return [dict(s) for s in segments], []

    ranges: dict[int, tuple[float, float]] = {}
    evidence: dict[int, dict[str, Any]] = {}
    for m in mismatches:
        try:
            time = float(m["time"])
            shown = int(m["shown"])
        except (KeyError, TypeError, ValueError):
            continue
        idx = _segment_index_at(out, time)
        if idx is None:
            continue
        declared = int(out[idx]["slide"])
        if shown == declared:
            log.warning(
                "   [Riparazione] A %.1fs la slide %s è quella dichiarata ma il "
                "frame non è riconoscibile (similarità %.2f): probabile frame "
                "di transizione, nessuno spostamento.",
                time,
                declared,
                float(m.get("similarity") or 0.0),
            )
            continue
        target: int | None = None
        if idx + 1 < len(out) and int(out[idx + 1]["slide"]) == shown:
            target = idx + 1  # confine in ritardo: va anticipato
        elif idx - 1 >= 0 and int(out[idx - 1]["slide"]) == shown:
            target = idx  # confine in anticipo: va posticipato
        if target is None:
            log.warning(
                "   [Riparazione] A %.1fs il video mostra la slide %s nel "
                "segmento della slide %s: non è un confine adiacente spostato "
                "(probabile problema di rendering). Nessuna correzione "
                "automatica: verifica le slide renderizzate.",
                time,
                shown,
                declared,
            )
            continue

        current = float(out[target]["start"])
        if target == idx + 1:
            # La slide `shown` è già a schermo a `time`: il confine deve stare
            # a `time` o prima, mai oltre (l'evidenza è più forte del modello).
            lo = max(
                float(out[idx]["start"]) + min_segment_seconds,
                current - max_shift_seconds,
            )
            hi = min(time, current)
        else:
            # La slide precedente è ancora a schermo a `time`: il confine deve
            # stare a `time` o dopo.
            lo = max(time, current)
            hi = min(float(out[idx]["end"]) - min_segment_seconds, current + max_shift_seconds)
        slide_key = int(out[target]["slide"])
        if slide_key in ranges:
            rlo, rhi = ranges[slide_key]
            ranges[slide_key] = (min(rlo, lo), max(rhi, hi))
        else:
            ranges[slide_key] = (lo, hi)
            evidence[slide_key] = {"shown": shown, "frame_time": round(time, 1)}

    if not ranges:
        return [dict(s) for s in segments], []

    refined = refine_llm_segment_boundaries(
        out,
        list(words),
        slide_texts,
        embed_fn,
        window_seconds=max_shift_seconds,
        min_segment_seconds=min_segment_seconds,
        context_seconds=context_seconds,
        refine_slides=set(ranges),
        search_window=ranges,
    )

    applied: list[dict[str, Any]] = []
    for i, seg in enumerate(refined):
        slide = int(seg["slide"])
        if i >= len(out) or slide not in ranges:
            continue
        old = float(out[i]["start"])
        new = float(seg["start"])
        if abs(new - old) < 0.5:
            continue
        applied.append(
            {
                "slide": slide,
                "shown": evidence.get(slide, {}).get("shown"),
                "frame_time": evidence.get(slide, {}).get("frame_time"),
                "old_start": round(old, 3),
                "new_start": round(new, 3),
            }
        )
    if not applied:
        log.warning(
            "   [Riparazione] Il controllo del video ha segnalato %d segmenti ma "
            "il parlato non offre una posizione migliore per i confini: "
            "timeline invariata.",
            len(mismatches),
        )
        return [dict(s) for s in segments], []

    for rep in applied:
        log.info(
            "   [Riparazione] Slide %s: inizio spostato da %.1fs a %.1fs (%+.1fs).",
            rep["slide"],
            float(rep["old_start"]),
            float(rep["new_start"]),
            float(rep["new_start"]) - float(rep["old_start"]),
        )
    return refined, applied


def repair_segments_from_frames_from_words(
    segments: Sequence[Mapping[str, Any]],
    mismatches: Sequence[Mapping[str, Any]],
    words_raw: list[Word],
    slide_texts: list[str],
    total_duration: float,
    options: SemanticOptions | None = None,
    context_seconds: float = 12.0,
    max_shift_seconds: float = 180.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Come ``repair_segments_from_frame_mismatches`` col modello embedding reale.

    Carica fastembed (con fallback sul modello alternativo) esattamente come
    ``refine_llm_timeline_from_words``. Se il modello non è disponibile i
    segmenti tornano invariati: la riparazione è un'opportunità, non un
    requisito della pipeline (nessuna interruzione).
    """
    opts = options or SemanticOptions()
    if len(segments) < 2 or not words_raw:
        return [dict(s) for s in segments], []
    model = _load_embed_model(
        opts.model_name or DEFAULT_EMBEDDING_MODEL,
        opts.cache_dir or DEFAULT_EMBEDDING_CACHE_DIR,
        alternate_name=opts.alternate_model_name or DEFAULT_EMBEDDING_MODEL_ALTERNATE,
    )
    if model is None:
        log.warning(
            "   [Riparazione] Modello embedding non disponibile: nessuna correzione automatica."
        )
        return [dict(s) for s in segments], []
    return repair_segments_from_frame_mismatches(
        segments,
        mismatches,
        words_raw,
        slide_texts,
        total_duration,
        _make_embed_fn(model),
        min_segment_seconds=max(5.0, opts.min_slide_duration),
        context_seconds=context_seconds,
        max_shift_seconds=max_shift_seconds,
    )
