#!/usr/bin/env python3
"""Sottotitoli SRT e capitoli dal lavoro che la pipeline ha gia' fatto.

I due file che servono per pubblicare un video non richiedono analisi nuova: la
trascrizione ha il tempo di ogni parola al decimo di secondo, la timeline sa
quando entra ogni slide. Qui c'e' solo la formattazione, e sta fuori da
``video.py`` perche' il video non li usa: chi scrive un .srt non deve
importare moviepy.

Un limite da dichiarare: le parole hanno lo ``start`` ma non la fine (il
formato della trascrizione). La fine di un sottotitolo e' quindi l'inizio
della parola successiva, che e' anche il punto in cui il parlato riprende:
meglio una pausa a schermo che due righe sovrapposte.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from config import atomic_write_text, log

# Una riga da 42 caratteri sta in un sottotitolo a due righe su schermo normale
# senza costringere l'occhio a rincorrerla.
MAX_CARATTERI_CUE = 42
# Oltre ~7s un sottotitolo resta a schermo mentre il parlato e' andato avanti.
MAX_SECONDI_CUE = 7.0
# Coda dell'ultima riga: senza, l'ultimo sottotitolo dura zero e sparisce.
CODA_ULTIMA_PAROLA = 0.8
# Titolo di capitolo: oltre ~60 caratteri YouTube lo tronca da solo, e peggio.
MAX_CARATTERI_CAPITOLO = 60


def _hhmmss(secondi: float, separatore: str) -> str:
    """``HH:MM:SS<sep>mmm`` con i millesimi: il formato che vogliono SRT e VTT."""
    if secondi < 0:
        secondi = 0.0
    interi = int(secondi)
    ore, resto = divmod(interi, 3600)
    minuti, sec = divmod(resto, 60)
    millesimi = round((secondi - interi) * 1000)
    if millesimi >= 1000:  # arrotondamento a 1.000: porta l'arrotondamento avanti
        millesimi -= 1000
        sec += 1
        if sec >= 60:
            sec -= 60
            minuti += 1
            if minuti >= 60:
                minuti -= 60
                ore += 1
    return f"{ore:02d}:{minuti:02d}:{sec:02d}{separatore}{millesimi:03d}"


def timestamp_srt(secondi: float) -> str:
    """Timestamp SRT (virgola prima dei millesimi)."""
    return _hhmmss(secondi, ",")


def timestamp_vtt(secondi: float) -> str:
    """Timestamp WebVTT (punto prima dei millesimi)."""
    return _hhmmss(secondi, ".")


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


def _fine_frase(parola: str) -> bool:
    """True se chiude una frase: la riga va chiusa qui, non a meta' periodo."""
    return parola.endswith((".", "!", "?", ":", ";"))


def costruisci_cue(
    words: Sequence[Mapping[str, Any]],
    *,
    max_caratteri: int = MAX_CARATTERI_CUE,
    max_secondi: float = MAX_SECONDI_CUE,
) -> list[tuple[float, float, str]]:
    """Raggruppa le parole in sottotitoli leggibili: (inizio, fine, testo).

    Le regole sono tre, in ordine di forza: non superare ``max_caratteri``,
    non restare a schermo oltre ``max_secondi``, e chiudere a fine frase
    quando la riga e' gia' abbastanza piena (chiudere ogni punto produrrebbe
    sottotitoli di tre parole).
    """
    cue: list[tuple[float, float, str]] = []
    corrente: list[str] = []
    inizio = 0.0

    def chiudi(fine: float) -> None:
        # Nessun allungamento della fine: il sottotitolo finisce dove comincia
        # la parola successiva, cosi' due righe non si sovrappongono mai a
        # schermo (un allungamento "estetico" le farebbe accavallare).
        nonlocal corrente
        if not corrente:
            return
        # Mai oltre ``max_secondi``: con le parole rade il sottotitolo
        # precedente resterebbe a schermo mentre il parlato e' gia' avanti.
        cue.append((inizio, min(fine, inizio + max_secondi), " ".join(corrente)))
        corrente = []

    for w in words:
        testo = str(w.get("word", "")).strip()
        if not testo or "start" not in w:
            continue  # una parola senza tempo non puo' stare in un sottotitolo
        try:
            t = float(w["start"])
        except (TypeError, ValueError):
            continue
        if not corrente:
            inizio = t
            corrente = [testo]
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

    if corrente:
        # L'ultima riga non ha una parola dopo da cui prendere la fine: le si
        # dà una coda, altrimenti durerebbe zero e sparirebbe subito.
        ultimo = float(words[-1].get("start", inizio)) if words else inizio
        chiudi(max(ultimo + CODA_ULTIMA_PAROLA, inizio + CODA_ULTIMA_PAROLA))
    return cue


def build_srt(words: Sequence[Mapping[str, Any]]) -> str:
    """Contenuto di un file .srt. Stringa vuota se non ci sono parole."""
    righe: list[str] = []
    for i, (inizio, fine, testo) in enumerate(costruisci_cue(words), start=1):
        righe.append(str(i))
        righe.append(f"{timestamp_srt(inizio)} --> {timestamp_srt(fine)}")
        righe.append(testo)
        righe.append("")
    return "\n".join(righe)


def build_vtt(words: Sequence[Mapping[str, Any]]) -> str:
    """Contenuto di un file .vtt (stesse righe, timestamp col punto)."""
    righe: list[str] = ["WEBVTT", ""]
    for inizio, fine, testo in costruisci_cue(words):
        righe.append(f"{timestamp_vtt(inizio)} --> {timestamp_vtt(fine)}")
        righe.append(testo)
        righe.append("")
    return "\n".join(righe)


# Dove finisce il titolo e comincia il resto: le slide hanno spesso
# "Titolo — sottotitolo" oppure "Titolo: spiegazione", e il primo pezzo e' quello
# che identifica la pagina. Si cerca la PRIMA occorrenza di uno qualunque.
_SEPARATORI_TITOLO = (". ", " - ", " \u2014 ", " \u2013 ", " | ", ": ", "; ", "?", "!")


def _titolo_capitolo(testo: str, numero: int) -> str:
    """Titolo breve e leggibile per un capitolo.

    Il testo arriva dall'OCR, che su una slide decorativa restituisce anche
    simboli, lettere isolate e frammenti: un capitolo tipo "LI illusione del
    Risotto - Menù degustazione stellare. -…" e' peggio di "Slide 1". Qui si
    tiene la prima frase, si tolgono le briciole ai due estremi e, se non
    resta testo vero, si ripiega sul numero della slide.
    """
    compatto = " ".join(str(testo or "").split())
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

    if sum(c.isalpha() for c in compatto) < 5:
        return f"Slide {numero}"
    if len(compatto) <= MAX_CARATTERI_CAPITOLO:
        return compatto
    tagliato = compatto[:MAX_CARATTERI_CAPITOLO]
    if " " in tagliato:
        tagliato = tagliato.rsplit(" ", 1)[0]
    return tagliato + "…"


def build_capitoli(timeline: Mapping[int, float], slide_texts: Sequence[str]) -> str:
    """Capitoli in formato YouTube: ``0:00 Titolo``, uno per slide, in ordine.

    Il primo capitolo deve stare a 0:00 (lo pretende YouTube) e la timeline
    mette sempre la slide 1 a zero: se cosi' non fosse, il primo viene
    comunque ancorato a zero invece di produrre un file che il sito rifiuta.
    """
    righe: list[str] = []
    for pos, slide in enumerate(sorted(timeline)):
        inizio = float(timeline[slide])
        if pos == 0:
            inizio = 0.0
        testo = slide_texts[slide - 1] if 0 <= slide - 1 < len(slide_texts) else ""
        righe.append(f"{timestamp_capitolo(inizio)} {_titolo_capitolo(testo, slide)}")
    return "\n".join(righe)


def scrivi_sottotitoli(
    output_path: Path,
    words: Sequence[Mapping[str, Any]],
    timeline: Mapping[int, float],
    slide_texts: Sequence[str],
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
        contenuto = build_srt(words)
        if contenuto:
            srt = base.with_suffix(".srt")
            atomic_write_text(srt, contenuto)
        if timeline:
            capitoli = Path(f"{base}.capitoli.txt")
            atomic_write_text(capitoli, build_capitoli(timeline, slide_texts) + "\n")
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
