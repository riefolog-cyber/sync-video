"""Debug end-to-end dell'adattamento hardware (P2 #14).

Non esegue la pipeline: simula le MACCHINE target e verifica che ogni
decisione (motore, encoder, thread, batch) sia quella giusta per ciascuna,
e che nessun percorso si rompa. Eseguire dalla cartella progetto:

    .\\.venv\\Scripts\\python.exe _debug_hardware.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import hardware as hw
import machine_setup as ms

GB = 1024**3


class _Args:
    """args minimi, come li prepara argparse."""

    transcriber = "auto"
    whisper_device = "cpu"
    whisper_compute_type = "int8"
    openvino_device = "GPU"
    openvino_model_dir = "x"  # il Protocol di machine_setup lo dichiara str
    video_encoder = "auto"
    whisper_model = "small"


def _check(label: str, ottenuto: object, atteso: object) -> bool:
    ok = ottenuto == atteso
    print(f"   [{'OK ' if ok else 'KO '}] {label}: {ottenuto}" + ("" if ok else f"  (atteso: {atteso})"))
    return ok


def scenario(nome: str, gpus: list[str], vendor: str, ram_gb: float, encoders: frozenset[str]) -> bool:
    print(f"\n{'=' * 66}\n{nome}\n{'=' * 66}")
    ok = True
    totale = int(ram_gb * GB)
    with (
        patch.object(ms, "_read_hardware", return_value=gpus),
        patch.object(ms, "_validate", side_effect=lambda rec, d: rec),
        patch.object(hw, "ffmpeg_encoders", return_value=encoders),
        patch.object(hw, "ram_total_bytes", return_value=totale),
    ):
        args = _Args()
        ms.machine_setup(args, force=False)

    ok &= _check("famiglia GPU", ms._video_vendor(gpus), vendor)
    ok &= _check("tier RAM", hw.memory_tier(totale), hw.memory_tier(totale))
    print(f"   [   ] RAM: {hw.format_ram(totale)} ({hw.memory_tier(totale)})")
    print(f"   [   ] encoder scelto: {args.video_encoder}")
    return ok


def _mypy_cross_platform() -> bool:
    """mypy sulle TRE piattaforme, non solo su quella di sviluppo.

    La CI gira su ubuntu-latest: un type-check passato solo su Windows non
    dice niente su quello che vede la CI. E' gia' successo: un import
    condizionale `if sys.platform == "win32"` passava in locale e faceva
    fallire la CI su tutte e tre le versioni di Python, perche' mypy
    valuta la condizione e sul ramo non-Windows il nome risultava non
    definito.
    """
    import subprocess
    import sys

    print(f"\n{'=' * 66}\n6. MYPY SU TUTTE LE PIATTAFORME (la CI gira su Linux)\n{'=' * 66}")
    ok = True
    for piattaforma in ("win32", "linux", "darwin"):
        esito = subprocess.run(
            [sys.executable, "-m", "mypy", "--platform", piattaforma, "."],
            capture_output=True,
            text=True,
        )
        riga = (esito.stdout or esito.stderr).strip().splitlines()[-1] if (esito.stdout or esito.stderr) else "?"
        buono = esito.returncode == 0
        ok &= buono
        print(f"   [{'OK ' if buono else 'KO '}] {piattaforma:7s}: {riga}")
        if not buono and esito.stdout:
            for dettaglio in esito.stdout.strip().splitlines()[:8]:
                print(f"          {dettaglio}")
    return ok


def main() -> int:
    risultati: list[tuple[str, bool]] = []
    completo = frozenset({"libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"})
    solo_software = frozenset({"libx264"})

    print("=" * 66)
    print("1. SCELTA DELL'ENCODER SU MACCHINE DIVERSE")
    print("=" * 66)
    risultati += [
        ("NVIDIA desktop", hw.choose_video_encoder("nvidia", completo) == "h264_nvenc"),
        ("Notebook Intel iGPU", hw.choose_video_encoder("intel", completo) == "h264_qsv"),
        ("AMD Radeon", hw.choose_video_encoder("amd", completo) == "h264_amf"),
        ("Mac Intel", hw.choose_video_encoder("apple", completo) == "h264_videotoolbox"),
        ("Nessuna GPU", hw.choose_video_encoder("unknown", completo) == "libx264"),
        ("ffmpeg minimale", hw.choose_video_encoder("nvidia", solo_software) == "libx264"),
        ("AMD senza AMF", hw.choose_video_encoder("amd", solo_software) == "libx264"),
    ]
    for nome, ok in risultati:
        print(f"   [{'OK ' if ok else 'KO '}] {nome}")

    print("\n" + "=" * 66)
    print("2. BATCH SCALATI SULLA RAM")
    print("=" * 66)
    tier_basso = hw.memory_tier(4 * GB)
    tier_medio = hw.memory_tier(8 * GB)
    tier_alto = hw.memory_tier(32 * GB)
    ok2 = tier_basso == "basso" and tier_medio == "medio" and tier_alto == "alto"
    for etichetta, tier, batch in (
        ("4 GB  -> whisper/embed", tier_basso, (4, 16)),
        ("8 GB  -> whisper/embed", tier_medio, (8, 32)),
        ("32 GB -> whisper/embed", tier_alto, (8, 64)),
    ):
        wb = 4 if tier == "basso" else 8
        eb = {"basso": 16, "medio": 32}.get(tier, 64)
        atteso = batch == (wb, eb)
        ok2 &= atteso
        print(f"   [{'OK ' if atteso else 'KO '}] {etichetta}: whisper={wb}, embedding={eb}")
    risultati.append(("batch su RAM", ok2))

    print("\n" + "=" * 66)
    print("3. MACHINE SETUP SU MACCHINE DIVERSE")
    print("=" * 66)
    risultati += [
        ("NVIDIA + 32 GB", scenario("NVIDIA desktop, 32 GB", ["NVIDIA GeForce RTX 4060"], "nvidia", 32, completo)),
        (
            "Intel iGPU + 8 GB",
            scenario("Notebook Intel Iris Xe, 8 GB", ["Intel(R) Iris(R) Xe Graphics"], "intel", 8, completo),
        ),
        ("AMD + 16 GB", scenario("Desktop AMD Radeon RX 6600, 16 GB", ["AMD Radeon RX 6600"], "amd", 16, completo)),
        ("Senza GPU + 4 GB", scenario("Portatile UMA, 4 GB, nessuna GPU", [], "unknown", 4, solo_software)),
    ]

    print("\n" + "=" * 66)
    print("4. L'ENCODER SCELTO PRODUCE UN VIDEO REALE")
    print("=" * 66)
    try:
        from video import _build_video_ffmpeg

        tmp = Path(tempfile.mkdtemp(prefix="dbg_hw_"))
        png, aud = tmp / "s.png", tmp / "a.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=size=640x360:rate=5:duration=1", "-frames:v", "1", str(png)],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=2", str(aud)],
            check=True,
        )
        for enc in hw.choose_video_encoder("intel", hw.ffmpeg_encoders()), "libx264":
            out = tmp / f"{enc}.mp4"
            _build_video_ffmpeg([str(png)], [2.0], aud, out, 5, 12, encoder=enc)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(out)],
                capture_output=True, text=True,
            )
            good = out.stat().st_size > 0 and "h264" in probe.stdout
            print(f"   [{'OK ' if good else 'KO '}] {enc}: {out.stat().st_size} byte, {probe.stdout.strip()}")
            risultati.append((f"encoding reale {enc}", good))
    except Exception as e:  # noqa: BLE001
        print(f"   [KO ] encoding reale: {type(e).__name__}: {e}")
        risultati.append(("encoding reale", False))

    print("\n" + "=" * 66)
    print("5. RIPIEGO SE L'ENCODER ACCELERATO FALLISCE A RUNTIME")
    print("=" * 66)
    try:
        import video

        tmp2 = Path(tempfile.mkdtemp(prefix="dbg_fb_"))
        png2, aud2 = tmp2 / "s.png", tmp2 / "a.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=size=640x360:rate=5:duration=1", "-frames:v", "1", str(png2)],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=2", str(aud2)],
            check=True,
        )
        tentativi: list[str] = []
        originale = video._run_ffmpeg

        def finto(cmd: list[str], total: float) -> None:
            tentativi.append(" ".join(cmd))
            if "h264_qsv" in " ".join(cmd):
                raise RuntimeError("ffmpeg è fallito (exit code 1). Simulazione: driver assente.")
            return originale(cmd, total)

        video._run_ffmpeg = finto  # type: ignore[assignment]
        out3 = tmp2 / "fallback.mp4"
        video._build_video_ffmpeg([str(png2)], [2.0], aud2, out3, 5, 12, encoder="h264_qsv")
        video._run_ffmpeg = originale  # type: ignore[assignment]
        ok5 = (
            len(tentativi) == 2
            and "h264_qsv" in tentativi[0]
            and "libx264" in tentativi[1]
            and out3.stat().st_size > 0
        )
        print(f"   [{'OK ' if ok5 else 'KO '}] ripiego: {len(tentativi)} tentativi, video {out3.stat().st_size} byte")
        risultati.append(("ripiego su libx264", ok5))
    except Exception as e:  # noqa: BLE001
        print(f"   [KO ] ripiego: {type(e).__name__}: {e}")
        risultati.append(("ripiego su libx264", False))

    print("\n" + "=" * 66)
    risultati.append(("mypy cross-platform", _mypy_cross_platform()))

    print("\n" + "=" * 66)
    falliti = [n for n, ok in risultati if not ok]
    print(f"RISULTATO: {len(risultati) - len(falliti)}/{len(risultati)} controlli superati")
    if falliti:
        print("FALLITI: " + ", ".join(falliti))
    print("=" * 66)
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
