#!/usr/bin/env python3
"""Stampa (o scrive) una riga della tabella delle baseline dalla run appena fatta.

Le baseline vivono in `docs/verifica.md`: una riga per run verde, con data,
slide, ancore, durata minima, fiducia e dubbi. Prima si copiavano a mano dal
riepilogo finale; qui si prendono da `.cache/sync_report.json`.

Uso:
    python src/scripts/riga_baseline.py                     # stampa la riga
    python src/scripts/riga_baseline.py --scrivi docs/verifica.md
    python src/scripts/riga_baseline.py --confronta docs/verifica.md

`--scrivi` mette la riga al posto di quella con la stessa data (o in fondo alla
tabella). `--confronta` dice di quanto l'ultima run si e' mossa rispetto
all'ultima riga della tabella: e' il controllo che a occhio non si fa.

Esce con 0 se ha fatto il suo lavoro, 1 se non c'e' un report (la run non e'
ancora stata fatta) o se il file indicato non contiene la tabella.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, cast

# Deve stare PRIMO: mette la radice del progetto nel sys.path.
from _bootstrap import CODICE

sys.path.insert(0, str(CODICE))

from config import CACHE_DIR
from pipeline_report import format_time

# La tabella in docs/verifica.md: le sei colonne che `riga` produce.
INTESTAZIONE = "| data |"


def riga(report: dict[str, Any], data: str) -> str:
    """La riga markdown: data, slide, ancore, durata minima, fiducia, dubbi."""
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


def _sorgente(report: dict[str, Any]) -> str:
    """Da quale lezione viene la riga: un report vecchio e' la trappola."""
    sorgenti = report.get("sorgenti")
    if not isinstance(sorgenti, dict) or not (sorgenti.get("pdf") or sorgenti.get("audio")):
        return "sconosciuta (report senza 'sorgenti')"
    return " + ".join(str(sorgenti[k]) for k in ("pdf", "audio") if sorgenti.get(k))


def _secondi(testo: str) -> float | None:
    """'29s' / '1m30s' / '?' -> secondi (None se non si capisce)."""
    m = re.fullmatch(r"(?:(\d+)m)?(\d+)s", testo.strip())
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2))


def _celle(riga_md: str) -> list[str]:
    return [cella.strip() for cella in riga_md.strip().strip("|").split("|")]


def _prima_cella(riga_md: str) -> str:
    """La data della riga, o '' se la riga non e' una riga di tabella."""
    celle = _celle(riga_md)
    return celle[0] if celle else ""


def _blocco_tabella(righe: list[str]) -> tuple[int, int] | None:
    """(prima riga del corpo, ultima riga) della tabella delle baseline."""
    inizio = next(
        (i for i, r in enumerate(righe) if r.strip().lower().startswith(INTESTAZIONE)),
        None,
    )
    if inizio is None:
        return None
    fine = inizio
    while fine + 1 < len(righe) and righe[fine + 1].lstrip().startswith("|"):
        fine += 1
    return inizio + 2, fine


def aggiorna(testo: str, nuova: str, data: str) -> str:
    """Mette `nuova` nella tabella: sostituisce la riga di quella data, o la aggiunge.

    Solleva ValueError se la tabella non c'e' (file sbagliato): meglio un
    errore chiaro che una riga appesa in mezzo a un'altra tabella.
    """
    a_capo = "\r\n" if "\r\n" in testo else "\n"
    righe = testo.splitlines()
    posto = _blocco_tabella(righe)
    if posto is None:
        raise ValueError(f"tabella delle baseline non trovata (cerco una riga che inizia con {INTESTAZIONE!r})")
    prima, ultima = posto
    for i in range(prima, ultima + 1):
        if _prima_cella(righe[i]) == data:
            righe[i] = nuova
            break
    else:
        righe.insert(ultima + 1, nuova)
    return a_capo.join(righe) + (a_capo if testo.endswith(("\n", "\r")) else "")


def ultima(righe: list[str]) -> list[str] | None:
    """L'ultima riga della tabella (quella con cui confrontarsi)."""
    posto = _blocco_tabella(righe)
    if posto is None:
        return None
    prima, finita = posto
    corpo = [righe[i] for i in range(prima, finita + 1) if righe[i].strip()]
    return _celle(corpo[-1]) if corpo else None


def confronto(vecchia: list[str], nuova: list[str]) -> list[str]:
    """Le differenze fra due righe della tabella, in chiaro."""
    etichette = ("slide", "ancore", "durata minima", "fiducia", "dubbi")
    indici = (1, 2, 3, 4, 5)
    differenze: list[str] = []
    if len(vecchia) <= max(indici) or len(nuova) <= max(indici):
        return ["   riga di tabella incompleta: confronto saltato"]
    for etichetta, i in zip(etichette, indici, strict=True):
        a, b = vecchia[i], nuova[i]
        if a == b:
            continue
        extra = ""
        if etichetta == "durata minima":
            sa, sb = _secondi(a), _secondi(b)
            if sa is not None and sb is not None:
                extra = f" ({sb - sa:+.0f}s)"
        differenze.append(f"   {etichetta}: {a} -> {b}{extra}")
    return differenze


def _leggi_report() -> dict[str, Any] | None:
    path = CACHE_DIR / "sync_report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Nessun report da leggere in {path}: {e}")
        print("Lancia prima la pipeline (anche con --dry-run).")
        return None
    if not isinstance(report, dict):
        print(f"{path} non contiene un report.")
        return None
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data",
        default=time.strftime("%d/%m"),
        help="data da scrivere nella riga (default: oggi)",
    )
    parser.add_argument(
        "--scrivi",
        metavar="FILE",
        help="scrive la riga nella tabella delle baseline di questo file",
    )
    parser.add_argument(
        "--confronta",
        metavar="FILE",
        help="dice di quanto la run si e' mossa rispetto all'ultima riga della tabella",
    )
    args = parser.parse_args(argv)

    report = _leggi_report()
    if report is None:
        return 1
    nuova = riga(report, args.data)

    # Confrontare dopo aver scritto convincerebbe sempre che non e' cambiato
    # niente: si legge l'originale PRIMA della scrittura.
    testo_originale: str | None = None
    if args.scrivi and args.confronta and Path(args.scrivi) == Path(args.confronta):
        try:
            testo_originale = Path(args.scrivi).read_text(encoding="utf-8")
        except OSError as e:
            print(f"Non riesco a leggere {args.scrivi}: {e}")
            return 1

    if args.scrivi:
        path = Path(args.scrivi)
        try:
            testo = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"Non riesco a leggere {path}: {e}")
            return 1
        try:
            path.write_text(aggiorna(testo, nuova, args.data), encoding="utf-8", newline="")
        except (OSError, ValueError) as e:
            print(f"Tabella non aggiornata: {e}")
            return 1
        print(f"Riga scritta in {path}: {nuova}")

    if args.confronta:
        path = Path(args.confronta)
        try:
            righe = (testo_originale if testo_originale is not None else path.read_text(encoding="utf-8")).splitlines()
        except OSError as e:
            print(f"Non riesco a leggere {path}: {e}")
            return 1
        vecchia = ultima(righe)
        if vecchia is None:
            print(f"{path}: tabella delle baseline non trovata.")
            return 1
        print(f"Confronto con l'ultima baseline ({vecchia[0]}), sorgente: {_sorgente(report)}")
        differenze = confronto(vecchia, _celle(nuova))
        print("\n".join(differenze) if differenze else "   Nessun cambiamento.")
        return 0

    if not args.scrivi:
        print(f"# sorgente: {_sorgente(report)}", file=sys.stderr)
        print(nuova)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
