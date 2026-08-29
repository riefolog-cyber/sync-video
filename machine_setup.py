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

La prima run rileva, installa/scarica ciò che serve e persiste la scelta
in ``.cache/machine_setup.json`` + ``.env``. Le run successive usano la
configurazione salvata senza rifare il rilevamento (salvo ``--force-setup``).
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from config import BASE_DIR, CACHE_DIR, log

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
    # Linux
    out = _run(["lspci", "-nn"])
    gpus: list[str] = []
    for line in out.splitlines():
        low = line.lower()
        if any(k in low for k in ("vga compatible", "3d controller", "display controller")):
            gpus.append(line.strip())
    return gpus


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


def _cuda_available() -> bool:
    """True se CTranslate2 vede una GPU CUDA utilizzabile."""
    try:
        import ctranslate2

        return bool(ctranslate2.get_cuda_device_count() > 0)
    except Exception:
        return False


def openvino_gpu_available() -> bool:
    """True se il runtime OpenVINO espone un device 'GPU' (iGPU Intel)."""
    try:
        from openvino import Core

        return "GPU" in Core().available_devices
    except Exception:
        return False


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
            "openvino_device": "GPU" if openvino_gpu_available() else "CPU",
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
    if not model_dir.exists():
        from transcription import download_openvino_model

        download_openvino_model(model_dir)
    return model_dir.exists()


def _provision(rec: dict, model_dir: Path) -> dict:
    """Applica il provisioning necessario per il motore consigliato."""
    if rec["transcriber"] == "openvino":
        if not _provision_openvino(model_dir):
            log.warning("   ⚠️ OpenVINO non pronto, ripiego su faster-whisper su CPU.")
            return dict(_CPU_FALLBACK)
    elif rec["whisper_device"] == "cuda" and not _cuda_available():
        log.warning("   ⚠️ CUDA non disponibile, uso faster-whisper su CPU.")
        return dict(_CPU_FALLBACK)
    return rec


# =====================================================================
# PERSISTENZA
# =====================================================================
def _read_config() -> dict:
    try:
        data = json.loads(MACHINE_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(rec: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MACHINE_CONFIG_PATH.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_env(rec: dict) -> None:
    """Appende le variabili consigliate a .env (senza sovrascrivere chiavi esistenti)."""
    env_path = BASE_DIR / ".env"
    existing: set[str] = set()
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                existing.add(line.partition("=")[0].strip())

    updates = {
        "TRANSCRIBER": rec["transcriber"],
        "WHISPER_DEVICE": rec["whisper_device"],
        "WHISPER_COMPUTE_TYPE": rec["whisper_compute_type"],
    }
    if rec.get("openvino_device"):
        updates["OPENVINO_DEVICE"] = rec["openvino_device"]

    added = [k for k in updates if k not in existing]
    if added:
        with env_path.open("a", encoding="utf-8") as f:
            for key in added:
                f.write(f"{key}={updates[key]}\n")
        log.info("   ✅ Configurazione salvata in .env: %s", ", ".join(added))


class _TranscriberArgs(Protocol):
    """Interfaccia minima degli args usata da ``_apply``/``machine_setup``."""

    transcriber: str
    whisper_device: str
    whisper_compute_type: str
    openvino_device: str
    openvino_model_dir: str


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
def machine_setup(args: _TranscriberArgs, force: bool = False) -> None:
    """Rileva l'hardware e configura il miglior motore (idempotente).

    Se già configurato (``machine_setup.json`` presente) e ``force`` è False,
    riapplica solo la scelta salvata senza rifare il rilevamento.
    """
    if not force and MACHINE_CONFIG_PATH.exists():
        cfg = _read_config()
        if cfg:
            _apply(args, cfg)
            return

    log.info("🔧 Rilevamento configurazione hardware al primo avvio...")
    gpus = detect_gpus()
    rec = recommend(gpus)
    rec = _provision(rec, Path(getattr(args, "openvino_model_dir", CACHE_DIR / "whisper_openvino_small")))

    log.info("   GPU rilevate: %s", ", ".join(gpus) if gpus else "(nessuna)")
    log.info("   Motore scelto: %s (%s)", rec["transcriber"], rec["reason"])
    if rec["transcriber"] == "openvino":
        log.info("   Device OpenVINO: %s", rec["openvino_device"])
    elif rec["whisper_device"] == "cuda":
        log.info("   Device faster-whisper: CUDA (float16)")

    _apply(args, rec)
    _write_config(rec)
    _update_env(rec)
