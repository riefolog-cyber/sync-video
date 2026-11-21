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
    """Tempi di inizio ordinati, scartando le voci illeggibili."""
    tempi: list[float] = []
    for w in words:
        try:
            tempi.append(float(w.get("start", 0.0)))
        except (TypeError, ValueError):
            continue
    tempi.sort()
    return tempi


def confini_disponibili(
    words: Sequence[Mapping[str, Any]],
) -> tuple[list[float], list[float]]:
    """(pause, estremi di parola) fra cui un confine puo' essere spostato.

    Le pause sono il CENTRO del silenzio: il punto piu' lontano da entrambe le
    parole, cioe' quello in cui un taglio non tronca niente. Gli estremi di
    parola sono gli inizi, piu' la fine stimata dell'ultima.
    """
    starts = _starts(words)
    if not starts:
        return [], []
    pause: list[float] = []
    estremi = list(starts)
    for i in range(1, len(starts)):
        if starts[i] - starts[i - 1] >= GAP_PAUSA + DURATA_PAROLA_DEFAULT:
            fine_precedente = starts[i] - DURATA_PAROLA_DEFAULT
            pause.append((fine_precedente + starts[i]) / 2.0)
    estremi.append(starts[-1] + DURATA_PAROLA_DEFAULT)
    return pause, estremi


def _dentro_una_parola(t: float, starts: Sequence[float]) -> bool:
    """True se ``t`` cade fra l'inizio di una parola e il suo (stimato) finire."""
    for i, inizio in enumerate(starts):
        fine = starts[i + 1] if i + 1 < len(starts) else inizio + DURATA_PAROLA_DEFAULT
        if inizio + 1e-6 < t < fine - 1e-6:
            return True
    return False


def _piu_vicino(valore: float, candidati: Sequence[float], tolleranza: float) -> float | None:
    migliore: float | None = None
    for c in candidati:
        if abs(c - valore) > tolleranza:
            continue
        if migliore is None or abs(c - valore) < abs(migliore - valore):
            migliore = c
    return migliore


def aggancia_confini(
    timeline: Mapping[int, float],
    anchors: Mapping[int, float] | None,
    words: Sequence[Mapping[str, Any]],
    *,
    tolleranza: float = TOLLERANZA,
    min_secondi: float = 3.0,
) -> tuple[dict[int, float], list[tuple[int, float, float, str]]]:
    """Restituisce (timeline agganciata, mosse) con ``mosse`` = (slide, prima, dopo, tipo).

    ``tipo`` e' ``"pausa"`` o ``"parola"``: serve al log e al report perche' sono
    due qualita' diverse di correzione (una pausa e' una scelta, un estremo di
    parola e' un rattoppo).
    """
    nuove = {int(s): float(t) for s, t in timeline.items()}
    starts = _starts(words)
    if not starts or len(nuove) < 2:
        return nuove, []
    pause, estremi = confini_disponibili(words)
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
        nuove[slide] = nuova
        mosse.append((slide, prima, nuova, tipo))
    return nuove, mosse
