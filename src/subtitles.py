#!/usr/bin/env python3
"""Sottotitoli SRT e capitoli dal lavoro che la pipeline ha gia' fatto.

I due file che servono per pubblicare un video non richiedono analisi nuova: la
trascrizione ha il tempo di ogni parola al decimo di secondo, la timeline sa
quando entra ogni slide. Qui c'e' solo la formattazione, e sta fuori da
``video.py`` perche' il video non li usa: chi scrive un .srt non deve
importare moviepy.

La fine di un sottotitolo e' l'inizio della parola successiva, che e' anche
il punto in cui il parlato riprende: meglio una pausa a schermo che due righe
sovrapposte. Quando la trascrizione porta anche la ``end`` della parola la si
usa per MISURARE il silenzio: un silenzio lungo chiude la riga (il testo resta
pero' visibile fino alla parola dopo, non si accorcia al primo istante di
silenzio).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from config import atomic_write_text, log

# Una riga da 42 caratteri sta in un sottotitolo su schermo normale senza
# costringere l'occhio a rincorrerla; due righe sono il massimo leggibile, e
# sono quelle che stanno in un cue.
MAX_CARATTERI_RIGA = 42
MAX_CARATTERI_CUE = MAX_CARATTERI_RIGA * 2
# Un silenzio piu' lungo di cosi' chiude la riga: e' la punteggiatura che la
# trascrizione non ha.
PAUSA_TAGLIO = 0.4
# Durata da assumere per una parola quando la trascrizione non dice la fine.
DURATA_PAROLA_STIMATA = 0.30
# Oltre ~7s un sottotitolo resta a schermo mentre il parlato e' andato avanti.
MAX_SECONDI_CUE = 7.0
# Velocita' di lettura e durata minima: oltre ~20 caratteri al secondo il
# sottotitolo si legge solo fermando il video, e sotto il secondo lampeggia.
CPS_MAX = 20.0
MIN_SECONDI_CUE = 1.0
# Coda dell'ultima riga: senza, l'ultimo sottotitolo dura zero e sparisce.
CODA_ULTIMA_PAROLA = 0.8
# Quanto una riga deve sembrare testo per diventare un titolo di capitolo:
# l'OCR delle slide infografiche restituisce anche righe di soli trattini.
QUALITA_MINIMA_TITOLO = 0.6
# Titolo di capitolo: oltre ~60 caratteri YouTube lo tronca da solo, e peggio.
MAX_CARATTERI_CAPITOLO = 60
# YouTube scarta i capitoli piu' corti di 10s: qui sotto si fondono con i
# consecutivi invece di scrivere un file che il sito ignorerebbe a meta'.
MIN_SECONDI_CAPITOLO = 10.0


def timestamp_srt(secondi: float) -> str:
    """``HH:MM:SS,mmm``: l'unico formato che serve (SRT).

    Si arrotonda ai millisecondi UNA volta sola e poi si divide: contare i
    millesimi a parte costringeva a gestire a mano il riporto a 1.000, e un
    ``59.9996`` diventava ``00:00:59,1000``.
    """
    ms = max(0, round(secondi * 1000))
    ore, resto = divmod(ms, 3_600_000)
    minuti, resto = divmod(resto, 60_000)
    sec, millesimi = divmod(resto, 1000)
    return f"{ore:02d}:{minuti:02d}:{sec:02d},{millesimi:03d}"


def timestamp_capitolo(secondi: float) -> str:
    """Timestamp da capitolo: ``0:00`` oppure ``1:02:03``.

    YouTube accetta anche le ore, ma solo quando servono: ``0:04:12`` funziona,
    ``00:04:12`` e' rumore per l'occhio.
    """
    interi = max(0, int(secondi))
    ore, resto = divmod(interi, 3600)
    minuti, sec = divmod(resto, 60)
    if ore:
        return f"{ore}:{minuti:02d}:{sec:02d}"
    return f"{minuti}:{sec:02d}"


def _fine_dichiarata(w: Mapping[str, Any]) -> float:
    """La ``end`` della parola se la trascrizione la porta, altrimenti 0."""
    try:
        return max(0.0, float(w["end"]))
    except (KeyError, TypeError, ValueError):
        return 0.0


def _fine_parola(w: Mapping[str, Any], inizio: float) -> float:
    """Fine della parola: la ``end`` dichiarata, o una stima ragionevole."""
    fine = _fine_dichiarata(w)
    return fine if fine > inizio else inizio + DURATA_PAROLA_STIMATA


def _due_righe(testo: str, max_riga: int) -> str:
    """Testo su al massimo due righe bilanciate (nel SRT separate da ``\\n``).

    Si cerca il taglio che lascia le due righe piu' vicine in lunghezza: un
    taglio a meta' esatto del testo puo' lasciare una riga di tre parole e una
    di otto, che si legge peggio di due righe pari.
    """
    if len(testo) <= max_riga:
        return testo
    parole = testo.split()
    migliore: tuple[int, int] | None = None
    for i in range(1, len(parole)):
        sinistra = " ".join(parole[:i])
        destra = " ".join(parole[i:])
        if len(sinistra) > max_riga or len(destra) > max_riga:
            continue
        sbilancio = abs(len(sinistra) - len(destra))
        if migliore is None or sbilancio < migliore[0]:
            migliore = (sbilancio, i)
    if migliore is None:
        # Nessun taglio sta nelle due righe (parola lunghissima): una riga
        # sola lunga si legge meglio di uno spezzamento inventato.
        return testo
    i = migliore[1]
    return " ".join(parole[:i]) + "\n" + " ".join(parole[i:])


def _fine_frase(parola: str) -> bool:
    """True se chiude una frase: la riga va chiusa qui, non a meta' periodo."""
    return parola.endswith((".", "!", "?", ":", ";"))


def costruisci_cue(
    words: Sequence[Mapping[str, Any]],
    *,
    max_caratteri: int = MAX_CARATTERI_CUE,
    max_secondi: float = MAX_SECONDI_CUE,
    pausa: float = PAUSA_TAGLIO,
) -> list[tuple[float, float, str]]:
    """Raggruppa le parole in sottotitoli leggibili: (inizio, fine, testo).

    Le regole sono quattro, in ordine di forza: non superare ``max_caratteri``,
    non restare a schermo oltre ``max_secondi``, chiudere dove c'e' un
    silenzio lungo ``pausa``, e chiudere a fine frase quando la riga e' gia'
    abbastanza piena (chiudere ogni punto produrrebbe sottotitoli di tre
    parole).
    """
    cue: list[tuple[float, float, str]] = []
    corrente: list[str] = []
    inizio = 0.0
    ultimo = 0.0  # inizio dell'ultima parola accettata
    fine_precedente = 0.0  # fine (stimata) dell'ultima parola accettata
    coda = 0.0  # fine dichiarata dell'ultima parola: la coda non deve tagliarla

    def chiudi(fine: float) -> None:
        # Nessun allungamento della fine: il sottotitolo finisce dove comincia
        # la parola successiva, cosi' due righe non si sovrappongono mai a
        # schermo (un allungamento "estetico" le farebbe accavallare).
        nonlocal corrente
        if not corrente:
            return
        # Mai oltre ``max_secondi``: con le parole rade il sottotitolo
        # precedente resterebbe a schermo mentre il parlato e' gia' avanti.
        testo = _due_righe(" ".join(corrente), max(1, max_caratteri // 2))
        cue.append((inizio, min(fine, inizio + max_secondi), testo))
        corrente = []

    for w in words:
        testo = str(w.get("word", "")).strip()
        if not testo or "start" not in w:
            continue  # una parola senza tempo non puo' stare in un sottotitolo
        try:
            t = float(w["start"])
        except (TypeError, ValueError):
            continue
        ultimo = t
        coda = _fine_dichiarata(w)  # solo l'ultima conta: le altre sono gia' chiuse
        fine_parola = _fine_parola(w, t)
        silenzio_lungo = corrente and (t - fine_precedente) >= pausa
        if not corrente or silenzio_lungo:
            if corrente:
                chiudi(t)
            inizio = t
            corrente = [testo]
            fine_precedente = fine_parola
            continue
        proposta = " ".join([*corrente, testo])
        troppo_lunga = len(proposta) > max_caratteri
        troppo_lenta = (t - inizio) > max_secondi
        frase_piena = _fine_frase(corrente[-1]) and len(" ".join(corrente)) >= max_caratteri // 2
        if troppo_lunga or troppo_lenta or frase_piena:
            chiudi(t)
            inizio = t
            corrente = [testo]
        else:
            corrente.append(testo)
        fine_precedente = fine_parola

    if corrente:
        # L'ultima riga non ha una parola dopo da cui prendere la fine: le si
        # dà una coda, altrimenti durerebbe zero e sparirebbe subito.
        chiudi(max(ultimo + CODA_ULTIMA_PAROLA, coda))
    return cue


def _testo_piatto(testo: str) -> str:
    """Testo su una riga sola: per misurare la lettura il \\n non conta."""
    return " ".join(testo.split())


def _adegua_lettura(
    cue: Sequence[tuple[float, float, str]],
    *,
    cps_max: float = CPS_MAX,
    min_secondi: float = MIN_SECONDI_CUE,
    max_caratteri: int = MAX_CARATTERI_CUE,
    max_secondi: float = MAX_SECONDI_CUE,
) -> list[tuple[float, float, str]]:
    """Unisce i cue che restano a schermo troppo poco per essere letti.

    La densita' (caratteri al secondo) non si puo' correggere qui: dipende da
    quanto parla il relatore, e spezzare un cue in proporzione lascia il
    rapporto identico (meta' testo per meta' tempo). Quello che si puo' fare e'
    togliere i lampi: un cue sotto il secondo si unisce al successivo, ma solo
    se il testo unito resta in un cue e non supera ``cps_max``.
    """
    uniti: list[tuple[float, float, str]] = []
    for inizio, fine, testo in cue:
        if uniti:
            p_inizio, p_fine, p_testo = uniti[-1]
            unito = _testo_piatto(p_testo) + " " + _testo_piatto(testo)
            durata = fine - p_inizio
            if (
                p_fine - p_inizio < min_secondi
                and len(unito) <= max_caratteri
                and durata <= max_secondi
                and len(unito) / max(durata, 1e-9) <= cps_max
            ):
                uniti[-1] = (p_inizio, fine, _due_righe(unito, max(1, max_caratteri // 2)))
                continue
        uniti.append((inizio, fine, testo))
    return uniti


def build_srt(
    words: Sequence[Mapping[str, Any]], durata: float | None = None
) -> str:
    """Contenuto di un file .srt. Stringa vuota se non ci sono parole.

    ``durata`` e' quella dell'audio: nessun sottotitolo deve restare a schermo
    dopo la fine del video.
    """
    cue = _adegua_lettura(costruisci_cue(words))
    righe: list[str] = []
    numero = 0
    for inizio, fine, testo in cue:
        if durata is not None:
            if inizio >= durata:
                # Il clamp porterebbe `fine` sotto `inizio`: un cue con i tempi
                # invertiti viene rifiutato dal player (o peggio, letto al
                # contrario). Si scarta e la numerazione resta consecutiva.
                log.debug("   Cue oltre la fine dell'audio scartato (%.1fs).", inizio)
                continue
            fine = min(fine, durata)
        if fine <= inizio:
            # Difesa finale: anche senza `durata` un tempo invertito non e' un
            # sottotitolo valido.
            log.debug("   Cue con tempi invertiti scartato (%.1f-%.1fs).", inizio, fine)
            continue
        numero += 1
        righe.append(str(numero))
        righe.append(f"{timestamp_srt(inizio)} --> {timestamp_srt(fine)}")
        righe.append(testo)
        righe.append("")
    return "\n".join(righe)


# Dove finisce il titolo e comincia il resto: le slide hanno spesso
# "Titolo — sottotitolo" oppure "Titolo: spiegazione", e il primo pezzo e' quello
# che identifica la pagina. Si cerca la PRIMA occorrenza di uno qualunque.
_SEPARATORI_TITOLO = (". ", " - ", " \u2014 ", " \u2013 ", " | ", ": ", "; ", "?", "!")


def _qualita_titolo(riga: str) -> float:
    """Quanto una riga dell'OCR sembra testo vero, da 0 a 1.

    L'OCR di una slide infografica restituisce anche le righe del grafico
    (``+ ———-__ 87% 7 >90%``). Il rapporto fra lettere e simboli le distingue,
    e un gruppo di tre o piu' trattini e' quasi sempre un asse o una tabella.
    """
    visibili = [c for c in riga if not c.isspace()]
    if not visibili:
        return 0.0
    lettere = sum(c.isalpha() for c in visibili)
    cifre = sum(c.isdigit() for c in visibili)
    punteggio = (lettere + 0.6 * cifre) / len(visibili)
    if re.search(r"[^\w\s]{3,}", riga):
        punteggio *= 0.5
    parole = riga.split()
    if parole:
        isolate = sum(len(p) == 1 for p in parole) / len(parole)
        punteggio *= 1.0 - 0.6 * isolate
    return punteggio


def _miglior_riga(testo: str) -> str:
    """La riga della slide che somiglia di piu' a un titolo.

    Il titolo non e' sempre la prima riga: su una slide con un grafico in cima
    lo e' quella sotto. Se l'OCR ha restituito tutto su una riga, si prova
    anche a spezzarla sui separatori tipici dei titoli.
    """
    righe = [r.strip() for r in testo.splitlines() if r.strip()]
    if len(righe) <= 1:
        unico = righe[0] if righe else ""
        parti = [p.strip() for p in re.split(r" [\u2014\u2013|] ", unico) if p.strip()]
        righe = parti or [unico]
    return max(righe, key=_qualita_titolo)


def _titolo_capitolo(testo: str, numero: int) -> str:
    """Titolo breve e leggibile per un capitolo.

    Il testo arriva dall'OCR, che su una slide decorativa restituisce anche
    simboli, lettere isolate e frammenti: un capitolo tipo "LI illusione del
    Risotto - Menù degustazione stellare. -…" e' peggio di "Slide 1". Qui si
    tiene la prima frase, si tolgono le briciole ai due estremi e, se non
    resta testo vero, si ripiega sul numero della slide.
    """
    compatto = " ".join(_miglior_riga(str(testo or "")).split())
    if not compatto:
        return f"Slide {numero}"

    # Prima frase/segmento (solo dopo i primi caratteri, altrimenti un titolo
    # che comincia con un punto verrebbe buttato via).
    tagli = [
        pos + (1 if sep[0] in ".?!" else 0)
        for sep in _SEPARATORI_TITOLO
        if (pos := compatto.find(sep, 6)) != -1
    ]
    if tagli:
        compatto = compatto[: min(tagli)]

    compatto = compatto.lstrip(" -\u2014\u2013|/\\\"'\u201c\u201d\u2018\u2019()[]{}")
    parole = compatto.split()
    # Briciole in testa: "LI" e' un apostrofo letto male, ma "Il" e' un
    # articolo. Il discriminante e' la minuscola, non la lunghezza.
    while parole and len(parole[0]) <= 3 and parole[0].isupper():
        parole.pop(0)
    # Briciole in coda: simboli e lettere isolate. I numeri restano: "10%" o
    # "Art. 4" fanno parte del titolo, e buttarli via lo rende inutile.
    while parole and (
        not any(c.isalnum() for c in parole[-1])
        or (len(parole[-1]) == 1 and parole[-1].isalpha())
    ):
        parole.pop()
    compatto = " ".join(parole).strip(" -\u2014\u2013|:;,")

    if sum(c.isalpha() for c in compatto) < 5 or _qualita_titolo(compatto) < QUALITA_MINIMA_TITOLO:
        return f"Slide {numero}"
    if len(compatto) <= MAX_CARATTERI_CAPITOLO:
        return compatto
    tagliato = compatto[:MAX_CARATTERI_CAPITOLO]
    if " " in tagliato:
        tagliato = tagliato.rsplit(" ", 1)[0]
    return tagliato + "…"


def _titolo_unito(titoli: Sequence[str]) -> str:
    """Titoli di capitoli fusi in uno, entro il limite di YouTube."""
    titolo = " / ".join(t for t in titoli if t)
    if len(titolo) <= MAX_CARATTERI_CAPITOLO:
        return titolo
    tagliato = titolo[:MAX_CARATTERI_CAPITOLO]
    if " " in tagliato:
        tagliato = tagliato.rsplit(" ", 1)[0]
    return tagliato.rstrip(" /") + "…"


def build_capitoli(
    timeline: Mapping[int, float],
    slide_texts: Sequence[str],
    numeri_slide: Sequence[int] | None = None,
) -> str:
    """Capitoli in formato YouTube: ``0:00 Titolo``, in ordine.

    Il primo capitolo deve stare a 0:00 (lo pretende YouTube) e la timeline
    mette sempre la slide 1 a zero: se cosi' non fosse, il primo viene
    comunque ancorato a zero invece di produrre un file che il sito rifiuta.

    **Vincolo YouTube**: un capitolo dura almeno 10 secondi (e ne servono almeno
    3 perche' la lista compaia). Una slide che resta 8s NON diventerebbe un
    capitolo: verrebbe scartata insieme al suo titolo. I capitoli consecutivi
    piu' corti di ``MIN_SECONDI_CAPITOLO`` si fondono quindi in uno solo, che
    parte dal primo e tiene i titoli di tutti. L'ultimo capitolo non ha una
    fine nota (dipende dalla durata del video) e resta cosi' com'e'.

    ``numeri_slide`` serve quando la sequenza mostrata non e' 1..N (per esempio
    dopo ``--skip-slides``): le chiavi della timeline sono le POSIZIONI, quindi
    il fallback "Slide N" deve usare il numero vero della slide li' mostrata.
    """
    capitoli: list[tuple[float, str]] = []
    for pos, slide in enumerate(sorted(timeline)):
        inizio = float(timeline[slide])
        if pos == 0:
            inizio = 0.0
        testo = slide_texts[slide - 1] if 0 <= slide - 1 < len(slide_texts) else ""
        numero = int(numeri_slide[pos]) if numeri_slide is not None and pos < len(numeri_slide) else slide
        capitoli.append((inizio, _titolo_capitolo(testo, numero)))

    # Unisce i gruppi di capitoli troppo corti: si chiude un gruppo solo quando
    # il capitolo corrente ha raggiunto il minimo (o e' l'ultimo, la cui fine
    # non si conosce). Tenere l'inizio del primo del gruppo e' fondamentale:
    # il primo capitolo di YouTube deve restare ancorato a 0:00.
    gruppi: list[list[tuple[float, str]]] = []
    gruppo_corrente: list[tuple[float, str]] = []
    fusi = 0
    for i, capitolo in enumerate(capitoli):
        gruppo_corrente.append(capitolo)
        # Ultimo capitolo: la fine e' la durata del video (durata sconosciuta).
        durata = capitoli[i + 1][0] - capitolo[0] if i + 1 < len(capitoli) else None
        if durata is None or durata >= MIN_SECONDI_CAPITOLO:
            if len(gruppo_corrente) > 1:
                fusi += len(gruppo_corrente) - 1
            gruppi.append(gruppo_corrente)
            gruppo_corrente = []

    if fusi:
        log.info("   Capitoli piu' corti di 10s assorbiti dai successivi (%d).", fusi)
    return "\n".join(
        f"{timestamp_capitolo(gruppo[0][0])} {_titolo_unito([t for _, t in gruppo])}"
        for gruppo in gruppi
    )


def scrivi_sottotitoli(
    output_path: Path,
    words: Sequence[Mapping[str, Any]],
    timeline: Mapping[int, float],
    slide_texts: Sequence[str],
    durata: float | None = None,
    numeri_slide: Sequence[int] | None = None,
) -> tuple[Path | None, Path | None]:
    """Scrive ``<video>.srt`` e ``<video>.capitoli.txt`` accanto al video.

    Restituisce i percorsi scritti (None se non c'era niente da scrivere).
    Un errore di scrittura non ferma la run: il video e' il prodotto, questi
    sono un contorno, e perdere il contorno non giustifica perdere il video.
    """
    base = output_path.with_suffix("")
    srt: Path | None = None
    capitoli: Path | None = None
    try:
        contenuto = build_srt(words, durata=durata)
        if contenuto:
            srt = base.with_suffix(".srt")
            atomic_write_text(srt, contenuto)
        if timeline:
            capitoli = Path(f"{base}.capitoli.txt")
            atomic_write_text(
                capitoli, build_capitoli(timeline, slide_texts, numeri_slide) + "\n"
            )
    except OSError as e:
        log.warning("   Sottotitoli/capitoli non scritti (%s).", e)
        return srt if srt and srt.exists() else None, capitoli if capitoli and capitoli.exists() else None
    if srt:
        log.info("   Sottotitoli: %s", srt.name)
    if capitoli:
        log.info("   Capitoli: %s", capitoli.name)
    if not srt and not capitoli:
        log.debug("   Nessun sottotitolo da scrivere (trascrizione o timeline vuote).")
    return srt, capitoli
