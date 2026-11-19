#!/usr/bin/env python3
"""Riepilogo leggibile di una lezione, accanto al video.

Il referto tecnico (`sync_report.json`) vive nella cache ed e' pensato per gli
strumenti (`analysis_sync.py`, `--review`). Chi produce una lezione vuole una
pagina da leggere: quante pagine, con quante ancore, la sezione piu' corta e
cosa conviene guardare a mano.

Il file e' `riepilogo.md`, nella stessa cartella del video: una cartella = una
lezione, quindi non serve un nome per lezione.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from config import atomic_write_text, log
from pipeline_report import format_time


def _numeri(segmenti: list[dict[str, Any]]) -> tuple[int, float, float, dict[str, Any]]:
    """Pagine, durata totale, durata minima e il segmento piu' corto."""
    pagine = len(segmenti)
    if not pagine:
        return 0, 0.0, 0.0, {}
    totale = max(float(seg.get("end") or 0.0) for seg in segmenti)
    corto = min(segmenti, key=lambda s: float(s.get("duration") or 0.0))
    return pagine, totale, float(corto.get("duration") or 0.0), corto


def _dubbi(anchors: dict[str, Any]) -> list[str]:
    """I confini in cui l'annuncio e il video non coincidono."""
    righe: list[str] = []
    for item in cast("list[dict[str, Any]]", anchors.get("mentions_off") or []):
        slide = int(item.get("slide") or 0)
        mention = float(item.get("mention") or 0.0)
        declared = float(item.get("declared") or 0.0)
        verso = "in ritardo" if mention < declared else "in anticipo"
        riga = (
            f"- slide {slide}: annunciata a {format_time(mention)} ma mostrata da "
            f"{format_time(declared)} — il video è {verso} sull'annuncio"
        )
        if item.get("preview"):
            riga += f" (anteprima: {item['preview']})"
        righe.append(riga)
    return righe


def riepilogo_markdown(report: dict[str, object], nome_video: str = "") -> str:
    """Il riepilogo in markdown, a partire da `sync_report.json`."""
    segmenti = [s for s in cast("list[dict[str, Any]]", report.get("segments") or []) if isinstance(s, dict)]
    anchors = cast("dict[str, Any]", report.get("anchors") or {})
    notes = cast("dict[str, Any]", report.get("notes") or {})

    pagine, totale, minima, corto = _numeri(segmenti)
    media = totale / pagine if pagine else 0.0
    transitions = int(anchors.get("transitions") or 0)
    anchored = int(anchors.get("anchored") or 0)
    senza = sorted(int(s) for s in cast("list[Any]", anchors.get("unanchored_slides") or []))

    righe: list[str] = ["# Riepilogo della lezione", ""]
    if nome_video:
        righe.append(f"Video: `{nome_video}`")
    versione = report.get("versione")
    if versione:
        righe.append(f"Versione del programma: `{versione}`")
    righe += ["", "| Cosa | Valore |", "|---|---|"]
    righe.append(f"| Pagine | {pagine} |")
    righe.append(f"| Parlato | {format_time(totale)} |")
    if pagine:
        righe.append(f"| Durata media per pagina | {format_time(media)} |")
        righe.append(
            f"| Pagina più breve | {int(corto.get('slide') or 0)} ({format_time(minima)}) |"
        )
    if transitions:
        righe.append(f"| Ancore usate | {anchored} su {transitions} |")
        righe.append(f"| Pagine senza annuncio | {', '.join(str(s) for s in senza) or 'nessuna'} |")
    dpi = notes.get("dpi_effettivo")
    if dpi:
        righe.append(f"| DPI effettivo | {dpi} |")

    righe += ["", "## Cosa guardare a mano", ""]
    dubbi = _dubbi(anchors)
    if dubbi:
        righe += dubbi
    elif transitions and anchored >= transitions:
        righe.append("Niente: tutti i confini sono quelli annunciati a voce.")
    else:
        righe.append("Alcuni confini sono stimati dal contenuto (vedi le pagine senza annuncio).")

    tempi = cast("dict[str, Any]", notes.get("tempi") or {})
    if tempi:
        righe += ["", "## Tempi della run", ""]
        righe.append(
            " · ".join(
                f"{nome} {tempi[nome]}s" for nome in ("ocr", "trascrizione", "sincronizzazione") if nome in tempi
            )
        )

    correzioni = cast("list[Any]", notes.get("correzioni_nomi") or [])
    if correzioni:
        righe += ["", "## Nomi corretti nella trascrizione", ""]
        righe += [f"- `{c}`" for c in correzioni[:20]]

    righe.append("")
    return "\n".join(righe)


def scrivi_riepilogo(report: dict[str, object], output_path: Path) -> Path | None:
    """Scrive `riepilogo.md` accanto al video. Non interrompe mai la run."""
    try:
        path = output_path.parent / "riepilogo.md"
        atomic_write_text(path, riepilogo_markdown(report, output_path.name))
        log.info("   Riepilogo della lezione: %s", path)
        return path
    except OSError as e:
        log.debug("   Riepilogo non scritto (%s): %s", output_path.parent, e)
        return None
