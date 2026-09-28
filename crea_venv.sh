#!/usr/bin/env bash
# Crea la cartella .venv del progetto: un Python separato, con i pacchetti
# del progetto, che nessun altro programma installato sul PC puo' cambiare.
# Da quando .venv esiste, basta usare .venv/bin/python per lanciare la pipeline.
#
# È il gemello di crea_venv.bat per macOS e Linux (su Windows continua a
# servire crea_venv.bat: i .bat non esistono su macOS/Linux, e i .sh su Windows).
#
# Uso:  ./crea_venv.sh [--ricrea]
#   --ricrea   cancella la .venv esistente e la ricrea da zero
#   --help     mostra questo messaggio
#
# I modelli gia' scaricati (cartella .cache e cache di HuggingFace) si riusano:
# non vengono riscaricati.

set -euo pipefail

cd "$(dirname "$0")"

RICREA=0
for arg in "$@"; do
    case "$arg" in
        --ricrea) RICREA=1 ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Argomento non riconosciuto: $arg" >&2
            exit 2
            ;;
    esac
done

echo "========================================"
echo "   Sync Video: ambiente dedicato (.venv)"
echo "========================================"
echo
echo "Un Python separato per questo progetto: quello che si installa qui non"
echo "puo' essere cambiato dagli altri programmi del PC (e viceversa)."
echo "La prima volta scarica i pacchetti: qualche minuto."
echo

# --- Python di sistema, NON quello della venv che stiamo creando ---
# Si preferisce 3.11/3.12/3.13 in quest'ordine, prendendo il primo >= 3.10.
PY_CMD=""
for v in 3.11 3.12 3.13 python3 python; do
    if command -v "$v" >/dev/null 2>&1; then
        if "$v" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
            PY_CMD="$v"
            break
        fi
    fi
done

if [ -z "$PY_CMD" ]; then
    echo "[ERRORE] Python 3.10+ non trovato."
    echo "Installalo da https://www.python.org/downloads/ (macOS) o dal gestore"
    echo "di pacchetti della distribuzione (Linux)."
    exit 9009
fi

echo "Python di base: $("$PY_CMD" --version 2>&1)"

VENV_DIR="$PWD/.venv"
VENV_PY="$VENV_DIR/bin/python"

if [ -e "$VENV_PY" ] && [ "$RICREA" = "1" ]; then
    echo "Rimozione della .venv esistente..."
    rm -rf "$VENV_DIR"
fi

if [ ! -e "$VENV_PY" ]; then
    echo "Creazione della venv..."
    "$PY_CMD" -m venv "$VENV_DIR"
    if [ ! -e "$VENV_PY" ]; then
        echo "[ERRORE] Creazione della venv fallita."
        exit 1
    fi
else
    echo "La .venv esiste gia': verifico e completo i pacchetti."
    echo "Per rifarla da zero: ./crea_venv.sh --ricrea"
fi

echo "Aggiornamento di pip..."
"$VENV_PY" -m pip install --upgrade pip || echo "[AVVISO] Aggiornamento di pip fallito: proseguo."

echo "Installazione dei pacchetti del progetto..."
# requirements.txt ha marcatori PEP 508 che escludono faster-whisper e OpenVINO
# su Windows ARM (CTranslate2 non pubblica wheel win_arm64): su quelle macchine
# la riga viene saltata senza far fallire l'installazione della riga intera.
if ! "$VENV_PY" -m pip install -r requirements.txt; then
    echo "[ERRORE] Installazione dei pacchetti fallita: vedi il messaggio sopra."
    exit 1
fi

echo "Verifica degli import..."
# faster_whisper non e' installabile su ARM: su quel caso si verifica solo cio'
# che esiste davvero, senza far fallire il setup per un pacchetto assente per
# costruzione (la trascrizione non sara' disponibile, e lo dice _engine_note).
if ! "$VENV_PY" - <<'PYCHECK'
import importlib.util
import sys

# Obbligatori su ogni piattaforma.
required = ["fastembed", "moviepy", "pymupdf", "pytesseract"]
# faster-whisper manca su ARM (CTranslate2 non ha wheel win_arm64).
optional = ["faster_whisper", "openvino", "openvino_genai"]

missing = []
for name in required:
    try:
        __import__(name)
    except ImportError as e:
        missing.append(f"{name} -> {e}")

present = [n for n in optional if importlib.util.find_spec(n) is not None]

if missing:
    for m in missing:
        print(f"   MANCANTE: {m}", file=sys.stderr)
    raise SystemExit(1)

print("   Import verificati.")
print(f"   Opzionali presenti: {', '.join(present) if present else '(nessuno)'}")
if "faster_whisper" not in present:
    print("   Nota: faster-whisper non installabile su questa CPU (CTranslate2 non")
    print("   pubblica wheel ARM): la trascrizione audio non sara' disponibile.")
PYCHECK
then
    echo "[ERRORE] Qualche pacchetto non si importa."
    exit 1
fi

echo
echo "========================================"
echo "   Pronto: usa .venv/bin/python main.py"
echo "========================================"
exit 0
