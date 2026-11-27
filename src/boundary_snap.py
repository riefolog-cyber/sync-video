#!/usr/bin/env python3
"""Aggancia i confini stimati a una pausa, o almeno a un confine di parola.

Il motore mette il confine dove la similarita' si inverte, e il pavimento
anti-flicker lo sposta di quanto serve per la leggibilita': due operazioni che
non guardano le parole. Il taglio che ne esce puo' cadere a meta' parola, e
quello si sente subito (``analysis_sync.py`` lo conta come META-PAROLA).

Qui si sposta **solo** il confine di una slide non ancorata, di poco
(``tolleranza``), e solo verso un punto migliore:

- una pausa vera (silenzio fra due parole >= ``GAP_PAUSA``), se c'e' entro la
  tolleranza: e' il punto in cui il relatore respira, il posto naturale;
- altrimenti un confine di parola, ma solo se il confine attuale cade DENTRO
  una parola: li' il taglio e' un difetto conclamato, e qualsiasi estremo di
  parola e' meglio.

I confini annunciati a voce non si toccano: sono misure dello speaker, non
stime. E nessuno spostamento puo' violare il minimo di durata delle slide
vicine: se lo farebbe, si salta (meglio un taglio a meta' parola che una slide
illeggibile).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from typing import Any

# Silenzio (in secondi) perche' fra due parole si possa parlare di pausa.
GAP_PAUSA = 0.25
# Quanto ci si puo' allontanare dal confine calcolato: oltre, non e' piu' lo
# stesso confine ma un altro posto.
TOLLERANZA = 1.5
# Le parole hanno solo lo ``start``; per l'ultima (o per una parola isolata) la
# fine si stima con una durata media italiana.
DURATA_PAROLA_DEFAULT = 0.30


def _starts(words: Sequence[Mapping[str, Any]]) -> list[float]:
    """Tempi di inizio ordinati, scartando le voci illeggibili.

    ``NaN`` e ``inf`` sono valori che ``float()`` accetta ma che rompono
    l'ordinamento e ``bisect``: un solo ``NaN`` rendeva il vettore non
    ordinato, e la ricerca del confine poteva saltare parole vere. Un tempo
    non finito non e' una parola databile, quindi si scarta come le stringhe
    non numeriche.
    """
    tempi: list[float] = []
    for w in words:
        if "start" not in w:
            continue  # senza tempo non e' una parola databile
        try:
            inizio = float(w["start"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(inizio):
            continue
        tempi.append(inizio)
    tempi.sort()
    return tempi


def starts_parole(words: Sequence[Mapping[str, Any]]) -> list[float]:
    """`_starts` pubblico: chi chiama piu' funzioni qui le calcola una volta.

    Il vettore e' ordinato: e' la premessa per le ricerche con ``bisect`` qui
    sotto. Chi lo passa alle funzioni di questo modulo deve usare QUESTO (o
    un vettore equivalente), non la lista grezza delle parole.
    """
    return _starts(words)


def confini_disponibili(
    words: Sequence[Mapping[str, Any]],
    starts: Sequence[float] | None = None,
) -> tuple[list[float], list[float]]:
    """(pause, estremi di parola) fra cui un confine puo' essere spostato.

    ``starts`` (opzionale): vettore ordinato degli inizi di parola, calcolato
    una volta dal chiamante con ``starts_parole``. Senza, si ricalcola qui.

    Le pause stanno poco PRIMA della parola successiva, dentro il silenzio:
    la slide nuova entra quando il relatore riattacca, non a meta' della
    pausa (anticiparla di secondi e' peggio che arrivare al limite del
    silenzio). Gli estremi di parola sono gli inizi, piu' la fine stimata
    dell'ultima.
    """
    if starts is None:
        starts = _starts(words)
    if not starts:
        return [], []
    pause: list[float] = []
    estremi = list(starts)
    for i in range(1, len(starts)):
        # Una parola "occupa" DURATA_PAROLA_DEFAULT: quello che avanza fino
        # alla successiva e' il silenzio vero (le due definizioni di fine
        # parola in questo modulo sono questa sola).
        silenzio = starts[i] - (starts[i - 1] + DURATA_PAROLA_DEFAULT)
        if silenzio >= GAP_PAUSA:
            pause.append(starts[i] - DURATA_PAROLA_DEFAULT / 2.0)
    estremi.append(starts[-1] + DURATA_PAROLA_DEFAULT)
    return pause, estremi


def _dentro_una_parola(t: float, starts: Sequence[float]) -> bool:
    """True se ``t`` cade dentro lo spazio (stimato) di una parola.

    Lo spazio e' ``start + DURATA_PAROLA_DEFAULT``, la stessa fine stimata che
    usa ``confini_disponibili``: due definizioni diverse nella stessa funzione
    facevano divergere il calcolo del silenzio e quello del taglio.

    ``starts`` e' ordinato: si salta direttamente al primo inizio che puo'
    contenere ``t`` con ``bisect`` invece di scandire tutte le parole. Il
    confronto dentro il ciclo resta letteralmente quello di prima, cosi' i
    casi limite in virgola mobile danno lo stesso risultato.
    """
    i = bisect.bisect_right(starts, t - DURATA_PAROLA_DEFAULT)
    while i < len(starts) and starts[i] + 1e-6 < t:
        if t < starts[i] + DURATA_PAROLA_DEFAULT - 1e-6:
            return True
        i += 1
    return False


def confini_dentro_parole(
    timeline: Mapping[int, float],
    words: Sequence[Mapping[str, Any]],
    starts: Sequence[float] | None = None,
) -> list[int]:
    """Slide il cui confine cade DENTRO una parola: verrebbe troncata a meta'.

    E' la misura della leggibilita' prima e dopo lo snap, non una regola: nel
    referto dice se l'aggancio su questo materiale ha servito a qualcosa.

    ``starts`` opzionale: lo stesso vettore ordinato passato ad
    ``aggancia_confini``, per non ricalcolarlo a ogni misurazione.
    """
    if starts is None:
        starts = _starts(words)
    if not starts:
        return []
    ordinate = sorted(timeline)
    return [s for s in ordinate[1:] if _dentro_una_parola(float(timeline[s]), starts)]


def _piu_vicino(valore: float, candidati: Sequence[float], tolleranza: float) -> float | None:
    """Candidato entro ``tolleranza`` piu' vicino a ``valore``.

    ``candidati`` e' ordinato (pause e estremi lo sono per costruzione): con
    ``bisect`` bastano i due valori che incorniciano ``valore``, non serve
    scandire tutta la lista. In caso di pari distanza vince il piu' piccolo,
    esattamente come nella scansione originaria che incontrava prima lui.
    """
    if not candidati:
        return None
    i = bisect.bisect_left(candidati, valore)
    migliore: float | None = None
    for j in (i - 1, i):
        if (
            0 <= j < len(candidati)
            and abs(candidati[j] - valore) <= tolleranza
            and (migliore is None or abs(candidati[j] - valore) < abs(migliore - valore))
        ):
            migliore = candidati[j]
    return migliore


def aggancia_confini(
    timeline: Mapping[int, float],
    anchors: Mapping[int, float] | None,
    words: Sequence[Mapping[str, Any]],
    *,
    tolleranza: float = TOLLERANZA,
    min_secondi: float,  # obbligatorio: il default vive in --semantic-min-duration
    starts: Sequence[float] | None = None,
    total_duration: float | None = None,
) -> tuple[dict[int, float], list[tuple[int, float, float, str]]]:
    """Restituisce (timeline agganciata, mosse) con ``mosse`` = (slide, prima, dopo, tipo).

    ``tipo`` e' ``"pausa"`` o ``"parola"``: serve al log e al report perche' sono
    due qualita' diverse di correzione (una pausa e' una scelta, un estremo di
    parola e' un rattoppo). ``starts`` e' il vettore ordinato opzionale di
    ``starts_parole``: passandolo, chi misura prima e dopo non paga due
    ricostruzioni del vettore ne' il ricalcolo interno di
    ``confini_disponibili``.

    ``total_duration`` e' la durata dell'audio: senza, l'ULTIMA slide non ha
    un vicino dopo e può essere accorciata sotto ``min_secondi`` (il controllo
    guarda solo il successivo), trasformandola in un lampo finale. Quando e'
    noto, la fine dell'audio fa da successivo.
    """
    nuove = {int(s): float(t) for s, t in timeline.items()}
    if starts is None:
        starts = _starts(words)
    if not starts or len(nuove) < 2:
        return nuove, []
    # Lo stesso vettore serve a pause, estremi e prova "dentro parola": calcolarlo
    # una volta sola evita tre ordinamenti delle stesse parole.
    pause, estremi = confini_disponibili(words, starts)
    ancore = anchors or {}
    ordinate = sorted(nuove)
    mosse: list[tuple[int, float, float, str]] = []

    for i, slide in enumerate(ordinate):
        if slide == ordinate[0] or slide in ancore:
            continue
        prima = nuove[slide]
        nuova = _piu_vicino(prima, pause, tolleranza)
        tipo = "pausa"
        if nuova is None and _dentro_una_parola(prima, starts):
            nuova = _piu_vicino(prima, estremi, tolleranza)
            tipo = "parola"
        if nuova is None or abs(nuova - prima) < 1e-3:
            continue
        # Il minimo di durata delle due slide vicine vale anche qui: uno
        # spostamento che rende illeggibile una slide e' peggio del taglio.
        precedente = nuove[ordinate[i - 1]]
        successiva = nuove[ordinate[i + 1]] if i + 1 < len(ordinate) else None
        if nuova - precedente < min_secondi - 1e-9:
            continue
        if successiva is not None and successiva - nuova < min_secondi - 1e-9:
            continue
        if (
            successiva is None
            and total_duration is not None
            and total_duration - nuova < min_secondi - 1e-9
        ):
            continue
        nuove[slide] = nuova
        mosse.append((slide, prima, nuova, tipo))
    return nuove, mosse
