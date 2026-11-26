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

import json
import math
import time
from pathlib import Path
from typing import Any, cast

from config import atomic_write_text, log
from pipeline_report import format_time


def _intero_sicuro(valore: object) -> int:
    """Un valore sporco nel report non deve cancellare l'intero riepilogo.

    `json.loads` accetta `Infinity`, e `int(float("inf"))` solleva
    OverflowError: va catturato insieme a TypeError/ValueError.
    """
    try:
        return int(cast("str | int | float", valore))
    except (TypeError, ValueError, OverflowError):
        return 0


def _float_sicuro(valore: object) -> float:
    """Come `_intero_sicuro`, per i secondi dei segmenti e dei dubbi.

    `float("Infinity")` non solleva: restituisce inf, che poi `format_time`
    stampa come "inf" o solleva. Un valore non finito e' sporco quanto una
    stringa, quindi va scartato qui.
    """
    try:
        numero = float(cast("str | int | float", valore))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return numero if math.isfinite(numero) else 0.0


def _numeri(segmenti: list[dict[str, Any]]) -> tuple[int, float, float, dict[str, Any]]:
    """Pagine, durata totale, durata minima e il segmento piu' corto."""
    pagine = len(segmenti)
    if not pagine:
        return 0, 0.0, 0.0, {}
    totale = max(_float_sicuro(seg.get("end")) for seg in segmenti)
    corto = min(segmenti, key=lambda s: _float_sicuro(s.get("duration")))
    return pagine, totale, _float_sicuro(corto.get("duration")), corto


def _dubbi(anchors: dict[str, Any]) -> list[str]:
    """I confini in cui l'annuncio e il video non coincidono."""
    righe: list[str] = []
    for item in cast("list[dict[str, Any]]", anchors.get("mentions_off") or []):
        if not isinstance(item, dict):
            continue
        slide = _intero_sicuro(item.get("slide"))
        mention = _float_sicuro(item.get("mention"))
        declared = _float_sicuro(item.get("declared"))
        verso = "in ritardo" if mention < declared else "in anticipo"
        riga = (
            f"- slide {slide}: annunciata a {format_time(mention)} ma mostrata da "
            f"{format_time(declared)} — il video è {verso} sull'annuncio"
        )
        if item.get("preview"):
            riga += f" (anteprima: {item['preview']})"
        righe.append(riga)
    return righe


def _data_lezione(output_path: Path) -> str:
    """Il giorno in cui la lezione e' stata prodotta (mtime del video)."""
    try:
        return time.strftime("%d/%m/%Y", time.localtime(output_path.stat().st_mtime))
    except OSError:
        return ""


def riepilogo_markdown(
    report: dict[str, object],
    nome_video: str = "",
    data: str = "",
    dry_run: bool = False,
    video_presente: bool = True,
) -> str:
    """Il riepilogo in markdown, a partire da `sync_report.json`."""
    segmenti = [s for s in cast("list[dict[str, Any]]", report.get("segments") or []) if isinstance(s, dict)]
    anchors = cast("dict[str, Any]", report.get("anchors") or {})
    if not isinstance(anchors, dict):
        anchors = {}
    # ATTENZIONE: `_build_sync_report` fa `report.update(notes)`, quindi nel
    # report VERO le note (tempi, sorgenti, ripresa) stanno in cima, non sotto
    # "notes". Cercarle solo in `report["notes"]` rendeva invisibili i tempi e
    # la ripresa nei riepiloghi reali (i test passavano perché il report finto
    # era annidato). Si accettano entrambe le forme.
    notes: dict[str, Any] = dict(report)
    annidate = report.get("notes")
    if isinstance(annidate, dict):
        notes.update(annidate)
    # Un abort per audio sostituito / trascrizione troncata NON e' un dry-run:
    # dirlo nel riepilogo e' importante quanto non generare il video.
    motivo = str(report.get("motivo_interruzione") or "").strip()
    if motivo:
        dry_run = False

    pagine, totale, minima, corto = _numeri(segmenti)
    media = totale / pagine if pagine else 0.0
    transitions = _intero_sicuro(anchors.get("transitions") or 0)
    anchored = _intero_sicuro(anchors.get("anchored") or 0)
    senza = sorted(
        _intero_sicuro(s) for s in cast("list[Any]", anchors.get("unanchored_slides") or [])
    )

    righe: list[str] = ["# Riepilogo della lezione", ""]
    if nome_video:
        # Il nome c'e' ma il file puo' non esserci piu' (spostato, cancellato
        # dopo la run). Un riepilogo che dice "Video: ..." e basta faceva
        # credere che ci fosse: e' successo davvero, e il file e' l'unico
        # posto dove l'utente lo legge.
        manca = ""
        if not video_presente and not dry_run:
            manca = " — **assente dalla cartella**"
        righe.append(f"Video: `{nome_video}`{manca}")
    if data:
        righe.append(f"Lezione del {data}")
    sorgenti = report.get("sorgenti")
    if isinstance(sorgenti, dict) and (sorgenti.get("pdf") or sorgenti.get("audio")):
        etichette = {"pdf": "slide da", "audio": "voce da"}
        pezzi = [
            f"{etichette[chiave]} `{sorgenti[chiave]}`"
            for chiave in ("pdf", "audio")
            if sorgenti.get(chiave)
        ]
        righe.append("Sorgenti: " + ", ".join(pezzi))
    versione = report.get("versione")
    if versione:
        righe.append(f"Versione del programma: `{versione}`")
    righe += ["", "| Cosa | Valore |", "|---|---|"]
    if motivo:
        etichetta = {
            "audio_sostituito_durante_la_run": "interrotto: podcast sostituito durante la run",
            "trascrizione_troncata": "interrotto: trascrizione incompleta",
        }.get(motivo, f"interrotto: {motivo}")
        righe.append(f"| Esito | **{etichetta}** |")
    righe.append(f"| Pagine | {pagine} |")
    righe.append(f"| Parlato | {format_time(totale)} |")
    if pagine:
        righe.append(f"| Durata media per pagina | {format_time(media)} |")
        righe.append(
            f"| Pagina più breve | {_intero_sicuro(corto.get('slide'))} ({format_time(minima)}) |"
        )
    if transitions:
        righe.append(f"| Ancore usate | {anchored} su {transitions} |")
        righe.append(f"| Pagine senza annuncio | {', '.join(str(s) for s in senza) or 'nessuna'} |")
    dpi = notes.get("dpi_effettivo")
    if dpi:
        righe.append(f"| DPI effettivo | {dpi} |")
    ripresa = notes.get("ripresa_da")
    if ripresa:
        righe.append(
            f"| Trascrizione ripresa da | {format_time(_float_sicuro(ripresa))} |"
        )
    if dry_run:
        righe.append("| Video | **non creato** (dry-run) |")

    righe += ["", "## Cosa guardare a mano", ""]
    dubbi = _dubbi(anchors)
    coda = notes.get("coda_senza_parole")
    coda_secondi = round(_float_sicuro(coda))
    if coda_secondi > 60:
        dubbi = [f"Il testo si ferma {format_time(coda_secondi)} prima della fine dell'audio: "
                 "gli ultimi confini non sono verificabili col parlato.", *dubbi]
    if pagine <= 1:
        # Con una pagina sola non esistono confini, ma la trascrizione può
        # essere troncata: in quel caso c'e' COMUNQUE qualcosa da controllare.
        if dubbi:
            righe += dubbi
        else:
            righe.append("Una sola pagina: non c'è nessun confine da controllare.")
    elif dubbi:
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


def scrivi_riepilogo(
    report: dict[str, object],
    output_path: Path,
    *,
    data: str | None = None,
    dry_run: bool = False,
    video_presente: bool = True,
) -> Path | None:
    """Scrive `riepilogo.md` accanto al video. Non interrompe MAI la run.

    Qualunque errore resta un avviso, compreso un report malformato: il file e'
    una cortesia scritta DOPO il video, e un traceback qui rovinerebbe una run
    gia' riuscita.
    """
    try:
        path = output_path.parent / "riepilogo.md"
        giorno = _data_lezione(output_path) if data is None else data
        if not giorno:
            # La run normale scrive il riepilogo PRIMA del video: stat fallisce
            # e il file restava senza data per sempre (nessuna scrittura dopo,
            # in assenza di dubbi/riparazioni). La data della run e' oggi.
            giorno = time.strftime("%d/%m/%Y")
        atomic_write_text(
            path,
            riepilogo_markdown(
                report,
                output_path.name,
                giorno,
                dry_run,
                # Chi sa se il video c'e' lo dice: la pipeline scrive il
                # riepilogo PRIMA di renderizzare, quindi li' il file non c'e'
                # ancora e un controllo di esistenza darebbe un falso allarme
                # ("assente" su un video creato due minuti dopo).
                video_presente=video_presente,
            ),
        )
        log.info("   Riepilogo della lezione: %s", path)
        return path
    except Exception as e:  # noqa: BLE001 - cortesia: mai fatale
        log.warning("   Riepilogo non scritto (%s): %s", output_path.parent, e)
        return None


def rigenera(cache_dir: Path, output_path: Path) -> int:
    """Riscrive `riepilogo.md` dal report dell'ultima run (`--solo-riepilogo`).

    Esce 0 se il file e' scritto, 1 se manca il report: la pipeline non gira,
    quindi costa un secondo invece di una trascrizione.
    """
    report_path = cache_dir / "sync_report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.error("Nessun report da leggere in %s: %s", report_path, e)
        log.error("Lancia prima la pipeline (anche con --dry-run).")
        return 1
    if not isinstance(report, dict):
        log.error("%s non contiene un report.", report_path)
        return 1
    # `--solo-riepilogo` NON deve cancellare il verdetto "video non creato"
    # di un --dry-run/--preview: il report lo dice, e un video vecchio che
    # sta ancora nella cartella non lo rende valido per quella timeline.
    # Se il video manca SENZA che il report sia un dry-run, resta il messaggio
    # diverso ("assente dalla cartella"): sono due situazioni distinte.
    interrotto = bool(report.get("motivo_interruzione"))
    dry_run = bool(report.get("senza_video") or report.get("anteprima")) and not interrotto
    return 0 if scrivi_riepilogo(
        report, output_path, dry_run=dry_run, video_presente=output_path.exists()
    ) is not None else 1
