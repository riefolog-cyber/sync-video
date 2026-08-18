#!/usr/bin/env python3
"""
Configurazione centralizzata per il pipeline slide-audio.
Costanti, argparse, logging e setup automatico dipendenze.
"""

import argparse
import contextlib
import logging
import os
import subprocess
import sys
from pathlib import Path

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
            if end_quote != -1:
                value = value[1:end_quote]
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

    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

    def tqdm(iterable, desc="", **kwargs):  # type: ignore[no-redef]
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
_REQUIRED_PACKAGES = {
    "pymupdf": "pymupdf",
    "PIL": "pillow",
    "pytesseract": "pytesseract",
    "pydub": "pydub",
    "moviepy": "moviepy",
    "numpy": "numpy",
    "tqdm": "tqdm",
    "fastembed": "fastembed",
    "faster_whisper": "faster-whisper",
}

_TESSERACT_DOWNLOAD_URL = "https://github.com/UB-Mannheim/tesseract/wiki"
_FFMPEG_DOWNLOAD_URL = "https://ffmpeg.org/download.html"


def _try_system_install(name: str, winget_id: str, apt_pkg: str, brew_pkg: str) -> bool:
    """Tenta auto-install tool di sistema via package manager nativo.

    Prova in ordine: winget (Windows), apt-get (Linux), brew (macOS).
    Restituisce True se l'installazione è riuscita.
    """
    commands = []
    if sys.platform == "win32":
        commands.append(
            (
                ["winget", "install", "--accept-source-agreements", "--accept-package-agreements", winget_id],
                f"winget install {winget_id}",
            )
        )
    elif sys.platform == "darwin":
        if brew_pkg:
            commands.append((["brew", "install", brew_pkg], f"brew install {brew_pkg}"))
    if apt_pkg:
        commands.append(
            (
                ["sudo", "apt-get", "install", "-y", apt_pkg],
                f"sudo apt-get install -y {apt_pkg}",
            )
        )

    for cmd, label in commands:
        try:
            print(f"   ⏳ {label} ...", end=" ", flush=True)
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=True)
            print("✅")
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            print("❌ (tento alternativa)")
    return False


def _try_pip_install(package: str) -> bool:
    """Tenta di installare un pacchetto via pip. Restituisce True se ok."""
    try:
        print(f"   ⏳ pip install {package} ...", end=" ", flush=True)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        print("✅")
        return True
    except subprocess.CalledProcessError:
        print("❌")
        return False
    except subprocess.TimeoutExpired:
        print("⏰ timeout")
        return False


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

    # --- Pip packages ---
    missing = []
    for import_name, pip_name in _REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append((import_name, pip_name))

    if missing:
        log.info("🔧 Pacchetti mancanti: %s", ", ".join(p[1] for p in missing))
        log.info("   Installazione automatica in corso...")

        all_ok = True
        for _import_name, pip_name in missing:
            log.info("   pip install %s ...", pip_name)
            if _try_pip_install(pip_name):
                log.info("   ✅ %s installato.", pip_name)
            else:
                log.warning("   ❌ %s NON installato.", pip_name)
                all_ok = False

        if not all_ok:
            log.error(
                "\nAlcuni pacchetti non sono stati installati. Installa manualmente:\n"
                "   pip install %s\n"
                "Oppure: pip install -r requirements.txt",
                " ".join(p[1] for p in missing),
            )
            sys.exit(1)

        log.info("   ✅ Tutti i pacchetti installati.")

    # --- Tesseract OCR ---
    import pytesseract  # garantito installato dal bootstrap pip qui sopra

    # --- Tessdata locale portabile (ita.traineddata senza bisogno di admin) ---
    # Se esiste una cartella "tessdata" nel progetto, usala al posto di quella
    # di sistema: evita "TesseractError: language 'ita' not found".
    _local_tessdata = BASE_DIR / "tessdata"
    if _local_tessdata.is_dir() and any(_local_tessdata.glob("*.traineddata")):
        os.environ["TESSDATA_PREFIX"] = str(_local_tessdata)
        log.debug("   TESSDATA_PREFIX impostato su: %s", _local_tessdata)

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
            winget_id="UB-Mannheim.Tesseract",
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

    if not tesseract_found:
        log.error(
            "\n❌ TESSERACT OCR NON TROVATO — necessario per estrarre il testo dalle slide.\n"
            "   Auto-install fallita. Scaricalo da: %s\n"
            "   Il modello lingua italiana (ita.traineddata) è già incluso in tessdata/.\n"
            "   Poi riavvia main.py.\n",
            _TESSERACT_DOWNLOAD_URL,
        )
        sys.exit(1)

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
            winget_id="ffmpeg",
            apt_pkg="ffmpeg",
            brew_pkg="ffmpeg",
        )
        if not ok:
            log.warning(
                "⚠️  ffmpeg non trovato e auto-install fallita.\n"
                "   Installa manualmente: %s\n"
                "   (winget install ffmpeg — su Windows 10/11)",
                _FFMPEG_DOWNLOAD_URL,
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
DEFAULT_SEMANTIC_WINDOW = _env_float("SEMANTIC_WINDOW", 4.0)  # secondi per blocco
DEFAULT_SEMANTIC_MIN_DURATION = _env_float("SEMANTIC_MIN_DURATION", 3.0)  # durata minima slide
DEFAULT_SEMANTIC_MIN_SIM = _env_float("SEMANTIC_MIN_SIM", 0.10)  # soglia qualità
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
DEFAULT_VIDEO_THREADS = min(8, os.cpu_count() or 4)
DEFAULT_OCR_DPI = _env_int("OCR_DPI", 300)
DEFAULT_OCR_LANG = os.environ.get("OCR_LANG", "ita")
DEFAULT_OCR_WORKERS = _env_int("OCR_WORKERS", min(4, os.cpu_count() or 2))
DEFAULT_TRANSITION_DURATION = 0.0  # secondi (0 = nessuna transizione)
DEFAULT_TRANSCRIBER = os.environ.get("TRANSCRIBER", "whisper")  # 'whisper'
DEFAULT_WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")  # tiny/base/small/medium/large
DEFAULT_WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")  # 'cpu' o 'cuda'
DEFAULT_WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")  # int8 (cpu) / float16 (cuda)
# Motore OpenVINO GenAI (più veloce su iGPU Intel). Modello IR pre-convertito,
# scaricabile da HuggingFace: OpenVINO/whisper-small-fp16-ov
DEFAULT_OPENVINO_MODEL_DIR = os.environ.get("OPENVINO_MODEL_DIR", str(CACHE_DIR / "whisper_openvino_small"))
DEFAULT_OPENVINO_DEVICE = os.environ.get("OPENVINO_DEVICE", "GPU")  # 'GPU' (iGPU) o 'CPU'
DEFAULT_OPENVINO_MODEL_ID = "OpenVINO/whisper-small-fp16-ov"

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
        default="auto",
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
        "--openvino-download",
        action="store_true",
        help="Scarica una tantum il modello Whisper OpenVINO IR "
        f"({DEFAULT_OPENVINO_MODEL_ID}) in {DEFAULT_OPENVINO_MODEL_DIR}, "
        "poi esce. Necessario prima del primo uso con --transcriber openvino/auto.",
    )

    # Opzioni
    parser.add_argument("--lang", default=DEFAULT_OCR_LANG, help=f"Lingua OCR (default: {DEFAULT_OCR_LANG})")
    parser.add_argument(
        "--transitions",
        type=float,
        default=DEFAULT_TRANSITION_DURATION,
        help="Durata dissolvenza tra slide in secondi (0=nessuna)",
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
        help="Disattiva il fallback automatico al flusso ordinato "
        "quando il flusso auto-rilevato è 'free' (nessuna ancora "
        "'slide N'). Default: attivo (vedi config.py), così un "
        "podcast senza ancore usa l'allineamento ordinato con soli "
        "embeddings (~1 min, durate bilanciate) invece della "
        "selezione libera via LLM (~16 min con 9Router).",
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
        "--semantic-min-sim",
        type=float,
        default=DEFAULT_SEMANTIC_MIN_SIM,
        help=f"Soglia di similarità media sotto cui la sincronizzazione "
        f"semantica viene scartata (default: {DEFAULT_SEMANTIC_MIN_SIM})",
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
        default="auto",
        choices=["off", "auto", "9router"],
        help="Selezione slide via LLM. 'auto' (default: "
        "prova 9Router online, poi fallback embedding), 'off' "
        "(solo embedding locale), '9router' (solo 9Router "
        "online). Nel flusso libero (senza segnali 'slide "
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
    parser.add_argument("--log-file", default=None, help="Percorso file di log (salva i log anche su file)")

    args = parser.parse_args(argv)

    # Post-processing
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
    return args
