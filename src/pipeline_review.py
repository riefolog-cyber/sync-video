#!/usr/bin/env python3
"""Revisione dei confini dubbi di una run gia' fatta (P1: fuori da main.py).

Rilegge report e trascrizione dalla cache e rigenera solo le anteprime, senza
rifare la pipeline. La cartella cache arriva esplicita (niente globale): nei
test si punta a una cartella temporanea, e main.py inietta la sua CACHE_DIR
con lo shim (stesso schema di pipeline_cache / pipeline_report).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast

from chunks import Word
from config import log
from pipeline_cache import latest_transcript_words, load_sync_report
from pipeline_report import format_time as _format_time
from pipeline_report import slide_list_text as _slide_list_text
from timeline import announcements_off_timeline


def _boundary_preview_moments(item: dict[str, object]) -> list[tuple[str, float]]:
    """I due istanti di un confine dubbio: l'annuncio e il confine dichiarato.

    Sono le due cose che il dubbio mette in contraddizione. Il primo dice cosa
    c'era a schermo mentre il parlato annunciava la slide (è la domanda a cui
    rispondere: a schermo c'era già?), il secondo dove la timeline la fa
    comparire.
    """
    slide = int(cast("int", item.get("slide") or 0))
    mention = float(cast("float", item.get("mention") or 0.0))
    declared = float(cast("float", item.get("declared") or 0.0))
    return [
        (f"parlato: annuncio 'slide {slide}' a {_format_time(mention)}", mention),
        (f"video: la slide {slide} compare qui ({_format_time(declared)})", declared),
    ]


def _write_boundary_previews(
    video_path: str | Path,
    items: Sequence[dict[str, object]],
    review_dir: Path,
) -> None:
    """Scrive il foglio di controllo di ogni confine dubbio e lo lega al dubbio.

    I nomi sono deterministici (``confine_slideNN_II.png``): due run di seguito
    si sovrascrivono invece di accumulare, che è il difetto costato 191 file /
    297 MB alla cartella dei frame prima che venisse ripulita a ogni verifica.
    Solo i dubbi con un'anteprima davvero scritta ricevono la chiave ``preview``:
    nel dry-run il video non esiste e il riepilogo non deve citare un file che
    non c'è.
    """
    for i, item in enumerate(items):
        slide = int(cast("int", item.get("slide") or 0))
        # Import differito da main (stesso schema di cache_maintenance): evita
        # il ciclo main<->review e rispetta mock.patch("main.boundary_review_sheet")
        # dei test, che il riferimento diretto qui ignorerebbe.
        from main import boundary_review_sheet as _foglio

        sheet = _foglio(
            video_path,
            _boundary_preview_moments(item),
            review_dir / f"confine_slide{slide:02d}_{i:02d}.png",
        )
        if sheet is not None:
            item["preview"] = str(sheet)


def _boundary_doubt_text(item: dict[str, object]) -> str:
    """Testo (in parole semplici) di un dubbio "annuncio contro confine".

    Estratto perché lo stampa sia il riepilogo finale sia il comando di sola
    revisione (`--review`): due copie della stessa frase prima o poi divergono,
    e la revisione deve ripetere esattamente quello che ha detto la run.
    """
    slide = int(cast("int", item.get("slide") or 0))
    mention = float(cast("float", item.get("mention") or 0.0))
    declared = float(cast("float", item.get("declared") or 0.0))
    preview = f" (anteprima: {item.get('preview')})" if item.get("preview") else ""
    return (
        f"nel parlato la slide {slide} viene annunciata a {_format_time(mention)} "
        f"ma il video la mostra da {_format_time(declared)}: "
        + (
            "il video è in ritardo sull'annuncio"
            if mention < declared
            else "il video è in anticipo sull'annuncio"
        )
        + " — il momento dell'annuncio è certo, quello del video è una stima: "
        "guarda lì"
        + preview
    )


def _review_boundary_doubts(video_path: Path, *, cache_dir: Path) -> None:
    """Rivede i confini dubbi di una run GIÀ fatta, senza rifare la pipeline.

    Il report e la trascrizione della run sono già in cache: la revisione
    legge quelli e rigenera solo le anteprime dal video che esiste. Funziona
    anche sui report scritti PRIMA del controllo incrociato (lì `mentions_off`
    non c'è e viene ricalcolato dalle parole in cache), che è il caso della run
    del 14/10: rifarla per guardare un confine sarebbe costato 8 minuti.
    """
    report_path = cache_dir / "sync_report.json"
    report = load_sync_report(cache_dir=cache_dir)
    if not report:
        log.error(
            "\n   [Revisione] Nessun report da rivedere in %s: lancia la pipeline "
            "una volta, poi ripeti con --review.",
            report_path,
        )
        return
    segments = cast("list[dict[str, object]]", report.get("segments") or [])
    starts: dict[int, float] = {}
    for seg in segments:
        starts[int(cast("int", seg.get("slide") or 0))] = float(
            cast("float", seg.get("start") or 0.0)
        )
    if not starts:
        log.error("   [Revisione] Il report non contiene segmenti: niente da rivedere.")
        return

    anchors = cast("dict[str, object]", report.get("anchors") or {})
    items = [
        dict(item)
        for item in cast("list[dict[str, object]]", anchors.get("mentions_off") or [])
    ]
    ricalcolati = False
    if not items:
        words = cast("list[Word]", latest_transcript_words(cache_dir=cache_dir))
        if words:
            items = [
                {"slide": s, "mention": m, "declared": d}
                for s, m, d in announcements_off_timeline(words, starts, max(starts))
            ]
            ricalcolati = True

    quando = ""
    with suppress(OSError):
        quando = time.strftime(" (%d/%m %H:%M)", time.localtime(report_path.stat().st_mtime))
    log.info("\n" + "=" * 70)
    log.info(" REVISIONE DEI CONFINI — nessuna pipeline rifatta")
    log.info("=" * 70)
    log.info("   Report: %s%s", report_path, quando)
    log.info(
        "   %d slide, %s di parlato.",
        len(segments),
        _format_time(
            max((float(cast("float", seg.get("end") or 0.0)) for seg in segments), default=0.0)
        ),
    )
    transitions = int(cast("int", anchors.get("transitions") or 0))
    if transitions:
        log.info(
            "   Confini ancorati nell'ultima run: %s su %s.",
            int(cast("int", anchors.get("anchored") or 0)),
            transitions,
        )
    # Un confine senza annuncio è una stima, non una misura: nei dubbi sui
    # confini va detto anche quando non c'è nessuna contraddizione, altrimenti
    # "niente da rivedere" suonerebbe come "tutto misurato".
    senza_ancora = sorted(
        int(s) for s in cast("Sequence[int]", anchors.get("unanchored_slides") or [])
    )
    if senza_ancora:
        log.info(
            "   Confini stimati dal contenuto (nessun annuncio 'slide N'): %s.",
            _slide_list_text(senza_ancora),
        )
    if not items:
        log.info(
            "   Nessuna contraddizione fra gli annunci 'slide N' e i confini "
            "dichiarati: niente da rivedere.%s",
            " (controllo ricalcolato dalle parole in cache)" if ricalcolati else "",
        )
        log.info("=" * 70)
        return
    if video_path.exists():
        _write_boundary_previews(video_path, items, cache_dir / "review")
    else:
        log.info("   Video assente (%s): i dubbi restano, senza anteprima.", video_path)
    if ricalcolati:
        log.info(
            "   (il report non li aveva salvati: ricalcolati dalle parole in cache)"
        )
    log.info("   Da controllare a mano:")
    for item in items:
        log.info("     - %s", _boundary_doubt_text(item))
    log.info("=" * 70)
