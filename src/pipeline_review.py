#!/usr/bin/env python3
"""Revisione dei confini dubbi di una run gia' fatta (P1: fuori da main.py).

Rilegge report e trascrizione dalla cache e rigenera solo le anteprime, senza
rifare la pipeline. La cartella cache arriva esplicita (niente globale): nei
test si punta a una cartella temporanea, e main.py inietta la sua CACHE_DIR
con lo shim (stesso schema di pipeline_cache / pipeline_report).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast

from chunks import Word
from config import log
from pipeline_cache import latest_transcript_words, load_sync_report, save_sync_report
from pipeline_report import format_time as _format_time
from pipeline_report import slide_list_text as _slide_list_text
from riepilogo import scrivi_riepilogo
from timeline import announcements_off_timeline


def _intero_sicuro(valore: object) -> int:
    """Intero da un report sporco: mai un traceback in `--review`."""
    try:
        return int(cast("str | int | float", valore))
    except (TypeError, ValueError, OverflowError):
        return 0


def _numero_sicuro(valore: object) -> float:
    """Float finito da un report sporco (JSON accetta ``Infinity``)."""
    try:
        numero = float(cast("str | int | float", valore))
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return numero if math.isfinite(numero) else 0.0


def _boundary_preview_moments(item: dict[str, object]) -> list[tuple[str, float]]:
    """I due istanti di un confine dubbio: l'annuncio e il confine dichiarato.

    Sono le due cose che il dubbio mette in contraddizione. Il primo dice cosa
    c'era a schermo mentre il parlato annunciava la slide (è la domanda a cui
    rispondere: a schermo c'era già?), il secondo dove la timeline la fa
    comparire.
    """
    slide = _intero_sicuro(item.get("slide"))
    mention = _numero_sicuro(item.get("mention"))
    declared = _numero_sicuro(item.get("declared"))
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

    I nomi deterministici NON bastano a evitare l'accumulo: se la run dopo ha
    meno dubbi, le anteprime in piu' della run prima restano li' a testimoniare
    un problema gia' corretto. A ogni scrittura si rimuovono quindi tutte le
    anteprime di confine non prodotte in questa run.
    """
    scritte: set[Path] = set()
    for i, item in enumerate(items):
        slide = _intero_sicuro(item.get("slide"))
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
            scritte.add(Path(sheet))
        else:
            # Una anteprime NON scritta non puo' restare citata nel report:
            # la pulizia qui sotto cancella il PNG e il collegamento verrebbe
            # lasciato appeso a un file inesistente.
            item.pop("preview", None)
    if review_dir.exists():
        for vecchia in review_dir.glob("confine_slide*.png"):
            if vecchia not in scritte:
                with suppress(OSError):
                    vecchia.unlink()


def _boundary_doubt_text(item: dict[str, object]) -> str:
    """Testo (in parole semplici) di un dubbio "annuncio contro confine".

    Estratto perché lo stampa sia il riepilogo finale sia il comando di sola
    revisione (`--review`): due copie della stessa frase prima o poi divergono,
    e la revisione deve ripetere esattamente quello che ha detto la run.
    """
    slide = _intero_sicuro(item.get("slide"))
    mention = _numero_sicuro(item.get("mention"))
    declared = _numero_sicuro(item.get("declared"))
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


def _review_boundary_doubts(video_path: Path, *, cache_dir: Path) -> int:
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
        return 1
    if report.get("anteprima") or report.get("senza_video"):
        # Un abort per audio sostituito / trascrizione troncata NON e' un
        # preview: il motivo e' salvato nel report e va riferito bene.
        motivo = str(report.get("motivo_interruzione") or "").strip()
        if not motivo:
            log.error(
                "\n   [Revisione] Il report in cache viene da --preview/--dry-run: "
                "nessun video e' stato generato con quella timeline. Rilancia la "
                "pipeline completa prima di rivedere i confini."
            )
            return 3
        spiegazione = {
            "audio_sostituito_durante_la_run": "il podcast e' stato sostituito durante la run",
            "trascrizione_troncata": "la trascrizione era incompleta",
            "allineamento_sospetto": "l'allineamento delle slide era sospetto",
            "prima_sezione_disallineata": "la prima sezione non parlava della slide 1",
            "encoding_fallito": "la generazione del video e' fallita",
        }.get(motivo, "la run e' stata interrotta")
        log.error(
            "\n   [Revisione] Nessun video e' stato generato con questa timeline: %s. "
            "Rilancia la pipeline con il materiale definitivo prima di rivedere i confini.",
            spiegazione,
        )
        return 3
    grezzo = report.get("segments")
    segments = [s for s in cast("list[object]", grezzo or []) if isinstance(s, dict)]
    if not segments:
        log.error("   [Revisione] Il report non contiene segmenti leggibili.")
        return 1
    starts: dict[int, float] = {}
    for seg in segments:
        slide = _intero_sicuro(seg.get("slide"))
        # Ultima occorrenza: con --skip-slides la stessa slide puo' comparire
        # piu' volte, e l'annuncio Pronunciato piu' tardi parla dell'ultima.
        # Tenere la prima faceva dire "video in anticipo" e piazzava
        # l'anteprima sul confine sbagliato.
        starts[slide] = _numero_sicuro(seg.get("start"))
    if not starts:
        log.error("   [Revisione] Il report non contiene segmenti: niente da rivedere.")
        return 1

    # N reale del deck = numero di segmenti del report: con --skip-slides la
    # run NON elimina i segmenti ne' li mette a durata 0, sostituisce il
    # numero di slide con quello valido precedente (vedi main.py). Il vecchio
    # commento parlava di segmenti a durata 0 che in realta' non esistono.
    total_slides_review = len(segments)
    # Se il report non ha `mentions_off` e la trascrizione non c'e', il
    # controllo NON e' mai stato fatto: dirlo e' obbligatorio, altrimenti
    # "niente da rivedere" e' una falsa rassicurazione.
    controllo_non_fatto = False
    motivo_non_fatto = ""

    grezzo_anchors = report.get("anchors")
    # Un report scritto a mano puo' avere `anchors` non-dict: `riepilogo` lo
    # tollera, `--review` deve farlo senza traceback.
    anchors: dict[str, object] = (
        cast("dict[str, object]", grezzo_anchors) if isinstance(grezzo_anchors, dict) else {}
    )
    # `mentions_off=[]` e' un dato: la run ha confrontato gli annunci e non ne
    # ha trovati fuori posto. `mentions_off` assente e' un report VECCHIO, che
    # va ricalcolato. Con `if not items` i due casi coincidevano e la revisione
    # stampava "(il report non li aveva salvati...)" anche quando la run ne
    # aveva salvati 0 — facendo credere che il controllo non fosse girato.
    mentions_salvati = "mentions_off" in anchors
    grezzo_items = anchors.get("mentions_off")
    items = [
        dict(item)
        for item in cast("list[object]", grezzo_items or [])
        if isinstance(item, dict)
    ]
    ricalcolati = False
    if not mentions_salvati:
        controllo_non_fatto = True
        # `main()` mette la chiave della trascrizione EFFETTIVA in cima al
        # report (tramite `report.update(notes)`), non dentro `anchors`.
        # Leggerla solo da li' faceva cadere sempre su None e rileggere la
        # trascrizione piu' recente per mtime, che dopo un escalation beam non
        # e' necessariamente quella su cui e' stata costruita la timeline.
        # Il fallback annidato serve solo ai report vecchi che la salvavano
        # dentro `anchors`.
        preferita = cast(
            "str | None",
            report.get("transcript_cache_key") or anchors.get("transcript_cache_key"),
        )
        words = cast(
            "list[Word]",
            latest_transcript_words(
                cache_dir=cache_dir,
                preferred_key=preferita,
            ),
        )
        if words:
            controllo_non_fatto = False
            # Usare solo lo `spoken_offset` (correzione manuale) ignorava
            # quella automatica del mapping: i dubbi veri venivano azzerati.
            # Il report nuovo salva l'offset EFFETTIVO; sui vecchi, con
            # mapping corretto, meglio non dichiarare "tutto a posto".
            effective = anchors.get("effective_offset")
            if effective is None and anchors.get("mapping_corrected"):
                log.warning(
                    "   Report vecchio con correzione automatica del mapping: "
                    "offset non noto, non ricalcolo i dubbi per non falsarli."
                )
                motivo_non_fatto = "offset del mapping automatico non noto"
                controllo_non_fatto = True
            else:
                offset = int(cast("int", effective if effective is not None else (anchors.get("spoken_offset") or 0)))
                items = [
                    {"slide": s, "mention": m, "declared": d}
                    for s, m, d in announcements_off_timeline(
                        words,
                        starts,
                        total_slides_review,
                        slide_offset=offset,
                    )
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
        _format_time(max((_numero_sicuro(seg.get("end")) for seg in segments), default=0.0)),
    )
    transitions = _intero_sicuro(anchors.get("transitions"))
    if transitions:
        log.info(
            "   Confini ancorati nell'ultima run: %s su %s.",
            _intero_sicuro(anchors.get("anchored")),
            transitions,
        )
    # Un confine senza annuncio è una stima, non una misura: nei dubbi sui
    # confini va detto anche quando non c'è nessuna contraddizione, altrimenti
    # "niente da rivedere" suonerebbe come "tutto misurato".
    senza_ancora = sorted(
        _intero_sicuro(s)
        for s in cast("list[object]", anchors.get("unanchored_slides") or [])
    )
    if senza_ancora:
        log.info(
            "   Confini stimati dal contenuto (nessun annuncio 'slide N'): %s.",
            _slide_list_text(senza_ancora),
        )
    if not items and controllo_non_fatto:
        # NON e' un esito pulito: il controllo non e' girato (report vecchio
        # senza la chiave e trascrizione assente/illeggibile). Dirlo evita
        # che l'utente creda che i confini siano stati verificati.
        log.warning(
            "   [Revisione] Controllo annunci/confini NON eseguito: %s. "
            "Rilancia la pipeline per ricostruire il referto.",
            motivo_non_fatto or "report senza 'mentions_off' e nessuna trascrizione leggibile in cache",
        )
        return 3
    if not items:
        log.info(
            "   Nessuna contraddizione fra gli annunci 'slide N' e i confini "
            "dichiarati: niente da rivedere.%s",
            " (controllo ricalcolato dalle parole in cache)" if ricalcolati else "",
        )
        # Nessun dubbio anche in `--review`: le anteprime della vecchia run
        # vanno comunque via, altrimenti restano a indicare un problema che
        # non c'e' piu'.
        _write_boundary_previews(video_path, [], cache_dir / "review")
    elif video_path.exists():
        _write_boundary_previews(video_path, items, cache_dir / "review")
    else:
        log.info("   Video assente (%s): i dubbi restano, senza anteprima.", video_path)
        # Nessun video: ogni anteprime della run precedente e' di un altro
        # artefatto. Si pulisce e si tolgono i collegamenti, altrimenti il
        # comando dice "senza anteprima" e poi stampa "anteprima: <path>".
        _write_boundary_previews(video_path, [], cache_dir / "review")
        for item in items:
            item.pop("preview", None)
    if ricalcolati:
        log.info(
            "   (il report non li aveva salvati: ricalcolati dalle parole in cache)"
        )
    if items:
        log.info("   Da controllare a mano:")
        for item in items:
            log.info("     - %s", _boundary_doubt_text(item))

    # Le anteprime appena create vanno nella cache: senza questa scrittura
    # `--review` le genera ma il referto e la pagina restano quelli della run
    # precedente, senza i collegamenti ai file (proprio cio' che il commento
    # di `_write_boundary_previews` dice essere obbligatorio). Vengono salvate
    # anche le voci ricalcolate: la revisione successiva non deve rifare il
    # confronto sulle parole in cache. Il report e' quello letto all'inizio,
    # con `mentions_off` aggiornato dalle copie che hanno ricevuto `preview`.
    if items or ricalcolati:
        anchors["mentions_off"] = items
        report["anchors"] = anchors
        try:
            save_sync_report(report, cache_dir=cache_dir)
            # `scrivi_riepilogo` ingoia gli errori e restituisce None: senza
            # controllare il ritorno, un riepilogo NON scritto veniva dato per
            # successo e restavano i collegamenti della run precedente.
            scritto = scrivi_riepilogo(report, video_path, video_presente=video_path.exists())
        except OSError as e:
            log.error("   [Revisione] Impossibile salvare report/riepilogo (%s).", e)
            return 1
        if scritto is None:
            log.error("   [Revisione] Riepilogo non scritto: non dichiaro successo.")
            return 1
        # `save_sync_report` ingoia gli OSError: verificare il file sul disco
        # e' l'unico modo di non dichiarare successo quando non e' successo.
        try:
            riletto = json.loads((cache_dir / "sync_report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            riletto = None
        if riletto != report:
            log.error("   [Revisione] Il report non e' stato aggiornato su disco.")
            return 1
    log.info("=" * 70)
    return 0
