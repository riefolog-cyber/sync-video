#!/usr/bin/env python3
"""Monitoraggio periodico della regola di scelta del modello embedding.

Adempie alla REGOLA DI SCELTA MODELLO documentata in config.py:
"e5-large è il modello DA PREFERIRE; prima di cambiare DEFAULT_EMBEDDING_MODEL,
ripetere il test A/B completo. Tenere d'occhio i rilasci di
intfloat/multilingual-e5 e i nuovi sentence-embedding multilingue ONNX più
efficienti."

Questo script NON modifica config.py né il modello: verifica a ogni esecuzione
se ci sono segnali che rendono opportuno (ri)fare il test A/B, ovvero:

  1. intfloat/multilingual-e5-large (modello preferito) è stato aggiornato
     dopo l'ultimo controllo;
  2. è comparsa una nuova versione/quantizzazione ONNX del modello preferito
     (es. *-ONNX, *-instruct) o di un modello multilingue affine;
  3. sono usciti nuovi sentence-embedding multilingue ONNX (candidati).

Scrive un report in ``.cache/embedding_model_check.json`` (baseline) e un
report leggibile in ``.cache/embedding_model_check_report.md``. I candidati
già visti finiscono nello stato: non scatta per sempre lo stesso allarme.
Uscita:
  0 = nessuna azione necessaria;
  2 = azione consigliata (fai il test A/B prima di cambiare modello);
  3 = non verificabile (rete o dipendenza mancante: il controllo va ripetuto).

Uso:
  python check_embedding_models.py            # esegui controllo ora
  python check_embedding_models.py --force    # ignora baseline, forza report
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Deve stare PRIMO: mette la radice nel sys.path. Solo config (che e' leggero)
# viene importato subito dopo, per leggere la CACHE_DIR effettiva; senza il
# bootstrap ne i test ne lo script troverebbero i moduli da scripts/.
from _bootstrap import RADICE

# La radice la dà il bootstrap: `__file__` sta in scripts/, quindi `parent`
# qui sarebbe la cartella sbagliata (e la cache finirebbe in scripts/.cache).
BASE_DIR = RADICE
# Lo stato segue la cache della pipeline (spostabile con SYNC_VIDEO_CACHE_DIR):
# scrivere sempre RADICE/".cache" lasciava il monitoraggio convinto che il
# modello non fosse mai stato controllato su quei PC.
from config import CACHE_DIR  # noqa: E402 - dopo _bootstrap, come gli altri moduli

STATE_FILE = CACHE_DIR / "embedding_model_check.json"
REPORT_FILE = CACHE_DIR / "embedding_model_check_report.md"

PREFERRED_MODEL = "intfloat/multilingual-e5-large"

# Modelli "affini" al preferito: varianti ONNX/quantizzate dello stesso e5 o
# modelli multilingue usati come fallback/alternativa nel progetto.
TRACKED_MODELS = [
    "intfloat/multilingual-e5-large",
    "intfloat/multilingual-e5-large-instruct",
    "intfloat/multilingual-e5-base",
    "intfloat/multilingual-e5-small",
    "Xenova/multilingual-e5-large",
    "Qdrant/multilingual-e5-large-onnx",
    "onnx-community/multilingual-e5-base-ONNX",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
]

# Query HF per individuare nuovi candidati multilingue ONNX.
SEARCH_QUERY = "multilingual sentence embeddings onnx"
SEARCH_LIMIT = 30

# Modelli noti al primo avvio (inclusi in TRACKED_MODELS). Serve a distinguere
# "nuovo candidato" da "già valutato in precedenza". I modelli visti dopo
# vengono persistiti nello stato e si sommano a questo insieme: la baseline
# di codice resta immutabile, lo stato cresce con le run.
KNOWN_MODELS: set[str] = set(TRACKED_MODELS)
KNOWN_MODELS_KEY = "known_models"


class NetworkError(RuntimeError):
    """Controllo non verificabile: HF irraggiungibile o libreria mancante."""


def _is_network_error(exc: BaseException) -> bool:
    """Riconosce un errore di rete/HTTP 5xx (non un modello 404)."""
    import urllib.error

    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    if "HfHubHTTPError" in type(exc).__name__ or "HTTPError" in type(exc).__name__:
        response = getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
        return code is None or int(code) >= 500
    msg = str(exc).lower()
    return any(
        k in msg
        for k in ("connection", "timeout", "timed out", "unreachable", "network", "name or service")
    )


def _known_models(state: dict | None) -> set[str]:
    """Modelli già noti: base di codice + quelli persistiti nello stato."""
    saved = (state or {}).get(KNOWN_MODELS_KEY) or []
    return set(KNOWN_MODELS) | {str(m) for m in saved}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _model_updated(latest: str | None, baseline: str | None) -> bool:
    """True se il modello è cambiato rispetto alla baseline (se presente)."""
    if not latest:
        return False
    if not baseline:
        return True  # prima osservazione: considera "nuovo segnale" solo se --force non attivo? vedi chiamante
    try:
        return datetime.fromisoformat(latest) > datetime.fromisoformat(baseline)
    except ValueError:
        return latest != baseline


def _check_network() -> None:
    try:
        from huggingface_hub import HfApi  # noqa: F401
    except ImportError as e:
        raise NetworkError(f"huggingface_hub non installato ({e}): pip install huggingface_hub") from e


def _gather(api: Any, known: set[str] | None = None) -> dict:
    """Interroga HF: info modelli tracciati + ricerca nuovi candidati.

    Un errore di rete che impedisce di leggere TUTTI i modelli tracciati rende
    il controllo non verificabile: solleva NetworkError invece di spacciare
    una risposta vuota per "nessuna azione". Un singolo 404 resta tollerato.
    """
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi()
    known = set(KNOWN_MODELS) if known is None else known

    tracked = {}
    network_errors: list[str] = []
    for mid in TRACKED_MODELS:
        try:
            info = api.model_info(mid, files_metadata=False)
            tracked[mid] = {
                "lastModified": info.lastModified.isoformat() if info.lastModified else None,
                "downloads": info.downloads,
            }
        except Exception as e:  # noqa: BLE001 - un modello mancante non deve bloccare il resto
            tracked[mid] = {"lastModified": None, "downloads": None, "error": str(e)}
            if _is_network_error(e):
                network_errors.append(f"{mid}: {e}")

    new_candidates = {}
    search_network_error = False
    try:
        models = api.list_models(
            search=SEARCH_QUERY,
            sort="lastModified",
            direction=-1,
            limit=SEARCH_LIMIT,
        )
        for m in models:
            mid = m.modelId
            tags = m.tags or []
            # I modelli già visti non sono più "nuovi": segnalali una volta.
            if mid in known or mid in TRACKED_MODELS:
                continue
            # Interessano solo embedding multilingue ONNX/quantizzati.
            name_l = mid.lower()
            tags_l = {t.lower() for t in tags}
            is_onnx = any("onnx" in t for t in tags_l) or "onnx" in name_l
            is_multiling = "multilingual" in name_l or "multilingual" in tags_l
            if not (is_onnx and is_multiling):
                continue
            new_candidates[mid] = {
                "lastModified": m.lastModified.isoformat() if m.lastModified else None,
                "downloads": m.downloads,
            }
    except Exception as e:  # noqa: BLE001
        if _is_network_error(e):
            search_network_error = True
            network_errors.append(f"ricerca candidati: {e}")
        else:
            print(f"AVVISO: ricerca candidati fallita: {e}")

    tracked_ok = any(info.get("lastModified") or info.get("downloads") is not None for info in tracked.values())
    if network_errors and (search_network_error or not tracked_ok):
        raise NetworkError("; ".join(network_errors))

    return {"tracked": tracked, "new_candidates": new_candidates}


def _recommendation(
    prev: dict | None, data: dict, known: set[str] | None = None
) -> tuple[bool, list[str]]:
    """Confronta con la baseline e produce le raccomandazioni.

    I candidati già noti (baseline di codice o run precedenti) non generano
    altro rumore: il primo avviso è sufficiente.
    """
    reasons: list[str] = []
    action = False

    # Prima esecuzione (o baseline cancellata): registra solo lo stato.
    if prev is None:
        return False, ["Prima esecuzione: baseline registrata. Nessuna azione richiesta."]

    known = _known_models(prev) if known is None else known

    # 1. Modello preferito aggiornato?
    pref = data["tracked"].get(PREFERRED_MODEL, {})
    prev_pref = prev.get("tracked", {}).get(PREFERRED_MODEL, {}) if prev else {}
    if _model_updated(pref.get("lastModified"), prev_pref.get("lastModified")):
        action = True
        reasons.append(
            f"{PREFERRED_MODEL} è stato aggiornato (ultima modifica: {pref.get('lastModified')}). "
            "Rifai il test A/B per confermare che resti il modello migliore."
        )

    # 2. Varianti ONNX del preferito / modelli affini aggiornati?
    for mid, info in data["tracked"].items():
        if mid == PREFERRED_MODEL:
            continue
        prev_info = (prev or {}).get("tracked", {}).get(mid, {}) if prev else {}
        if info.get("lastModified") and _model_updated(info["lastModified"], prev_info.get("lastModified")):
            action = True
            reasons.append(
                f"{mid} aggiornato ({info['lastModified']}, {info.get('downloads')} download). "
                "Può essere un candidato: valuta il test A/B."
            )

    # 3. Nuovi candidati multilingue ONNX? (solo se non li abbiamo già visti)
    for mid, info in data["new_candidates"].items():
        if mid in known:
            continue
        action = True
        reasons.append(
            f"Nuovo candidato rilevato: {mid} ({info.get('lastModified')}, "
            f"{info.get('downloads')} download). Inserirlo nel test A/B."
        )

    return action, reasons


def _atomic_write(path: Path, text: str) -> None:
    """Scrittura atomica: import lazily, come gli altri moduli core di questo script."""
    from config import atomic_write_text

    atomic_write_text(path, text)


def _save_state(data: dict, known: set[str] | None = None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Persiste i modelli già visti: al secondo run non deve scattare di nuovo
    # lo stesso "nuovo candidato" (e non va riscritto lo stato se la rete manca).
    models = sorted(set(KNOWN_MODELS) | (known or set()))
    _atomic_write(
        STATE_FILE,
        json.dumps(
            {"checked_at": _utcnow(), KNOWN_MODELS_KEY: models, **data},
            ensure_ascii=False,
            indent=2,
        ),
    )


def _load_state() -> dict | None:
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else None
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _write_report(action: bool, reasons: list[str], data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    lines = [
        "# Controllo modello embedding",
        "",
        f"- Data controllo: {_utcnow()}",
        f"- Modello preferito: {PREFERRED_MODEL}",
        f"- Esito: **{'AZIONE CONSIGLIATA' if action else 'NESSUNA AZIONE'}**",
        "",
    ]
    if reasons:
        lines += ["## Motivi", ""]
        lines += [f"- {r}" for r in reasons]
        lines += [""]
    lines += ["## Modelli tracciati", "", "| Modello | Ultima modifica | Download |", "|---|---|---|"]
    for mid, info in data["tracked"].items():
        lm = info.get("lastModified") or "-"
        dl = info.get("downloads") if info.get("downloads") is not None else "-"
        lines.append(f"| {mid} | {lm} | {dl} |")
    lines += ["", "## Nuovi candidati rilevati", ""]
    if data["new_candidates"]:
        lines.append("| Modello | Ultima modifica | Download |")
        lines.append("|---|---|---|")
        for mid, info in data["new_candidates"].items():
            lm = info.get("lastModified") or "-"
            dl = info.get("downloads") if info.get("downloads") is not None else "-"
            lines.append(f"| {mid} | {lm} | {dl} |")
    else:
        lines.append("Nessun nuovo candidato.")
    lines += [
        "",
        "## Regola di scelta modello (da config.py)",
        "",
        "e5-large è il modello DA PREFERIRE. Prima di cambiare "
        "DEFAULT_EMBEDDING_MODEL, ripetere il test A/B completo verificando: "
        "similarità media, durate bilanciate e zero slide anomale. Un modello "
        "più veloce che abbassa il segnale semantico NON va adottato.",
    ]
    _atomic_write(REPORT_FILE, "\n".join(lines) + "\n")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitoraggio modello embedding (regola e5).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignora la baseline e forza il report anche senza novità",
    )
    args = parser.parse_args(argv)

    try:
        _check_network()
        from huggingface_hub import HfApi

        prev = _load_state()
        known = _known_models(prev)
        data = _gather(HfApi(), known=known)
    except NetworkError as e:
        # Non scrivere uno stato vuoto: un errore di rete NON è "nessuna azione".
        print(f"ERRORE: controllo non verificabile (rete/dipendenza: {e}). Riprova più tardi.", file=sys.stderr)
        print("Esito: NON VERIFICABILE")
        return 3
    except Exception as e:  # noqa: BLE001 - API/JSON inattesi: non è un verdetto
        print(f"ERRORE: controllo non verificabile ({e}). Riprova più tardi.", file=sys.stderr)
        print("Esito: NON VERIFICABILE")
        return 3

    if args.force:
        reasons = ["--force: report generato per verifica manuale."]
        action = False
    else:
        action, reasons = _recommendation(prev, data, known=known)

    # Ogni candidato osservato diventa noto: non si ripete l'allarme.
    known |= set(data["new_candidates"])
    _write_report(action, reasons, data)
    _save_state(data, known=known)

    print(f"\nControllo completato. Report: {REPORT_FILE}")
    if reasons:
        print("Motivi:")
        for r in reasons:
            print(f"  - {r}")
    print(f"Esito: {'AZIONE CONSIGLIATA (fai il test A/B)' if action else 'nessuna azione necessaria'}")
    return 2 if action else 0


if __name__ == "__main__":
    sys.exit(main())
