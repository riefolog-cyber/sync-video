#!/usr/bin/env python3
"""
Controllo aggiornamenti pacchetti al primo avvio (solo notifica).

Verifica via PyPI JSON API (in parallelo) se i pacchetti usati dal progetto
hanno versioni più recenti di quelle installate. NON installa nulla:
segnala solo e rispetta le versioni pinnate in requirements.txt.

- Risultati cachati in ``.cache/updates_check.json`` (TTL configurabile)
  per non battere PyPI a ogni avvio.
- Errori di rete silenziosi: se PyPI non è raggiungibile, salta senza bloccare.
"""

import importlib.metadata
import json
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from typing import Any

from config import BASE_DIR, CACHE_DIR, atomic_write_text, log

UPDATES_CACHE = CACHE_DIR / "updates_check.json"
DEFAULT_UPDATE_TTL_HOURS = 6.0  # ri-check di rete ogni N ore

# Pacchetti da controllare: i nomi pip usati dal progetto (requirements.txt +
# dipendenze opzionali del machine setup + dev).
_PACKAGES: tuple[str, ...] = (
    "pymupdf",
    "pillow",
    "pytesseract",
    "pydub",
    "moviepy",
    "numpy",
    "tqdm",
    "fastembed",
    "faster-whisper",
    "requests",
    "openvino",
    "openvino-genai",
    "mypy",
    "ruff",
)

# Pin voluti e documentati: questi NON vanno aggiornati (motivo nel commento).
_PINNED: dict[str, str] = {
    # Pinnato a 0.5.1: le versioni successive usano mean pooling invece di CLS
    # per e5-large, cambiando gli embedding rispetto alla baseline validata.
    "fastembed": "==0.5.1 (pinnato: embedding validati A/B)",
}


def _installed_version(pip_name: str) -> str | None:
    """Versione installata del pacchetto, o None se non presente."""
    try:
        return importlib.metadata.version(pip_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _latest_version_pypi(pip_name: str) -> str | None:
    """Ultima versione su PyPI compatibile con il Python in esecuzione.

    Il campo ``info.version`` è la versione più recente GLOBALE, ma può
    richiedere un Python più nuovo (es. numpy 2.5.x richiede >=3.12): pip
    non la installerebbe mai. Si filtrano quindi le release per
    ``requires_python`` e si restituisce la più alta compatibile. None se
    rete/API fallisce.
    """
    url = f"https://pypi.org/pypi/{pip_name}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "slide2video-update-check/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    return _latest_compatible(data)


def _latest_compatible(data: dict[str, Any]) -> str | None:
    """Ultima versione tra le release compatibili col Python corrente."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    releases = data.get("releases", {})
    best: tuple[Version, str] | None = None
    for version_str, files in releases.items():
        if not files:
            continue
        requires_python = files[0].get("requires_python")
        if requires_python and not SpecifierSet(requires_python).contains(
            f"{sys.version_info.major}.{sys.version_info.minor}"
        ):
            continue
        try:
            version = Version(version_str)
        except InvalidVersion:
            continue
        if best is None or version > best[0]:
            best = (version, version_str)
    return best[1] if best else None


def _is_pinned(pip_name: str) -> bool:
    """True se il pacchetto ha un pin voluto (requirements comment o _PINNED)."""
    if pip_name in _PINNED:
        return True
    req_path = BASE_DIR / "requirements.txt"
    if req_path.exists():
        for line in req_path.read_text(encoding="utf-8").splitlines():
            if re.match(rf"^{re.escape(pip_name)}\s*==\s*\S+", line):
                return True
    return False


def _pin_note(pip_name: str) -> str:
    """Nota sul pin per i pacchetti pinnati."""
    return _PINNED.get(pip_name, "")


def check_updates(ttl_hours: float = DEFAULT_UPDATE_TTL_HOURS) -> list[dict]:
    """Verifica aggiornamenti dei pacchetti usati (con cache TTL).

    Returns:
        Lista di dict: {"name", "installed", "latest", "pinned", "note"} per
        ogni pacchetto con una versione più recente disponibile.
    """
    cache = _read_cache()
    now = time.time()
    if cache and now - cache.get("ts", 0) < ttl_hours * 3600:
        log.debug("   Check aggiornamenti: cache valida (%.1fh).", ttl_hours)
        return list(cache.get("outdated", []))

    outdated: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_latest_version_pypi, p): p for p in _PACKAGES}
        for fut in as_completed(futures):
            pip_name = futures[fut]
            latest = fut.result()  # _latest_version_pypi non solleva mai (None su errore)
            installed = _installed_version(pip_name)
            if latest is None or installed is None or latest == installed:
                continue
            pinned = _is_pinned(pip_name)
            outdated.append(
                {
                    "name": pip_name,
                    "installed": installed,
                    "latest": latest,
                    "pinned": pinned,
                    "major": _is_major_jump(installed, latest),
                    "note": _pin_note(pip_name),
                }
            )

    outdated.sort(key=lambda d: d["name"].lower())
    _write_cache({"ts": now, "outdated": outdated})
    return outdated


def print_updates(outdated: list[dict]) -> None:
    """Stampa la notifica aggiornamenti (solo se ce ne sono)."""
    if not outdated:
        log.info("   ✅ Tutti i pacchetti usati sono aggiornati.")
        return
    log.info("   📦 Sono disponibili aggiornamenti per %d pacchetto/i:", len(outdated))
    for d in outdated:
        suffix = f" — ⚠️ {d['note']}" if d["pinned"] and d["note"] else ""
        suffix = " — 🔒 pinnato" if d["pinned"] and not d["note"] else suffix
        suffix = " — ⚠️ major version" if d.get("major") and not d["pinned"] else suffix
        log.info("      %s: %s -> %s%s", d["name"], d["installed"], d["latest"], suffix)
    log.info("      Aggiorna manualmente con: pip install -U <pacchetto> (verifica i pinnati e le major).")


def _is_major_jump(installed: str, latest: str) -> bool:
    """True se latest salta la major version rispetto a installed.

    Un salto di major (es. Pillow 10->12) spesso introduce breaking change
    (regressioni come il bug di ri-salvataggio PNG di Pillow 12). Gli upgrade
    automatici si limitano a minor/patch; le major vanno verificate a mano.
    """
    try:
        from packaging.version import Version

        return Version(latest).major > Version(installed).major
    except Exception:  # versioni non parseabili -> cautelativo
        return True


def _upgradable(outdated: list[dict]) -> list[dict]:
    """Pacchetti aggiornabili in automatico.

    Esclusi: i pinnati e i salti di major version (che vanno valutati a mano).
    """
    return [d for d in outdated if not d.get("pinned") and not d.get("major")]


# Pinnati che possono essere testati A/B prima dell'aggiornamento: se il test
# conferma che la versione candidata produce embedding equivalenti, il pinnato
# viene incluso nell'aggiornamento automatico; altrimenti resta pinnato.
_AB_TESTABLE_PINNED: tuple[str, ...] = ("fastembed",)


def _pinned_with_update(outdated: list[dict]) -> list[dict]:
    """Pinnati con una versione più recente disponibile e testabili A/B."""
    return [d for d in outdated if d["pinned"] and d["name"] in _AB_TESTABLE_PINNED]


def _run_pinned_ab_test(pkg: dict) -> str | None:
    """Esegue il test A/B per un pinnato testabile. Ritorna il verdetto o None.

    Riutilizza il report già salvato in ``.cache/fastembed_ab.json`` se
    riguarda la stessa versione candidata e lo stesso modello: il test
    completo crea una venv temporanea e installa fastembed candidato, quindi
    ripeterlo a ogni avvio sarebbe lento.
    """
    from config import DEFAULT_EMBEDDING_CACHE_DIR, DEFAULT_EMBEDDING_MODEL

    candidate = pkg["latest"]
    report_path = CACHE_DIR / "fastembed_ab.json"
    try:
        if report_path.exists():
            cached = json.loads(report_path.read_text(encoding="utf-8"))
            if (
                cached.get("candidate") == candidate
                and cached.get("model") == DEFAULT_EMBEDDING_MODEL
                and cached.get("verdict") in ("EQUIVALENTE", "DIVERGENTE")
            ):
                log.info("   🧪 Test A/B %s (%s): riutilizzo report cache.", pkg["name"], candidate)
                return str(cached["verdict"])
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    try:
        import check_fastembed_upgrade as ab  # type: ignore[import-not-found]
    except Exception as e:
        log.warning("   ⚠️ Impossibile eseguire il test A/B per %s: %s", pkg["name"], e)
        return None

    log.info("   🧪 Test A/B isolato per %s (%s -> %s)...", pkg["name"], pkg["installed"], candidate)
    texts = ab.collect_real_texts()
    if texts is None or not texts["slides"] or not texts["blocks"]:
        log.warning("   ⚠️ Test A/B saltato: nessun dato reale in .cache.")
        return None

    all_texts = texts["slides"] + texts["blocks"]
    base = ab.baseline_embeddings(all_texts, model=DEFAULT_EMBEDDING_MODEL)
    if base is None:
        log.warning("   ⚠️ Test A/B saltato: baseline non calcolata.")
        return None
    cand, _ = ab.candidate_embeddings(
        all_texts, candidate, model=DEFAULT_EMBEDDING_MODEL,
        cache_dir=DEFAULT_EMBEDDING_CACHE_DIR,
    )
    if cand is None:
        log.warning("   ⚠️ Test A/B saltato: candidata non calcolata.")
        return None

    res = ab.compare(base, cand, n_slides=len(texts["slides"]))
    log.info(
        "      coseno medio %.3f | decision match %.2f (timeline reale) | argmax %.2f (%d blocchi) -> %s",
        res["cosine_mean"], res["decision_match"], res["argmax_match"], int(res["n_blocks"]), res["verdict"],
    )
    with suppress(OSError):
        report_path.write_text(
            json.dumps({**res, "candidate": candidate, "model": DEFAULT_EMBEDDING_MODEL}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return str(res["verdict"])


def _pip_upgrade(packages: list[str]) -> bool:
    """Aggiorna i pacchetti indicati (pip install -U). True se riuscito."""
    try:
        log.info("   ⏳ pip install -U %s ...", " ".join(packages))
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-U", *packages],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
        )
        log.info("   ✅ Aggiornati: %s", ", ".join(packages))
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.warning("   ❌ Aggiornamento fallito: %s", e)
        return False


def _ask_yes_no(prompt: str) -> bool:
    """Chiede conferma all'utente (S/N). True se risponde sì.

    Svuota lo stdout prima di leggere: quando Python è avviato da un .bat la
    stdout è bufferizzata e il prompt di `input()` non comparirebbe finché il
    buffer non si svuota (i log di loguru, su stderr, invece compaiono subito).
    """
    try:
        sys.stdout.write(f"   {prompt} [s/N]: ")
        sys.stdout.flush()
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("s", "si", "y", "yes")


def run_update_check(
    ttl_hours: float = DEFAULT_UPDATE_TTL_HOURS,
    ask_to_update: bool = True,
) -> None:
    """Entry point: check + notifica; se richiesto chiede S/N e aggiorna.

    Se ``ask_to_update`` è True e ci sono aggiornamenti NON pinnati, chiede
    all'utente se installarli in automatico. I pinnati non vengono mai toccati.
    """
    log.info("🔍 Controllo aggiornamenti pacchetti (PyPI)...")
    outdated = check_updates(ttl_hours=ttl_hours)
    print_updates(outdated)

    upgradable = _upgradable(outdated)
    pinned_upd = _pinned_with_update(outdated)
    if not upgradable and not pinned_upd:
        return

    if not ask_to_update:
        log.info("   Aggiornamento automatico disabilitato (--no-update).")
        return

    major = [d["name"] for d in outdated if d.get("major") and not d["pinned"]]
    if major:
        log.info("   ⚠️ Salti di major version NON aggiornati automaticamente: %s", ", ".join(major))

    names = [d["name"] for d in upgradable]

    # Per i pinnati testabili, il verdetto del test A/B decide se includerli:
    # EQUIVALENTE -> si aggiorna; DIVERGENTE/None -> resta pinnato.
    for d in pinned_upd:
        verdict = _run_pinned_ab_test(d)
        if verdict == "EQUIVALENTE":
            log.info("   ✅ Test A/B: %s è equivalente -> incluso nell'aggiornamento.", d["name"])
            names.append(d["name"])
        elif verdict == "DIVERGENTE":
            log.info("   🔒 Test A/B: %s resta pinnato (embedding divergenti).", d["name"])
        else:
            log.info("   🔒 %s resta pinnato (test non disponibile).", d["name"])

    if not names:
        log.info("   Nessun pacchetto da aggiornare.")
        return

    if _ask_yes_no(f"Aggiornare {len(names)} pacchetto/i ({', '.join(names)})?"):
        _pip_upgrade(names)
        # Invalida la cache così al prossimo avvio riverifica da zero
        UPDATES_CACHE.unlink(missing_ok=True)
    else:
        log.info("   Ok, nessun aggiornamento installato.")


def _read_cache() -> dict:
    try:
        data = json.loads(UPDATES_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _write_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(UPDATES_CACHE, json.dumps(data, ensure_ascii=False, indent=2))
