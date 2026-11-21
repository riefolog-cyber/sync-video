#!/usr/bin/env python3
"""Correzioni manuali della timeline: dal file alla timeline validata.

Chi guarda il video sa dove deve entrare una slide; il motore lo stima. Questo
modulo e' la porta d'ingresso di quel sapere, e l'unico punto in cui una
persona puo' cambiare un confine senza rifare l'analisi.

Formato (uno dei due, indifferentemente):

    {"5": 252.0, "9": 431.25}
    [{"slide": 5, "start": 252.0}, {"slide": 9, "start": 431.25}]

Regola del progetto rispettata qui dentro: **si rifiuta, non si aggiusta**. Un
override che rompe l'ordine o il minimo leggibile produce un video sbagliato in
modo difficile da vedere; un errore chiaro all'avvio costa dieci secondi.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _numero(valore: Any, cosa: str) -> float:
    """Numero finito da un valore JSON, o ValueError con il nome del campo."""
    if isinstance(valore, bool) or not isinstance(valore, (int, float, str)):
        raise ValueError(f"{cosa}: atteso un numero di secondi, trovato {valore!r}")
    try:
        numero = float(valore)
    except (TypeError, ValueError):
        raise ValueError(f"{cosa}: '{valore}' non e' un numero di secondi") from None
    if numero != numero or numero in (float("inf"), float("-inf")):
        raise ValueError(f"{cosa}: '{valore}' non e' un tempo finito")
    return numero


def fissaggi_da_voci(voci: Sequence[str]) -> dict[int, float]:
    """``["5=252.0", "9=431.25"]`` -> ``{5: 252.0, 9: 431.25}``.

    E' il formato di ``--fix-slide``: chi corregge un confine non deve sapere
    come e' fatto il JSON.
    """
    fissaggi: dict[int, float] = {}
    for voce in voci:
        slide_txt, sep, tempo_txt = voce.partition("=")
        if not sep:
            raise ValueError(f"'{voce}': atteso slide=secondi (esempio: 5=252.0)")
        try:
            slide = int(slide_txt.strip())
        except ValueError:
            raise ValueError(
                f"'{voce}': '{slide_txt.strip()}' non e' un numero di slide"
            ) from None
        if slide in fissaggi:
            raise ValueError(f"slide {slide}: indicata piu' di una volta")
        fissaggi[slide] = _numero(tempo_txt.strip(), f"slide {slide}")
    return fissaggi


def _da_dict(dati: Mapping[str, Any]) -> dict[int, float]:
    override: dict[int, float] = {}
    for chiave, valore in dati.items():
        try:
            slide = int(str(chiave).strip())
        except (TypeError, ValueError):
            raise ValueError(f"chiave '{chiave}' non e' un numero di slide") from None
        override[slide] = _numero(valore, f"slide {slide}")
    return override


def _da_lista(dati: list[Any]) -> dict[int, float]:
    override: dict[int, float] = {}
    for i, voce in enumerate(dati, start=1):
        if not isinstance(voce, Mapping):
            raise ValueError(f"voce {i}: atteso un oggetto con 'slide' e 'start'")
        if "slide" not in voce or "start" not in voce:
            raise ValueError(f"voce {i}: manca 'slide' o 'start'")
        slide = int(_numero(voce["slide"], f"voce {i} 'slide'"))
        override[slide] = _numero(voce["start"], f"slide {slide}")
    return override


def leggi_override(path: Path) -> dict[int, float]:
    """Legge il file di override. Solleva ValueError con un messaggio azionabile.

    Accetta un oggetto ``{"slide": secondi}`` o una lista di ``{"slide", "start"}``:
    il primo si scrive a mano, il secondo e' quello che si ottiene copiando da
    ``timeline_finale.json`` (dove gli stessi numeri si chiamano ``start``).
    """
    try:
        testo = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"non riesco a leggere {path.name}: {e}") from e
    try:
        dati = json.loads(testo)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name} non e' JSON valido (riga {e.lineno}, colonna {e.colno})") from e
    if isinstance(dati, Mapping):
        if not dati:
            raise ValueError(f"{path.name} e' vuoto: niente da applicare")
        return _da_dict(dati)
    if isinstance(dati, list):
        if not dati:
            raise ValueError(f"{path.name} e' vuoto: niente da applicare")
        return _da_lista(dati)
    raise ValueError(f"{path.name}: atteso un oggetto o una lista, trovato {type(dati).__name__}")


def applica_override(
    timeline: Mapping[int, float],
    override: Mapping[int, float],
    total_duration: float,
    min_secondi: float,
) -> tuple[dict[int, float], list[tuple[int, float, float]]]:
    """Applica gli override alla timeline e la valida. Restituisce (nuova, mosse).

    Le regole, in ordine:
    - la slide 1 parte sempre da 0: e' l'inizio del video, non un confine;
    - la slide deve esistere nella timeline;
    - il tempo deve stare dentro l'audio;
    - dopo l'applicazione, ogni slide deve durare almeno ``min_secondi`` e i
      confini devono restare in ordine crescente (altrimenti il video
      mostrerebbe due slide insieme o andrebbe indietro).

    Spostare un'ancora annunciata a voce e' permesso (il video non seguira'
    quel timestamp): chi chiama lo segnala con `override_su_ancore`.
    """
    if not override:
        return dict(timeline), []

    nuove = {int(s): float(t) for s, t in timeline.items()}
    mosse: list[tuple[int, float, float]] = []
    for slide in sorted(override):
        if slide == 1:
            raise ValueError("la slide 1 parte sempre da 0: non e' un confine spostabile")
        if slide not in nuove:
            raise ValueError(
                f"slide {slide} non esiste nella timeline (slide disponibili: 1-{max(nuove)})"
            )
        t = float(override[slide])
        if not (0.0 <= t < total_duration):
            raise ValueError(
                f"slide {slide}: {t:.1f}s e' fuori dall'audio (0-{total_duration:.1f}s)"
            )
        vecchio = nuove[slide]
        if abs(t - vecchio) > 1e-9:
            mosse.append((slide, vecchio, t))

    nuove.update({int(s): float(t) for s, t in override.items() if int(s) != 1})

    ordinate = sorted(nuove)
    for i, slide in enumerate(ordinate):
        if i + 1 < len(ordinate):
            durata = nuove[ordinate[i + 1]] - nuove[slide]
            if durata <= 0:
                raise ValueError(
                    f"slide {slide} ({nuove[slide]:.1f}s) finirebbe dopo la slide "
                    f"{ordinate[i + 1]} ({nuove[ordinate[i + 1]]:.1f}s)"
                )
            if durata < min_secondi - 1e-9:
                raise ValueError(
                    f"slide {slide} durerebbe {durata:.1f}s, sotto il minimo di "
                    f"{min_secondi:.0f}s: sposta anche la slide {ordinate[i + 1]} "
                    "o togli questo override"
                )

    # Non si rifiuta lo spostamento di un'ancora pronunciata: e' una scelta
    # esplicita che contraddice una misura, e il video la rispetta. Chi chiama
    # la segnala a voce (vedi `override_su_ancore`).
    return nuove, mosse


def aggiorna_file(path: Path, fissaggi: Mapping[int, float]) -> dict[int, float]:
    """Unisce ``fissaggi`` al file di override (creandolo) e lo riscrive.

    Serve a ``--fix-slide``: le correzioni da riga di comando e quelle scritte
    a mano finiscono nello stesso file, e una non cancella l'altra.
    """
    esistenti = leggi_override(path) if path.exists() else {}
    esistenti.update(fissaggi)
    testo = json.dumps({str(s): t for s, t in sorted(esistenti.items())}, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(testo + "\n", encoding="utf-8")
    return esistenti


def override_su_ancore(
    mosse: list[tuple[int, float, float]], anchors: Mapping[int, float] | None
) -> list[int]:
    """Slide spostate che avevano un'ancora pronunciata: da segnalare a voce."""
    if not anchors:
        return []
    return [slide for slide, _, _ in mosse if slide in anchors]
