#!/usr/bin/env python3
"""
Controllo aggiornamenti pacchetti al primo avvio (solo notifica).

Verifica via PyPI JSON API (in parallelo) se i pacchetti usati dal progetto
hanno versioni più recenti di quelle installate. NON installa nulla:
segnala solo e rispetta le versioni pinnate in requirements.txt.

- Risultati cachati in ``.cache/updates_check.json`` (TTL configurabile)
  per non battere PyPI a ogni avvio.
- Errori di rete silenziosi: se PyPI non è raggiungibile, salta senza bloccare.
- Una versione puo' esistere su PyPI ed essere comunque non installabile: PyPI
  dice cosa c'e', non cosa sta in piedi con le dipendenze gia' installate. Le
  voci che il resolver non sceglierebbe si marcano come bloccate (``blocked``),
  si escludono dagli aggiornamenti proposti e non finiscono nell'invito a
  ``pip install -U``: vedi ``_installed_constraints`` e ``_blockers``.
"""

import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from typing import Any

from config import _REQUIRED_PACKAGES, BASE_DIR, CACHE_DIR, _installed_version, atomic_write_text, log

UPDATES_CACHE = CACHE_DIR / "updates_check.json"
DEFAULT_UPDATE_TTL_HOURS = 6.0  # ri-check di rete ogni N ore

# Pacchetti da controllare: i nomi pip usati dal progetto (requirements.txt +
# dipendenze opzionali del machine setup).
#
# mypy e ruff NON sono qui: sono strumenti di sviluppo, non dipendenze di chi
# usa il programma, e stanno in requirements-dev.txt (test_bootstrap tiene
# questa lista uguale a requirements.txt).
_PACKAGES: tuple[str, ...] = (
    "av",
    "pymupdf",
    "pillow",
    "pytesseract",
    "pydub",
    "moviepy",
    "numpy",
    "packaging",
    "tqdm",
    "fastembed",
    "faster-whisper",
    "openvino",
    "openvino-genai",
)

# Pin voluti e documentati: questi NON vanno aggiornati (motivo nel commento).
_PINNED: dict[str, str] = {
    # Pinnato a 0.5.1: le versioni successive usano mean pooling invece di CLS
    # per e5-large, cambiando gli embedding rispetto alla baseline validata.
    "fastembed": "pinnato a 0.5.1: embedding e5-large validati A/B",
}

# Sorgenti dei vincoli dichiarati dal progetto (distinti dalla metadata installata).
_PROJECT_REQ_SOURCE = "requirements.txt"
_CONFIG_REQ_SOURCE = "config._REQUIRED_PACKAGES"

# Moduli da importare nel health-check dopo un upgrade (nome pip -> nome import).
# Quelli non presenti qui si importano col nome pip con i trattini sostituiti.
_IMPORT_NAMES: dict[str, str] = {
    "pillow": "PIL",
    "pymupdf": "pymupdf",
    "faster-whisper": "faster_whisper",
    "openvino-genai": "openvino_genai",
}

# Un verdetto A/B resta spendibile solo per pochi giorni: dopo, testi reali e
# versione installata possono essere cambiati anche se la candidata e' la stessa.
_AB_REPORT_TTL_DAYS = 7


def _latest_version_pypi(pip_name: str) -> str | None:
    """Ultima versione su PyPI INSTALLABILE con il Python in esecuzione.

    Il campo ``info.version`` è la versione più recente GLOBALE, ma può
    richiedere un Python più nuovo (es. numpy 2.5.x richiede >=3.12) o essere
    una pre-release: pip non la installerebbe mai. Si filtra quindi l'elenco
    delle release (vedi ``_latest_compatible``). None se rete/API/versione
    non è determinabile: questa funzione non deve mai sollevare, perché il
    chiamante la interroga dentro un thread pool.
    """
    data = _fetch_pypi_json(pip_name)
    if data is None:
        return None

    try:
        return _latest_compatible(data)
    except Exception as e:  # API/versione inattesa: mai bloccare l'avvio
        log.debug("   Check aggiornamenti: versione di %s non determinabile (%s).", pip_name, e)
        return None


def _fetch_pypi_json(pip_name: str) -> dict[str, Any] | None:
    """JSON delle release di un pacchetto su PyPI. None se rete/API non risponde.

    Non solleva mai: la interroga anche il thread pool del check aggiornamenti, dove
    un'eccezione farebbe fallire il controllo intero. La riusa la diagnostica, che ha
    bisogno dell'elenco COMPLETO delle versioni, non solo della più alta.
    """
    url = f"https://pypi.org/pypi/{pip_name}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "slide2video-update-check/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception:
        return None


def _latest_compatible(data: dict[str, Any]) -> str | None:
    """Ultima versione STABILE e compatibile col Python corrente.

    Il filtro sta in ``_installable_releases`` (regola unica, condivisa con la
    diagnostica che ha bisogno di tutte le versioni). Qui si prende la più alta; a
    parità di versione normalizzata ("1.0" e "1.0.0" sono la stessa versione) resta
    la PRIMA incontrata nell'ordine di PyPI, come prima dell'estrazione del filtro.
    """
    best: tuple[Any, str] | None = None
    for version, version_str in _installable_releases(data):
        if best is None or version > best[0]:
            best = (version, version_str)
    return best[1] if best else None


def _installable_releases(data: dict[str, Any]) -> list[tuple[Any, str]]:
    """(Version, stringa) delle release che pip installerebbe davvero, in ordine di PyPI.

    Regola unica delle release utilizzabili:
    - quelle con tutti i file yanked sono escluse (pip rifiuta una release ritirata);
    - le pre-release sono escluse (senza ``--pre`` pip non le installa: annunciarle
      darebbe un falso aggiornamento, il report direbbe "X -> Y" e pip non farebbe
      nulla);
    - deve esserci almeno una WHEEL non yanked con ``requires_python`` compatibile.

    Un sdist senza metadata non dimostra che la release sia installabile: PyPI
    pubblica il file, ma pip potrebbe non riuscire a costruirlo e, senza
    ``requires_python``, non si puo' nemmeno sapere se il Python corrente e'
    ammesso. Quindi un file senza vincolo, da solo, NON basta: serve una wheel non
    yanked che dichiari esplicitamente un vincolo compatibile. Guardare solo il
    primo file utile sbaglierebbe in entrambi i versi: un file con un vincolo
    troppo stretto puo' far scartare una release che ha anche una wheel perfettamente
    installabile, e un primo file senza vincolo puo' far accettare una release i cui
    altri file sono tutti incompatibili.

    Il confronto usa la versione COMPLETA del Python (3.10.5, non 3.10): con
    ``>=3.10.3`` un 3.10.5 va ammesso, e ``major.minor`` lo escluderebbe a torto.
    """
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    python = platform.python_version()
    releases = data.get("releases", {})
    out: list[tuple[Any, str]] = []
    for version_str, files in releases.items():
        files_validi = [f for f in files if not f.get("yanked")] if files else []
        if not files_validi:
            continue
        try:
            version = Version(version_str)
        except InvalidVersion:
            continue
        if version.is_prerelease:
            continue
        # ALMENO UNA wheel non yanked deve dichiarare un vincolo compatibile:
        # un file senza requires_python (tipico sdist) non basta da solo.
        compatibile = False
        for file in files_validi:
            if not str(file.get("filename", "")).lower().endswith(".whl"):
                continue  # sdist/archivio: senza metadata non ne dimostro l'installabilita'
            requires_python = file.get("requires_python")
            if not requires_python:
                continue  # senza vincolo non si puo' affermare che la wheel sia installabile
            try:
                if SpecifierSet(requires_python).contains(python):
                    compatibile = True
                    break
            except InvalidSpecifier:
                continue  # dichiarazione invalida: quella wheel non e' utilizzabile
        if not compatibile:
            continue
        out.append((version, version_str))
    return out


def _all_compatible_versions(data: dict[str, Any]) -> list[str]:
    """Tutte le versioni installabili di una entry PyPI, dalla più vecchia alla più nuova."""
    return [version_str for _, version_str in sorted(_installable_releases(data))]


def _available_versions(pip_name: str) -> list[str] | None:
    """Tutte le versioni installabili di un pacchetto su PyPI. None se indecidibile.

    None non è "nessuna versione": è "non lo so" (rete assente, API inattesa). La
    diagnostica deve dire questo, non inventare un tetto.
    """
    data = _fetch_pypi_json(pip_name)
    if data is None:
        return None
    try:
        return _all_compatible_versions(data)
    except Exception as e:
        log.debug("   Diagnostica: versioni di %s non determinabili (%s).", pip_name, e)
        return None


def _pinned_requirement(pip_name: str) -> str | None:
    """Lo specifier del pin in requirements.txt per quel pacchetto (es. ``==0.5.1``), o None.

    Serve alla diagnostica: per un pacchetto fermo dal pin, la risposta a "chi lo
    vincola" è requirements.txt, e va mostrata con la versione esatta del pin, non
    come un generico "è pinnato".
    """
    req_path = BASE_DIR / "requirements.txt"
    if req_path.exists():
        for line in req_path.read_text(encoding="utf-8").splitlines():
            trovato = re.match(rf"^{re.escape(pip_name)}\s*==\s*([^\s;#]+)", line, re.IGNORECASE)
            if trovato:
                return f"=={trovato.group(1)}"
    return None


def _is_pinned(pip_name: str) -> bool:
    """True se il pacchetto ha un pin voluto (_PINNED o requirements.txt)."""
    return pip_name in _PINNED or _pinned_requirement(pip_name) is not None


def _is_newer(latest: str, installed: str) -> bool:
    """True se `latest` è una versione STRIETTAMENTE più recente di `installed`.

    Confronto per ordine (PEP 440), non per disuguaglianza di stringhe. Le
    stringhe danno tre falsi positivi che si vedono nella pratica:

    - downgrade: ``"1.26.3" != "1.27.0"`` è vero, ma 1.26.3 non è un upgrade;
    - versione locale: ``"2.5.0" != "2.5.0+cu124"`` è vero, ma la build locale
      è più recente della release e va lasciata stare (succede con torch/cuda,
      openvino e pacchetti vendorizzati);
    - ordinamento lessicografico: ``"1.9.0" > "1.10.0"`` come stringhe.

    Se le due versioni non sono confrontabili (PyPI può rispondere con qualcosa
    che non è una versione PEP 440), si resta sul confronto storico per
    disuguaglianza: è impreciso, ma evita di perdere un aggiornamento reale, e
    segnalare cose che non lo sono è un fastidio minore.
    """
    if latest == installed:
        return False
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(latest) > Version(installed)
        except InvalidVersion:
            return True
    except ImportError:
        return True


# Tipo dei vincoli raccolti dalle dipendenze installate: nome pacchetto ->
# [(chi lo impone, sua versione, specifier richiesto), ...].
_Constraints = dict[str, list[tuple[str, str, str]]]


def _installed_constraints() -> _Constraints:
    """Chi, fra i pacchetti INSTALLATI, vincola cosa: le loro richieste reali.

    Serve a non annunciare come aggiornabile una versione che pip non
    installerebbe: ``info.version`` di PyPI dice cosa esiste, non cosa sta in
    piedi in questo ambiente. Il caso reale (misurato il 2026-10-10): Pillow 12
    esiste, ma fastembed 0.5.1 dichiara ``pillow<11.0.0`` e moviepy 2.2.1
    ``pillow<12.0``, quindi il report annunciava "pillow 10.4.0 -> 12.3.0" con
    l'etichetta "major version" mentre ``pip install -U`` non portava a 12: il
    resolver restava su 10.x, e con la versione fissata a mano il comando
    falliva (``ResolutionImpossible``). Un aggiornamento annunciato e non
    ottenibile e' rumore che si ripete a ogni run, per sempre.

    Una passata sola sulle distribuzioni installate, poi si interroga la mappa:
    rileggere la metadata per ogni pacchetto candidato sarebbe O(candidati x
    distribuzioni). I marker vengono valutati qui, con l'ambiente corrente, cosi'
    un requisito che vale solo su un'altra piattaforma non blocca nulla.
    """
    vincoli: _Constraints = {}
    try:
        from packaging.requirements import Requirement
    except ImportError:  # senza packaging si resta senza vincoli (nessun blocco inventato)
        return vincoli

    for dist in importlib.metadata.distributions():
        try:
            nome = dist.metadata["Name"]
            versione = dist.version
        except Exception:  # metadata illeggibile/senza nome: si salta, non si solleva
            continue
        if not nome:
            continue
        try:
            richieste = dist.requires or []
        except Exception:
            continue
        for testo in richieste:
            try:
                req = Requirement(testo)
                if not req.specifier:
                    continue  # richiesta senza versione: non vieta niente
                if req.marker is not None and not req.marker.evaluate():
                    continue  # requisito che non si applica a questo ambiente
            except Exception:
                continue
            vincoli.setdefault(req.name.lower(), []).append((nome, versione, str(req.specifier)))

    # I cap del progetto (av<19, pillow<11, numpy>=1.26...) sono dichiarati in
    # requirements.txt e in config._REQUIRED_PACKAGES, NON nella metadata delle
    # dipendenze installate. Senza questa aggiunta un upgrade che li viola
    # sembrava installabile e finiva nell'invito a `pip install -U`.
    for nome, vincoli_progetto in _project_constraints().items():
        vincoli.setdefault(nome, []).extend(vincoli_progetto)
    return vincoli


def _project_constraints() -> _Constraints:
    """Vincoli di versione dichiarati dal progetto, nella stessa forma di `_installed_constraints`.

    I pin ``==`` sono esclusi di proposito: sono già la condizione ``pinned`` e il
    test A/B è il modo previsto per sbloccarli. Metterli anche tra i blocchi
    renderebbe quel percorso irraggiungibile (e i test A/B non girerebbero mai).
    """
    try:
        from packaging.requirements import Requirement
    except ImportError:  # senza packaging non si inventa nessun blocco
        return {}

    sorgenti: list[tuple[str, list[str]]] = []
    req_path = BASE_DIR / "requirements.txt"
    if req_path.exists():
        with suppress(OSError):
            sorgenti.append((_PROJECT_REQ_SOURCE, req_path.read_text(encoding="utf-8").splitlines()))
    # Le chiavi di _REQUIRED_PACKAGES sono nomi di import, i VALORI sono i
    # requisiti pip veri (ed e' li' che il bootstrap installa davvero).
    sorgenti.append((_CONFIG_REQ_SOURCE, list(_REQUIRED_PACKAGES.values())))

    out: _Constraints = {}
    for sorgente, righe in sorgenti:
        for riga in righe:
            testo = riga.split("#", 1)[0].strip()
            if not testo:
                continue
            try:
                req = Requirement(testo)
            except Exception:
                continue  # riga non parseabile: non produce un blocco inventato
            if not req.specifier or str(req.specifier).strip().startswith("=="):
                continue
            if req.marker is not None and not req.marker.evaluate():
                continue
            out.setdefault(req.name.lower(), []).append((sorgente, "", str(req.specifier)))
    return out


def _blockers(pip_name: str, candidate: str, vincoli: _Constraints) -> list[str]:
    """Perche' la versione candidata non e' installabile: chi la vieta, e come.

    Restituisce motivi leggibili (lista vuota se nessuno la vieta). Il confronto
    usa gli specifier veri, non la major: ``<11.0.0,>=10.3.0`` rifiuta 12.3.0 e
    accetta 10.4.0, quindi guardare le major sbaglierebbe in entrambi i versi.
    Un pacchetto non blocca se stesso, e una versione illeggibile non accusa
    nessuno: meglio tacere che inventare un blocco.
    """
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        candidata = Version(candidate)
    except Exception:
        return []

    motivi: list[str] = []
    for chi, versione_chi, spec in vincoli.get(pip_name.lower(), []):
        if chi.lower() == pip_name.lower():
            continue
        try:
            if SpecifierSet(spec).contains(candidata, prereleases=True):
                continue
        except Exception:
            continue
        motivo = f"{chi} {versione_chi} impone {spec}" if versione_chi else f"{chi} impone {spec}"
        if motivo not in motivi:
            motivi.append(motivo)
    return motivi


def _pin_note(pip_name: str) -> str:
    """Nota sul pin per i pacchetti pinnati, con la versione attuale di requirements.txt.

    La nota storica dice "pinnato a 0.5.1": dopo che `_aggiorna_pin` ha aggiornato
    il pin (e il file letto al prossimo avvio e' quello nuovo), ripetere il vecchio
    numero sarebbe fuorviante. Se requirements.txt dichiara `==X`, si mostra X e
    si conserva il motivo dopo i due punti; altrimenti resta la nota storica.
    """
    nota = _PINNED.get(pip_name, "")
    if not nota:
        return ""
    pin = _pinned_requirement(pip_name)
    if not pin:
        return nota
    versione = pin[2:]  # pin e' della forma "==0.6.0"
    _, separatore, motivo = nota.partition(": ")
    return f"pinnato a {versione}{separatore}{motivo}"


def check_updates(ttl_hours: float = DEFAULT_UPDATE_TTL_HOURS) -> list[dict]:
    """Verifica aggiornamenti dei pacchetti usati (con cache TTL).

    Returns:
        Lista di dict: {"name", "installed", "latest", "pinned", "major",
        "note", "blocked"} per ogni pacchetto con una versione più recente
        disponibile. ``blocked`` elenca i vincoli delle dipendenze installate
        che quella versione non soddisfa (vuoto se è installabile).
    """
    cache = _read_cache()
    now = time.time()
    if cache and now - cache.get("ts", 0) < ttl_hours * 3600:
        log.debug("   Check aggiornamenti: cache valida (%.1fh).", ttl_hours)
        # La cache puo' essere ancora dentro il TTL ma contenere una versione
        # installata ormai cambiata (upgrade/downgrade fatto fuori dal tool).
        # Rileggere l'installata per ogni voce evita di annunciare "X -> Y"
        # quando X non e' piu' quello che c'e' nell'ambiente.
        cached = list(cache.get("outdated", []))
        valide = [d for d in cached if _installed_version(d.get("name", "")) == d.get("installed")]
        if len(valide) == len(cached):
            return valide
        # Non rinnovare il timestamp con una cache vuota (o piu' corta): e' il
        # bug del health-check fallito. pip ha installato, il controllo e' andato
        # male e la cache e' stata mantenuta: al prossimo avvio la voce sembrerebbe
        # stale e il vecchio codice scriveva ts=now con outdated=[], certificando
        # "tutti aggiornati" per 6 ore senza aver controllato niente. Si invalida
        # la cache e si rifa' il controllo subito, sotto.
        log.debug(
            "   Check aggiornamenti: %d voce/i di cache scartate (installata cambiata): "
            "cache invalidata, controllo ripetuto.",
            len(cached) - len(valide),
        )
        UPDATES_CACHE.unlink(missing_ok=True)
        cache = {}

    outdated: list[dict] = []
    # Due contatori DISTINTI: `unreachable` conta solo i `latest is None` (PyPI
    # non ha risposto), `non_installati` conta gli `installed is None` (pacchetto
    # assente: si salta, non e' un errore di rete). Confonderli faceva scattare
    # l'avviso "PyPI non raggiungibile" quando semplicemente qualcosa non era
    # installato, e faceva passare una rete down come "tutti aggiornati" in cache.
    unreachable = 0
    non_installati = 0
    # I vincoli delle dipendenze installate si leggono UNA volta per tutti i
    # candidati: la scansione della metadata e' la parte costosa.
    vincoli = _installed_constraints()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_latest_version_pypi, p): p for p in _PACKAGES}
        for fut in as_completed(futures):
            pip_name = futures[fut]
            latest = fut.result()  # _latest_version_pypi non solleva mai (None su errore)
            installed = _installed_version(pip_name)
            if installed is None:
                non_installati += 1  # non installato: si salta, non e' un errore di rete
                continue
            if latest is None:
                unreachable += 1
                continue
            # Confronto per ORDINE, non per disuguaglianza di stringhe:
            # `latest != installed` segnalava come "outdated" anche un downgrade
            # (1.27.0 -> 1.26.3) e ogni versione con suffisso locale
            # (2.5.0+cu124 contro 2.5.0), quindi pacchetti gia' aggiornati
            # finivano in _upgradable e l'utente veniva invitato a reinstallarli
            # a ogni run, per sempre.
            if not _is_newer(latest, installed):
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
                    "blocked": _blockers(pip_name, latest, vincoli),
                }
            )

    # Il denominatore e' il numero di pacchetti INSTALLATI: su ARM alcuni pacchetti
    # opzionali non sono presenti, e con la rete down `unreachable` non arrivava mai
    # a len(_PACKAGES): la cache vuota veniva scritta e diceva "tutti aggiornati".
    installati = len(_PACKAGES) - non_installati
    if installati and unreachable >= installati:
        log.warning(
            "   ⚠️ PyPI non raggiungibile (%d/%d pacchetti installati senza risposta): "
            "impossibile verificare gli aggiornamenti. Niente viene messo in cache, "
            "così il controllo verrà ritentato.",
            unreachable,
            installati,
        )
        return list(cache.get("outdated", []))

    outdated.sort(key=lambda d: d["name"].lower())
    if unreachable:
        log.debug("   %d pacchetti installati senza risposta su %d.", unreachable, installati)
    # Un controllo parziale non puo' diventare un "tutti aggiornati" certificato:
    # se un pacchetto installato non ha risposto e nessun altro ha segnalato un
    # aggiornamento, NON si scrive la cache e il controllo verra' ripetuto.
    if unreachable and not outdated:
        log.debug(
            "   Check aggiornamenti: %d pacchetto/i senza risposta e nessun aggiornamento "
            "trovato: cache non scritta per non certificare un controllo incompleto.",
            unreachable,
        )
        return list(cache.get("outdated", []))
    if non_installati:
        log.debug("   %d pacchetti non installati: saltati (non e' un errore di rete).", non_installati)
    _write_cache({"ts": now, "outdated": outdated})
    return outdated


def print_updates(outdated: list[dict]) -> None:
    """Stampa la notifica aggiornamenti (solo se ce ne sono).

    Le voci si distinguono per motivo, perche' il motivo decide cosa farne: e'
    PINNATA (scelta del progetto, l'aggiornamento e' testato A/B), e' una major
    (da valutare a mano) oppure e' BLOCCATA dalle dipendenze installate (non
    esiste: pip non la risolverebbe). Solo le voci libere finiscono nell'invito a
    ``pip install -U``: invitare a un comando che non porta alla versione
    annunciata e' peggio del silenzio, perche' l'utente lo esegue, non succede
    nulla e la segnalazione si ripresenta identica alla run successiva.
    """
    if not outdated:
        log.info("   ✅ Tutti i pacchetti usati sono aggiornati.")
        return
    log.info("   📦 Sono disponibili aggiornamenti per %d pacchetto/i:", len(outdated))
    for d in outdated:
        bloccato = d.get("blocked") or []
        if bloccato:
            # Chi blocca conta piu' del pin e della major: spiega perche' non c'e'
            # niente da fare, mentre "pinnato"/"major" suggeriscono un'azione.
            altri = f" (+{len(bloccato) - 1})" if len(bloccato) > 1 else ""
            suffix = f" — 🚫 bloccato da {bloccato[0]}{altri}"
        elif d["pinned"] and d["note"]:
            suffix = f" — 🔒 {d['note']}"
        elif d["pinned"]:
            suffix = " — 🔒 pinnato"
        elif d.get("major"):
            suffix = " — ⚠️ major version"
        else:
            suffix = ""
        log.info("      %s: %s -> %s%s", d["name"], d["installed"], d["latest"], suffix)

    libere = [d for d in outdated if not (d.get("blocked") or []) and not d["pinned"]]
    if libere:
        log.info("      Aggiorna manualmente con: pip install -U <pacchetto> (verifica i pinnati e le major).")
    bloccati = [d["name"] for d in outdated if d.get("blocked")]
    if bloccati:
        log.info(
            "      🚫 Non disponibili in questo ambiente: %s (le dipendenze installate "
            "ne vietano la versione). Sono elencati per trasparenza, non per essere installati: "
            "si sbloccano sciogliendo il vincolo a monte.",
            ", ".join(bloccati),
        )


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

    Esclusi: i pinnati (scelta del progetto), i salti di major version (da
    valutare a mano) e i bloccati dalle dipendenze installate (non sono una
    scelta: non si possono installare, e offrirli e' un vicolo cieco).
    """
    return [d for d in outdated if not d.get("pinned") and not d.get("major") and not (d.get("blocked") or [])]


def _frozen_reasons(d: dict) -> list[str]:
    """Perché una voce non si aggiorna: le sue etichette, in ordine di forza.

    L'unione dei motivi è esattamente la negazione di `_upgradable`: quello che il
    tool non aggiorna da solo è quello che la diagnostica deve spiegare. Tenerli
    adiacenti e con la stessa lista di condizioni è l'unico modo per non farli
    divergere (c'è un test che lo verifica).
    """
    motivi: list[str] = []
    if d.get("blocked") or []:
        motivi.append("bloccato dalle dipendenze installate")
    if d.get("pinned"):
        motivi.append("pinnato dal progetto")
    if d.get("major"):
        motivi.append("salto di major version")
    return motivi


# Pinnati che possono essere testati A/B prima dell'aggiornamento: se il test
# conferma che la versione candidata produce embedding equivalenti, il pinnato
# viene incluso nell'aggiornamento automatico; altrimenti resta pinnato.
_AB_TESTABLE_PINNED: tuple[str, ...] = ("fastembed",)


def _pinned_with_update(outdated: list[dict]) -> list[dict]:
    """Pinnati con una versione più recente disponibile e testabili A/B.

    I bloccati restano fuori: il test A/B misura se la candidata e' equivalente,
    ma se il grafo delle dipendenze non la installa il verdetto non e' spendibile
    (userebbe lo spazio di una venv temporanea per una risposta che pip rifiuta).
    """
    return [d for d in outdated if d["pinned"] and d["name"] in _AB_TESTABLE_PINNED and not (d.get("blocked") or [])]


def _hash_texts(texts: list[str]) -> str:
    """Hash stabile dei testi reali usati dal test A/B."""
    import hashlib

    return hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode("utf-8")).hexdigest()


def _ab_report_valido(
    cached: Any,
    candidate: str,
    pkg: dict,
    texts_hash: str,
    model: str,
    thresholds: tuple[float, float],
) -> bool:
    """True se il report puo' essere riutilizzato: contenuto identico e recente.

    Non basta che candidata e modello coincidano: se e' cambiata la versione di
    partenza, sono cambiate le soglie o sono cambiati i testi reali, il verdetto
    vecchio non descrive piu' il confronto che stiamo per fare. Un report senza
    questi campi (quelli scritti dalle versioni precedenti del tool) NON va
    riusato: meglio ripetere il test che fidarsi di un confronto cieco.
    """
    if not isinstance(cached, dict):
        return False
    if cached.get("candidate") != candidate or cached.get("model") != model:
        return False
    if cached.get("verdict") not in ("EQUIVALENTE", "DIVERGENTE"):
        return False
    if cached.get("installed") != pkg.get("installed") or cached.get("texts_hash") != texts_hash:
        return False
    # Baseline e candidata devono usare lo STESSO batch: il padding e' per-batch
    # (vedi semantic_sync), quindi due versioni identiche possono dare un coseno
    # diverso se_calcolate con batch diversi e il verdetto non e' confrontabile.
    from config import DEFAULT_EMBED_BATCH

    if cached.get("batch_size") != DEFAULT_EMBED_BATCH:
        return False
    sim_mean, decision_match = thresholds
    if cached.get("threshold_sim_mean") != sim_mean or cached.get("threshold_decision_match") != decision_match:
        return False
    ts = cached.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return False
    return time.time() - ts < _AB_REPORT_TTL_DAYS * 86400


def _run_pinned_ab_test(pkg: dict) -> str | None:
    """Esegue il test A/B per un pinnato testabile. Ritorna il verdetto o None.

    Riutilizza il report salvato in ``.cache/fastembed_ab.json`` solo se descrive
    ESATTAMENTE lo stesso confronto (candidata, modello, installata di partenza,
    soglie, testi, eta') e se e' recente. Il test completo crea una venv temporanea
    e installa la candidata, quindi ripeterlo a ogni avvio sarebbe lento; ma un
    verdetto riusato senza queste verifiche e' peggio che rifare il test.
    """
    from config import DEFAULT_EMBED_BATCH, DEFAULT_EMBEDDING_CACHE_DIR, DEFAULT_EMBEDDING_MODEL

    candidate = pkg["latest"]
    report_path = CACHE_DIR / "fastembed_ab.json"
    try:
        # Lo script A/B sta in src/scripts/ e non e' un pacchetto: si rende
        # importabile aggiungendo la sua cartella al sys.path. Senza questo,
        # `import` fallirebbe e il test A/B non girerebbe mai (e' successo
        # dopo lo spostamento del codice in src/: il percorso vecchio non
        # esiste piu' e il verdetto era sempre "test non disponibile").
        _scripts = BASE_DIR / "src" / "scripts"
        if not (_scripts / "check_fastembed_upgrade.py").exists():
            _scripts = BASE_DIR / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))
        import check_fastembed_upgrade as ab  # type: ignore[import-not-found]
    except Exception as e:
        log.warning("   ⚠️ Impossibile eseguire il test A/B per %s: %s", pkg["name"], e)
        return None

    texts = ab.collect_real_texts()
    if texts is None or not texts["slides"] or not texts["blocks"]:
        log.warning("   ⚠️ Test A/B saltato: nessun dato reale in .cache.")
        return None

    all_texts = texts["slides"] + texts["blocks"]
    texts_hash = _hash_texts(all_texts)
    soglie = (
        getattr(ab, "DEFAULT_SIM_MEAN_THRESHOLD", 0.90),
        getattr(ab, "DEFAULT_DECISION_MATCH_THRESHOLD", 0.95),
    )
    try:
        if report_path.exists():
            cached = json.loads(report_path.read_text(encoding="utf-8"))
            if _ab_report_valido(cached, candidate, pkg, texts_hash, DEFAULT_EMBEDDING_MODEL, soglie):
                log.info("   🧪 Test A/B %s (%s): riutilizzo report cache.", pkg["name"], candidate)
                return str(cached["verdict"])
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    log.info("   🧪 Test A/B isolato per %s (%s -> %s)...", pkg["name"], pkg["installed"], candidate)
    base = ab.baseline_embeddings(all_texts, model=DEFAULT_EMBEDDING_MODEL)
    if base is None:
        log.warning("   ⚠️ Test A/B saltato: baseline non calcolata.")
        return None

    cand = None
    venv_dir = None
    try:
        cand, venv_dir = ab.candidate_embeddings(
            all_texts, candidate, model=DEFAULT_EMBEDDING_MODEL,
            cache_dir=DEFAULT_EMBEDDING_CACHE_DIR,
        )
    finally:
        # `candidate_embeddings` restituisce (embeddings, venv_dir): la venv e'
        # temporanea e va rimossa anche se il calcolo della candidata fallisce.
        if venv_dir is not None:
            shutil.rmtree(venv_dir, ignore_errors=True)
    if cand is None:
        log.warning("   ⚠️ Test A/B saltato: candidata non calcolata.")
        return None

    res = ab.compare(base, cand, n_slides=len(texts["slides"]))
    log.info(
        "      coseno medio %.3f | decision match %.2f (timeline reale) | argmax %.2f (%d blocchi) -> %s",
        res["cosine_mean"], res["decision_match"], res["argmax_match"], int(res["n_blocks"]), res["verdict"],
    )
    report = {
        **res,
        "candidate": candidate,
        "installed": pkg["installed"],
        "model": DEFAULT_EMBEDDING_MODEL,
        "texts_hash": texts_hash,
        "batch_size": DEFAULT_EMBED_BATCH,
        "ts": time.time(),
    }
    with suppress(OSError):
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(res["verdict"])


def _tail_lines(testo: str | None, quante: int = 12) -> list[str]:
    """Ultime righe non vuote di un output di pip."""
    righe = [riga.strip() for riga in (testo or "").splitlines() if riga.strip()]
    return righe[-quante:]


def _upgrade_requirement(pkg: dict) -> str:
    """Requisito pip da passare a pip per l'upgrade: ``nome==latest`` se noto.

    I nomi nudi con ``-U`` lasciavano a pip scegliere la versione: il resolver
    poteva restare fermo a quella vecchia (cap delle dipendenze) e riportare
    successo, oppure saltare a una versione diversa da quella annunciata nel
    report. Vincolare la versione annunciata rende l'esito verificabile; le voci
    senza una versione parseabile restano nomi nudi.
    """
    nome = str(pkg["name"])
    latest = str(pkg.get("latest") or "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+\-_!]*", latest) and re.search(r"\d", latest):
        return f"{nome}=={latest}"
    return nome


def _health_check(packages: list[str]) -> bool:
    """Controllo dopo l'install: importa i moduli chiave in un interprete pulito.

    Un exit 0 di pip non basta: puo' aver installato una versione che rompe
    un'import. L'import VA fatto in un subprocess con ``python -I -c``:
    ``importlib.import_module`` nel processo corrente restituisce il modulo gia'
    presente in ``sys.modules`` (il test A/B l'ha gia' importato), quindi
    verificava il codice VECCHIO e il controllo non serviva a niente. ``-I``
    isola l'interprete (niente PYTHONPATH/user site), cosi' restano fuori anche
    import inquinati dall'ambiente che ha lanciato il tool. Se il controllo
    fallisce l'upgrade va considerato non riuscito: la cache resta valida e il
    pin A/B non viene scritto.

    ``pip check`` e' GLOBALE: puo' segnalare conflitti preesistenti, colpa di
    pacchetti che non abbiamo toccato. E' quindi uno smoke test informativo: fa
    fallire solo se la dipendenza rotta e' dichiarata da uno dei pacchetti appena
    aggiornati (la riga comincia col suo nome); altrimenti il conflitto viene
    solo segnalato in debug.
    """
    moduli: list[str] = []
    for req in packages:
        nome = re.split(r"[<>=!~]", req, maxsplit=1)[0].strip().lower()
        moduli.append(_IMPORT_NAMES.get(nome, nome.replace("-", "_")))
    if moduli:
        codice = "import importlib; " + "; ".join(
            f"importlib.import_module({modulo!r})" for modulo in moduli
        )
        try:
            res = subprocess.run(
                [sys.executable, "-I", "-c", codice],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=120,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("   ❌ Health check fallito (import in interprete pulito): %s", e)
            return False
        if res.returncode != 0:
            log.warning(
                "   ❌ Health check fallito: import di %s in interprete pulito non riuscito:",
                ", ".join(moduli),
            )
            for riga in _tail_lines(res.stderr or res.stdout):
                log.warning("      %s", riga)
            return False
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # pip check e' uno smoke test: se non e' disponibile non si annulla un
        # upgrade i cui import sono gia' stati verificati in interprete pulito.
        log.debug("   ⚠️ pip check non disponibile (conflitti non verificati): %s", e)
        return True
    if res.returncode != 0:
        righe = _tail_lines(res.stderr or res.stdout)
        nomi = {re.split(r"[<>=!~]", req, maxsplit=1)[0].strip().lower() for req in packages}
        colpevoli = [
            riga
            for riga in righe
            if any(re.match(rf"{re.escape(nome)}(?:\s|$)", riga.lower()) for nome in nomi)
        ]
        if colpevoli:
            log.warning("   ❌ Health check fallito (pip check): dipendenze rotte dei pacchetti aggiornati:")
            for riga in colpevoli:
                log.warning("      %s", riga)
            return False
        log.debug(
            "   ⚠️ pip check segnala conflitti preesistenti, non dei pacchetti aggiornati: %s",
            " | ".join(righe),
        )
    log.debug(
        "   ✅ Health check superato (%d moduli importati in interprete pulito).",
        len(moduli),
    )
    return True


def _pip_upgrade(packages: list[str]) -> bool:
    """Aggiorna i pacchetti indicati (pip install -U). True se riuscito.

    L'output di pip viene catturato: in caso di errore se ne mostrano le
    ultime righe. Senza quelle l'utente vedrebbe solo "non-zero exit status 1"
    e non saprebbe se è la rete, un conflitto di versioni o i permessi; in
    caso di successo l'ultima riga di pip dice cosa è stato davvero installato
    (utile quando pip non cambia nulla). I requisiti completi (``nome==latest``)
    e ``--upgrade-strategy only-if-needed`` evitano che pip scelga una versione
    diversa da quella annunciata o aggiorni di nuovo le dipendenze.
    """
    # La riga stampata e' la STESSA lista di argomenti passata a subprocess.run:
    # scrivere a mano "pip install -U <pkg>" ometteva la strategia e i requisiti
    # nome==versione, quindi il comando visibile non era quello davvero eseguito.
    comando = [
        sys.executable, "-m", "pip", "install", "-U", "--upgrade-strategy", "only-if-needed", *packages
    ]
    log.info("   ⏳ %s ...", " ".join(comando))
    try:
        res = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        log.warning("   ❌ Aggiornamento fallito (timeout): %s", e)
        return False

    if res.returncode != 0:
        log.warning("   ❌ Aggiornamento fallito (pip exit %d):", res.returncode)
        for riga in _tail_lines(res.stderr or res.stdout):
            log.warning("      %s", riga)
        return False

    esito = _tail_lines(res.stdout, 1)
    log.info("   ✅ Installati: %s%s", ", ".join(packages), f" — {esito[0]}" if esito else "")
    if not _health_check(packages):
        log.warning("   ❌ Upgrade non confermato dal health check: cache e pin restano invariati.")
        return False
    return True


def _aggiorna_pin(versioni: dict[str, str]) -> None:
    """Allinea requirements.txt e pin_overrides.json dopo un upgrade A/B riuscito.

    Senza questo passo il prossimo avvio di `config._ensure_pip_packages`
    rilegge il pin vecchio e reinstalla la versione appena scartata dal test:
    l'upgrade durerebbe una sola run. In requirements.txt si cambia solo il
    numero dopo ``==`` (markers e commenti restano intatti); il file nella cache
    e' letto da config con le chiavi ``{"nome": "versione"}``.
    """
    req_path = BASE_DIR / "requirements.txt"
    if req_path.exists():
        try:
            testo = req_path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("   ⚠️ Pin non letto da requirements.txt: %s", e)
        else:
            righe = testo.splitlines()
            finale = testo.endswith("\n")
            aggiornati: list[str] = []
            for i, riga in enumerate(righe):
                for nome, versione in versioni.items():
                    trovato = re.match(rf"^({re.escape(nome)}\s*==\s*)([^\s;#]+)", riga, re.IGNORECASE)
                    if trovato and trovato.group(2) != versione:
                        righe[i] = trovato.group(1) + versione + riga[trovato.end(2) :]
                        aggiornati.append(f"{nome}=={versione}")
            if aggiornati:
                try:
                    atomic_write_text(req_path, "\n".join(righe) + ("\n" if finale else ""))
                except OSError as e:
                    log.warning("   ⚠️ Pin non scritto in requirements.txt: %s", e)
                else:
                    log.info("   📌 Pin aggiornato in requirements.txt: %s", ", ".join(aggiornati))
    try:
        atomic_write_text(CACHE_DIR / "pin_overrides.json", json.dumps(versioni, ensure_ascii=False, indent=2))
    except OSError as e:
        log.warning("   ⚠️ Override pin non scritto in pin_overrides.json: %s", e)


def _ask_yes_no(prompt: str) -> bool:
    """Chiede conferma all'utente (S/N). True se risponde sì.

    Svuota lo stdout prima di leggere: quando Python è avviato da un .bat la
    stdout è bufferizzata e il prompt di `input()` non comparirebbe finché il
    buffer non si svuota (i log di loguru, su stderr, invece compaiono subito).

    Ctrl+C (KeyboardInterrupt) vale come EOF: risposta "no", senza traceback.
    L'aggiornamento e' un passo opzionale, interromperlo non deve far credere
    che il programma sia rotto.
    """
    try:
        sys.stdout.write(f"   {prompt} [s/N]: ")
        sys.stdout.flush()
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("s", "si", "y", "yes")


def run_update_check(
    ttl_hours: float = DEFAULT_UPDATE_TTL_HOURS,
    ask_to_update: bool = True,
) -> None:
    """Entry point: check + notifica; se richiesto chiede S/N e aggiorna.

    Se ``ask_to_update`` è True e ci sono aggiornamenti NON pinnati, NON major e
    NON bloccati dalle dipendenze installate, chiede all'utente se installarli in
    automatico. Non si chiede mai di una cosa che pip non risolverebbe.
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

    # I bloccati restano fuori anche da qui: "da aggiornare a mano" e' un invito,
    # e per loro non esiste la versione che li sblocca.
    major = [d["name"] for d in outdated if d.get("major") and not d["pinned"] and not (d.get("blocked") or [])]
    if major:
        log.info("   ⚠️ Salti di major version NON aggiornati automaticamente: %s", ", ".join(major))

    # Si passano requisiti completi (nome==versione annunciata), non nomi nudi:
    # cosi' pip non puo' riportare successo senza aver installato quella versione
    # (e non puo' prenderne un'altra diversa da quella del report).
    names = [_upgrade_requirement(d) for d in upgradable]

    # Per i pinnati testabili, il verdetto del test A/B decide se includerli:
    # EQUIVALENTE -> SI, procedi; DIVERGENTE/None -> NO, lascia come ora.
    # Il test crea una venv temporanea e installa la candidata: e' lento,
    # quindi prima si chiede il permesso esplicito (S/N). Senza permesso
    # (o senza terminale: EOF) i pinnati restano come ora, senza verifiche.
    pin_accettati: dict[str, str] = {}
    if pinned_upd:
        nomi = ", ".join(f"{d['name']} ({d['installed']} -> {d['latest']})" for d in pinned_upd)
        if _ask_yes_no(
            f"Verificare compatibilita' di {len(pinned_upd)} pinnato/i ({nomi})? (lento: venv temporanea)"
        ):
            for d in pinned_upd:
                verdict = _run_pinned_ab_test(d)
                if verdict == "EQUIVALENTE":
                    log.info("   ✅ %s: equivalente -> SI, procedi (incluso nell'aggiornamento).", d["name"])
                    names.append(_upgrade_requirement(d))
                    pin_accettati[d["name"]] = d["latest"]
                elif verdict == "DIVERGENTE":
                    log.info("   🔒 %s: divergente -> NO, lascia come ora (resta pinnato).", d["name"])
                else:
                    log.info("   🔒 %s: test non disponibile -> NO, lascia come ora (resta pinnato).", d["name"])
        else:
            log.info("   🔒 Verifica pinnati saltata: lascio come ora (restano pinnati).")

    if not names:
        log.info("   Nessun pacchetto da aggiornare.")
        return

    if _ask_yes_no(f"Aggiornare {len(names)} pacchetto/i ({', '.join(names)})?"):
        esito = _pip_upgrade(names)
        # I pinnati A/B vanno "sbloccati" anche su disco: altrimenti il
        # prossimo bootstrap rimetterebbe la versione vecchia. Solo se pip ha
        # davvero installato: un upgrade fallito non deve cambiare i pin.
        if esito and pin_accettati:
            _aggiorna_pin(pin_accettati)
        # Invalida la cache SOLO se l'upgrade e' davvero riuscito (pip ok e
        # health-check ok): su fallimento i dati vecchi restano validi e il
        # prossimo avvio deve riprovare senza perdere il controllo gia' fatto.
        if esito:
            UPDATES_CACHE.unlink(missing_ok=True)
        else:
            log.info("   ⚠️ Cache mantenuta: al prossimo avvio il controllo verra' ripetuto.")
    else:
        log.info("   Ok, nessun aggiornamento installato.")


# =====================================================================
# DIAGNOSTICA: perché un pacchetto è fermo e cosa servirebbe per sbloccarlo
# =====================================================================

# Autore dei vincoli che non è un pacchetto installato: il pin del progetto.
# E' la stessa sorgente usata da `_project_constraints` per i cap di requirements.txt.
_PIN_SOURCE = _PROJECT_REQ_SOURCE


def _max_satisfying(versions: list[str] | None, specs: list[str]) -> str | None:
    """La versione più alta (fra quelle date) che soddisfa TUTTI gli specifier.

    None se nessuna la soddisfa oppure se l'elenco versioni non è disponibile: sono due
    casi diversi che il chiamante distingue guardando prima ``versions is None``, perché
    "non lo so" e "impossibile" vanno detti in modo diverso.

    Uno specifier illeggibile viene IGNORATO invece di far fallire il calcolo: stessa
    scelta di `_blockers`, un vincolo scritto male non deve produrre un verdetto.
    """
    if versions is None:
        return None
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    sets: list[Any] = []
    for spec in specs:
        try:
            sets.append(SpecifierSet(spec))
        except Exception:
            continue
    best: tuple[Any, str] | None = None
    for testo in versions:
        try:
            version = Version(testo)
        except InvalidVersion:
            continue
        if not all(s.contains(version, prereleases=True) for s in sets):
            continue
        if best is None or version > best[0]:
            best = (version, testo)
    return best[1] if best else None


def _frozen_diagnosis(
    name: str,
    installed: str,
    latest: str,
    vincoli: list[tuple[str, str, str]],
    versions: list[str] | None,
) -> dict:
    """Parte PURA della diagnostica: dati i vincoli e l'elenco versioni, calcola tutto.

    Nessuna rete e nessuna installazione qui dentro. Si calcola:

    - ``ceiling``: la più alta versione che TUTTI i vincoli accettano. È il vero "fin
      dove si può arrivare" (per pillow: 10.4.0, non 12.3.0); se coincide con quella
      installata, ora non c'è niente da sciogliere;
    - ``excluders``: i vincoli che rifiutano la candidata annunciata da PyPI;
    - ``levers``: per ognuno di quelli, fin dove si arriverebbe sciogliendo SOLO lui,
      lasciando in piedi gli altri — è la risposta a "sciogliendo quali vincoli".
      Sciogliere un vincolo significa aggiornare il pacchetto che lo impone (o togliere
      il pin): non è gratis, e le alternative vanno viste una per una.
    """
    def esclude(spec: str) -> bool:
        # Lo specifier rifiuta la candidata: nessuna versione (qui: la candidata) lo soddisfa.
        return _max_satisfying([latest], [spec]) is None

    levers: list[dict] = []
    for indice, (chi, versione_chi, spec) in enumerate(vincoli):
        if not esclude(spec):
            continue
        altri = [s for i, (_, _, s) in enumerate(vincoli) if i != indice]
        levers.append(
            {
                "by": chi,
                "version": versione_chi,
                "spec": spec,
                "project_pin": chi == _PIN_SOURCE,
                "source_pinned": chi != _PIN_SOURCE and _is_pinned(chi),
                "reachable": _max_satisfying(versions, altri),
            }
        )
    return {
        "name": name,
        "installed": installed,
        "latest": latest,
        "constraints": list(vincoli),
        "excluders": [tuple(v) for v in vincoli if esclude(v[2])],
        "ceiling": _max_satisfying(versions, [spec for _, _, spec in vincoli]),
        "levers": levers,
        "versions_known": versions is not None,
    }


def diagnose_frozen_packages(ttl_hours: float = 0.0) -> list[dict]:
    """Diagnostica i pacchetti che non si aggiornano: chi li vincola, fin dove si arriva.

    Sono le voci che `_upgradable` esclude (vedi `_frozen_reasons`): pinnate, major o
    bloccate — quelle per cui il report dice "no" senza dire da cosa dipende il no. Per
    ognuna si legge da PyPI l'elenco COMPLETO delle versioni e si calcolano tetto e leve.

    ``ttl_hours=0`` di default: una diagnostica chiesta a mano mostra lo stato di adesso,
    non quello di sei ore fa (la cache serve a non battere PyPI a ogni avvio, non a
    risparmiare su un comando esplicito).
    """
    outdated = check_updates(ttl_hours=ttl_hours)
    fermi = [d for d in outdated if _frozen_reasons(d)]
    if not fermi:
        return []

    vincoli = _installed_constraints()
    diagnosi: list[dict] = []
    for d in fermi:
        nome = d["name"]
        # Vincoli degli altri pacchetti installati + il pin del progetto, se c'è: senza il
        # pin, un pacchetto fermo per scelta del progetto sembrerebbe fermo senza motivo,
        # che è il contrario di quello che questa diagnostica deve dire.
        v = list(vincoli.get(nome.lower(), []))
        pin = _pinned_requirement(nome)
        if pin:
            v.append((_PIN_SOURCE, "", pin))
        voce = _frozen_diagnosis(nome, d["installed"], d["latest"], v, _available_versions(nome))
        voce["reasons"] = _frozen_reasons(d)
        diagnosi.append(voce)
    return diagnosi


def print_frozen_diagnosis(diagnosi: list[dict]) -> None:
    """Stampa il referto: chi vincola ogni pacchetto fermo, e cosa servirebbe per muoverlo.

    Non installa e non chiede conferme: spiega. Il valore sta nella differenza fra "non
    si aggiorna" e "non si PUÒ aggiornare": la prima è una scelta, la seconda il vincolo
    di un altro pacchetto, e qui si vedono una per una, con la versione che si
    raggiungerebbe sciogliendole separatamente.
    """
    if not diagnosi:
        log.info("   ✅ Nessun pacchetto fermo: ogni aggiornamento disponibile è installabile.")
        return
    log.info("   🧊 %d pacchetto/i fermo/i:", len(diagnosi))
    for d in diagnosi:
        log.info("      %s: %s installato · %s su PyPI", d["name"], d["installed"], d["latest"])
        log.info("         motivo: %s", "; ".join(d["reasons"]))

        if not d["constraints"]:
            log.info("         nessun vincolo da altri pacchetti: il freno è solo la scelta qui sopra")
        else:
            log.info("         chi lo vincola, fra i pacchetti installati:")
            for chi, versione_chi, spec in d["constraints"]:
                if chi == _PIN_SOURCE:
                    log.info("            🔒 %s: %s (pin del progetto)", chi, spec)
                    continue
                ammette = _max_satisfying([d["latest"]], [spec]) is not None
                log.info(
                    "            %s %s %s — richiede %s (%s %s)",
                    "✅" if ammette else "🚫",
                    chi,
                    versione_chi,
                    spec,
                    "ammette" if ammette else "esclude",
                    d["latest"],
                )

        if not d["versions_known"]:
            log.info("         tetto non calcolabile: PyPI non ha risposto per l'elenco delle versioni")
        elif d["ceiling"] is None:
            log.info("         tetto: nessuna versione pubblicata soddisfa tutti i vincoli")
        elif d["ceiling"] == d["installed"]:
            log.info("         tetto: %s — e' la versione installata, ora non c'è niente da muovere", d["ceiling"])
        else:
            log.info("         tetto: %s (installata %s)", d["ceiling"], d["installed"])

        if not d["excluders"]:
            log.info(
                "         la candidata %s non è esclusa da nessun vincolo: il freno è la riga 'motivo'",
                d["latest"],
            )
            continue
        quanti = len(d["excluders"])
        log.info(
            "         per arrivare a %s %s. Uno alla volta (gli altri restano):",
            d["latest"],
            "va sciolto 1 vincolo" if quanti == 1 else f"vanno sciolti {quanti} vincoli",
        )
        for leva in d["levers"]:
            if not d["versions_known"]:
                log.info("            senza %s %s -> non calcolabile (PyPI non ha risposto)", leva["by"], leva["spec"])
            elif leva["reachable"] is None:
                log.info(
                    "            senza %s %s -> nessuna versione compatibile con gli altri vincoli",
                    leva["by"],
                    leva["spec"],
                )
            elif leva["reachable"] == d["ceiling"]:
                log.info(
                    "            senza %s %s -> %s (nessun guadagno: un altro vincolo tappa allo stesso punto)",
                    leva["by"],
                    leva["spec"],
                    leva["reachable"],
                )
            else:
                log.info("            senza %s %s -> %s", leva["by"], leva["spec"], leva["reachable"])
            if leva["project_pin"]:
                log.info("               nota: è la scelta del progetto, si scioglie decidendo (test A/B o nuovo pin)")
            elif leva["source_pinned"]:
                log.info("               nota: %s è a sua volta pinnato dal progetto, prima si sblocca lui", leva["by"])


def run_frozen_report(ttl_hours: float = 0.0) -> None:
    """Entry point della diagnostica: spiega i pacchetti fermi e non tocca niente.

    Nessuna installazione e nessuna domanda: è un referto, quindi si può lanciare senza
    paura di cambiare l'ambiente.
    """
    log.info("🔎 Diagnostica pacchetti fermi (chi li vincola, fin dove si arriverebbe)...")
    print_frozen_diagnosis(diagnose_frozen_packages(ttl_hours=ttl_hours))


def _read_cache() -> dict:
    try:
        data = json.loads(UPDATES_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Un timestamp scritto come stringa (cache corrotta o scritta a mano) fa
    # sollevare TypeError in `check_updates` e bloccherebbe main(): cache non
    # valida si comporta come cache assente.
    ts = data.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return {}
    return data


def _write_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(UPDATES_CACHE, json.dumps(data, ensure_ascii=False, indent=2))
