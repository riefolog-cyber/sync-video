#!/usr/bin/env python3
"""
Estrazione deterministica della timeline dalla trascrizione Whisper.
Supporta entrambi i flussi: 'audio-slide' e 'slide-audio'.

Quando i riferimenti sono parziali o fuori ordine cronologico (anticipazioni,
citazioni a posteriori), la timeline viene completata usando le ancore reali
(sotto-sequenza crescente più lunga) + interpolazione delle slide mancanti.
"""

import re
from typing import cast

from chunks import Word
from config import log

# Parole che introducono un riferimento di slide. Include le varianti di
# trascrizione misheard comuni: "sla" e "asl" per "slide" ("passiamo
# alla sla e due", "passiamo alla asl cinque"), oltre alle deformazioni
# fonetiche "sallay" e "slaib" (whisper-small su pronuncia italiana).
# Le varianti non elencate vengono riconosciute dal fuzzy fonetico in
# ``_is_slide_word``.
_SLIDE_WORDS = frozenset({"slide", "diapositiva", "sla", "asl", "sallay", "slaib"})

# Parole di CHIUSURA/riepilogo finale (es. "e chiudiamo con la slide 14").
# Un "slide N" preceduto da queste parole non è una transizione di inizio
# sezione: è un riferimento a posteriori che, usato come ancora, sposterebbe
# la slide alla fine dell'audio (video che sembra troncarsi). Va scartato.
_CLOSING_MARKERS = frozenset({
    "chiudiamo", "chiudendo", "chiudere", "chiude",
    "concludiamo", "concludendo", "concludere", "conclusione",
})

# Verbi di transizione che, se più vicini alla slide del marcatore di chiusura,
# indicano un passaggio reale e non un ripasso finale (es. "chiudiamo questo
# argomento e passiamo alla slide 5"): in quel caso l'ancora è legittima.
_TRANSITION_VERBS = frozenset({"passiamo", "procediamo", "andiamo", "ora"})


def _is_closing_recap(
    words: list[Word], slide_word_idx: int, lookback: int = 6
) -> bool:
    """True se 'slide N' è preceduto (entro ``lookback`` parole) da un'espressione
    di chiusura finale (es. "e chiudiamo con la slide 14") senza una transizione
    intermedia più vicina. In quel caso il riferimento è un ripasso a posteriori,
    non una transizione di inizio sezione: non deve diventare un'ancora."""
    for w in reversed(words[max(0, slide_word_idx - lookback):slide_word_idx]):
        token = _normalize(w["word"])
        if token in _TRANSITION_VERBS:
            return False
        if token in _CLOSING_MARKERS:
            return True
    return False


# =====================================================================
# ESTRAZIONE DETERMINISTICA TIMELINE
# =====================================================================
def extract_timeline_from_transcript(
    words: list[Word],
    total_slides: int,
    total_duration: float,
    flow: str = "audio-slide",
    window_seconds: float = 2.0,
) -> dict[int, float] | None:
    """
    Estrae la timeline deterministicamente dalla trascrizione Whisper,
    usando i timestamp precisi (al decimo di secondo).

    Args:
        words: lista di parole Whisper con 'word' e 'start'
        total_slides: numero totale di slide
        total_duration: durata audio in secondi
        flow: "audio-slide" (frase "passiamo al blocco successivo")
              oppure "slide-audio" (frase "slide N")
        window_seconds: finestra per raggruppare parole consecutive

    Returns:
        Timeline {slide_num: start_second} oppure None se i segnali sono
        troppo pochi per una sincronizzazione affidabile (fallback LLM).
        Se alcune slide mancano di riferimento esplicito, vengono interpolate
        tra le ancore reali trovate (slide citate fuori ordine escluse via LIS).

    Note:
        In produzione main.py usa direttamente ``extract_slide_anchors``
        (solo le ancore ad alta precisione, lasciando il posizionamento al DP
        semantico). Questa funzione è il motore deterministico completo:
        viene usata dai test come superficie di verifica end-to-end del flusso.
    """
    if not words:
        return None

    if flow == "audio-slide":
        return _extract_audio_slide_flow(words, total_slides, total_duration, window_seconds)
    else:
        return _extract_slide_audio_flow(words, total_slides, total_duration)


def extract_slide_anchors(
    words: list[Word],
    total_slides: int,
    flow: str = "slide-audio",
    window_seconds: float = 2.0,
) -> dict[int, float]:
    """
    Restituisce i riferimenti temporali affidabili (filtrati con LIS, quindi
    senza anticipazioni o citazioni all'indietro) trovati nella trascrizione.

    Usato da main.py come ancore ad alta precisione per la sincronizzazione
    semantica: il DP monotono deve rispettare questi timestamp esatti per le
    slide corrispondenti.
    """
    if not words:
        return {}
    if flow == "audio-slide":
        # Clamp: al massimo total_slides - 1 transizioni (una per slide 2..N).
        # Troppe frasi "blocco successivo" non devono generare ancore oltre il
        # numero reale di slide del PDF (ignorate dal DP, ma confondono i log).
        transitions = _collect_transitions(words, window_seconds)
        return {i + 2: t for i, t in enumerate(transitions[: max(0, total_slides - 1)])}
    mentions = _collect_slide_mentions(words, total_slides)
    refs = {s: times[-1] for s, times in mentions.items()}
    anchors = _lis_anchors(refs)
    if mentions:
        # Recupero delle citazioni a posteriori (recap): se l'ultima menzione
        # di una slide cade fuori ordine (es. "come dicevamo nella slide 3"
        # dopo la slide 4) e la sua PRIMA menzione è invece in ordine, quella
        # prima menzione diventa l'ancora al posto di scartare la slide.
        recovered = _recover_first_in_order(anchors, mentions)
        if recovered:
            log.info(
                "   [Ancore] %d ancora/e recuperata/e dalla prima menzione "
                "in ordine: %s.",
                len(recovered),
                ", ".join(f"slide {s} a {t:.1f}s" for s, t in sorted(recovered.items())),
            )
            anchors = {**anchors, **recovered}
        dropped = sorted(s for s in mentions if s not in anchors)
        if dropped:
            log.warning(
                "   [Ancore] Riferimenti scartati (fuori ordine cronologico, "
                "nessuna menzione in ordine): %s.",
                ", ".join(f"slide {s}" for s in dropped),
            )
        missing = sorted(s for s in range(2, total_slides + 1) if s not in anchors)
        if missing:
            log.info(
                "   [Ancore] %d riferimenti trovati, %d usati come ancore; slide senza riferimento esplicito: %s.",
                len(refs),
                len(anchors),
                ", ".join(str(s) for s in missing) or "nessuna",
            )
    return anchors


def _recover_first_in_order(
    anchors: dict[int, float],
    mentions: dict[int, list[float]],
) -> dict[int, float]:
    """Recupera le ancore scartate (fuori ordine) usando la PRIMA menzione.

    Una citazione a posteriori (es. "come dicevamo nella slide 3" pronunciata
    dopo la slide 4) fa sì che l'ultima menzione (last-wins) cada fuori
    sequenza e il LIS la scarti, perdendo anche la menzione reale in ordine.
    Per ogni slide non ancorata si prova la menzione più antica che cade
    STRETTAMENTE tra le ancore vicine (o dopo l'ultima): se esiste, diventa
    l'ancora. Le ancore recuperate servono da riferimento per le successive.

    Restituisce le sole ancore recuperate ({} se nessuna).
    """
    recovered: dict[int, float] = {}
    current = dict(anchors)
    for s in sorted(mentions):
        if s in current:
            continue
        prev_t = max((t for k, t in current.items() if k < s), default=0.0)
        next_t = min((t for k, t in current.items() if k > s), default=float("inf"))
        for t in mentions[s]:  # già in ordine cronologico crescente
            if prev_t < t < next_t:
                recovered[s] = t
                current[s] = t
                break
    return recovered


# =====================================================================
# FLUSSO audio-slide: "Passiamo al blocco successivo"
# =====================================================================
def _extract_audio_slide_flow(
    words: list[Word],
    total_slides: int,
    total_duration: float,
    window_seconds: float,
) -> dict[int, float] | None:
    """
    Cerca occorrenze di "passiamo"/"procediamo"/"andiamo" seguite da "blocco" e
    "successivo"/"prossimo" entro una finestra temporale. Mappa sequenzialmente:
    1ª occorrenza = slide 2, 2ª = slide 3, ecc.

    Se le frasi-segnale non coprono TUTTE le slide, completa i buchi per
    estrapolazione se le transizioni trovate sono sufficienti; altrimenti None.
    """
    transitions = _collect_transitions(words, window_seconds)

    needed = total_slides - 1  # slide da 2 a N
    if len(transitions) < needed:
        completed = _complete_transitions(transitions, total_slides, total_duration)
        if completed is not None:
            log.warning(
                "   [Deterministico] Transizioni parziali (%d/%d): timeline "
                "completata con interpolazione delle slide mancanti.",
                len(transitions),
                needed,
            )
            return completed
        log.warning(
            "   [Deterministico] Trovate %d frasi 'passiamo al blocco successivo', "
            "ma servono %d per sincronizzare tutte le %d slide. "
            "Sincronizzazione impossibile senza distribuzione uniforme.",
            len(transitions),
            needed,
            total_slides,
        )
        return None

    log.info(
        "   [Deterministico] Trovate %d frasi-segnale: %s",
        len(transitions),
        ", ".join(f"{t:.1f}s" for t in transitions),
    )

    # Mappa sequenziale: 1ª occorrenza = slide 2, 2ª = slide 3, ecc.
    timeline: dict[int, float] = {1: 0.0}
    for i, t in enumerate(transitions):
        timeline[i + 2] = t

    return timeline


def _find_block_transition(
    words: list[Word],
    idx: int,
    window_seconds: float,
) -> float | None:
    """Timestamp della frase "passiamo ... blocco successivo/prossimo" che parte
    dall'indice del trigger, oppure None se non trovata entro la finestra.

    Condivisa tra ``_collect_transitions`` e ``detect_flow_from_words``: la
    scansione (parole successive entro ``window_seconds``, gestione "il blocco")
    è identica nei due casi.
    """
    start_time = words[idx]["start"]
    found_blocco = False
    found_successivo = False
    for j in range(idx + 1, min(idx + 15, len(words))):
        word_time = words[j]["start"]
        if word_time - start_time > window_seconds:
            break
        w_norm = _normalize(words[j]["word"])
        if w_norm == "blocco" or (
            w_norm == "il" and j + 1 < len(words) and _normalize(words[j + 1]["word"]) == "blocco"
        ):
            found_blocco = True
        if w_norm in ("successivo", "prossimo"):
            found_successivo = True
    if found_blocco and found_successivo:
        return start_time
    return None


def _collect_transitions(
    words: list[Word],
    window_seconds: float,
) -> list[float]:
    """Raccoglie i timestamp delle frasi "passiamo/procediamo/andiamo ...
    blocco successivo/prossimo"."""
    # Parole che introducono una transizione di blocco
    triggers = {"passiamo", "procediamo", "andiamo"}
    trigger_indices = [i for i, w in enumerate(words) if _normalize(w["word"]) in triggers]

    transitions: list[float] = []
    for idx in trigger_indices:
        transition_time = _find_block_transition(words, idx, window_seconds)
        if transition_time is not None:
            transitions.append(transition_time)
            log.debug(
                "   [Deterministico] Trovato 'passiamo al blocco successivo' a %.1fs",
                transition_time,
            )
    return transitions


def _complete_transitions(
    transitions: list[float],
    total_slides: int,
    total_duration: float,
) -> dict[int, float] | None:
    """Completa la timeline per il flusso audio-slide interpolando i buchi
    quando le transizioni trovate sono sufficienti, altrimenti None."""
    if not transitions:
        return None
    min_known = max(3, (total_slides + 1) // 2)  # slide 1 + transizioni richieste
    if 1 + len(transitions) < min_known:
        return None
    anchors = {i + 2: t for i, t in enumerate(transitions)}
    return _complete_from_anchors(anchors, total_slides, total_duration)


# =====================================================================
# FLUSSO slide-audio: "Passiamo alla slide N"
# =====================================================================
def _extract_slide_audio_flow(
    words: list[Word],
    total_slides: int,
    total_duration: float,
) -> dict[int, float] | None:
    """
    Cerca riferimenti "slide N" dove N può essere in cifre ("slide 3")
    o in parole italiane ("slide tre"). Gestisce anche il numero prima
    di "slide" ("passiamo ora nove ... la slide").

    Prima applica un post-filtro di monotonicità strettamente crescente.
    Se restano slide mancanti (tipico di riferimenti fuori ordine: anticipazioni
    o citazioni a posteriori), recupera usando la sotto-sequenza crescente più
    lunga (LIS) come ancore reali e interpola le slide senza riferimento.
    Restituisce None solo se i segnali sono troppo pochi (fallback LLM).
    """
    refs = _collect_slide_references(words, total_slides)
    if not refs:
        return None

    timeline: dict[int, float] = {1: 0.0, **refs}

    # --- Post-filtro: monotonicità strettamente crescente ---
    # Scarta i riferimenti che violano l'ordine cronologico, tipici di frasi
    # anticipate ("come vedremo nella slide 8" detto prima della slide 7).
    prev_time = 0.0
    for s in range(2, total_slides + 1):
        if s in timeline:
            if timeline[s] <= prev_time:
                log.warning(
                    "   [Deterministico] Riferimento 'slide %d' a %.1fs ignorato "
                    "(viola l'ordine crescente dopo la slide %d a %.1fs).",
                    s,
                    timeline[s],
                    s - 1,
                    prev_time,
                )
                del timeline[s]
            else:
                prev_time = timeline[s]

    if len(timeline) >= total_slides:
        return timeline

    # --- Riferimenti fuori ordine: ancore LIS + interpolazione ---
    # Un riferimento all'indietro (es. "l'ultima diapositiva... numero due"
    # a fine discorso) può "avvelenare" il filtro monotono. Recupera la
    # sotto-sequenza di riferimenti temporalmente coerente e completa i buchi.
    completed = _complete_from_anchors(refs, total_slides, total_duration)
    if completed is not None:
        log.warning(
            "   [Deterministico] Riferimenti parziali/fuori ordine: timeline "
            "completata con ancore reali + interpolazione delle slide mancanti."
        )
        return completed

    missing = [s for s in range(2, total_slides + 1) if s not in timeline]
    log.warning(
        "   [Deterministico] Riferimenti 'slide N' trovati solo per %d slide su %d "
        "(mancanti: %s). Sincronizzazione impossibile senza distribuzione uniforme.",
        len(timeline),
        total_slides,
        ", ".join(str(s) for s in missing),
    )
    return None


def _collect_slide_references(
    words: list[Word],
    total_slides: int,
    include_slide_one: bool = False,
) -> dict[int, float]:
    """Riferimenti 'slide N' con l'occorrenza PIÙ RECENTE (last-wins).

    Una citazione anticipata del numero totale delle slide (es. "le 13 slide
    di questo documento" a inizio episodio) non deve occupare la slide e far
    scartare la vera ancora "passiamo alla slide 13" pronunciata dopo.
    L'ordinamento cronologico resta filtrato dal chiamante (LIS).

    Nota: il recupero delle citazioni a posteriori (recap) richiede TUTTE le
    menzioni: usare ``_collect_slide_mentions`` + ``_recover_first_in_order``.
    """
    mentions = _collect_slide_mentions(words, total_slides, include_slide_one)
    return {s: times[-1] for s, times in mentions.items()}


def _collect_slide_mentions(
    words: list[Word],
    total_slides: int,
    include_slide_one: bool = False,
) -> dict[int, list[float]]:
    """Raccoglie TUTTE le menzioni 'slide N' / 'N ... slide' trovate,
    incluso quelle fuori ordine cronologico (gestite dal chiamante).

    Per ogni numero di slide conserva la LISTA delle occorrenze temporali in
    ordine cronologico (nessun last-wins): il chiamante decide quale usare
    (l'ultima per l'anticipazione, la prima per il recupero dei recap).
    Le citazioni di chiusura/ripasso finale sono sempre scartate.

    Con ``include_slide_one=True`` raccoglie anche la "slide 1" parlata: serve
    alla verifica LLM del mapping (la numerazione dello speaker può essere
    sfasata, es. "slide 1" mentre mostra la slide 2 del PDF). La slide 1 reale
    resta comunque SEMPRE a 0.0 e non viene mai vincolata come ancora.
    """
    min_slide = 1 if include_slide_one else 2
    refs: dict[int, list[float]] = {}
    for i, w in enumerate(words):
        w_norm = _normalize(w["word"])
        if _is_slide_word(w["word"]):
            # Pattern 1: "slide N" con N nelle 8 parole successive
            # (finestra ampia: gestisce "slide, come potete vedere, la numero tre").
            # `_embedded_slide_number` estrae il numero anche dalle forme FUSE
            # con cardinali italiani ("slaidotto" -> 8, "slaidue" -> 2): il solo
            # `_number_from_word` vede le cifre ("slaib6") ma non i numeri fusi.
            embedded = _embedded_slide_number(w_norm)
            if embedded is not None and min_slide <= embedded <= total_slides:
                if _is_closing_recap(words, i):
                    log.info(
                        "   [Ancore] 'slide %d' a %.1fs: citazione di chiusura, scartata.",
                        embedded,
                        w["start"],
                    )
                    continue
                refs.setdefault(embedded, []).append(_reference_boundary(words, i))
                log.debug(
                    "   [Deterministico] Trovato '%s' con numero incorporato a %.1fs",
                    w["word"],
                    w["start"],
                )
                continue
            for j in range(i + 1, min(i + 8, len(words))):
                slide_num = _number_from_word(_normalize(words[j]["word"]))
                if slide_num is not None:
                    if min_slide <= slide_num <= total_slides:
                        if _is_closing_recap(words, i):
                            log.info(
                                "   [Ancore] 'slide %d' a %.1fs: citazione di chiusura, scartata.",
                                slide_num,
                                w["start"],
                            )
                        else:
                            refs.setdefault(slide_num, []).append(_reference_boundary(words, j))
                            log.debug(
                                "   [Deterministico] Trovato 'slide %d' a %.1fs",
                                slide_num,
                                w["start"],
                            )
                    break
        else:
            # Pattern 2: numero prima di "slide" (es. "ora nove ... la slide").
            # Un numero GIA' parte di una frase "slide N" (preceduto da una
            # parola slide entro la finestra del pattern 1) NON va ri-rilevato
            # qui: altrimenti, con last-wins, il numero della slide precedente
            # scansonerebbe in avanti fino al "slide" della frase successiva e
            # sovrascriverebbe l'ancora con il timestamp sbagliato
            # (es. "slide 2 ... slide 3" -> refs[2] spostato su "slide 3").
            num = _number_from_word(w_norm)
            if (
                num is not None
                and min_slide <= num <= total_slides
                and not _preceded_by_slide_word(words, i)
            ):
                for j in range(i + 1, min(i + 7, len(words))):
                    if _is_slide_word(words[j]["word"]):
                        if _is_closing_recap(words, j):
                            log.info(
                                "   [Ancore] 'slide %d' a %.1fs: citazione di chiusura, scartata.",
                                num,
                                words[j]["start"],
                            )
                        else:
                            refs.setdefault(num, []).append(_reference_boundary(words, j))
                            log.debug(
                                "   [Deterministico] Trovato '%s ... slide' a %.1fs",
                                w["word"],
                                words[j]["start"],
                            )
                        break
    return refs


def _reference_boundary(words: list[Word], last_word_idx: int, max_gap: float = 2.0) -> float:
    """Confine naturale della frase 'slide N': inizio della parola successiva.

    L'ancora viene posta ALLA FINE del riferimento (dopo che lo speaker ha
    finito di pronunciare "slide N"), non all'inizio della parola "slide":
    così il taglio cade su un confine di parola e non spezza la parola ancora
    (verificato con analysis_sync.py: tagli 'META-PAROLA' a metà parola).

    Se dopo il numero non c'è una parola vicina (fine trascrizione o pausa
    lunga), l'ancora resta all'inizio della parola: in quel caso non c'è
    niente da spezzare dopo il taglio.
    """
    start = words[last_word_idx]["start"]
    if last_word_idx + 1 < len(words):
        nxt = words[last_word_idx + 1]["start"]
        if 0.0 <= nxt - start <= max_gap:
            return nxt
    return start


def extract_slide_one_references(
    words: list[Word],
    total_slides: int,
) -> dict[int, float]:
    """Riferimenti parlati alla 'slide 1' (es. "slide 1", "la prima slide").

    La slide 1 reale è SEMPRE a 0.0 e non viene mai vincolata come ancora:
    questi riferimenti servono solo alla verifica LLM del mapping, perché la
    numerazione dello speaker può essere sfasata (es. dice "slide 1" mentre
    mostra la slide 2 del PDF). Restituisce {1: timestamp} o {}.
    """
    if not words:
        return {}
    mentions = _collect_slide_mentions(words, total_slides, include_slide_one=True)
    # Prima menzione: è il momento reale della transizione alla slide 1.
    return {1: mentions[1][0]} if 1 in mentions else {}


def _lis_anchors(refs: dict[int, float]) -> dict[int, float]:
    """Mantiene solo la più lunga sotto-sequenza di riferimenti con slide
    crescenti nel tempo (Longest Increasing Subsequence): scarta anticipazioni
    e citazioni all'indietro che romperebbero l'ordine cronologico."""
    if not refs:
        return {}
    import bisect

    items = sorted(refs.items(), key=lambda kv: (kv[1], kv[0]))
    seq = [s for s, _ in items]
    n = len(seq)
    tails: list[int] = []
    tails_idx: list[int] = []
    prev = [-1] * n
    for i, s in enumerate(seq):
        pos = bisect.bisect_left(tails, s)
        if pos > 0:
            prev[i] = tails_idx[pos - 1]
        if pos == len(tails):
            tails.append(s)
            tails_idx.append(i)
        else:
            tails[pos] = s
            tails_idx[pos] = i
    ordered: list[tuple[int, float]] = []
    if tails_idx:
        k = tails_idx[-1]
        while k != -1:
            ordered.append(items[k])
            k = prev[k]
        ordered.reverse()
    return {s: t for s, t in ordered}


def _complete_from_anchors(
    refs: dict[int, float],
    total_slides: int,
    total_duration: float,
) -> dict[int, float] | None:
    """Costruisce una timeline completa usando le ancore reali (LIS) e
    interpolando linearmente le slide senza riferimento. La Slide 1 è sempre 0.0.
    Restituisce None se le ancore sono troppo poche per una timeline affidabile."""
    anchors = _lis_anchors(refs)
    min_anchors = max(3, (total_slides + 1) // 2)  # slide 1 + ancore richieste
    if len(anchors) + 1 < min_anchors:
        return None

    completed: dict[int, float] = dict(anchors)
    completed[1] = 0.0

    for s in range(2, total_slides + 1):
        if s in completed:
            continue
        lower = [k for k in completed if k < s]
        higher = [k for k in completed if k > s]
        if lower and higher:
            lo, hi = max(lower), min(higher)
            frac = (s - lo) / (hi - lo)
            completed[s] = completed[lo] + frac * (completed[hi] - completed[lo])
        elif higher:
            hi = min(higher)
            completed[s] = max(0.0, completed[hi] - total_duration * 0.02)
        else:
            lo = max(lower)
            step = max(5.0, total_duration * 0.005)
            non1 = sorted(k for k in completed if k != 1)
            if len(non1) >= 2:
                step = max(step, (completed[non1[-1]] - completed[non1[0]]) / max(1, non1[-1] - non1[0]))
            completed[s] = completed[lo] + step
        # Garanzia: strettamente crescente rispetto alla slide precedente
        if completed[s] <= completed[s - 1]:
            completed[s] = completed[s - 1] + max(1.0, total_duration * 0.001)

    # Clamp finale: se l'ultima slide supera la durata, scala tutto
    if completed[total_slides] > total_duration:
        scale = (total_duration - 1.0) / completed[total_slides] if completed[total_slides] > 0 else 1.0
        if 0 < scale < 1.0:
            for s in completed:
                completed[s] *= scale

    try:
        reconcile_timeline(completed, total_slides, total_duration)
    except ValueError:
        return None
    return completed


# =====================================================================
# UTILITY
# =====================================================================
# Numeri cardinali italiani (per "slide tre", "slide undici", ...)
# Nota: chiavi SENZA accenti (la normalizzazione li rimuove)
_ITALIAN_NUMBERS_BASE = {
    "uno": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
    "cinque": 5,
    "sei": 6,
    "sette": 7,
    "otto": 8,
    "nove": 9,
    "dieci": 10,
    "undici": 11,
    "dodici": 12,
    "tredici": 13,
    "quattordici": 14,
    "quindici": 15,
    "sedici": 16,
    "diciassette": 17,
    "diciotto": 18,
    "diciannove": 19,
    "venti": 20,
    "ventuno": 21,
    "ventidue": 22,
    "ventitre": 23,
    "ventiquattro": 24,
    "venticinque": 25,
    "ventisei": 26,
    "ventisette": 27,
    "ventotto": 28,
    "ventinove": 29,
    "trenta": 30,
    # Varianti di trascrizione Whisper comuni (misheard):
    "nonna": 9,  # per "nona slide" trascritto "nonna slide"
}

# Ordinali italiani, forma maschile e femminile ("terza diapositiva").
# La normale elisione dei composti vale anche qui (21° = "ventunesimo").
_ITALIAN_ORDINALS_UNITS = {
    1: ("primo", "prima"),
    2: ("secondo", "seconda"),
    3: ("terzo", "terza"),
    4: ("quarto", "quarta"),
    5: ("quinto", "quinta"),
    6: ("sesto", "sesta"),
    7: ("settimo", "settima"),
    8: ("ottavo", "ottava"),
    9: ("nono", "nona"),
    10: ("decimo", "decima"),
    11: ("undicesimo", "undicesima"),
    12: ("dodicesimo", "dodicesima"),
    13: ("tredicesimo", "tredicesima"),
    14: ("quattordicesimo", "quattordicesima"),
    15: ("quindicesimo", "quindicesima"),
    16: ("sedicesimo", "sedicesima"),
    17: ("diciassettesimo", "diciassettesima"),
    18: ("diciottesimo", "diciottesima"),
    19: ("diciannovesimo", "diciannovesima"),
}

_ITALIAN_ORDINALS_TENS = {
    20: "ventesimo",
    30: "trentesimo",
    40: "quarantesimo",
    50: "cinquantesimo",
    60: "sessantesimo",
    70: "settantesimo",
    80: "ottantesimo",
    90: "novantesimo",
}


def _generate_italian_numbers() -> dict:
    """Genera i numeri cardinali e ordinali italiani da 1 a 99.

    Estende il dizionario base con le combinazioni trentuno..novantanove,
    generando le forme corrette con elisione dell'ultima vocale del decine
    (es. 'trenta' + 'uno' -> 'trentuno'). Gli ordinali sono generati in
    entrambi i generi (es. 'terzo'/'terza' -> 3).
    """
    units = {
        1: "uno",
        2: "due",
        3: "tre",
        4: "quattro",
        5: "cinque",
        6: "sei",
        7: "sette",
        8: "otto",
        9: "nove",
    }
    tens = {
        30: "trenta",
        40: "quaranta",
        50: "cinquanta",
        60: "sessanta",
        70: "settanta",
        80: "ottanta",
        90: "novanta",
    }

    numbers = dict(_ITALIAN_NUMBERS_BASE)
    for t, tens_word in tens.items():
        for u, unit_word in units.items():
            # Regola italiana: elisione dell'ultima vocale del decine
            # solo se l'unità inizia per vocale (uno, otto)
            if unit_word[0] in "aeiou":
                tens_stem = tens_word[:-1]  # rimuovi l'ultima vocale
                numbers[tens_stem + unit_word] = t + u
            else:
                numbers[tens_word + unit_word] = t + u
        numbers[tens_word] = t  # anche la forma piena (es. "quaranta")

    # Ordinali: unità 1..19 dirette, decine piene 20..90, combinazioni 21..99
    for n, (m, f) in _ITALIAN_ORDINALS_UNITS.items():
        numbers[m] = n
        numbers[f] = n
    for d, tens_word in _ITALIAN_ORDINALS_TENS.items():
        numbers[tens_word] = d
        numbers[tens_word[:-1] + "a"] = d  # forma femminile (es. "trentesima")
        # 21° = "ventunesimo": usa la decina cardinale + suffisso ordinale
        tens_card = "venti" if d == 20 else tens[d]
        for u, (um, uf) in {
            1: ("unesimo", "unesima"),
            2: ("duesimo", "duesima"),
            3: ("treesimo", "treesima"),
            4: ("quattresimo", "quattresima"),
            5: ("cinquesimo", "cinquesima"),
            6: ("seiesimo", "seiesima"),
            7: ("settesimo", "settesima"),
            8: ("ottesimo", "ottesima"),
            9: ("novesimo", "novesima"),
        }.items():
            if um[0] in "aeiou":  # elisione: venti + unesimo -> ventunesimo
                tens_stem = tens_card[:-1]
                m_comp = tens_stem + um
                f_comp = tens_stem + uf
            else:
                m_comp = tens_card + um
                f_comp = tens_card + uf
            numbers[m_comp] = d + u
            numbers[f_comp] = d + u

    numbers["cento"] = 100
    numbers["centesimo"] = 100
    numbers["centesima"] = 100
    return numbers


_ITALIAN_NUMBERS = _generate_italian_numbers()


def _number_from_word(word: str) -> int | None:
    """
    Estrae un numero intero da una parola normalizzata:
    cifre ("3", "3,") oppure cardinali italiani ("tre", "undici").
    Restituisce None se non è un numero.
    """
    num_clean = re.sub(r"[^\d]", "", word)
    if num_clean and num_clean.isdigit():
        return int(num_clean)
    return _ITALIAN_NUMBERS.get(word)


def _normalize(word: str) -> str:
    """Normalizza una parola: lowercase, senza accenti, senza punteggiatura."""
    # Rimuovi punteggiatura
    w = re.sub(r"[^\w\s]", "", word.lower().strip())
    # Rimuovi accenti comuni italiani
    replacements = {
        "à": "a",
        "è": "e",
        "é": "e",
        "ì": "i",
        "ò": "o",
        "ù": "u",
    }
    for accented, plain in replacements.items():
        w = w.replace(accented, plain)
    return w


def _preceded_by_slide_word(words: list[Word], idx: int, lookback: int = 8) -> bool:
    """True se una parola slide precede ``words[idx]`` entro ``lookback`` parole.

    La finestra (8 parole) coincide con quella del pattern 1 ("slide N"): se un
    numero è preceduto da una parola slide entro questa distanza appartiene
    già a una frase "slide N" e non deve essere rilevato dal pattern 2
    ("N ... slide"), che guarda solo a numeri PRIMA di una parola slide.
    """
    return any(_is_slide_word(w["word"]) for w in words[max(0, idx - lookback):idx])


def _is_slide_word(word: str) -> bool:
    """
    True se ``word`` è "slide"/"diapositiva" o una variante fonetica ASR.

    Il modello di trascrizione (faster-whisper/OpenVINO) deforma spesso "slide"
    in base alla pronuncia italiana: "sla", "asl", "sallay", "slaib",
    "slayd", "slade", ... Oltre al match esatto in ``_SLIDE_WORDS``,
    riconosce le varianti che iniziano letteralmente per "sl" (la grafia
    della pronuncia all'italiana di "slide" è sempre "sl..."): questo
    esclude le parole comuni tipo "solo"/"salvo"/"sale" che contengono la
    sottosequenza consonantica "sl" ma non iniziano con essa. Il falso
    positivo residuo è mitigato dal chiamante, che richiede sempre un
    numero di slide adiacente (o incorporato, es. "slaib6").

    Riconosce anche le varianti FUSE con numero italiano: "Slaidotto"
    ("slide otto"), "Slaidue", "Slaitre", ... (Whisper fonde numero e
    "slide" nella stessa parola). La digit-check è già coperta da
    ``_embedded_slide_number`` ("slaib6").
    """
    w = _normalize(word)
    if not w:
        return False
    if w in _SLIDE_WORDS:
        return True
    if _embedded_slide_number(w) is not None:
        return True
    if not (3 <= len(w) <= 7):
        return False
    if not w.startswith("sl"):
        return False
    cons = re.sub(r"[aeiou]", "", w)
    cons = re.sub(r"(.)\1+", r"\1", cons)
    return cons.startswith("sl") and len(cons) <= 4


def _embedded_slide_number(w_norm: str) -> int | None:
    """Numero di slide incorporato in una variante fusa, o None.

    Estrae il numero da una parola "sl..." senza separatore:
    - forma digitale: "slaib6" -> 6 (via ``_number_from_word``);
    - forma fusa italiana: "slaidotto" -> 8, "slaitre" -> 3.

    Lo stem "sl..." deve restare di almeno 3 caratteri e il suffisso deve
    essere un numero italiano valido >= 2 (evita falsi positivi tipo "una",
    "sei" dentro parole comuni). Il chiamante verifica comunque che il
    numero sia nel range delle slide del PDF.
    """
    if not w_norm.startswith("sl"):
        return None
    n = _number_from_word(w_norm)
    if n is not None:
        return n
    for stem_len in range(3, len(w_norm)):
        suffix = w_norm[stem_len:]
        val = _ITALIAN_NUMBERS.get(suffix)
        if val is not None and val >= 2 and w_norm[:stem_len].startswith("sl"):
            return cast(int, val)
    return None


# =====================================================================
# AUTO-DETECTION FLUSSO (word-level)
# =====================================================================
def detect_flow_from_words(
    words: list[Word],
    window_seconds: float = 2.0,
) -> str | None:
    """
    Determina il flusso di sincronizzazione analizzando le parole raw:

    - ``"slide-audio"`` se appare "slide N" (N in cifre o in parole italiane,
      es. "slide tre") oppure un numero seguito da "slide"/"diapositiva"
    - ``"audio-slide"`` se appare "passiamo/procediamo/andiamo ... blocco
      successivo/prossimo"
    - ``None`` se nessun pattern chiaro (il chiamante applica il fallback)

    Sostituisce la regex su ``slide\\s*\\d+`` che non riconosceva i numeri
    in parole ("slide tre").
    """
    triggers = {"passiamo", "procediamo", "andiamo"}

    for i, w in enumerate(words):
        w_norm = _normalize(w["word"])

        # slide-audio: "slide N" (cifre, parole o forma fusa "slaidotto")
        if _is_slide_word(w["word"]):
            if _embedded_slide_number(w_norm) is not None:
                return "slide-audio"
            for j in range(i + 1, min(i + 4, len(words))):
                if _number_from_word(_normalize(words[j]["word"])) is not None:
                    return "slide-audio"

        # slide-audio: numero prima di "slide" (es. "nove ... la slide")
        if _number_from_word(w_norm) is not None:
            for j in range(i + 1, min(i + 7, len(words))):
                if _is_slide_word(words[j]["word"]):
                    return "slide-audio"

        # audio-slide: "passiamo ... blocco successivo"
        if w_norm in triggers and _find_block_transition(words, i, window_seconds) is not None:
            return "audio-slide"

    return None


# =====================================================================
# RICONCILIAZIONE TIMELINE (spostata da video.py per disaccoppiamento)
# =====================================================================
def reconcile_timeline(
    timeline_raw: dict[int, float],
    total_slides: int,
    total_duration: float,
) -> list[float]:
    """
    Riconcilia la timeline:
    - clampa i tempi negativi a 0.0
    - esige tempi strettamente crescenti
    - calcola le durate esatte (ultima slide fino alla fine dell'audio)

    Nessun fallback inventato: se la timeline non è valida (tempi non crescenti
    o durate non positive), alza ValueError per interrompere l'esecuzione.

    Returns:
        Lista di durate (una per slide, ordinate).
    """
    starts = [0.0] * (total_slides + 1)  # indice 0 inutilizzato

    for s_num in range(1, total_slides + 1):
        if s_num not in timeline_raw:
            raise ValueError(
                f"Slide {s_num} mancante dalla timeline. Impossibile sincronizzare senza tutti i timestamp."
            )
        starts[s_num] = max(0.0, timeline_raw[s_num])

    # Precisione assoluta: ordine strettamente crescente obbligatorio.
    # Se violato la sincronizzazione è impossibile → interrompi.
    for i in range(2, total_slides + 1):
        if starts[i] <= starts[i - 1]:
            raise ValueError(
                f"Sincronizzazione impossibile: la slide {i} a {starts[i]:.1f}s "
                f"non è strettamente dopo la slide {i - 1} a {starts[i - 1]:.1f}s. "
                "Timeline non valida (niente fallback inventati)."
            )

    # Calcola durate esatte
    durations: list[float] = []
    for i in range(1, total_slides + 1):
        dur = starts[i + 1] - starts[i] if i < total_slides else total_duration - starts[i]
        if dur <= 0:
            raise ValueError(
                f"Sincronizzazione impossibile: durata non positiva per la slide {i} ({dur:.1f}s). Timeline non valida."
            )
        durations.append(dur)
        log.info(
            "   -> Slide %d: da %.1fs a %.1fs (durata: %.1fs)",
            i,
            starts[i],
            starts[i] + dur,
            dur,
        )

    return durations
