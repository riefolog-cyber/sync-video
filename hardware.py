#!/usr/bin/env python3
"""
Rilevazione hardware per l'adattamento automatico (P2 #14).

Modulo a sola libreria standard, senza import del progetto: puo' essere
usato da ``config`` (default in fase di import), da ``video`` (encoder) e da
``machine_setup`` (riepilogo) senza creare cicli di import.

Cosa rileva e perche':
- **RAM totale e disponibile**: fin qui' il progetto non guardava mai la
  memoria. Il modello di embedding (multilingual-e5-large) tiene in memoria
  ~4,3 GB: su un portatile da 8 GB con il browser aperto e' il fallimento
  piu' probabile. La RAM totale (non quella disponibile) e' l'unica che
  entra in chiavi di cache e default: usare quella disponibile cambierebbe
  il batch size a ogni run e invaliderebbe la cache embedding ogni volta.
- **Encoder H.264 di ffmpeg**: il video era codificato sempre in libx264
  su CPU, anche sulle macchine con GPU che il progetto gia' rileva e usa
  per la trascrizione. Qui si sceglie l'encoder accelerato quando la GPU
  c'e' davvero, con ripiego su libx264.

Ogni probe e' difensivo e rapido: se fallisce si ripiega sui logici, mai
un'eccezione. Stessa natura dei probe in ``config._physical_cpus``.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Stessa posizione di config.CACHE_DIR, calcolata qui per non importare
# config (che a sua volta potrebbe voler importare questo modulo).
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"
HARDWARE_CACHE = CACHE_DIR / "hardware.json"

# Sotto questa soglia non tentiamo accelerazioni hardware: una GPU senza
# driver a modo puo' produrre un file illeggibile o far fallire ffmpeg, e
# il guadagno non vale un video che non esce.
_FFMPEG_TIMEOUT = 15
_CACHE_VALID_SECONDS = 7 * 24 * 3600  # una settimana: l'hardware non cambia


# =====================================================================
# RAM
# =====================================================================
def _ram_win32() -> tuple[int, int] | None:
    """(totale, disponibile) da GlobalMemoryStatusEx, o None.

    La struttura e l'import di `ctypes.wintypes` stanno QUI dentro, non a
    livello di modulo: vengono costruiti solo quando servono davvero, cioe'
    solo su Windows. A importarli sul modulo, ogni macchina Linux e macOS
    costruiva una struct del kernel che non usa, e `ctypes.wintypes` su
    piattaforme non-Windows e' documentato come non portabile: un import
    innocente puo' diventare un'eccezione a tempo di importazione, cioe'
    prima ancora che il programmo faccia qualcosa.
    """
    try:
        from ctypes import wintypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except Exception:
        return None


def ram_available_bytes() -> int | None:
    """Byte di RAM liberi in questo momento, o None se non rilevabile.

    Solo informativa (log di avviso): NON entra in nessuna chiave di cache
    ne' in nessun default, perche' cambia da un momento all'altro.
    """
    if sys.platform == "win32":
        coppia = _ram_win32()
        return coppia[1] if coppia else None
    if sys.platform == "darwin":
        # macOS non espone la memoria libera con una syscall semplice:
        # vm_stat e' rumoroso e vm_stat -f richiede root. Meglio il totale,
        # dichiarato come "disponibile" che sbaglia sempre per eccesso: un
        # avviso che over-stima la memoria libera e' quello giusto.
        return ram_total_bytes()
    try:  # Linux: MemAvailable richiede kernel >= 3.14
        for riga in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if riga.startswith("MemAvailable:"):
                return int(riga.split()[1]) * 1024
    except Exception:
        return None
    return None


def ram_total_bytes() -> int | None:
    """Byte di RAM installata, o None se non rilevabile.

    Unico segnale di memoria che entra nei default e nelle chiavi di cache:
    e' stabile nel tempo, a differenza della RAM disponibile.
    """
    if sys.platform == "win32":
        coppia = _ram_win32()
        return coppia[0] if coppia else None
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=_FFMPEG_TIMEOUT,
                check=False,
            ).stdout.strip()
            if out.isdigit():
                return int(out) or None
        except Exception:
            return None
        return None
    try:  # Linux
        for riga in Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if riga.startswith("MemTotal:"):
                return int(riga.split()[1]) * 1024
    except Exception:
        return None
    return None


def memory_tier(total_bytes: int | None) -> str:
    """'basso' | 'medio' | 'alto' | 'ignoto' dalla RAM totale.

    Le soglie sono scelte attorno a dove i default pesanti smettono di
    stare comodi:
      - basso  (< 6 GB): il modello di embedding da solo ne prende ~4,3 GB.
      - medio  (< 12 GB): sta, ma con margine stretto se c'e' un browser.
      - alto   (>= 12 GB): nessun vincolo pratico.
    """
    if not total_bytes:
        return "ignoto"
    gb = total_bytes / 1024**3
    if gb < 6:
        return "basso"
    if gb < 12:
        return "medio"
    return "alto"


def format_ram(total_bytes: int | None) -> str:
    """12345678901 -> '11.5 GB'; None -> 'sconosciuta'."""
    if not total_bytes:
        return "sconosciuta"
    return f"{total_bytes / 1024**3:.1f} GB"


# =====================================================================
# SPAZIO DISCO
# =====================================================================
def disk_free_bytes(path: Path) -> int | None:
    """Byte liberi sul disco che contiene `path`, o None se non rilevabile.

    Serve perche' al primo avvio il progetto scarica ~6.9 GB (modello di
    embedding + modello whisper) senza aver mai controllato che ci stiano:
    su un PC con 3 GB liberi il download parte, si riempie il disco e la run
    muore a meta' con un errore che non spiega la causa.
    """
    try:
        # Il percorso puo' non esistere ancora (la cache si crea al primo
        # download): shutil.disk_usage va su un antenato esistente.
        alvo = path
        while not alvo.exists() and alvo.parent != alvo:
            alvo = alvo.parent
        return int(shutil.disk_usage(alvo).free)
    except Exception:
        return None


# =====================================================================
# ENCODER VIDEO
# =====================================================================
# Gli encoder accelerati non hanno tutti gli stessi flag: `-preset` non
# esiste per videotoolbox e `-preset` ha valori diversi per nvenc/amf.
# Ogni riga porta gia' i suoi flag, cosi' non si inventa niente a runtime.
_ENCODER_ARGS: dict[str, list[str]] = {
    "libx264": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage"],
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq"],
    "h264_qsv": ["-c:v", "h264_qsv", "-preset", "veryfast", "-tune", "hq"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "speed"],
    "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-q:v", "65"],
}

# VAAPI escluso di proposito: funziona solo con il filtro hwupload e una
# -vaapi_device, percorsi fragili che falliscono a runtime. Megli libx264
# che un video che non esce.

# Ordine di preferenza per famiglia di GPU. Stessa precedenza di
# machine_setup.recommend(): NVIDIA prima di Intel (una dGPU dedicata
# batte un'iGPU anche per l'encoding).
#
# Attenzione: ogni catena puo' scendere SOLO su encoder che funzionano su
# quella famiglia. Per 'amd' QSV non e' un ripiego legittimo: richiede una
# GPU Intel, e se la macchina avesse anche quella, `_video_vendor` avrebbe
# restituito 'intel'. Le build di ffmpeg senza AMF capitano spesso: senza
# questo accorgimento una macchina AMD senza AMF finiva su h264_qsv e
# falliva a runtime.
_ENCODER_BY_VENDOR: dict[str, tuple[str, ...]] = {
    "nvidia": ("h264_nvenc", "h264_amf", "libx264"),
    "intel": ("h264_qsv", "h264_amf", "libx264"),
    "amd": ("h264_amf", "libx264"),
    "apple": ("h264_videotoolbox", "libx264"),
    # Nessuna GPU nota: si resta sul software. Un encoder accelerato
    # presente nella lista di ffmpeg ma senza device compatibile fallisce
    # solo a runtime, e un video che non esce e' peggio di uno lento.
    "unknown": ("libx264",),
}


def encoder_args(encoder: str) -> list[str]:
    """Flag ffmpeg per un encoder; sconosciuti -> libx264 (software)."""
    return list(_ENCODER_ARGS.get(encoder, _ENCODER_ARGS["libx264"]))


def choose_video_encoder(vendor: str, available_encoders: frozenset[str]) -> str:
    """Sceglie l'encoder H.264 per questa GPU.

    `available_encoders` e' l'elenco degli encoder COMPILATI in ffmpeg, non
    quelli utilizzabili: per questo la scelta parte sempre dalla famiglia
    della GPU. Su una macchina AMD, `h264_qsv` risulterebbe disponibile
    ma fallirebbe a runtime.
    """
    for candidato in _ENCODER_BY_VENDOR.get(vendor, _ENCODER_BY_VENDOR["unknown"]):
        if candidato == "libx264" or candidato in available_encoders:
            return candidato
    return "libx264"


def _cache_fingerprint() -> str:
    """Identita' macchina + ffmpeg: la cache vale solo per questa combinazione."""
    return f"{platform.system()}/{platform.machine()}"


def ffmpeg_encoders(refresh: bool = False) -> frozenset[str]:
    """Encoder H.264 che questo ffmpeg sa produrre (vuoto se non c'e' ffmpeg).

    `-encoders` elenca quello che e' compilato dentro ffmpeg, non quello che
    funziona: per questo va sempre incrociato con la GPU (vedi
    `choose_video_encoder`). Il risultato e' cachato su disco perche' il
    probe costa ~100 ms e l'hardware non cambia da un giorno all'altro.
    """
    if not refresh:
        cached = _read_encoder_cache()
        if cached is not None:
            return cached

    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=_FFMPEG_TIMEOUT,
            check=False,
        ).stdout
    except Exception:
        return frozenset()

    trovati = frozenset(nome for nome in _ENCODER_ARGS if _encoder_in_list(out, nome))
    _write_encoder_cache(trovati)
    return trovati


def _encoder_in_list(output: str, nome: str) -> bool:
    """True se ffmpeg elenca l'encoder. Le righe sono tipo ' V..... h264_nvenc  ...'."""
    for riga in output.splitlines():
        colonne = riga.split()
        # La colonna dei flag e' lunga 6 ("V....D"), poi il nome.
        if len(colonne) >= 2 and colonne[1] == nome:
            return True
    return False


def _read_encoder_cache() -> frozenset[str] | None:
    try:
        data = json.loads(HARDWARE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != _cache_fingerprint():
        return None
    if time.time() - float(data.get("ts", 0)) > _CACHE_VALID_SECONDS:
        return None
    encoders = data.get("encoders")
    if not isinstance(encoders, list) or not all(isinstance(e, str) for e in encoders):
        return None
    return frozenset(encoders)


def _write_encoder_cache(encoders: frozenset[str]) -> None:
    # Non-fattibile e' un caso normale (cache in sola lettura, progetto su
    # share di rete): la mancanza del file costa un probe in piu', niente di grave.
    try:
        payload = {"fingerprint": _cache_fingerprint(), "ts": time.time(), "encoders": sorted(encoders)}
        HARDWARE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HARDWARE_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, HARDWARE_CACHE)
    except OSError:
        return


def profile_summary(video_encoder: str | None = None) -> str:
    """Riga sintetica dell'hardware, per il riepilogo di avvio."""
    tier = memory_tier(ram_total_bytes())
    logici = os.cpu_count() or 0
    fisici = _physical_cpus_or_none()
    cores = f"{fisici} fisici" if fisici else f"{logici} logici"
    parti = [f"{cores}", f"RAM {format_ram(ram_total_bytes())} ({tier})"]
    if video_encoder and video_encoder != "libx264":
        parti.append(f"video {video_encoder}")
    else:
        parti.append("video libx264 (CPU)")
    return ", ".join(parti)


def _physical_cpus_or_none() -> int | None:
    """Delega a config: qui non si duplica il probe dei core fisici."""
    try:
        from config import _physical_cpus

        return _physical_cpus()
    except Exception:
        return None
