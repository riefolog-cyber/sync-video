#!/usr/bin/env python3
"""
Rilevamento automatico dell'hardware al primo avvio.

Sceglie il motore di trascrizione più adatto alla configurazione del PC:

- GPU NVIDIA presente              -> faster-whisper (CTranslate2) su CUDA (float16)
- iGPU Intel (Iris/UHD/Arc)        -> OpenVINO GenAI su device GPU
- nessuna GPU accelerabile         -> faster-whisper su CPU (int8)
- Apple Silicon / sconosciuto      -> faster-whisper su CPU (nessuna accelerazione)
- GPU AMD                          -> faster-whisper su CPU (OpenVINO supporta solo Intel)
- GPU Qualcomm Adreno (Snapdragon) -> faster-whisper su CPU (niente CUDA/OpenVINO su ARM)

Questa è la *raccomandazione*, data la GPU. Non è la scelta finale: una GPU
NVIDIA con CTranslate2 compilato senza CUDA, o una iGPU Intel senza driver,
vengono corrette da `_validate` a CPU (vedi sotto). La tabella dice "cosa
vorrebbe", non "cosa succede".

Che cosa viene persistito, e che cosa no
-----------------------------------------
In ``.cache/machine_setup.json`` si salvano i FATTI hardware (``fingerprint`` +
lista delle GPU), non la decisione. La distinzione non e' accademica: la
decisione ("usa CUDA", "usa OpenVINO") dipende da cose che cambiano nel tempo
(che pacchetti sono installati, che device il runtime espone *adesso*), mentre
l'hardware no. Persistere la decisione la faceva invecchiare: copiando la
cartella da un PC con GPU NVIDIA a uno senza, la run si chiudeva con un
traceback di CTranslate2 ("CUDA driver version is insufficient") DOPO aver
gia' fatto OCR e rendering, senza che nulla avesse notato che la GPU non c'era
piu'.

Quindi la decisione e' ricalcolata a ogni run da ``recommend()`` e poi validata
contro il runtime reale (``_validate``): una lista GPU stale puo' produrre una
raccomandazione sbagliata, ma non puo' piu' produrre un motore inutilizzabile,
perche' la validazione controlla che il device esista davvero e ripiega su CPU
avvisando. I fatti hardware sono invece cachati perche' rilevarli costa una
subprocess, e portano con se' l'impronta della macchina
(``platform.system()/platform.machine()``): se la cartella viene clonata o
spostata su un PC diverso, si rileva di nuovo.

Il provisioning (installare openvino-genai, scaricare il modello IR) avviene
solo quando l'hardware viene realmente rilevato, non a ogni run: e' la parte
costosa e con effetti di rete, e non ha senso ripeterla quando i fatti non
sono cambiati.

La decisione NON viene scritta in ``.env``: quel file resta agli override
espliciti dell'utente, che hanno la precedenza. Scrivere li' creava una
seconda fonte di verita' capace di rendere ``--force-setup`` inefficace (vedi
``_apply``).
"""

import importlib.util
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from config import CACHE_DIR, cuda_available, log, openvino_device_available
from hardware import choose_video_encoder, ffmpeg_encoders

MACHINE_CONFIG_PATH = CACHE_DIR / "machine_setup.json"

# Fallback prudente se il provisioning fallisce: faster-whisper su CPU.
_CPU_FALLBACK = {
    "transcriber": "whisper",
    "whisper_device": "cpu",
    "whisper_compute_type": "int8",
    "openvino_device": None,
    "reason": "fallback: faster-whisper su CPU",
}


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.stdout or ""
    except Exception:
        return ""


# =====================================================================
# RILEVAMENTO HARDWARE
# =====================================================================
def _gpus_linux() -> list[str]:
    """GPU su Linux, con tre fonti a cascata.

    `lspci` e' la fonte migliore ma sta nel pacchetto `pciutils`, che NON e'
    installato di default su molte distro (Debian/Ubuntu server, Alpine,
    container minimali). Se manca, `_run` ingoia l'eccezione e la lista
    resta vuota: senza i fallback qui sotto, una macchina NVIDIA finiva su
    faster-whisper CPU senza dire nulla, e cioe' 5-10x piu' lenta in
    silenzio. Un rilevamento che sbaglia perche' manca un tool e' peggio di
    nessun rilevamento, quindi si prova anche altrove.
    """
    # 1. lspci, se presente (nomi ricchi: vendor + device).
    out = _run(["lspci", "-nn"])
    gpus: list[str] = []
    for line in out.splitlines():
        low = line.lower()
        if any(k in low for k in ("vga compatible", "3d controller", "display controller")):
            gpus.append(line.strip())
    if gpus:
        return gpus

    # 2. nvidia-smi: risponde solo se c'e' una GPU NVIDIA con i driver
    #    installati, quindi e' un segnale affidabile da solo.
    nvidia = _run(["nvidia-smi", "-L"])
    if nvidia.strip():
        return [line.strip() for line in nvidia.splitlines() if line.strip()]

    # 3. sysfs: /sys/class/drm/card*/device/vendor e /uevent (PCI_ID).
    #    Funziona senza installare nulla, ma restituisce solo il vendor
    #    (0x8086 Intel, 0x10de NVIDIA, 0x1002/0x1022 AMD): basta per
    #    classificare, che e' tutto quello che serve.
    sysfs: list[str] = []
    try:
        import glob
        import re

        vendor_nomi = {
            "0x8086": "Intel",
            "0x10de": "NVIDIA",
            "0x1002": "AMD",
            "0x1022": "AMD",
            "0x10c8": "Qualcomm",
            "0x5143": "Qualcomm",
        }
        for uevent in glob.glob("/sys/class/drm/card*/device/uevent"):
            testo = Path(uevent).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"DRIVER=(\S+)", testo)
            vendor = m.group(1) if m else ""
            # Il vendor id e' in PCI_ID=VVVV:DDDD. Attenzione: nel sysfs e' in
            # MAIUSCOLO (0x10DE), mentre la tabella e' in minuscolo: senza
            # il lower() una GPU NVIDIA finiva 'unknown' e tornava su CPU,
            # cioe' esattamente il buco che questo fallback deve chiudere.
            p = re.search(r"PCI_ID=(\w{4}):(\w{4})", testo, re.IGNORECASE)
            nome = vendor_nomi.get(f"0x{p.group(1).lower()}", "") if p else ""
            etichetta = f"{nome} GPU ({vendor})" if nome and vendor else (nome or vendor)
            if etichetta and etichetta not in sysfs:
                sysfs.append(etichetta)
    except Exception:
        return sysfs
    return sysfs


def detect_gpus() -> list[str]:
    """Restituisce la lista delle GPU rilevate (nomi/descrizioni)."""
    if sys.platform == "win32":
        out = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name)",
            ]
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    if sys.platform == "darwin":
        out = _run(["system_profiler", "SPDisplaysDataType"])
        return [line.split(":", 1)[1].strip() for line in out.splitlines() if "Chipset Model" in line]
    return _gpus_linux()


def _classify_gpu(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("nvidia", "geforce", "quadro", "rtx", "gtx", "tesla")):
        return "nvidia"
    if "intel" in low or "iris" in low or "arc" in low or "uhd" in low:
        return "intel"
    if any(k in low for k in ("amd", "radeon", "vega", "ryzen")):
        return "amd"
    # Snapdragon X (Windows ARM): l'Adreno non è accelerabile da CUDA né da
    # OpenVINO (nessuna iGPU Intel); il setup deve ripiegare su CPU.
    if any(k in low for k in ("qualcomm", "adreno", "snapdragon")):
        return "qualcomm"
    return "unknown"


def recommend(gpus: list[str]) -> dict:
    """Consiglia il motore migliore per l'hardware rilevato."""
    has_nvidia = any(_classify_gpu(g) == "nvidia" for g in gpus)
    has_intel = any(_classify_gpu(g) == "intel" for g in gpus)

    if has_nvidia:
        return {
            "transcriber": "whisper",
            "whisper_device": "cuda",
            "whisper_compute_type": "float16",
            "openvino_device": None,
            "reason": "GPU NVIDIA rilevata: faster-whisper su CUDA (float16)",
        }
    if has_intel:
        return {
            "transcriber": "openvino",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "openvino_device": "GPU" if openvino_device_available() else "CPU",
            "reason": "iGPU Intel rilevata: OpenVINO GenAI",
        }
    has_qualcomm = any(_classify_gpu(g) == "qualcomm" for g in gpus)
    reason = (
        "GPU Qualcomm (Adreno/Snapdragon ARM): nessuna accelerazione disponibile, "
        "faster-whisper su CPU"
        if has_qualcomm
        else "Nessuna GPU accelerabile: faster-whisper su CPU"
    )
    return {
        "transcriber": "whisper",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "openvino_device": None,
        "reason": reason,
    }


# =====================================================================
# PROVISIONING (installa/scarica ciò che serve)
# =====================================================================
def _pip_install(package: str) -> bool:
    try:
        log.info("   ⏳ pip install %s ...", package)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
        )
        log.info("   ✅ %s installato.", package)
        return True
    except Exception as e:
        log.warning("   ❌ pip install %s fallita: %s", package, e)
        return False


def _provision_openvino(model_dir: Path) -> bool:
    """Garantisce openvino-genai installato e modello IR scaricato."""
    try:
        import openvino_genai  # noqa: F401
    except ImportError:
        if not _pip_install("openvino-genai"):
            return False
        # Riverifica: `pip install` avvenuto DOPO il tentativo di import in
        # questa run, quindi il modulo puo' non essere ancora importabile (il
        # finder lo aveva gia' messo in cache negativa, e comunque siamo già
        # dentro il bootstrap). Senza questo controllo si dichiarava pronto un
        # motore che poi falliva in trascrizione.
        if importlib.util.find_spec("openvino_genai") is None:
            log.warning("   ⚠️ openvino-genai installato ma non importabile in questa run.")
            return False
    if not model_dir.exists():
        from transcription import download_openvino_model

        download_openvino_model(model_dir)
    return model_dir.exists()


def _provision(rec: dict, model_dir: Path) -> dict:
    """Applica il provisioning necessario per il motore consigliato.

    Costa rete (pip install, download del modello IR): viene chiamato solo
    quando l'hardware e' stato davvero rilevato, non a ogni run.
    """
    if rec["transcriber"] == "openvino":
        if not _provision_openvino(model_dir):
            log.warning("   ⚠️ OpenVINO non pronto, ripiego su faster-whisper su CPU.")
            return dict(_CPU_FALLBACK)
    elif rec["whisper_device"] == "cuda" and not cuda_available():
        log.warning("   ⚠️ CUDA non disponibile, uso faster-whisper su CPU.")
        return dict(_CPU_FALLBACK)
    return rec


def _downgrade(rec: dict, motivo: str) -> dict:
    """Ripiega su faster-whisper su CPU, spiegando perche'."""
    log.warning("   ⚠️ %s: ripiego su faster-whisper su CPU.", motivo)
    out = dict(_CPU_FALLBACK)
    out["reason"] = f"{motivo} -> {_CPU_FALLBACK['reason']}"
    return out


def _validate(rec: dict, model_dir: Path | None) -> dict:
    """Controlla che il motore consigliato sia USABILE ADESSO, qui.

    È il controllo che rende impossibile morire su un device inesistente.
    Nessuna rete, nessun download: solo fatti del runtime corrente.

    Una lista GPU cached può essere stale (hardware cambiato, cartella clonata,
    GPU sostituita) e raccomandare CUDA su una macchina senza GPU NVIDIA, o
    OpenVINO dove il runtime non è installato. Prima la decisione salvata
    veniva applicata alla cieca e la run moriva con un traceback di CTranslate2
    DOPO OCR e rendering. Qui ogni raccomandazione viene confrontata con quello
    che il runtime espone davvero, e se non regge si ripiega su CPU avvisando.
    """
    # faster-whisper assente: su ARM è assente per costruzione (CTranslate2 non
    # ha wheel). Non è un problema di configurazione, e non c'è alternative
    # (cfr. _engine_note): si lascia la raccomandazione e lo si dice dopo.
    if rec["transcriber"] == "openvino":
        if importlib.util.find_spec("openvino_genai") is None:
            return _downgrade(rec, "openvino-genai non e' installato")
        if model_dir is not None and not model_dir.exists():
            return _downgrade(rec, f"modello OpenVINO assente in {model_dir}")
        # La GPU Intel c'e' ma il runtime potrebbe esporre solo la CPU (es.
        # driver non installato): si usa comunque OpenVINO, ma su CPU.
        if rec.get("openvino_device") == "GPU" and not openvino_device_available():
            log.warning(
                "   ⚠️ iGPU Intel presente ma il runtime OpenVINO non espone un device GPU: uso OpenVINO su CPU."
            )
            rec = dict(rec)
            rec["openvino_device"] = "CPU"
        return rec

    # whisper: CUDA e' la sola accelerazione possibile, e va verificata due
    # volte perche' fallisce in due modi diversi: driver troppo vecchio
    # ("CUDA driver version is insufficient") o CTranslate2 compilato senza
    # supporto CUDA, entrambi indistinguibili da qui e risolti dal ripiego.
    if rec.get("whisper_device") == "cuda":
        if importlib.util.find_spec("faster_whisper") is None:
            return _downgrade(rec, "faster-whisper non e' installato")
        if not cuda_available():
            return _downgrade(rec, "nessun device CUDA utilizzabile")
    return rec


# =====================================================================
# PERSISTENZA
# =====================================================================
def _fingerprint() -> str:
    """Identita' della macchina per cui' l'hardware e' stato rilevato.

    La cartella del progetto e' pensata per essere clonata su un altro PC (o
    spostata via pendrive/cartella di rete). Senza un'impronta, la lista GPU
    cachata sopravviverebbe al cambio di macchina e raccomanderebbe un motore
    per hardware che qui non esiste.

    Il confronto e' gratuito (nessuna subprocess, nessun import pesante): si
    controlla solo identita' di sistema e architettura. Un cambio di GPU a
    parita' di sistema resta delegato a `--force-setup`, che rileva davvero:
    rilevare le GPU costa una subprocess a ogni run, e per coprire un caso raro
    non vale il prezzo. E se la lista risultasse stale, `_validate` ripiega comunque
    su CPU invece di lasciare la run morire su un device inesistente.
    """
    return f"{platform.system()}/{platform.machine()}"


def _read_hardware() -> list[str] | None:
    """GPU rilevate in precedenza su QUESTA macchina, se disponibili.

    Restituisce None quando non si puo' fidarsi del file: assente, illeggibile,
    scritto su un'altra macchina, o di una versione precedente che non aveva la
    lista. In quel caso il chiamante rileva davvero.

    Nota: le chiavi di decisione che un file scritto da versioni precedenti puo'
    contenere (transcriber, whisper_device, ...) vengono IGNORATE di proposito:
    sono la decisione, che non deve sopravvivere. Il file puo' quindi contenere
    ancora quei campi senza che influiscano su nulla.
    """
    try:
        data = json.loads(MACHINE_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("fingerprint") != _fingerprint():
        return None
    gpus = data.get("gpus")
    if not isinstance(gpus, list) or not all(isinstance(g, str) for g in gpus):
        return None
    return gpus


def _write_hardware(gpus: list[str]) -> None:
    """Persiste i FATTI hardware, non la decisione sul motore.

    Il file non deve contenere `transcriber`/`whisper_device`/`openvino_device`:
    sono la decisione, e la decisione dipende da fatti che cambiano (che
    pacchetti sono installati, che device il runtime espone). Persisterla la
    faceva invecchiare fino a rompere la run. Qui si scrive solo l'inventario
    delle GPU, che e' lento da ottenere e stabile nel tempo.
    """
    payload = {"fingerprint": _fingerprint(), "gpus": gpus}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MACHINE_CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class _TranscriberArgs(Protocol):
    """Interfaccia minima degli args usata da ``_apply``/``machine_setup``."""

    transcriber: str
    whisper_device: str
    whisper_compute_type: str
    openvino_device: str
    openvino_model_dir: str
    video_encoder: str
    whisper_model: str


def _video_vendor(gpus: list[str]) -> str:
    """Famiglia della GPU che decide l'encoder video, con la precedenza di
    ``recommend``: NVIDIA prima di Intel (una dGPU dedicata batte un'iGPU
    anche per l'encoding)."""
    if any(_classify_gpu(g) == "nvidia" for g in gpus):
        return "nvidia"
    if any(_classify_gpu(g) == "intel" for g in gpus):
        return "intel"
    if any(_classify_gpu(g) == "amd" for g in gpus):
        return "amd"
    if sys.platform == "darwin":
        return "apple"
    return "unknown"


def _apply_video_encoder(args: _TranscriberArgs, gpus: list[str]) -> None:
    """Sceglie l'encoder H.264 e lo annuncia.

    L'override esplicito ha sempre precedenza: il flag
    ``--video-encoder`` se diverso da ``auto``, poi la variabile
    ``VIDEO_ENCODER``. Se l'encoder accelerato fallisce a runtime,
    ``video._build_video_ffmpeg`` lo intercetta e ripiega su libx264: qui si
    sceglie il percorso veloce, non si garantisce che funzioni.

    La decisione NON viene persistita: come il motore di trascrizione
    dipende da fatti che cambiano (ffmpeg installato dopo, driver
    aggiornati), e va ricalcolata a ogni run. Il probe degli encoder e'
    invece cachato, perche' costa ~100 ms.
    """
    scelto = (getattr(args, "video_encoder", "auto") or "auto").strip()
    if scelto == "auto":
        scelto = os.environ.get("VIDEO_ENCODER", "").strip()
    if not scelto:
        scelto = choose_video_encoder(_video_vendor(gpus), ffmpeg_encoders())
    args.video_encoder = scelto
    if scelto == "libx264":
        log.info("   Video: libx264 (CPU) — nessuna accelerazione video disponibile")
    else:
        log.info("   Video: %s (GPU accelerata)", scelto)


def _apply(args: _TranscriberArgs, rec: dict) -> None:
    """Applica la configurazione rilevata agli argomenti della run corrente.

    Se l'utente ha scelto esplicitamente un motore (``--transcriber`` diverso
    da ``auto``) non tocca nulla: rispetta la scelta manuale. In modalità
    ``auto`` imposta anche device e compute type coerenti con il motore.
    """
    if getattr(args, "transcriber", "auto") != "auto":
        return
    args.transcriber = rec["transcriber"]
    args.whisper_device = rec.get("whisper_device", "cpu")
    args.whisper_compute_type = rec.get("whisper_compute_type", "int8")
    if rec.get("openvino_device"):
        args.openvino_device = rec["openvino_device"]


# =====================================================================
# ENTRY POINT
# =====================================================================
def _engine_note(rec: dict) -> str:
    """Messaggio esplicito se il motore scelto non e' utilizzabile qui.

    Copre solo il caso che `_validate` NON puo' risolvere: su ARM
    faster-whisper non e' installabile (CTranslate2 non pubblica wheel
    win_arm64) e OpenVINO e' x86-only, quindi non esiste un percorso percorribile.
    Il progetto si avvia e fa tutto il resto (PDF, OCR, embedding, video), ma la
    trascrizione audio non e' disponibile: meglio dirlo che fallire piu' avanti con
    un ImportError.

    Non ricontrolla qui se openvino-genai o CUDA sono utilizzabili: quello e'
    compito di `_validate`, che in quel caso ripiega su CPU. Un secondo check qui
    sarebbe una terza fonte di verita' sullo stesso fatto, e una terza copia e'
    gia' stato il bug che ha fatto leggere a `openvino_usable()` una decisione dal
    file invece di guardare il runtime vero.
    """
    if importlib.util.find_spec("faster_whisper") is None:
        return ("faster-whisper non installabile su questa CPU: CTranslate2 non "
                "pubblica wheel ARM. La trascrizione audio non sara disponibile.")
    return ""


def _warn_ram_bassa(args: _TranscriberArgs) -> None:
    """Avvisa se su questo PC i modelli di default sono pesanti.

    Il batch size si e' gia' adattato alla RAM (vedi `config._RAM_TIER`), ma
    i MODELLI non si adattano: `multilingual-e5-large` tiene ~4,3 GB residenti
    e whisper `small` altri ~500 MB, qualunque sia la macchina.

    Non si abbassa il modello di nascosto: un risultato peggiore che nessuno
    ha scelto e' la cosa peggiore che un programma automatico possa fare, e
    il confronto con la baseline del progetto (che usa `small`) perderebbe
    significato. Si dice cosa sta succedendo e si lascia scegliere, con il
    valore da mettere in `.env` gia' scritto.

    Non avvisa se l'utente ha gia' scelto un modello piccolo: dirgli che deve
    scendere quando ci e' gia' sceso sarebbe rumore.
    """
    from hardware import format_ram, memory_tier, ram_total_bytes

    totale = ram_total_bytes()
    tier = memory_tier(totale)
    if tier == "ignoto" or tier == "alto":
        return

    modello = str(getattr(args, "whisper_model", "") or "")
    if modello in ("tiny", "base"):
        return

    log.warning(
        "   ⚠️  RAM %s (%s): i modelli di default sono pesanti per questa macchina.",
        format_ram(totale),
        tier,
    )
    log.warning(
        "      L'embedding multilingual-e5-large tiene ~4,3 GB in memoria, whisper %s altri ~500 MB. "
        "Il batch e' gia' ridotto automaticamente.",
        modello or "small",
    )
    log.warning(
        "      Per alleggerire, in .env:  WHISPER_MODEL=tiny  (trascrizione piu' rapida, "
        "meno accurata sulle ancore)."
    )


def machine_setup(args: _TranscriberArgs, force: bool = False) -> None:
    """Configura il motore di trascrizione piu' adatto (idempotente).

    La decisione NON viene ripresa da disco: `recommend()` la ricalcola a ogni
    run dalla lista GPU e `_validate()` la confronta con il runtime reale, quindi
    un file di cache stale non puo' far partire la pipeline su un device che non
    esiste. Di disco si riusano solo i fatti hardware (lista GPU + impronta della
    macchina), che sono lenti da ottenere ma stabili.

    Il provisioning (installare, scaricare il modello IR) avviene solo se
    l'hardware e' stato davvero rilevato in questa run.
    """
    model_dir = Path(getattr(args, "openvino_model_dir", CACHE_DIR / "whisper_openvino_small"))

    gpus = None if force else _read_hardware()
    if gpus is None:
        fresh_detection = True
        log.info("🔧 Rilevamento configurazione hardware al primo avvio...")
        gpus = detect_gpus()
        _write_hardware(gpus)
        log.info("   GPU rilevate: %s", ", ".join(gpus) if gpus else "(nessuna)")
    else:
        fresh_detection = False

    # La raccomandazione dipende dal runtime corrente, non dal file: qui e' l'unico
    # posto in cui viene decisa. Il provisioning (che costa rete) gira solo se
    # l'hardware e' stato davvero rilevato in questa run; altrimenti si valida.
    rec = recommend(gpus)
    rec = _provision(rec, model_dir) if fresh_detection else _validate(rec, model_dir)

    log.info("   Motore scelto: %s (%s)", rec["transcriber"], rec["reason"])
    if rec["transcriber"] == "openvino":
        log.info("   Device OpenVINO: %s", rec["openvino_device"])
    elif rec["whisper_device"] == "cuda":
        log.info("   Device faster-whisper: CUDA (float16)")

    _apply_video_encoder(args, gpus)
    _warn_ram_bassa(args)

    nota = _engine_note(rec)
    if nota:
        log.warning("   ⚠️  %s", nota)

    _apply(args, rec)
