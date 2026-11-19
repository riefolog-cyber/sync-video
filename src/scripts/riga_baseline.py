#!/usr/bin/env python3
"""Stampa una riga della tabella delle baseline dalla run appena fatta.

La tabella delle baseline in `README.md` (le 5 run verdi) e' scritta a mano:
si copiano i numeri dal riepilogo finale. Questo script li prende da
`.cache/sync_report.json` e stampa la riga gia' formattata, da incollare.

Uso:
    python src/scripts/riga_baseline.py
    python src/scripts/riga_baseline.py --data 12/11    # data da scrivere

Esce con 1 se non c'e' un report (la run non e' ancora stata fatta).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, cast

# Deve stare PRIMO: mette la radice del progetto nel sys.path.
from _bootstrap import CODICE

sys.path.insert(0, str(CODICE))

from config import CACHE_DIR
from pipeline_report import format_time


def riga(report: dict[str, Any], data: str) -> str:
    """La riga markdown: data, slide, ancore, durata minima, fiducia, frame-check."""
    segmenti = [s for s in cast("list[dict[str, Any]]", report.get("segments") or []) if isinstance(s, dict)]
    anchors = cast("dict[str, Any]", report.get("anchors") or {})

    durate = [float(s.get("duration") or 0.0) for s in segmenti]
    minima = format_time(min(durate)) if durate else "?"
    transitions = int(anchors.get("transitions") or 0)
    anchored = int(anchors.get("anchored") or 0)
    ancore = f"{anchored}/{transitions}" if transitions else "0"
    # Stessa lettura della run: se ogni transizione e' annunciata i confini sono
    # misurati, altrimenti stimati.
    fiducia = "ALTO" if transitions and anchored >= transitions else "STIMATA"
    dubbi = len(cast("list[Any]", anchors.get("mentions_off") or []))
    return f"| {data} | {len(segmenti)} | {ancore} | {minima} | {fiducia} | {dubbi} |"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        default=time.strftime("%d/%m"),
        help="data da scrivere nella riga (default: oggi)",
    )
    args = parser.parse_args(argv)

    path = CACHE_DIR / "sync_report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Nessun report da leggere in {path}: {e}")
        print("Lancia prima la pipeline (anche con --dry-run).")
        return 1
    if not isinstance(report, dict):
        print(f"{path} non contiene un report.")
        return 1
    print(riga(report, args.data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
