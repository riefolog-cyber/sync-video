#!/usr/bin/env python3
"""Prova sul MATERIALE REALE: la timeline prodotta e' quella giusta?

Tutti i test della suite sono finti: l'OCR di un PDF finto, la trascrizione di
un audio finto, un video costruito al volo. Le cose che contano — le ancore,
i confini, il riconoscimento — si verificano solo sul materiale vero. Questo
script lancia la pipeline sul PDF e sull'audio della cartella e confronta la
timeline con un RIFERIMENTO (il file golden): e' il controllo che dice "il
programma ancora produce quello che produceva".

Uso:
    python src/scripts/prova_materiale_reale.py             # verifica
    python src/scripts/prova_materiale_reale.py --aggiorna  # nuovo riferimento

Esiti:
    0  la timeline coincide con il riferimento (o il riferimento e' appena
       stato aggiornato);
    1  la timeline e' CAMBIATA: un confine si e' spostato (e di quanto);
    2  NON VERIFICABILE: manca il riferimento, oppure il PDF/audio nella
       cartella non sono quelli del riferimento. Non e' un fallimento del
       programma: senza lo stesso materiale il confronto non significherebbe
       niente. Prima usciva 1 anche qui, quindi un checkout nuovo sembrava
       rotto.

Il materiale (PDF e audio) NON e' versionato insieme al riferimento: sono
file dell'utente, esclusi da git. Il riferimento vale per la macchina che ha
il materiale con quegli hash; altrove la prova si limita a dirlo.

NOTA: la prima volta su un materiale nuovo il lavoro e' tutto (OCR +
trascrizione: qualche minuto). Dopo, le cache rendono la prova di qualche
secondo.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Deve stare PRIMO: mette la radice del progetto nel sys.path (vedi
# _bootstrap.py).
from _bootstrap import CODICE, RADICE

sys.path.insert(0, str(CODICE))

from config import DEFAULT_PDF, atomic_write_text
from pipeline_cache import file_hash

CACHE = RADICE / ".cache"
RIFERIMENTO = Path(__file__).resolve().parent / "materiale_riferimento.json"

# Tolleranza sul confronto dei confini (secondi): le ancore sono esatte, ma un
# millisecondo di arrotondamento non deve far fallire la prova.
TOLLERANZA = 0.05

# Esiti (vedi il docstring del modulo).
ESITO_OK = 0
ESITO_CAMBIATA = 1
ESITO_NON_VERIFICABILE = 2


def _materiale() -> tuple[Path, Path]:
    """Il PDF e l'audio che la pipeline userebbe (le sue scelte, non le mie)."""
    from pipeline_inputs import find_audio_file

    pdf = RADICE / DEFAULT_PDF
    if not pdf.exists():
        pdf = next(
            (p for p in sorted(RADICE.glob("*.pdf")) if p.is_file()), pdf
        )
    audio = find_audio_file(RADICE)
    if audio is None:
        raise SystemExit(
            "Nessun file audio nella cartella: metti il podcast (m4a/mp3/wav) "
            "accanto al PDF e rilancia."
        )
    return pdf, audio


def _pipeline() -> None:
    """Lancia la pipeline sul materiale (solo timeline: il video non serve)."""
    import main

    main.main(
        [
            "--dry-run",
            # Il riferimento e' la timeline senza aggancio: il confronto deve
            # essere fra cose uguali, lo snap ha una sua prova dedicata.
            "--no-snap-pauses",
            "--no-update-check",
            "--no-auto-setup",
            "--no-confirm",
        ]
    )


def _timeline() -> list[dict[str, object]]:
    """I segmenti della timeline prodotta dalla run."""
    path = CACHE / "timeline_finale.json"
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(
            f"Nessuna timeline in {path}: la run non e' andata a fine."
        ) from e
    return list(dati) if isinstance(dati, list) else []


def _ancore() -> int:
    """Quante ancore 'slide N' la run ha usato (dal report)."""
    try:
        report = json.loads((CACHE / "sync_report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    ancore = report.get("anchors") if isinstance(report, dict) else None
    if not isinstance(ancore, dict):
        return 0
    try:
        return int(ancore.get("anchored") or 0)
    except (TypeError, ValueError):
        return 0


def _differenze(
    attuale: list[dict[str, object]], riferimento: list[dict[str, object]]
) -> list[str]:
    """Le differenze fra la timeline di adesso e quella del riferimento."""
    if len(attuale) != len(riferimento):
        return [
            f"numero di segmenti diverso: {len(attuale)} adesso, "
            f"{len(riferimento)} nel riferimento"
        ]
    problemi: list[str] = []
    for ora, prima in zip(attuale, riferimento, strict=True):
        slide_ora = ora.get("slide")
        slide_prima = prima.get("slide")
        if slide_ora != slide_prima:
            problemi.append(f"slide {slide_ora} dove il riferimento ha {slide_prima}")
            continue
        try:
            inizio_ora = float(str(ora.get("start") or 0.0))
            inizio_prima = float(str(prima.get("start") or 0.0))
        except (TypeError, ValueError):
            problemi.append(f"segmento {slide_ora} non confrontabile")
            continue
        scarto = inizio_ora - inizio_prima
        if abs(scarto) > TOLLERANZA:
            problemi.append(
                f"slide {slide_ora}: il confine e' {scarto:+.2f}s rispetto al "
                f"riferimento ({inizio_ora:.2f}s adesso, {inizio_prima:.2f}s prima)"
            )
    return problemi


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aggiorna",
        action="store_true",
        help="scrive il riferimento con la timeline di adesso",
    )
    args = parser.parse_args(argv)

    pdf, audio = _materiale()
    print(f"Materiale: {pdf.name} + {audio.name}")
    print("Lancio la pipeline (solo timeline: il video non serve)...")
    _pipeline()

    segmenti = _timeline()
    ancore = _ancore()
    print(f"Timeline: {len(segmenti)} segmenti, {ancore} ancore 'slide N' usate.")

    materiale = {
        "pdf": file_hash(pdf),
        "audio": file_hash(audio),
    }
    riferimento: dict[str, object] = {}
    if RIFERIMENTO.exists():
        try:
            caricato = json.loads(RIFERIMENTO.read_text(encoding="utf-8"))
            if isinstance(caricato, dict):
                riferimento = caricato
        except (OSError, json.JSONDecodeError):
            riferimento = {}

    if args.aggiorna:
        # Scrittura atomica come tutto il resto del progetto: un crash a meta'
        # lascerebbe un riferimento mezzo scritto che poi "legge" come
        # materiale diverso.
        atomic_write_text(
            RIFERIMENTO,
            json.dumps(
                {
                    "materiale": materiale,
                    "ancore": ancore,
                    "segmenti": segmenti,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        print(f"Riferimento aggiornato: {RIFERIMENTO.name}")
        return ESITO_OK

    if not riferimento:
        print(
            "\nNessun riferimento: non c'e' niente contro cui confrontare.\n"
            "Crealo con:  python src/scripts/prova_materiale_reale.py --aggiorna"
        )
        return ESITO_NON_VERIFICABILE

    if riferimento.get("materiale") != materiale:
        print(
            "\nIl materiale NON e' quello del riferimento: il confronto non "
            "significherebbe niente, quindi non e' un errore del programma.\n"
            "Se il materiale e' cambiato di proposito, allinea il riferimento "
            "con --aggiorna."
        )
        return ESITO_NON_VERIFICABILE

    _segmenti_rif: list[dict[str, object]] = []
    _grezzi = riferimento.get("segmenti")
    if isinstance(_grezzi, list):
        _segmenti_rif = [x for x in _grezzi if isinstance(x, dict)]
    problemi = _differenze(segmenti, _segmenti_rif)
    if not problemi:
        print(
            f"\nOK: la timeline e' IDENTICA al riferimento "
            f"({len(segmenti)} segmenti, {ancore} ancore)."
        )
        return ESITO_OK
    print("\nTIMELINE CAMBIATA rispetto al riferimento:")
    for problema in problemi:
        print(f"  - {problema}")
    return ESITO_CAMBIATA


if __name__ == "__main__":
    raise SystemExit(main())
