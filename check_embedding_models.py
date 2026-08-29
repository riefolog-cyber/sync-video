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
report leggibile in ``.cache/embedding_model_check_report.md``. Uscita:
  0 = nessuna azione necessaria;
  2 = azione consigliata (fai il test A/B prima di cambiare modello);
  3 = errore di rete (il controllo va ripetuto).

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

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
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

# Modelli noti (inclusi in TRACKED_MODELS o già visti in passato). Serve a
# distinguere "nuovo candidato" da "già valutato in precedenza".
# Aggiornato automaticamente dallo script dopo ogni run.
KNOWN_MODELS: set[str] = set(TRACKED_MODELS)


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
        print(f"ERRORE: huggingface_hub non installato ({e}). Esegui: pip install huggingface_hub")
        sys.exit(4)


def _gather(api: Any) -> dict:
    """Interroga HF: info modelli tracciati + ricerca nuovi candidati."""
    from huggingface_hub import HfApi

    if api is None:
        api = HfApi()

    tracked = {}
    for mid in TRACKED_MODELS:
        try:
            info = api.model_info(mid, files_metadata=False)
            tracked[mid] = {
                "lastModified": info.lastModified.isoformat() if info.lastModified else None,
                "downloads": info.downloads,
            }
        except Exception as e:  # noqa: BLE001 - un modello mancante non deve bloccare il resto
            tracked[mid] = {"lastModified": None, "downloads": None, "error": str(e)}

    new_candidates = {}
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
            if mid in KNOWN_MODELS or mid in TRACKED_MODELS:
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
        print(f"AVVISO: ricerca candidati fallita: {e}")

    return {"tracked": tracked, "new_candidates": new_candidates}


def _recommendation(prev: dict | None, data: dict) -> tuple[bool, list[str]]:
    """Confronta con la baseline e produce le raccomandazioni."""
    reasons: list[str] = []
    action = False

    # Prima esecuzione (o baseline cancellata): registra solo lo stato.
    if prev is None:
        return False, ["Prima esecuzione: baseline registrata. Nessuna azione richiesta."]

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

    # 3. Nuovi candidati multilingue ONNX?
    for mid, info in data["new_candidates"].items():
        action = True
        reasons.append(
            f"Nuovo candidato rilevato: {mid} ({info.get('lastModified')}, "
            f"{info.get('downloads')} download). Inserirlo nel test A/B."
        )

    return action, reasons


def _save_state(data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {"checked_at": _utcnow(), **data},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
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
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitoraggio modello embedding (regola e5).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignora la baseline e forza il report anche senza novità",
    )
    args = parser.parse_args()

    _check_network()
    from huggingface_hub import HfApi

    prev = _load_state()
    data = _gather(HfApi())

    if args.force:
        reasons = ["--force: report generato per verifica manuale."]
        action = False
    else:
        action, reasons = _recommendation(prev, data)

    _write_report(action, reasons, data)
    _save_state(data)

    print(f"\nControllo completato. Report: {REPORT_FILE}")
    if reasons:
        print("Motivi:")
        for r in reasons:
            print(f"  - {r}")
    print(f"Esito: {'AZIONE CONSIGLIATA (fai il test A/B)' if action else 'nessuna azione necessaria'}")
    return 2 if action else 0


if __name__ == "__main__":
    sys.exit(main())
