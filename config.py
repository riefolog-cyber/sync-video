#!/usr/bin/env python3
"""
Configurazione centralizzata per il pipeline slide-audio.
Costanti, argparse, logging e setup automatico dipendenze.
"""

import argparse
import contextlib
import importlib.metadata
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# stdout/stderr in UTF-8 con fallback 'replace': evita UnicodeEncodeError
# (codice cp1252 di Windows) quando il bootstrap stampa emoji (es. ⏳ 🔧).
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        with contextlib.suppress(ValueError, OSError):
            _stream.reconfigure(encoding="utf-8", errors="replace")


def _load_env_file(path: Path) -> None:
    """Carica un file .env nell'ambiente (senza sovrascrivere variabili esistenti).

    Gestisce:
    - Commenti inline: ``KEY=value # commento``
    - Prefisso ``export``: ``export KEY=value``
    - BOM all'inizio del file
    - Virgolette singole/doppie attorno al valore
    """
    if not path.exists():
        return
    # Rimuovi BOM se presente (utf-8-sig gestisce automaticamente)
    content = path.read_text(encoding="utf-8-sig")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        # Salta righe vuote e commenti completi
        if not line or line.startswith("#"):
            continue
        # Rimuovi prefisso "export " se presente (stile bash)
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Rimuovi commenti inline (solo se non dentro virgolette)
        value = value.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            # Cerca la chiusura della virgoletta (prima di un eventuale commento)
            end_quote = value.find(quote, 1)
            # Virgoletta di apertura SENZA chiusura: il valore è malformato. Prima
            # si entrava nell'else e si tagliava al '#', restituendo anche
            # l'apostrofo (`T_A="ciao # nota` -> `"ciao`): la variabile risultava
            # valorizzata con spazi e virgolette, che un _env_int/_env_float
            # rifiutava con un avviso fuorviante ("non numerica") invece di
            # segnalare il .env rotto. Meglio togliere il commento come nel caso
            # senza virgolette e lasciare che l'errore emerga sul valore, che è
            # il vero contenuto scritto dall'utente.
            value = (
                value[1:end_quote]
                if end_quote != -1
                else value.split("#", 1)[0].strip().strip(quote)
            )
        else:
            value = value.split("#")[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


# Carica .env se presente (prima di argparse)
_load_env_file(Path(__file__).parent / ".env")


def _env_int(name: str, default: int) -> int:
    """Legge una variabile d'ambiente come intero, con fallback al default."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        log.warning("   Variabile %s non numerica ('%s'), uso default %d.", name, value, default)
        return default


def _env_float(name: str, default: float) -> float:
    """Legge una variabile d'ambiente come float, con fallback al default."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        log.warning("   Variabile %s non numerica ('%s'), uso default %s.", name, value, default)
        return default


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Scrive testo in modo atomico (file temporaneo + os.replace).

    Una scrittura interrotta (crash, Ctrl+C, disco pieno) non lascia mai
    un file di cache corrotto: il .tmp viene ignorato dai lettori e il
    file definitivo resta nella versione precedente.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


# =====================================================================
# PATH DI BASE
# =====================================================================
BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / ".cache"

# =====================================================================
# LOGGING (setup minimo prima del bootstrap)
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-5s | %(message)s",
)
log = logging.getLogger("slide2video")


def setup_debug_logging() -> None:
    """Attiva il livello DEBUG per diagnostica dettagliata."""
    log.setLevel(logging.DEBUG)
    log.debug("Logging DEBUG attivato.")


# ---------------------------------------------------------------------------
# tqdm: import opzionale con fallback (centralizzato per evitare duplicazione)
# ---------------------------------------------------------------------------
try:
    from tqdm import tqdm
except ImportError:

    def tqdm(iterable: Any, desc: str = "", **kwargs: Any) -> Any:  # type: ignore[no-redef]
        """Fallback se tqdm non è installato.
        Supporta total/unit/unit_scale per mostrare progresso reale."""
        total = kwargs.get("total")
        unit = kwargs.get("unit", "it")

        if total is None:
            log.info("   %s...", desc)
            return iterable

        # Mostra progresso ogni 10% se iterable è un generatore
        log.info("   %s (totale: %d %s)...", desc, total, unit)
        last_pct = -1
        for i, item in enumerate(iterable):
            if total > 0:
                pct = int(i * 100 / total)
                if pct != last_pct and pct % 10 == 0:
                    log.info("   %s: %d%% (%d/%d %s)", desc, pct, i, total, unit)
                    last_pct = pct
            yield item
        log.info("   %s: 100%% (%d/%d %s)", desc, total, total, unit)


# =====================================================================
# BOOTSTRAP — Controlla e installa automaticamente le dipendenze
# =====================================================================
# Requisiti dei pacchetti: nome di import -> requisito pip (con eventuale
# versione minima). La versione minima serve perché un pacchetto troppo
# vecchio può far fallire l'import di un ALTRO pacchetto, e senza il controllo
# il messaggio sarebbe quello sbagliato ("X manca") invece di quello giusto
# ("X è troppo vecchio").
_REQUIRED_PACKAGES = {
    "pymupdf": "pymupdf",
    "PIL": "pillow",
    "pytesseract": "pytesseract",
    "pydub": "pydub",
    "moviepy": "moviepy",
    # Soglia imposta da pandas (dipendenza di pytesseract) e da moviepy 2.x.
    # Con numpy 1.24.3, misurato il 16/09/2026: "ImportError: Please upgrade
    # numpy to >= 1.26.0" dentro pandas, che sembra un guasto di pytesseract.
    "numpy": "numpy>=1.26.0",
    "tqdm": "tqdm",
    "fastembed": "fastembed",
    "faster_whisper": "faster-whisper>=1.2.1",
    # Confronto versioni (qui sotto e in updates.py): senza questo pacchetto la
    # verifica delle versioni minime si disattiva in silenzio.
    "packaging": "packaging>=23.0",
}

# =====================================================================
# ARCHITETTURA: pacchetti senza build nativa ARM
# =====================================================================
# faster-whisper dipende da CTranslate2, che non pubblica wheel win_arm64 (ne
# OpenVINO, che e x86-only). Su ARM quei pacchetti semplicemente non si
# installano: se restassero tra i "richiesti", il bootstrap fallirebbe e il
# programma uscirebbe prima di arrivare alla trascrizione. Qui vengono quindi
# tolti dai pacchetti da controllare/installare, restando opzionali: senza
# motore di trascrizione il progetto non produce sottotitoli, ma su ARM non
# c'e alternative pronta (CTranslate2 non esiste per questa CPU).
#
# Nota: `_REQUIRED_PACKAGES` resta invariato di proposito, perche' i test e
# updates.py lo leggono come elenco completo delle dipendenze dichiarate.
IS_ARM = platform.machine().upper() in ("ARM64", "AARCH64")

_X64_ONLY_PACKAGES = frozenset({"faster_whisper"})


def _active_required_packages() -> dict[str, str]:
    """`_REQUIRED_PACKAGES` privata dei pacchetti non installabili su ARM.

    Su x64 restituisce l'intero dizionario: il comportamento del bootstrap e
    identico a prima, quindi la macchina x64 non cambia nulla.
    """
    if not IS_ARM:
        return dict(_REQUIRED_PACKAGES)
    return {name: req for name, req in _REQUIRED_PACKAGES.items() if name not in _X64_ONLY_PACKAGES}


# Intestazione di `tesseract --list-langs`, da cui si ricava la cartella
# tessdata che Tesseratch sta usando davvero:
#   List of available languages in "C:\Program Files\Tesseract-OCR/tessdata/" (2):
#   List of available languages in "/usr/share/tesseract-ocr/5/tessdata" (1):
# È l'unico modo affidabile di trovarla senza hardcodare i layout delle tre
# piattaforme (apt usa /usr/share/tesseract-ocr/<versione>/tessdata, brew
# /opt/homebrew/share/tessdata, l'installer Windows <exe>\tessdata).
_TESSDATA_PATH_RE = re.compile(r'List of available languages in "([^"]+)"')


def _list_tessdata(tesseract_exe: str) -> tuple[set[str], Path | None]:
    """Chiede a Tesseract le lingue disponibili e la cartella che sta usando.

    Restituisce (lingue, cartella). La cartella è None se Tesseract non
    risponde o se l'intestazione non è quella prevista.
    """
    try:
        res = subprocess.run([tesseract_exe, "--list-langs"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set(), None
    out = res.stdout or ""
    m = _TESSDATA_PATH_RE.search(out)
    system_dir = None
    if m:
        candidate = Path(m.group(1))
        if candidate.is_dir():
            system_dir = candidate
    # Le lingue sono le righe non vuote dopo l'intestazione.
    langs = {line.strip() for line in out.splitlines()[1:] if line.strip()}
    return langs, system_dir


def _mirror_system_tessdata(system_dir: Path, local_dir: Path) -> int:
    """Copia in `local_dir` i modelli di lingua di sistema che mancano.

    `TESSDATA_PREFIX` SOSTITUISCE la cartella di sistema invece di aggiungerla:
    puntarla a una cartella che contiene solo `ita.traineddata` fa sparire
    `eng` e `osd`. Il testo inglese dentro slide italiane (nomi di prodotto,
    termini tecnici, URL, codice) verrebbe riconosciuto dal modello italiano con
    precisione peggiore, e `osd` (rilevamento orientamento/script) non sarebbe
    più disponibile. Copiare — invece di symlinkare, che su Windows richiede
    privilegi — lascia la cartella locale identica a quella di sistema più
    l'italiano.

    Restituisce quanti file sono stati copiati.
    """
    copied = 0
    for src in sorted(system_dir.glob("*.traineddata")):
        dst = local_dir / src.name
        if dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            copied += 1
        except OSError as e:
            log.debug("   Copia di %s non riuscita: %s", src.name, e)
    return copied


_TESSERACT_DOWNLOAD_URL = "https://github.com/UB-Mannheim/tesseract/wiki"
_FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"


# =====================================================================
# PROBE DI DISPONIBILITA' (hardware accelerabile)
# =====================================================================
# Vivono qui, e non in machine_setup, per una ragione precisa: sono usate sia
# da machine_setup sia da transcription, e machine_setup importa transcription
# (per scaricare il modello OpenVINO). Mettendole in config, che non importa
# nessuno dei due, il grafo degli import resta aciclico: senza questo i due
# moduli si importavano a vicenda e il ciclo reggeva solo grazie a un import
# differito dentro una funzione, fragile a ogni refactor futuro.
def cuda_available() -> bool:
    """True se CTranslate2 vede almeno una GPU CUDA utilizzabile."""
    try:
        import ctranslate2

        return bool(ctranslate2.get_cuda_device_count() > 0)
    except Exception:  # noqa: BLE001 - nessun CTranslate2, build senza CUDA, driver assente: qui significa la stessa cosa
        # Nessun CTranslate2, build senza CUDA, driver assente: tutte cose che
        # qui significano la stessa cosa, cioe' "CUDA non e' un canale valido".
        return False


def openvino_device_available() -> bool:
    """True se il runtime OpenVINO espone un device 'GPU' (iGPU Intel).

    La sola CPU non basta: su AMD/ARM OpenVINO vede al piu' la CPU, e usarla
    non da' guadagno di velocita' rispetto a faster-whisper.
    """
    try:
        from openvino import Core

        return "GPU" in Core().available_devices
    except Exception:  # noqa: BLE001 - runtime assente o rotto: su macchine diverse fallisce in modi diversi
        return False


# Timeout per l'installazione di uno strumento di sistema. Tesseract (~40 MB) e
# ffmpeg (~80 MB) su una connessione lenta superano facilmente 120s: un timeout
# scaduto viene letto come "installazione fallita" anche se il pacchetto sta per
# landare, e il programma prosegue senza lo strumento che credeva di aver
# installato. 600s copre con margine le installazioni reali.
_SYSTEM_INSTALL_TIMEOUT = 600

# brew in modalita' non interattiva. Senza questi flag brew puo' chiedere una
# conferma (aggiornamento delle formule, telemetria, cleanup) e il bootstrap si
# blocca su una domanda che nessuno puo' vedere, dato che l'output della
# subprocess e' DEVNULL: dall'esterno sembra un hang.
_BREW_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
    "HOMEBREW_NO_ENV_HINTS": "1",
    "NONINTERACTIVE": "1",
}


# Modelli che il programma scarica al primo uso, con la dimensione TIPICA
# misurata su questo progetto (non una stima): l'annuncio del primo avvio
# serve proprio a evitare che qualcuno interpreti il silenzio come un blocco.
#
# I 6.4 GB non sono un modello solo: fastembed tiene sia la copia "fast-" sia
# quella ONNX di multilingual-e5-large (2.1 + 4.3 GB). E' un suo
# comportamento, non una scelta del progetto, ma e' quello che l'utente vede
# sul disco e quindi quello che va detto.


def _modelli_mancanti(
    embedding_cache: Path,
    whisper_dirs: Sequence[Path],
) -> list[tuple[str, str]]:
    """Modelli che verranno scaricati, come (nome, dimensione tipica).

    Vuoto = tutto in cache, e il primo avvio non deve dire nulla.
    """
    mancanti: list[tuple[str, str]] = []
    if not _gia_scaricato(embedding_cache):
        mancanti.append(("modello embedding (multilingual-e5-large)", "~6 GB"))
    for d in whisper_dirs:
        if not _gia_scaricato(d):
            mancanti.append((d.name, "~0.5 GB"))
    return mancanti


def _gia_scaricato(directory: Path) -> bool:
    """True se la cartella di cache del modello contiene qualcosa.

    Non si usa "esiste la cartella": fastembed la crea prima di scaricare, quindi
    una cartella vuota farebbe dire "già scaricato" al primo avvio, che è
    esattamente il caso in cui l'annuncio serve.
    """
    try:
        return any(p.is_file() and p.stat().st_size > 0 for p in directory.rglob("*"))
    except OSError:
        return False


def _annuncia_primo_avvio(mancanti: Sequence[tuple[str, str]]) -> None:
    if not mancanti:
        return
    righe = "\n".join(f"   - {nome} ({dim})" for nome, dim in mancanti)
    log.info(
        "\n⏳ PRIMO AVVIO — downloads in corso, NON è un blocco.\n"
        "%s\n"
        "   Servono qualche minuto e qualche GB di disco (la cartella .cache/).\n"
        "   Le run successive non scaricano più nulla. Per saltarli:\n"
        "   --semantic-model <altro> / WHISPER_MODEL=tiny\n"
        "   (un modello più piccolo è più rapido ma meno accurato sulle ancore).",
        righe,
    )


def _try_system_install(name: str, winget_id: str, apt_pkg: str, brew_pkg: str) -> bool:
    """Tenta auto-install tool di sistema via package manager nativo.

    Prova nell'ordine giusto per la piattaforma: winget (Windows), brew
    (macOS), apt (Linux). Restituisce True se l'installazione è riuscita.
    """
    # Ogni tentativo è (env_extra, argv, label): env_extra rende il package
    # manager non interattivo, così il bootstrap non si blocca su una conferma
    # che nessuno può vedere (l'output è DEVNULL).
    commands: list[tuple[dict[str, str], list[str], str]] = []
    if sys.platform == "win32":
        commands.append(
            (
                {},
                ["winget", "install", "--accept-source-agreements", "--accept-package-agreements", winget_id],
                f"winget install {winget_id}",
            )
        )
    elif sys.platform == "darwin":
        if brew_pkg:
            commands.append((_BREW_ENV, ["brew", "install", brew_pkg], f"brew install {brew_pkg}"))
    # Solo Linux ha apt. Senza questo gate, su macOS un brew fallito finiva con
    # `sudo apt-get`: su un Mac quello chiede la password sudo dentro una
    # subprocess muta, e l'utente vede solo un processo apparentemente bloccato.
    elif apt_pkg:
        commands.append(({}, ["sudo", "apt-get", "install", "-y", apt_pkg], f"sudo apt-get install -y {apt_pkg}"))

    for env_extra, cmd, label in commands:
        try:
            print(f"   ⏳ {label} ...", end=" ", flush=True)
            subprocess.run(
                cmd,
                env={**os.environ, **env_extra},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_SYSTEM_INSTALL_TIMEOUT,
                check=True,
            )
            print("✅")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            print("❌ (tento alternativa)")
    return False


def _try_pip_install(package: str, upgrade: bool = False) -> bool:
    """Tenta di installare (o aggiornare, con upgrade=True) un pacchetto via pip."""
    try:
        print(f"   ⏳ pip install {'-U ' if upgrade else ''}{package} ...", end=" ", flush=True)
        cmd = [sys.executable, "-m", "pip", "install"]
        if upgrade:
            cmd.append("-U")
        cmd.append(package)
        subprocess.check_call(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
        print("✅")
        return True
    except subprocess.CalledProcessError:
        print("❌")
        return False
    except subprocess.TimeoutExpired:
        print("⏰ timeout")
        return False


def _req_name(req: str) -> str:
    """Nome pip di un requisito: 'numpy>=1.26.0' -> 'numpy'."""
    return re.split(r"[<>=!~\s]", req, maxsplit=1)[0]


def _installed_version(pip_name: str) -> str | None:
    """Versione installata del pacchetto, o None se non presente."""
    try:
        return importlib.metadata.version(pip_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _spec_ok(req: str, installed: str) -> bool:
    """True se ``installed`` soddisfa la parte di versione di ``req``.

    Se la verifica non è possibile (packaging assente, versione non
    parseabile) si prosegue: il bootstrap non deve bloccare l'avvio per un
    dubbio, semmai per un guasto accertato.
    """
    name = _req_name(req)
    spec = req[len(name) :].strip()
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet

        return bool(SpecifierSet(spec).contains(installed))
    except Exception:  # noqa: BLE001 - verifica non disponibile: non bloccare
        return True


def _missing_or_old(req: str) -> str | None:
    """Motivo per cui un requisito va installato/aggiornato, o None se è a posto."""
    installed = _installed_version(_req_name(req))
    if installed is None:
        return "assente"
    if _spec_ok(req, installed):
        return None
    return f"versione {installed}"


def _import_error_detail(exc: BaseException) -> str:
    """Errore d'import con la sua causa radice, se presente.

    L'eccezione esterna è spesso generica ("C extension: None not built"): la
    riga utile per capire cosa fare (es. "Please upgrade numpy to >= 1.26.0")
    sta nella causa.
    """
    detail = f"{type(exc).__name__}: {exc}"
    cause = exc.__cause__
    if cause is not None:
        detail += f" | causa: {type(cause).__name__}: {cause}"
    return detail


def _is_env_blocked_import(import_name: str, exc: Exception) -> bool:
    """True se l'errore d'import di un pacchetto opzionale è "installato ma
    bloccato dall'ambiente" (es. DLL PyAV bloccata su Windows, ffmpeg di
    sistema mancante) e NON "pacchetto mancante" (che va installato)."""
    if import_name not in ("moviepy", "faster_whisper"):
        return False
    # ModuleNotFoundError = pacchetto (o dipendenza, es. numpy/av) davvero
    # mancante: va installato, non ignorato.
    if isinstance(exc, ModuleNotFoundError):
        return False
    return isinstance(exc, (ImportError, RuntimeError))


def _ensure_pip_packages() -> None:
    """Installa/aggiorna i pacchetti richiesti e verifica che si importino.

    Due passaggi, in quest'ordine:
    1. metadata (``importlib.metadata``, nessun import): i pacchetti assenti o
       sotto la versione minima vengono installati con ``pip install -U``. Un
       pacchetto troppo vecchio può far fallire l'import di un ALTRO pacchetto,
       e il messaggio giusto in quel caso è "X è vecchio", non "Y manca".
    2. import di ciascun pacchetto: se resta un import rotto con il pacchetto
       installato, l'errore viene mostrato per intero (con la causa radice)
       invece di lasciar esplodere un traceback a metà avvio.

    Esce con codice 1 se qualcosa resta non installabile o non importabile.
    """
    to_install = []
    for pip_req in _active_required_packages().values():
        reason = _missing_or_old(pip_req)
        if reason:
            to_install.append((pip_req, reason))

    if to_install:
        log.info("🔧 Pacchetti da installare/aggiornare: %s", ", ".join(p[0] for p in to_install))
        log.info("   Installazione automatica in corso...")

        all_ok = True
        for pip_req, reason in to_install:
            log.info("   %s (%s) ...", pip_req, reason)
            if _try_pip_install(pip_req, upgrade=True):
                log.info("   ✅ %s ok.", pip_req)
            else:
                log.warning("   ❌ %s NON installato.", pip_req)
                all_ok = False

        if not all_ok:
            log.error(
                "\nAlcuni pacchetti non sono stati installati. Installa manualmente:\n"
                "   pip install -U %s\n"
                "Oppure: pip install -r requirements.txt",
                " ".join(p[0] for p in to_install),
            )
            sys.exit(1)

        log.info("   ✅ Tutti i pacchetti installati.")

    # --- Verifica degli import (dopo gli eventuali aggiornamenti) ---
    broken = []
    for import_name, pip_req in _active_required_packages().items():
        try:
            __import__(import_name)
        except (ImportError, RuntimeError) as e:
            # Alcuni pacchetti possono fallire l'import anche se installati:
            # MoviePy se manca uno strumento di sistema (FFmpeg), oppure
            # faster-whisper se Windows blocca una DLL di PyAV. In entrambi
            # i casi il pacchetto è presente: la verifica dello strumento di
            # sistema avviene più avanti nel bootstrap e la trascrizione usa
            # OpenVINO (che non dipende da av).
            if _is_env_blocked_import(import_name, e):
                log.debug(
                    "   %s installato ma import bloccato dall'ambiente (%s): proseguo.",
                    pip_req,
                    e,
                )
                continue
            broken.append((pip_req, e))

    if broken:
        # Installato ma non importabile: quasi sempre una dipendenza con la
        # versione sbagliata, non il pacchetto in sé (16/09/2026: numpy 1.24.3
        # faceva fallire pytesseract, perché pytesseract importa pandas).
        log.error(
            "\n❌ Pacchetti installati ma non importabili: %s\n"
            "   Causa più probabile: una dipendenza dell'ambiente ha la versione\n"
            "   sbagliata (es. numpy troppo vecchio per il pandas di pytesseract).\n"
            "   Diagnosi completa:  %s -m pip check\n"
            "   Riparazione:        %s -m pip install -U <pacchetto>",
            ", ".join(req for req, _ in broken),
            sys.executable,
            sys.executable,
        )
        for pip_req, err in broken:
            log.error("   %s -> %s", pip_req, _import_error_detail(err))
        sys.exit(1)


def bootstrap() -> None:
    """
    Verifica e installa automaticamente tutte le dipendenze.
    - Python >= 3.10
    - Pacchetti pip (auto-install)
    - Tesseract OCR (auto-install via winget/apt/brew)
    - ffmpeg (auto-install via winget/apt/brew)

    Deve essere chiamata esplicitamente da main.py all'avvio.
    """
    # --- Python version ---
    if sys.version_info < (3, 10):  # noqa: UP036 - guardia runtime per utenti finali
        log.error("Richiesto Python 3.10 o superiore. Versione attuale: %s", sys.version)
        sys.exit(1)

    # --- Pip packages (assenti, vecchi, o non importabili) ---
    _ensure_pip_packages()

    # --- Tesseract OCR ---
    import pytesseract  # garantito installato dal bootstrap pip qui sopra

    # La cartella locale del progetto: contiene ita.traineddata quando
    # l'italiano non e' installato nel sistema (vedi _ensure_italian_tessdata,
    # che decide se e come usarla). Non viene impostata qui come
    # TESSDATA_PREFIX: la scelta richiede di sapere cosa ha gia' Tesseract.
    _local_tessdata = BASE_DIR / "tessdata"

    _CANDIDATES = [
        # Windows
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        # Linux
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        # macOS
        "/opt/homebrew/bin/tesseract",
        "/usr/local/opt/tesseract/bin/tesseract",
    ]
    tesseract_found = False
    for c in _CANDIDATES:
        if os.path.exists(c):
            pytesseract.pytesseract.tesseract_cmd = c
            log.debug("   Tesseract trovato: %s", c)
            tesseract_found = True
            break

    if not tesseract_found:
        # Prova nel PATH
        try:
            subprocess.run(
                ["tesseract", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False
            )
            log.debug("   Tesseract trovato nel PATH.")
            tesseract_found = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not tesseract_found:
        log.info("🔧 Tesseract OCR non trovato — tentativo auto-install...")
        _try_system_install(
            "Tesseract OCR",
            winget_id="UB-Mannheim.TesseractOCR",
            apt_pkg="tesseract-ocr",
            brew_pkg="tesseract",
        )
        # Riverifica dopo installazione
        for c in _CANDIDATES:
            if os.path.exists(c):
                pytesseract.pytesseract.tesseract_cmd = c
                tesseract_found = True
                log.info("   ✅ Tesseract installato: %s", c)
                break
        if not tesseract_found:
            try:
                subprocess.run(
                    ["tesseract", "--version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                tesseract_found = True
                log.info("   ✅ Tesseract installato nel PATH.")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    # Il vecchio re-scan delle due directory Windows standard è stato rimosso:
    # sono già in _CANDIDATES e arrivare a questo punto prova che nessuna delle
    # due esiste. Le stringhe usavano anche backslash raddoppiati in raw string,
    # quindi avrebbero cercato un path diverso da quello reale.
    if not tesseract_found:
        log.error(
            "\n❌ TESSERACT OCR NON TROVATO — necessario per estrarre il testo dalle slide.\n"
            "   Auto-install fallita. Scaricalo da: %s\n"
            "   Il modello lingua italiana (ita.traineddata) è già incluso in tessdata/.\n"
            "   Poi riavvia main.py.\n",
            _TESSERACT_DOWNLOAD_URL,
        )
        sys.exit(1)

    # --- Lingua italiana per Tesseract (portabile su tutte le piattaforme) ---
    # ita.traineddata NON e' nel repo (cartella tessdata/ gitignored). Su alcune
    # piattaforme (brew tesseract, apt tesseract-ocr) l'italiano non e' incluso:
    # se manca, viene scaricato in una cartella locale del progetto e usato via
    # TESSDATA_PREFIX. Nessun intervento manuale.
    tesseract_exe = pytesseract.pytesseract.tesseract_cmd or "tesseract"
    _langs, _system_tessdata = _list_tessdata(tesseract_exe)

    if "ita" not in _langs:
        _local_tessdata.mkdir(parents=True, exist_ok=True)
        # TESSDATA_PREFIX SOSTITUISCE la cartella di sistema: prima di puntarla
        # qui si specchiano dentro i modelli che gia' esistono, cosi' eng e osd
        # non spariscono (il testo inglese dentro slide italiane verrebbe
        # riconosciuto col modello italiano, peggiorandone la precisione).
        if _system_tessdata is not None:
            _mirrored = _mirror_system_tessdata(_system_tessdata, _local_tessdata)
            if _mirrored:
                log.info(
                    "   Tessdata locale: copiati %d modelli di lingua di sistema (eng/osd).",
                    _mirrored,
                )
        ita_path = _local_tessdata / "ita.traineddata"
        if not ita_path.exists():
            log.info("🔧 Lingua italiana Tesseract mancante: scarico ita.traineddata (una tantum)...")
            try:
                import urllib.request

                # URL fisso del repo ufficiale tessdata_fast (nessun input utente)
                urllib.request.urlretrieve(
                    "https://github.com/tesseract-ocr/tessdata_fast/raw/main/ita.traineddata",
                    ita_path,
                )
                log.info("   ✅ ita.traineddata scaricato in %s", _local_tessdata)
            except Exception as e:  # noqa: BLE001 - rete: non deve bloccare il bootstrap
                log.warning("   \u26a0\ufe0f  Scaricamento ita.traineddata fallito: %s", e)
        if ita_path.exists():
            os.environ["TESSDATA_PREFIX"] = str(_local_tessdata)
            log.debug("   TESSDATA_PREFIX impostato su: %s", _local_tessdata)

    # --- ffmpeg (verifica rapida) ---
    try:
        subprocess.run(
            ["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False
        )
        log.debug("   ffmpeg trovato.")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.info("🔧 ffmpeg non trovato — tentativo auto-install...")
        ok = _try_system_install(
            "ffmpeg",
            winget_id="Gyan.FFmpeg.Shared",
            apt_pkg="ffmpeg",
            brew_pkg="ffmpeg",
        )
        if not ok:
            # ffmpeg e' indispensabile: video.py lo lancia come processo per
            # l'encoding (non e' una libreria linkata). Senza, la run arriva in
            # fondo e muore con un FileNotFoundError dopo dieci minuti di
            # trascrizione e OCR, che e' la diagnosi piu' lontana dalla causa
            # che si possa immaginare. Tesseract, due righe sopra, esce subito:
            # qui si fa lo stesso.
            try:
                subprocess.run(
                    ["ffmpeg", "-version"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                log.info("   ✅ ffmpeg installato nel PATH.")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                log.error(
                    "\n❌ FFMPEG NON TROVATO — obbligatorio per montare il video.\n"
                    "   Auto-install fallita. Scaricalo da: %s\n"
                    "   Oppure installalo con: winget install Gyan.FFmpeg.Shared\n"
                    "   (apt: `apt-get install ffmpeg` / brew: `brew install ffmpeg`)\n"
                    "   Poi riavvia.\n",
                    _FFMPEG_DOWNLOAD_URL,
                )
                sys.exit(1)

    # --- Primo avvio: i modelli ML si scaricano al primo uso ---
    # Lo dico PRIMA, non mentre succede: il silenzio di un download da qualche
    # gigabyte è indistinguibile da un blocco, e la cosa piu' probabile a quel
    # punto e' chiudere il programma e non riaprirlo.
    _annuncia_primo_avvio(
        _modelli_mancanti(
            Path(DEFAULT_EMBEDDING_CACHE_DIR),
            [Path(DEFAULT_OPENVINO_MODEL_DIR)],
        )
    )


# =====================================================================
# VALORI DI DEFAULT (sovrascrivibili da CLI)
# =====================================================================
DEFAULT_PDF = "presentazione.pdf"  # supporta anche .ppt/.pptx (convertiti automaticamente)
DEFAULT_OUTPUT_VIDEO = "video_finale.mp4"
DEFAULT_SLIDES_DIR = "temp_slides"

# --- Sincronizzazione semantica (sentence embeddings, offline) ---
# Modelli multilingue ONNX (fastembed). Supportano l'italiano senza prefissi.
#
# ⚠️ REGOLA DI SCELTA MODELLO (documentata ad oggi):
#   e5-large è il modello DA PREFERIRE. Testato A/B su podcast reale
#   (10 slide, flusso ordinato senza LLM): similarità media 0.791 vs
#   0.380 (MiniLM) e 0.421 (mpnet-base), e durate tutte bilanciate
#   (104-188s) senza anomalie, mentre MiniLM/mpnet producevano slide da
#   8-20s e 332s. Confermato anche sul podcast da 8 slide (0.834 vs 0.612
#   MiniLM e 0.534 mpnet, stesse ancore esatte a delta 0.00s).
#
#   QUANDO VALUTARE UN'ALTERNATIVA: solo se è (1) con accuratezza uguale o
#   maggiore E (2) più veloce/leggera. La precisione della sincronizzazione
#   è la priorità assoluta: un modello più veloce che abbassa la similarità
#   media o il segnale semantico NON va adottato. Prima di cambiare
#   DEFAULT_EMBEDDING_MODEL, ripetere il test A/B completo (vedi
#   config.py / README) verificando similarità media, durate bilanciate e
#   zero slide anomale. Tenere d'occhio i rilasci di intfloat/multilingual-e5
#   e i nuovi sentence-embedding multilingue ONNX più efficienti.
#
# Default: multilingual-e5-large (1024 dim, ~2.2 GB).
# Costo: +2.2 GB di download e ~37s di embedding per 24 min di podcast.
DEFAULT_EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL",
    "intfloat/multilingual-e5-large",
)
# Fallback automatico: se il modello principale non si carica (es. download
# interrotto), la sincronizzazione riprova con mpnet prima di arrendersi.
DEFAULT_EMBEDDING_MODEL_ALTERNATE = os.environ.get(
    "EMBEDDING_MODEL_ALTERNATE",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
)
DEFAULT_EMBEDDING_CACHE_DIR = os.environ.get("EMBEDDING_CACHE_DIR", str(CACHE_DIR / "embedding_model"))
# Thread ONNX per il calcolo degli embedding: il default di fastembed è
# conservativo e su CPU multi-core spreca core. Il sweet spot empirico su
# laptop Intel client è ~8 (a 16+ la banda memoria saturazione e peggiora).
# Override con EMBED_THREADS.
DEFAULT_EMBED_THREADS = _env_int("EMBED_THREADS", min(8, os.cpu_count() or 4))

DEFAULT_SEMANTIC_WINDOW = _env_float("SEMANTIC_WINDOW", 4.0)  # secondi per blocco
DEFAULT_SEMANTIC_MIN_DURATION = _env_float("SEMANTIC_MIN_DURATION", 3.0)  # durata minima slide
# L'unica soglia di qualità. Sulla scala grezza dei coseni la soglia sarebbe un
# presidio finto: misurata su dati reali, anche un testo senza senso dà 0.75
# contro una slide e il parlato reale non scende mai sotto 0.75, quindi una
# soglia a 0.10 non può mai scattare. (Il flag --semantic-min-sim e il relativo
# ramo di guardia sono stati rimossi: non esisteva una soglia grezza che
# discriminasse, e fingere di averne una era peggio che non averla.)
# Guard-rail sulla scala NORMALIZZATA (z-score per slide, la stessa usata dal
# posizionamento).
# Misurato su podcast reale (120 allineamenti plausibili per variante):
#   allineamento giusto        0.61-0.75
#   slide mescolate            0.30-0.43
#   ordine invertito           0.18
#   slide non correlate        0.07-0.09
#   slide quasi-duplicate      0.006
#   deck senza firma per slide 0.15  <- caso limite: non deve decidere il guard
# 0.45 = nessuna variante sbagliata accettata, nessun deck con contenuto
# per-slide rifiutato. Sotto questa soglia NON si scarta la timeline: si segnala
# (escalation al LLM, --strict-sync, riepilogo finale).
DEFAULT_SEMANTIC_MIN_Z = _env_float("SEMANTIC_MIN_Z", 0.45)
# Temperatura della "competizione softmax" tra slide per blocco: più è bassa,
# più il posizionamento privilegia i picchi locali (evita che una slide-riepilogo
# con similarità uniforme catturi metà dell'audio).
# 0.15 = bilanciamento ottimale trovato con test A/B su podcast reale.
DEFAULT_SEMANTIC_TEMPERATURE = _env_float("SEMANTIC_TEMPERATURE", 0.15)
# Fallback automatico al flusso ordinato quando il flusso auto-rilevato è
# 'free' (nessuna ancora 'slide N' né 'blocco successivo' pronunciata).
# Su podcast reali la selezione libera via LLM produce durate bilanciate ma
# richiede ~16 min (9Router); l'allineamento ordinato con soli embeddings
# produce durate altrettanto bilanciate (es. 47.8-136.0s) in ~1 min senza
# dipendenza dall'LLM. Disattivabile con --no-free-ordered-fallback.
DEFAULT_FREE_ORDERED_FALLBACK = os.environ.get("FREE_ORDERED_FALLBACK", "1") == "1"

# --- Parametri tecnici (sovrascrivibili da .env) ---
DEFAULT_TRANSCRIPT_WINDOW = 3.0  # secondi per raggruppamento parole
DEFAULT_MIN_WORD_LENGTH = 2  # ignora parole più corte
DEFAULT_VIDEO_FPS = _env_int("VIDEO_FPS", 5)  # fps > 1 evita che l'ultimo frame tagli l'audio finale
DEFAULT_VIDEO_BUFFER_SEC = _env_float("VIDEO_BUFFER_SEC", 3.0)  # secondi extra sul video per proteggere l'audio finale
# Risoluzione massima video (larghezza, altezza) — sovrascrivibile con VIDEO_RES="WxH"
_VIDEO_RES_ENV = os.environ.get("VIDEO_RES")
if _VIDEO_RES_ENV and "x" in _VIDEO_RES_ENV:
    try:
        _w, _h = _VIDEO_RES_ENV.lower().split("x")[:2]
        DEFAULT_VIDEO_RES = (int(_w), int(_h))
    except ValueError:
        log.warning("   VIDEO_RES non valido ('%s'), uso default 1920x1080.", _VIDEO_RES_ENV)
        DEFAULT_VIDEO_RES = (1920, 1080)
else:
    DEFAULT_VIDEO_RES = (1920, 1080)
# Thread di encoding del video. Stesso tetto di 8 dei thread embedding: il
# default nasce dalla misura sullo Snapdragon X Elite, dove oltre 8 thread la
# banda memoria satura e il risultato peggiora. Su una CPU con piu' core
# fisici quel tetto e' pero' una scelta conservativa ereditata, non un muro:
# conviene misurarlo (stessa procedura di EMBED_THREADS) prima di alzarlo.
# Override con VIDEO_THREADS.
DEFAULT_VIDEO_THREADS = _env_int("VIDEO_THREADS", min(8, os.cpu_count() or 4))
# Motore di rendering video: 'ffmpeg' (concat demuxer, encoding diretto, veloce)
# o 'moviepy' (percorso legacy, richiesto per --transitions > 0).
_VIDEO_ENGINE_ENV = os.environ.get("VIDEO_ENGINE", "").strip().lower()
if _VIDEO_ENGINE_ENV in {"ffmpeg", "moviepy"}:
    DEFAULT_VIDEO_ENGINE = _VIDEO_ENGINE_ENV
else:
    if _VIDEO_ENGINE_ENV:
        log.warning("   VIDEO_ENGINE non valido ('%s'), uso 'ffmpeg'.", _VIDEO_ENGINE_ENV)
    DEFAULT_VIDEO_ENGINE = "ffmpeg"
DEFAULT_OCR_DPI = _env_int("OCR_DPI", 300)
DEFAULT_OCR_LANG = os.environ.get("OCR_LANG", "ita")
DEFAULT_OCR_WORKERS = _env_int("OCR_WORKERS", min(4, os.cpu_count() or 2))
DEFAULT_TRANSITION_DURATION = 0.0  # secondi (0 = nessuna transizione)
DEFAULT_TRANSCRIBER = os.environ.get("TRANSCRIBER", "auto")  # 'auto'/'openvino'/'whisper'
DEFAULT_WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")  # tiny/base/small/medium/large
DEFAULT_WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")  # 'cpu' o 'cuda'
DEFAULT_WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")  # int8 (cpu) / float16 (cuda)
# Thread per faster-whisper. Il default di faster-whisper sottoutilizza le CPU
# con piu' core (misurato su Snapdragon X Elite: 8 thread ~27% piu' veloci di 4 su
# clip da 60s), e il cap a 8 e' per la stessa ragione di EMBED_THREADS: su
# Snapdragon la banda memoria satura oltre 8. Altrove il limite e' arbitrario,
# quindi e' esposto. Override con WHISPER_THREADS.
DEFAULT_WHISPER_THREADS = _env_int("WHISPER_THREADS", min(8, os.cpu_count() or 4))

# Beam size faster-whisper. 1 (greedy) = default MISURATO.
#
# Misura su podcast reale (1058s di audio, faster-whisper small int8):
#   beam 5 sequenziale   91.5s su uno slice di 240s  (RTF 0.381)
#   beam 1 sequenziale   74.6s                       (RTF 0.311)
#   beam 1 + batch 8     40.1s                       (RTF 0.167)  <- default
#   beam 1 + batch 8 + 12 thread: 44.2s (PEGGIO: 8 thread e' il sweet spot)
#
# Precisione della sincronizzazione sull'audio INTERO, 10 ancore 'slide N':
#   beam 5 -> beam 1: delta medio 0.050s, delta MASSIMO 0.150s, nessuna ancora
#   persa o aggiunta. Il drift e' 20x sotto la durata minima di una slide
#   (DEFAULT_SEMANTIC_MIN_DURATION = 3s), quindi non sposta alcun confine.
# Override con WHISPER_BEAM (5 = massima accuratezza del testo).
DEFAULT_WHISPER_BEAM = _env_int("WHISPER_BEAM", 1)
# Decoding a batch (BatchedInferencePipeline di faster-whisper): stesso modello,
# stessi pesi, stessa decodifica -> nessuna perdita di qualita', solo throughput.
# 8 = batch ottimale misurato (16 non migliora, 40.1s vs 40.1s).
# 0 o 1 = decodifica sequenziale (fallback automatico se la classe manca).
# Override con WHISPER_BATCH.
DEFAULT_WHISPER_BATCH = _env_int("WHISPER_BATCH", 8)
# Beam size della decodifica ACCURATA: usato quando la timeline NON è vincolata
# dalle ancore 'slide N', cioè quando il testo è l'unico segnale di
# sincronizzazione (flusso libero, o quasi nessuna slide annunciata).
# Override con WHISPER_BEAM_ACCURATE.
DEFAULT_WHISPER_BEAM_ACCURATE = _env_int("WHISPER_BEAM_ACCURATE", 5)
# Scelta automatica del beam (vedi main._needs_accurate_beam). La decisione non
# può essere presa prima di trascrivere: le ancore si conoscono solo dal testo.
# Quindi si trascrive veloce (greedy) e, SOLO se le ancore vincolano meno di
# questa frazione delle slide, la trascrizione viene rifatta con la decodifica
# accurata: lì i confini sono stimati dal contenuto e la qualità del testo conta.
#   slide vincolate / (slide totali - 1) < AUTO_BEAM_PINNED_RATIO -> rifà
# Override con AUTO_BEAM_PINNED_RATIO (1.1 = forza sempre il percorso accurato).
AUTO_BEAM_PINNED_RATIO = _env_float("AUTO_BEAM_PINNED_RATIO", 0.5)
# Attiva la scelta automatica (disattivabile con --no-auto-beam o AUTO_BEAM=0).
DEFAULT_AUTO_BEAM = os.environ.get("AUTO_BEAM", "1") == "1"
# Quanto deve vincere la decodifica ACCURATA (in `avg_z`) per essere preferita
# alla veloce quando le due trascrizioni vengono confrontate. 0.0 = basta non
# perdere: a parità resta l'accurata, ma se la veloce ha il segnale migliore si
# usa quella e non si pagano minuti di trascrizione per nulla.
#
# ⚠️ RISOLUZIONE MISURATA DEL PUNTEGGIO (probe su podcast reale, 261 blocchi):
# confrontando ~18 timeline candidate (giusta, spostate di 4s, invertite,
# mescolate, casuali) con la verità nota, il punteggio ordinato per accuratezza:
#   - differenze GRANDI = separazione netta: ordine invertito 0.05 contro 0.68
#     della timeline corretta (accuracy 0.00 contro 0.97);
#   - differenze PICCOLE = non distinguibili: spostando un confine di 8-20s il
#     punteggio può SALIRE (0.530) mentre l'accuratezza scende (0.97 -> 0.84),
#     perché il picco di somiglianza sta oltre il confine reale (lo speaker
#     anticipa l'argomento). Sotto ~0.05-0.10 il punteggio non dice chi è
#     meglio: concordanza con la verità 79.7%, Spearman +0.743.
# Quindi: 0.0 = "la misura decide sempre", anche su scarti che non distingue.
# Con 0.05 (consigliato) o 0.10 la veloce subentra solo quando il vantaggio è
# fuori dalla banda di rumore.
# Override con AUTO_BEAM_AB_MARGIN.
AUTO_BEAM_AB_MARGIN = _env_float("AUTO_BEAM_AB_MARGIN", 0.0)
# Il punteggio di somiglianza è un PROXY: due segnali espliciti lo battono sempre
# (vedi main._use_accurate_transcript).
#
# 1. ANCORE. Se la decodifica accurata perde un'ancora che la veloce aveva, la
#    veloce vince a prescindere dal punteggio: le ancore sono riferimenti
#    espliciti nel parlato, il punteggio è una stima. Caso reale misurato: beam 5
#    ha saltato 10,4s di parlato (345,9s -> 356,3s) contenenti l'annuncio
#    "passiamo slide 2", passando da 2 ancore a 1 e facendo partire un remap
#    della numerazione (slide 1 -> 8) che ha compresso 7 slide in 81s.
# 2. CONFONDIBILITÀ. Se il deck ha slide quasi-duplicate oltre questa soglia, la
#    misura di allineamento è rumore: il punteggio non vota e resta la scelta
#    prudente (accurata). Stessa soglia di semantic_sync.weak_signal.
#    Override con AUTO_BEAM_CONFUSABILITY_MAX (1.1 = ignora sempre la
#    confondibilità).
AUTO_BEAM_CONFUSABILITY_MAX = _env_float("AUTO_BEAM_CONFUSABILITY_MAX", 0.5)
# Motore OpenVINO GenAI (più veloce su iGPU Intel). Modello IR pre-convertito,
# scaricabile da HuggingFace: OpenVINO/whisper-small-fp16-ov
DEFAULT_OPENVINO_MODEL_DIR = os.environ.get("OPENVINO_MODEL_DIR", str(CACHE_DIR / "whisper_openvino_small"))
DEFAULT_OPENVINO_DEVICE = os.environ.get("OPENVINO_DEVICE", "GPU")  # 'GPU' (iGPU) o 'CPU'
DEFAULT_OPENVINO_MODEL_ID = os.environ.get("OPENVINO_MODEL_ID", "OpenVINO/whisper-small-fp16-ov")

# =====================================================================
# STOPWORDS ITALIANE
# =====================================================================
STOPWORDS_ITA = frozenset(
    [
        "il",
        "lo",
        "la",
        "i",
        "gli",
        "le",
        "di",
        "a",
        "da",
        "in",
        "con",
        "su",
        "per",
        "tra",
        "fra",
        "un",
        "una",
        "uno",
        "del",
        "dello",
        "della",
        "dei",
        "degli",
        "delle",
        "al",
        "allo",
        "alla",
        "ai",
        "agli",
        "alle",
        "dal",
        "dallo",
        "dalla",
        "dai",
        "dagli",
        "dalle",
        "nel",
        "nello",
        "nella",
        "nei",
        "negli",
        "nelle",
        "sul",
        "sullo",
        "sulla",
        "sui",
        "sugli",
        "sulle",
        "che",
        "non",
        "ci",
        "ne",
        "si",
        "mi",
        "ti",
        "vi",
        "ma",
        "ed",
        "anche",
    ]
)

# Parole di TRANSIZIONE che NON vanno MAI filtrate — segnalano cambi di slide
TRANSITION_WORDS_ITA = frozenset(
    [
        "passiamo",
        "vediamo",
        "guardiamo",
        "guardate",
        "osserviamo",
        "slide",
        "diapositiva",
        "prossima",
        "successiva",
        "precedente",
        "andiamo",
        "parliamo",
        "ecco",
        "eccoci",
        "quindi",
        "dunque",
        "allora",
        "ora",
        "adesso",
        "invece",
        "prima",
        "dopo",
        "infine",
        "iniziamo",
        "concludiamo",
        "conclusione",
        "passo",
        "passa",
        "affrontiamo",
        "occupiamoci",
        "dedichiamoci",
        "concentriamoci",
        "torniamo",
        "riprendiamo",
        "introduciamo",
        "presentiamo",
        "mostriamo",
        "illustriamo",
        "spieghiamo",
        "approfondiamo",
        # Per flusso "audio dibattito → slide": segnale "Passiamo al blocco successivo"
        "blocco",
        "successivo",
    ]
)


def get_stopwords(lang: str = "ita") -> frozenset:
    """Restituisce le stopwords per la lingua, escludendo le parole di transizione."""
    if lang == "ita":
        return STOPWORDS_ITA - TRANSITION_WORDS_ITA
    return frozenset()


# =====================================================================
# ARGPARSE
# =====================================================================
def parse_args(argv: list | None = None) -> argparse.Namespace:
    """Configura e parsare gli argomenti da riga di comando."""
    parser = argparse.ArgumentParser(
        description="Sincronizza PDF + audio in un video con timeline generata "
        "da embeddings semantici (offline, senza LLM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python main.py --pdf pres.pdf --audio podcast.m4a
  python main.py --pdf pres.pdf --audio podcast.m4a --dry-run --debug
  python main.py --pdf pres.pdf --audio podcast.m4a --transitions 0.5 --lang eng
  python main.py --pdf pres.pdf --audio podcast.m4a --preview
        """,
    )

    # File I/O
    parser.add_argument(
        "--pdf", default=DEFAULT_PDF, help=f"Percorso presentazione PDF, PPT o PPTX (default: {DEFAULT_PDF})"
    )
    parser.add_argument("--audio", default=None, help="Percorso file audio (default: cerca podcast.mp3/m4a/wav)")
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT_VIDEO, help=f"Percorso video output (default: {DEFAULT_OUTPUT_VIDEO})"
    )
    parser.add_argument(
        "--slides-dir", default=DEFAULT_SLIDES_DIR, help=f"Directory temporanea slide (default: {DEFAULT_SLIDES_DIR})"
    )

    # Transcriber (faster-whisper, unico motore)
    parser.add_argument(
        "--whisper-model",
        default=DEFAULT_WHISPER_MODEL,
        help=f"Dimensione modello faster-whisper (tiny/base/small/medium/large). "
        f"tiny/base = più veloce, medium/large = più preciso (default: {DEFAULT_WHISPER_MODEL})",
    )
    parser.add_argument(
        "--transcriber",
        default=DEFAULT_TRANSCRIBER,
        choices=["auto", "openvino", "whisper"],
        help="Motore di trascrizione. 'auto' (default): usa OpenVINO GenAI "
        "se il modello IR è presente, altrimenti faster-whisper. "
        "'openvino': solo OpenVINO (errore se manca). 'whisper': solo "
        "faster-whisper. OpenVINO è ~1.5x più veloce sulla iGPU Intel.",
    )
    parser.add_argument(
        "--openvino-model-dir",
        default=DEFAULT_OPENVINO_MODEL_DIR,
        help=f"Directory del modello Whisper OpenVINO IR (default: {DEFAULT_OPENVINO_MODEL_DIR})",
    )
    parser.add_argument(
        "--openvino-device",
        default=DEFAULT_OPENVINO_DEVICE,
        help=f"Device OpenVINO: 'GPU' (iGPU Intel, default) o 'CPU' (default: {DEFAULT_OPENVINO_DEVICE})",
    )
    parser.add_argument(
        "--whisper-device",
        default=DEFAULT_WHISPER_DEVICE,
        help=f"Device faster-whisper: 'cpu' o 'cuda' (default: {DEFAULT_WHISPER_DEVICE})",
    )
    parser.add_argument(
        "--whisper-compute-type",
        default=DEFAULT_WHISPER_COMPUTE_TYPE,
        help=f"Compute type faster-whisper (default: {DEFAULT_WHISPER_COMPUTE_TYPE})",
    )
    parser.add_argument(
        "--whisper-beam",
        type=int,
        default=DEFAULT_WHISPER_BEAM,
        help=f"Beam size faster-whisper (default: {DEFAULT_WHISPER_BEAM}). "
        f"1 = più veloce (misurato: ancore 'slide N' entro 0.15s da beam 5), "
        f"5 = massima accuratezza del testo. Con 1 (default) la pipeline sceglie "
        f"DA SOLA: se le slide sono vincolate dalle ancore 'slide N' tiene la "
        f"decodifica veloce, altrimenti rifà la trascrizione a beam "
        f"{DEFAULT_WHISPER_BEAM_ACCURATE} (il testo è l'unico segnale di "
        f"sincronizzazione). Un valore >= 2 disattiva la scelta automatica",
    )
    parser.add_argument(
        "--no-auto-beam",
        action="store_false",
        dest="auto_beam",
        default=DEFAULT_AUTO_BEAM,
        help="Disattiva la scelta automatica del beam: usa esattamente "
        "--whisper-beam, senza mai rifare la trascrizione per accuratezza",
    )
    parser.add_argument(
        "--whisper-batch",
        type=int,
        default=DEFAULT_WHISPER_BATCH,
        help=f"Batch size del decoding faster-whisper (default: {DEFAULT_WHISPER_BATCH}). "
        f"Stesso modello e stessa decodifica, solo più throughput; 0 = sequenziale. "
        f"Se il decoder a batch non è disponibile la trascrizione ripiega da sola "
        f"sul percorso sequenziale",
    )
    parser.add_argument(
        "--no-auto-setup",
        action="store_true",
        help="Disabilita il rilevamento automatico hardware al primo avvio",
    )
    parser.add_argument(
        "--force-setup",
        action="store_true",
        help="Rifai il rilevamento hardware anche se già configurato",
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="Disabilita il controllo aggiornamenti pacchetti all'avvio",
    )
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="Controlla gli aggiornamenti ma non chiede di installarli (solo notifica)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Non chiedere conferma prima della sincronizzazione stimata "
        "(procede automaticamente anche con slide non annunciate)",
    )
    parser.add_argument(
        "--require-full-anchors",
        action="store_true",
        help="Nel flusso ordinato, interrompi se il podcast non annuncia "
        "TUTTE le slide (ancore 'slide N' incomplete): le slide non "
        "annunciate verrebbero stimate per contenuto, con durate poco "
        "affidabili. Utile in batch/CI (es. genera_video.bat), dove non "
        "si vuole generare un video degradato: rigenera l'audio e rilancia.",
    )
    parser.add_argument(
        "--openvino-download",
        action="store_true",
        help="Scarica una tantum il modello Whisper OpenVINO IR "
        f"({DEFAULT_OPENVINO_MODEL_ID}) in {DEFAULT_OPENVINO_MODEL_DIR}, "
        "poi esce. Necessario prima del primo uso con --transcriber openvino/auto.",
    )
    parser.add_argument(
        "--prefetch-models",
        action="store_true",
        help="Scarica tutti i modelli ML (embedding e5, Whisper, OpenVINO IR) "
        "poi esce, senza toccare PDF o audio. Utile dopo un clone o un "
        "cambio di macchina: tiene i download fuori dalla prima run reale, "
        "dove un timeout di rete troncherebbe tutto a meta' senza distinguere "
        "'modello mancante' da 'download fallito'.",
    )

    # Opzioni
    parser.add_argument("--lang", default=DEFAULT_OCR_LANG, help=f"Lingua OCR (default: {DEFAULT_OCR_LANG})")
    parser.add_argument(
        "--transitions",
        type=float,
        default=DEFAULT_TRANSITION_DURATION,
        help="Durata dissolvenza tra slide in secondi (0=nessuna)",
    )
    parser.add_argument(
        "--engine",
        default=DEFAULT_VIDEO_ENGINE,
        choices=["ffmpeg", "moviepy"],
        help="Motore di rendering video: 'ffmpeg' usa il concat demuxer "
        "(encoding diretto, molto più veloce, nessun crossfade); 'moviepy' "
        "usa il percorso legacy (più lento, richiesto per --transitions > 0). "
        f"(default: {DEFAULT_VIDEO_ENGINE})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Ferma dopo la generazione timeline, non produce video")
    parser.add_argument(
        "--preview", action="store_true", help="Mostra la timeline in formato visuale e esci (non genera il video)"
    )
    parser.add_argument("--no-cache", action="store_true", help="Ignora la cache e rifai tutto da zero")
    parser.add_argument("--debug", action="store_true", help="Logging DEBUG dettagliato")
    parser.add_argument(
        "--ocr-workers",
        type=int,
        default=DEFAULT_OCR_WORKERS,
        help=f"Thread paralleli per OCR (default: {DEFAULT_OCR_WORKERS})",
    )
    parser.add_argument(
        "--dpi", type=int, default=DEFAULT_OCR_DPI, help=f"DPI rendering slide (default: {DEFAULT_OCR_DPI})"
    )
    parser.add_argument(
        "--flow",
        default=None,
        choices=["slide-audio", "audio-slide", "free"],
        help="Flusso di sincronizzazione: 'slide-audio' (speaker dicono "
        "'passiamo alla slide X'), 'audio-slide' (speaker dicono "
        "'passiamo al blocco successivo') oppure 'free' (riordino "
        "libero: le slide possono apparire in qualsiasi ordine e "
        "ripetersi, seguendo il contenuto del podcast). Default: "
        "auto-detect: slide-audio/audio-slide se il podcast segue "
        "i vincoli del prompt NotebookLM ('slide N' / 'blocco "
        "successivo'), altrimenti 'free'.",
    )
    parser.add_argument(
        "--no-free-ordered-fallback",
        action="store_true",
        # Default preso dalla variabile d'ambiente: senza questo default=True
        # della costante era irrilevante e FREE_ORDERED_FALLBACK=0 non faceva
        # nulla (costante mai letta).
        default=not DEFAULT_FREE_ORDERED_FALLBACK,
        help="Disattiva il fallback automatico al flusso ordinato "
        "quando il flusso auto-rilevato è 'free' (nessuna ancora "
        "'slide N'). Default: attivo (vedi config.py), così un "
        "podcast senza ancore usa l'allineamento ordinato con soli "
        "embeddings (~1 min, durate bilanciate) invece della "
        "selezione libera via LLM (~16 min con 9Router).",
    )
    parser.add_argument(
        "--skip-slides",
        default="",
        help="Slide da NON mostrare nel video (lista separata da "
        "virgole, es. 4,9,11). Le slide saltate dal podcast vengono "
        "escluse: i loro segmenti mostrano la slide precedente valida, "
        "così l'audio resta sincronizzato e il video mostra solo le "
        "slide effettivamente coperte. Il PDF resta intatto (le ancore "
        "'slide N' del parlato non si spostano). Solo flusso ordinato "
        "(slide-audio/audio-slide); nel flusso libero è ignorato.",
    )
    parser.add_argument(
        "--strict-sync",
        action="store_true",
        help="Interrompe la pipeline se un controllo di sincronizzazione "
        "fallisce: prima di generare il video se un segmento di durata "
        "anomala risulta disallineato dal contenuto (il parlato somiglia a "
        "un'altra slide) o se la revisione LLM (--llm-review) segnala "
        "discrepanze; dopo la generazione se la verifica frame vs slide "
        "(--verify-video, attivata automaticamente da questo flag) trova "
        "segmenti che mostrano una slide diversa. Il report completo dei "
        "segmenti è sempre salvato in .cache/sync_report.json. "
        "Default: disattivato (avviso soltanto).",
    )
    parser.add_argument(
        "--verify-video",
        action="store_true",
        help="Dopo la generazione estrae un frame a metà di ogni segmento e "
        "lo confronta con la slide attesa, per certificare cosa è davvero a "
        "schermo (non solo che la timeline sia coerente). I frame estratti "
        "restano in .cache/verify_frames/ e l'esito finisce in "
        "sync_report.json. Attivata automaticamente da --strict-sync. "
        "Costo: una decina di secondi su un video di 15 minuti.",
    )
    parser.add_argument(
        "--semantic-model",
        default=DEFAULT_EMBEDDING_MODEL,
        help=f"Modello embedding per la sincronizzazione semantica "
        f"(default: {DEFAULT_EMBEDDING_MODEL}; fallback "
        f"automatico: {DEFAULT_EMBEDDING_MODEL_ALTERNATE})",
    )
    parser.add_argument(
        "--semantic-cache-dir",
        default=DEFAULT_EMBEDDING_CACHE_DIR,
        help="Directory di cache per i modelli embedding (default: .cache/embedding_model)",
    )
    parser.add_argument(
        "--semantic-window",
        type=float,
        default=DEFAULT_SEMANTIC_WINDOW,
        help=f"Secondi per blocco trascrizione nella sincronizzazione semantica (default: {DEFAULT_SEMANTIC_WINDOW})",
    )
    parser.add_argument(
        "--semantic-min-duration",
        type=float,
        default=DEFAULT_SEMANTIC_MIN_DURATION,
        help=f"Durata minima di una slide nella sincronizzazione "
        f"semantica, in secondi (default: {DEFAULT_SEMANTIC_MIN_DURATION})",
    )
    parser.add_argument(
        "--no-auto-repair",
        dest="auto_repair",
        action="store_false",
        help="Disattiva la riparazione automatica: quando la verifica del video "
        "(--verify-video) trova un segmento con la slide sbagliata, la pipeline "
        "sposta da sola quel confine con il motore embedding e rigenera il video. "
        "Con questo flag l'esito resta un avviso (il video non viene rifatto)",
    )
    parser.add_argument(
        "--semantic-min-z",
        type=float,
        default=DEFAULT_SEMANTIC_MIN_Z,
        help=f"Soglia sul picco medio NORMALIZZATO (z-score per slide) sotto cui "
        f"la sincronizzazione è a bassa fiducia: segnala, alimenta l'escalation "
        f"al LLM e il gate --strict-sync (default: {DEFAULT_SEMANTIC_MIN_Z})",
    )
    parser.add_argument(
        "--semantic-temperature",
        type=float,
        default=DEFAULT_SEMANTIC_TEMPERATURE,
        help=f"Temperatura della competizione softmax tra slide per blocco: "
        f"più bassa = privilegia i picchi locali, evita che una "
        f"slide-riepilogo catturi metà dell'audio "
        f"(default: {DEFAULT_SEMANTIC_TEMPERATURE})",
    )

    # Selezione slide via LLM (opzionale, supera il tetto dell'embedding)
    parser.add_argument(
        "--llm",
        default="off",
        choices=["off", "auto", "9router"],
        help="Selezione slide via LLM. 'off' = solo embedding "
        "locale (nessuna rete). 'auto' e '9router' sono oggi "
        "EQUIVALENTI: l'unico provider è 9Router, quindi entrambi "
        "usano la stessa cascata di tre modelli (comboact → "
        "Mistral 24B → Gemma 31B) e, se il router non risponde, "
        "ripiegano sull'embedding locale (flusso libero: "
        "interruzione esplicita). Nel flusso libero (senza segnali 'slide "
        "N') sceglie la slide per ogni chunk; nei flussi "
        "ordinati (slide-audio/audio-slide) posiziona SOLO "
        "le slide senza ancora esplicita, rispettando le "
        "ancore deterministiche. Se nessun servizio "
        "risponde si ripiega sull'embedding senza interrompere.",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Override del modello LLM (es. comboact, "
        "openrouter/google/gemma-4-26b-a4b-it:free, "
        "cf/@cf/mistralai/mistral-small-3.1-24b-instruct). "
        "Default: la combo 'comboact' configurata per "
        "l'endpoint 9Router.",
    )
    parser.add_argument(
        "--llm-chunk", type=float, default=30.0, help="Secondi per chunk trascrizione inviata all'LLM (default: 30.0)"
    )
    parser.add_argument(
        "--llm-review",
        action="store_true",
        help="Dopo la timeline LLM nel flusso libero, esegue un "
        "secondo passaggio LLM che ri-verifica la selezione "
        "chunk->slide e avvisa (senza modificare la timeline) "
        "sui chunk sospetti. Costo: una chiamata extra (free, "
        "cachata). Default: disattivato.",
    )
    parser.add_argument(
        "--llm-wait-timeout",
        type=float,
        default=0.0,
        help="Secondi massimi di attesa che 9Router sia avviato "
        "prima di ripiegare sull'embedding, quando serve l'LLM ma "
        "il router non risponde. 0 (default) = attesa "
        "illimitata: il processo si mette in pausa con un "
        "avviso e riprende appena 9Router è online; si può "
        "premere 'S' in ogni momento per saltare e usare "
        "subito l'embedding locale.",
    )
    parser.add_argument(
        "--llm-local-threshold",
        type=int,
        default=2,
        help="Nel flusso ordinato (slide-audio/audio-slide), numero "
        "massimo di slide senza ancora gestite dal raffinamento "
        "locale (embeddings) al posto di 9Router. Oltre questa "
        "soglia l'LLM cloud viene usato come prima (default: 2). "
        "Imposta 0 per usare sempre 9Router nel flusso ibrido.",
    )
    parser.add_argument("--log-file", default=None, help="Percorso file di log (salva i log anche su file)")

    args = parser.parse_args(argv)

    # Post-processing
    # --strict-sync è la modalità "non consegnare un video sospetto": attiva
    # anche la verifica frame vs slide, che è l'unico controllo sull'artefatto.
    if args.strict_sync:
        args.verify_video = True
    if args.debug:
        setup_debug_logging()

    # Logging su file se richiesto
    if args.log_file:
        file_handler = logging.FileHandler(args.log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s"))
        log.addHandler(file_handler)
        log.info("   Log salvato anche in: %s", args.log_file)

    # Risolvi percorsi relativi a BASE_DIR
    args.pdf_path = BASE_DIR / args.pdf
    args.output_video = BASE_DIR / args.output
    args.slides_dir = BASE_DIR / args.slides_dir

    # Sincronizzazione semantica
    args.semantic_cache_dir = args.semantic_cache_dir or DEFAULT_EMBEDDING_CACHE_DIR
    # Una finestra non positiva non ha senso e, senza clamp, faceva girare
    # build_windows all'infinito. build_windows si difende comunque da solo
    # (difesa in profondita'), ma qui l'utente viene avvisato che il valore
    # richiesto non e' quello applicato, invece di scoprirlo dalla qualita'
    # dell'allineamento.
    if args.semantic_window <= 0:
        log.warning(
            "   ⚠️  --semantic-window %.3f non valido: uso il minimo di 1.0s per blocco.", args.semantic_window
        )
        args.semantic_window = 1.0
    return args
