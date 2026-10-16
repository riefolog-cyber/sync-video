#!/usr/bin/env python3
"""Verifica che questo PC sia in grado di produrre un video.

A chi serve: a chi riceve il programma. `controlli.bat` è lo strumento di
chi sviluppa (ruff, mypy, test) e non dice nulla a un collega che ha un PC
rotto. Qui la domanda è una sola: "su questo computer Sync Video riuscirà a
fare un video?", e la risposta è riga per riga, con cosa fare quando no.

Vincolo di progettura: questo modulo NON importa, in cima, nessuno dei moduli
pesanti (ocr, video, transcription). Se lo facesse, andrebbe in crash su un PC dove un
pacchetto manca — cioè esattamente il caso in cui l'utente ha bisogno di
questa pagina. Ogni import pesante e dentro la prova che lo usa, con
try/except, cosi' un pacchetto assente produce "KO, con motivo" e non un
traceback.

Esegui con: python src/verifica_pc.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hardware import (
    PYTHON_MAX,
    PYTHON_MIN,
    disk_free_bytes,
    format_ram,
    memory_tier,
    project_root,
    ram_total_bytes,
)

GB = 1024**3

# Il margine di sicurezza NON vive qui. Era 1 GB in questo file e 2 GB in
# config.py, con un commento che giurava fossero "la stessa soglia": non lo
# erano, e la conseguenza era il referto che diceva "ne servono ~7.4 GB" quando
# la guardia vera ne chiede 8.4. Un collega con 8 GB liberi leggeva [ ok ] qui e
# poi il programma gli rifiutava il download: il diagnostico prometteva il
# contrario di quello che il programma fa.
#
# Ora la soglia si legge da config dentro prova_spazio(), che la importa come
# tutto il resto di config. Una sola sorgente, e un test che verifica che il
# referto non sia piu' permissivo della guardia reale.

OK, KO, AVV = "OK", "KO", "AVV"


@dataclass
class Esito:
    nome: str
    stato: str
    dettaglio: str
    rimedio: str = ""


@dataclass
class Relazione:
    esiti: list[Esito] = field(default_factory=list)

    def aggiungi(self, esito: Esito) -> None:
        self.esiti.append(esito)

    def conta(self, stato: str) -> int:
        return sum(1 for e in self.esiti if e.stato == stato)


def _gb(n: int | float) -> str:
    return f"{n / GB:.1f} GB"


# =====================================================================
# Singole prove. Ognuna restituisce un Esito e NON solleva mai.
# =====================================================================
def prova_python(r: Relazione) -> None:
    v = sys.version_info
    testo = f"Python {v.major}.{v.minor}.{v.micro}"
    # PYTHON_MAX e' INCLUSIVO, e il confronto e' su (major, minor) solo.
    # Confronto di tupla intera: (3, 13, 1, ...) NON e' minore di (3, 13),
    # quindi un 3.13.1 reale risulterebbe "troppo recente" pur essendo la
    # versione verde in CI. Confronto stretto: (3, 13) < (3, 13) e' falso,
    # quindi il 3.13 stesso verrebbe respinto.
    dentro = PYTHON_MIN <= (v.major, v.minor) <= PYTHON_MAX
    if dentro:
        r.aggiungi(Esito("Python", OK, testo))
    elif (v.major, v.minor) < PYTHON_MIN:
        r.aggiungi(Esito("Python", KO, testo, "Servono Python 3.10 o superiore."))
    else:
        # Meglio qui che al bootstrap: il bootstrap si ferma DENTRO pip, e li'
        # il messaggio e' della compilazione di pillow, non della causa.
        r.aggiungi(
            Esito(
                "Python",
                KO,
                f"{testo} troppo recente",
                "Versioni verificate: 3.10-3.13. Su questa versione le dipendenze "
                "non hanno i binari (pillow<11 si ferma a 3.13). "
                "Installa Python 3.12 da python.org e rilancia: lo trova da solo.",
            )
        )


def prova_pacchetti(r: Relazione) -> None:
    """Ogni pacchetto richiesto deve IMPORTARE, non solo essere nell'elenco.

    E' la distinzione che conta: un pacchetto installato con una versione
    sbagliata si rompe solo all'import (misurato: numpy 1.24.3 faceva
    fallire l'import di pandas, dentro pytesseract, e il guasto sembrava
    di pytesseract).
    """
    try:
        from config import _active_required_packages
    except Exception as e:  # noqa: BLE001 - config rotto = lo diciamo
        r.aggiungi(Esito("Pacchetti", KO, f"config.py non si importava: {e}"))
        return

    rotti: list[str] = []
    mancanti: list[str] = []
    for import_name, pip_req in _active_required_packages().items():
        try:
            __import__(import_name)
        except ImportError:
            mancanti.append(pip_req.split(">")[0].split("=")[0].split("<")[0].strip())
        except Exception as e:  # noqa: BLE001 - runtime error = dipendenza rotta
            rotti.append(f"{pip_req} ({type(e).__name__})")

    if rotti:
        r.aggiungi(
            Esito(
                "Pacchetti",
                KO,
                f"{len(rotti)} installati ma non importabili: {', '.join(rotti)}",
                "Dipendenza con versione sbagliata: pip install -U <pacchetto>",
            )
        )
    elif mancanti:
        r.aggiungi(
            Esito(
                "Pacchetti",
                KO,
                f"mancanti: {', '.join(mancanti)}",
                "Rilancia genera_video.bat: li installa da solo. Se non parte, pip install -r requirements.txt",
            )
        )
    else:
        r.aggiungi(Esito("Pacchetti", OK, f"{len(_active_required_packages())} pacchetti importano"))


def _tesseract_exe() -> str | None:
    from config import _TESSERACT_CANDIDATE

    for c in _TESSERACT_CANDIDATE:
        if Path(c).exists():
            return c
    return shutil.which("tesseract")


def prova_tesseract(r: Relazione) -> None:
    exe = _tesseract_exe()
    if exe is None:
        r.aggiungi(
            Esito(
                "Tesseract OCR",
                KO,
                "non trovato",
                "winget install UB-Mannheim.TesseractOCR",
            )
        )
        return
    try:
        from config import _list_tessdata  # type: ignore[attr-defined]

        lingue, _ = _list_tessdata(exe)
    except Exception as e:  # noqa: BLE001
        r.aggiungi(Esito("Tesseract OCR", AVV, f"{exe} (lingue non leggibili: {e})"))
        return
    # L'italiano puo' non stare nei modelli di sistema: il bootstrap lo
    #scarica nella cartella del progetto e usa TESSDATA_PREFIX. Guardare
    # solo il sistema dava un falso "manca l'italiano" su un PC gia' pronto.
    locale = project_root() / "tessdata" / "ita.traineddata"
    if "ita" in lingue:
        dove = "di sistema"
    elif locale.exists():
        dove = f"nella cartella del progetto ({locale.name})"
    else:
        r.aggiungi(
            Esito(
                "Tesseract OCR",
                AVV,
                f"{Path(exe).name} presente ma senza italiano",
                "Rilancia genera_video.bat: scarica da solo ita.traineddata",
            )
        )
        return
    r.aggiungi(Esito("Tesseract OCR", OK, f"{Path(exe).name}, italiano {dove}"))


def prova_ffmpeg(r: Relazione) -> None:
    exe = shutil.which("ffmpeg")
    if exe is None:
        r.aggiungi(
            Esito(
                "ffmpeg",
                KO,
                "non trovato nel PATH",
                "winget install Gyan.FFmpeg.Shared, poi riapri la finestra",
            )
        )
        return
    try:
        out = subprocess.run(
            [exe, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        r.aggiungi(Esito("ffmpeg", AVV, f"presente ma non risponde: {e}"))
        return
    # Non serve solo "ffmpeg c'è": quello che conta e' se l'encoding puo'
    # andare in accelerazione hardware. Un PC con GPU ma senza i suoi encoder
    # produce il video molto piu' lento, e il collega lo scopre come "il
    # programma e' lentissimo" invece che come "manca un encoder".
    hw = [n for n in ("h264_nvenc", "h264_qsv", "h264_amf") if n in out]
    dettaglio = f"{Path(exe).name}, " + (", ".join(hw) if hw else "solo encoder software")
    if not hw:
        r.aggiungi(
            Esito(
                "ffmpeg",
                AVV,
                dettaglio,
                "Nessun encoder accelerato: il video si produce, ma molto piu' lentamente.",
            )
        )
    else:
        r.aggiungi(Esito("ffmpeg", OK, dettaglio))


def prova_hardware(r: Relazione) -> None:
    ram = ram_total_bytes()
    if ram is None:
        r.aggiungi(Esito("Hardware", AVV, "RAM non rilevabile"))
        return
    tier = memory_tier(ram)
    stato = OK if tier in ("medio", "alto") else AVV
    r.aggiungi(
        Esito(
            "Hardware",
            stato,
            f"RAM {format_ram(ram)} ({tier})",
            "" if stato == OK else "Il batch si riduce da solo; il modello migliore resta, la run sara' piu' lenta.",
        )
    )


def prova_spazio(r: Relazione, radice: Path) -> None:
    from config import (
        _MARGINE_DISCO,
        DEFAULT_EMBEDDING_CACHE_DIR,
        DEFAULT_EMBEDDING_MODEL,
        _spazio_modelli_necessario,
    )

    # Si passa il modello ESPLICITAMENTE come fa il bootstrap: con il
    # parametro vuoto la funzione non trovava la dimensione, restituiva None,
    # e `if not necessario` leggeva quel None come "modelli gia' in cache".
    # Su un PC nuovo il referto diceva quindi che i modelli erano in cache
    # mentre la riga sotto diceva che non lo erano: proprio sul controllo che
    # serve a non riempire il disco.
    necessario = _spazio_modelli_necessario(
        Path(DEFAULT_EMBEDDING_CACHE_DIR), "small", DEFAULT_EMBEDDING_MODEL
    )
    if necessario == 0:
        r.aggiungi(Esito("Spazio disco", OK, "modelli gia' in cache"))
        return
    if necessario is None:
        # 0 e None non sono la stessa cosa: 0 = tutto scaricato, None = non si
        # riesce a calcolare. Dire "gia' in cache" davanti a un None e'
        # inventare una certezza, ed e' il motivo per cui la riga si chiude qui.
        r.aggiungi(
            Esito(
                "Spazio disco",
                AVV,
                "spazio necessario non calcolabile",
                "Non e' un problema se il download va a buon fine, ma il controllo "
                "preventivo non puo' fare: verifica che ci siano GB liberi.",
            )
        )
        return
    richiesto = necessario + _MARGINE_DISCO
    # Due destinazioni come la guardia vera (_avvisa_spazio_insufficiente in
    # config.py): la cache embedding segue il progetto, quella whisper segue
    # la home dell'utente, e possono stare su dischi diversi. Controllarne una
    # sola dava "ok" qui e blocco all'avvio (o viceversa).
    from config import _cache_whisper_faster

    destinazioni = [(radice, "cartella del progetto")]
    cache_whisper = _cache_whisper_faster("small")
    if cache_whisper is not None:
        destinazioni.append((cache_whisper, "cache HuggingFace (whisper)"))
    for percorso, etichetta in destinazioni:
        libero = disk_free_bytes(percorso)
        if libero is None:
            r.aggiungi(Esito("Spazio disco", AVV, f"{etichetta}: non interrogabile"))
        elif libero < richiesto:
            r.aggiungi(
                Esito(
                    "Spazio disco",
                    KO,
                    f"{_gb(libero)} liberi su {etichetta}, ne servono ~{_gb(richiesto)}",
                    "Libera spazio, oppure --ignora-spazio se si libera da solo durante il download",
                )
            )
        else:
            r.aggiungi(Esito("Spazio disco", OK, f"{_gb(libero)} liberi, ne servono ~{_gb(richiesto)}"))


# Host da cui dipende il primo avvio: pip (pacchetti) e HuggingFace (modelli).
# Timeout corto per host: su PC offline il DNS fallisce in fretta, ma dietro
# certi proxy la connessione puo' pendere fino al timeout — 6+6 secondi al
# peggio, solo in questo controllo.
_RETE_HOST = ("https://pypi.org/simple/", "https://huggingface.co/")
_RETE_TIMEOUT = 6


def _host_raggiungibile(url: str, timeout: int = _RETE_TIMEOUT) -> bool:
    """True se l'host risponde 2xx/3xx. Qualsiasi errore = irraggiungibile."""
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout) as risposta:
            return bool(200 <= risposta.status < 400)
    except Exception:  # noqa: BLE001 - DNS, timeout, proxy, TLS: tutto e' "non raggiungibile"
        return False


def prova_rete(r: Relazione) -> None:
    """Rete verso gli host dei download: solo AVV, mai KO.

    Un KO bloccherebbe il referto (exit 1) su un PC che magari deve solo
    produrre un video con modelli gia' in cache, dove la rete non serve piu'.
    La rete serve solo a SCARICARE: se manca, il primo avvio fallisce in pip
    o nei modelli, e questa riga dice dove guardare.
    """
    non_raggiunti = [url for url in _RETE_HOST if not _host_raggiungibile(url)]
    if not non_raggiunti:
        r.aggiungi(Esito("Rete", OK, "pypi.org e huggingface.co raggiungibili"))
    else:
        nomi = ", ".join(url.split("/")[2] for url in non_raggiunti)
        r.aggiungi(
            Esito(
                "Rete",
                AVV,
                f"non raggiungibile: {nomi}",
                "Senza rete i download (pacchetti pip, modelli) falliscono: "
                "se il programma si ferma li', il problema e' questo, non il programma.",
            )
        )


def prova_modelli(r: Relazione) -> None:
    from config import DEFAULT_EMBEDDING_CACHE_DIR

    cache = Path(DEFAULT_EMBEDDING_CACHE_DIR)
    if not cache.exists() or not any(p.is_file() for p in cache.rglob("*")):
        r.aggiungi(
            Esito(
                "Modelli",
                AVV,
                "modello di sincronizzazione non ancora scaricato",
                "Rilancia genera_video.bat: lo scarica (download ~2 GB, ~6 GB su disco, una tantum)",
            )
        )
        return
    peso = sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()) / GB
    r.aggiungi(Esito("Modelli", OK, f"modello di sincronizzazione in cache ({peso:.1f} GB)"))


def prova_motore(r: Relazione) -> None:
    """Il motore di trascrizione: non basta sapere cosa verrebbe scelto.

    `openvino_usable()` dice se il runtime e' davvero utilizzabile ADESSO
    (import, device esposto, modello presente), che e' diverso dal fatto che
    la GPU ci sia. E' il punto in cui un PC "ha la GPU" ma OpenVINO non
    parte, e senza questa prova il difetto si vede solo al primo video.
    """
    try:
        from transcription import _OPENVINO_ATTEMPT_FILENAME, openvino_usable, resolved_transcriber
    except ImportError as e:
        r.aggiungi(Esito("Motore", AVV, f"non verificabile, transcription.py non si importa ({e})"))
        return
    # Tentativo GPU morto in precedenza: la prossima trascrizione di QUEL
    # materiale parte su CPU da sola. Si segnala qui perche' spiega un giro
    # lento che altrimenti sembrerebbe un regresso inspiegabile.
    try:
        from config import CACHE_DIR

        segnale = CACHE_DIR / _OPENVINO_ATTEMPT_FILENAME
        incompiuto = segnale.exists()
        audio_nome: str | None = None
        if incompiuto:
            try:
                import json

                audio_nome = Path(
                    str(json.loads(segnale.read_text(encoding="utf-8")).get("audio", ""))
                ).name or None
            except (OSError, ValueError, AttributeError):
                audio_nome = None
    except (ImportError, OSError):
        incompiuto, audio_nome = False, None
    if incompiuto:
        r.aggiungi(
            Esito(
                "Motore",
                AVV,
                "tentativo GPU precedente incompiuto"
                + (f" su {audio_nome}" if audio_nome else "")
                + " (probabile crash): per quel materiale la prossima trascrizione parte su CPU",
                "Un giro piu' lento, poi tutto torna da solo. Per riprovare subito "
                "la GPU: genera_video.bat --transcriber openvino.",
            )
        )
    try:
        from config import DEFAULT_OPENVINO_MODEL_DIR

        scelto = resolved_transcriber("auto", Path(DEFAULT_OPENVINO_MODEL_DIR))
    except Exception as e:  # noqa: BLE001
        r.aggiungi(Esito("Motore", AVV, f"non determinabile: {e}"))
        return

    if scelto == "openvino":
        # openvino_usable() risponde VERO quando e' utilizzabile. La prima
        # versione di questa prova leggeva il ritorno al contrario e segnalava
        # KO su un PC dove OpenVINO andava benissimo.
        if openvino_usable(Path(DEFAULT_OPENVINO_MODEL_DIR)):
            r.aggiungi(Esito("Motore", OK, "openvino (iGPU/CPU Intel)"))
        else:
            r.aggiungi(
                Esito(
                    "Motore",
                    KO,
                    "OpenVINO scelto ma non utilizzabile (runtime, modello IR o device GPU manca)",
                    "Il programma ripiega su faster-whisper su CPU: funziona, ma piu' lento.",
                )
            )
    else:
        r.aggiungi(_motore_senza_openvino(scelto))


def _motore_senza_openvino(scelto: str) -> Esito:
    """Spiega PERCHE' siamo su CPU: tre cause, tre rimedi diversi.

    Senza questa distinzione il referto diceva la cosa piu' fuorviante che
    potesse: su un PC appena estratto scriveva "whisper su CPU" come se fosse
    una scelta definitiva, e il collega lo leggeva come "questo PC non ha
    accelerazione". Misurato: stesso PC e stessa cartella, prima del run
    "whisper su CPU", dopo il run "openvino (iGPU/CPU Intel)".

    Non basta distinguere "manca il runtime" da "manca il modello": se il PC
    non ha una iGPU Intel, scaricare il modello non cambierebbe niente.
    Promettere OpenVINO li' sarebbe falso nello stesso modo di prima, solo con
    un'altra frase. Per questo la terza condizione e' openvino_device_available.
    """
    dettaglio = f"{scelto} su CPU"
    try:
        import openvino_genai  # noqa: F401
    except ImportError:
        return Esito(
            "Motore",
            AVV,
            dettaglio + " (OpenVINO non e' installato)",
            "Funziona cosi', ma piu' lento. Rilancia aggiornamenti.bat per "
            "provare a installare OpenVINO su questo PC.",
        )

    try:
        from transcription import openvino_device_available
    except ImportError:
        return Esito("Motore", AVV, dettaglio)

    if not openvino_device_available():
        return Esito(
            "Motore",
            AVV,
            dettaglio + " (nessuna iGPU Intel su questo PC)",
            "Senza GPU OpenVINO non guadagna nulla, quindi il programma resta "
            "sul CPU di proposito. Funziona.",
        )

    return Esito(
        "Motore",
        AVV,
        dettaglio + " (manca solo il modello OpenVINO)",
        " Temporaneo: appena genera_video.bat scarica il modello, su questo PC "
        "la trascrizione passa a OpenVINO. Non e' un problema.",
    )


def prova_converter(r: Relazione) -> None:
    """Converter per il .pptx: dice se serve esportare il PDF a mano."""
    try:
        from ocr import _find_genoffice, _find_onlyoffice, _find_powerpoint, _find_soffice
    except ImportError as e:
        r.aggiungi(Esito("Converter PPTX", AVV, f"non verificabile ({e})"))
        return
    trovati = [
        nome
        for nome, funzione in (
            ("LibreOffice", _find_soffice),
            ("GenOffice", _find_genoffice),
            ("PowerPoint", _find_powerpoint),
            ("ONLYOFFICE", _find_onlyoffice),
        )
        if funzione()
    ]
    if trovati:
        r.aggiungi(Esito("Converter PPTX", OK, ", ".join(trovati)))
    else:
        r.aggiungi(
            Esito(
                "Converter PPTX",
                AVV,
                "nessun converter installato",
                "Se parti da un .pptx, salvalo come PDF (PowerPoint, LibreOffice, Google Slides) "
                "oppure il programma si ferma invece di produrre un video peggiore.",
            )
        )


def prova_materiali(r: Relazione, radice: Path) -> None:
    # Stessa regola di main.find_presentation_file (che qui non si importa:
    # main.py trascina moviepy e gli altri pesi, e questo modulo deve girare
    # anche a pacchetti mancanti): prima i nomi esatti, poi qualsiasi altra
    # presentazione, la piu' recente. Altrimenti il referto direbbe "manca"
    # dove il programma invece procederebbe.
    nomi_presentazione = ("presentazione.pdf", "presentazione.pptx", "presentazione.ppt")
    file = [p for p in radice.iterdir() if p.is_file()]
    presentazione = next((p for n in nomi_presentazione for p in file if p.name.lower() == n), None)
    if presentazione is None:
        candidati = [p for p in file if p.suffix.lower() in {".pdf", ".pptx", ".ppt"}]
        if candidati:
            presentazione = max(candidati, key=lambda p: p.stat().st_mtime)
    audio_candidati = [
        p for p in file if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac"}
    ]
    audio = max(audio_candidati, key=lambda p: p.stat().st_mtime) if audio_candidati else None
    if presentazione and audio:
        r.aggiungi(Esito("Materiali", OK, f"{presentazione.name} + {audio.name}"))
    else:
        manca = []
        if not presentazione:
            manca.append("la presentazione (presentazione.pdf o .pptx)")
        if not audio:
            manca.append("l'audio del podcast (.m4a o .mp3)")
        r.aggiungi(
            Esito(
                "Materiali",
                AVV,
                f"manca: {', '.join(manca)}",
                "Mettili nella cartella e rilancia genera_video.bat.",
            )
        )


# =====================================================================
def _righe(rep: Relazione) -> list[str]:
    """Le righe del referto come testo (stampa e bundle diagnostico)."""
    righe: list[str] = []
    for e in rep.esiti:
        simbolo = {OK: "[ ok ]", KO: "[ KO ]", AVV: "[avv]"}[e.stato]
        righe.append(f"  {simbolo} {e.nome:<18} {e.dettaglio}")
        if e.stato != OK and e.rimedio:
            righe.append(f"          -> {e.rimedio}")
    return righe


def _stampa(rep: Relazione) -> None:
    for riga in _righe(rep):
        print(riga)


def _prove(radice: Path) -> list[tuple[Callable[..., None], tuple[object, ...]]]:
    """L'elenco dei controlli con i loro argomenti, in ordine di stampa.

    Sta qui (e non dentro main) perché anche diagnostica.py li esegue per il
    bundle da inviare: due elenchi separati drifterebbero.
    """
    return [
        (prova_python, ()),
        (prova_pacchetti, ()),
        (prova_tesseract, ()),
        (prova_ffmpeg, ()),
        (prova_hardware, ()),
        (prova_spazio, (radice,)),
        (prova_rete, ()),
        (prova_modelli, ()),
        (prova_motore, ()),
        (prova_converter, ()),
        (prova_materiali, (radice,)),
    ]


def _esegui_prove(rep: Relazione, prove: list[tuple[Callable[..., None], tuple[object, ...]]]) -> None:
    """Esegue i controlli senza mai cadere: uno che esplode non abbassa gli altri."""
    for prova, argomenti in prove:
        try:
            prova(rep, *argomenti)
        except Exception as e:  # noqa: BLE001 - una prova non deve far cadere le altre
            nome = getattr(prova, "__name__", "verifica").replace("prova_", "")
            rep.aggiungi(Esito(nome, KO, f"controllo fallito: {type(e).__name__}: {e}"))


def main() -> int:
    radice = project_root()
    print()
    print("=" * 74)
    print("   SYNC VIDEO - verifica del computer")
    print("=" * 74)
    print(f"   Cartella: {radice}")
    print()

    rep = Relazione()
    _esegui_prove(rep, _prove(radice))

    _stampa(rep)

    ko, avv = rep.conta(KO), rep.conta(AVV)
    print()
    if ko:
        print(f"   ESITO: {ko} problema(i) che impediscono il video, {avv} da verificare.")
        print("   Parti dai problemi marcati KO, sopra.")
    else:
        coda = f" ({avv} da verificare)" if avv else ""
        print(f"   ESITO: il PC puo' produrre un video{coda}.")
    print("=" * 74)
    print()
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
