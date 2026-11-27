"""Debug end-to-end dell'adattamento hardware (P2 #14).

Non esegue la pipeline: simula le MACCHINE target e verifica che ogni
decisione (motore, encoder, thread, batch) sia quella giusta per ciascuna,
e che nessun percorso si rompa. Eseguire dalla cartella progetto:

    .\\.venv\\Scripts\\python.exe _debug_hardware.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
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


def _encoding_ok(
    out: Path, probe_stdout: str, encoder_effettivo: list[str], richiesto: str
) -> bool:
    """True solo se il video e' valido ED e' stato usato l'encoder richiesto.

    `ffprobe` riporta `h264` anche per il ripiego libx264: senza il confronto
    con l'encoder EFFETTIVO, il controllo sull'accelerazione hardware dava OK
    mentre il percorso accelerato non era mai riuscito.
    """
    return (
        out.exists()
        and out.stat().st_size > 0
        and "h264" in probe_stdout
        and encoder_effettivo == [richiesto]
    )


def _check(label: str, ottenuto: object, atteso: object) -> bool:
    ok = ottenuto == atteso
    print(f"   [{'OK ' if ok else 'KO '}] {label}: {ottenuto}" + ("" if ok else f"  (atteso: {atteso})"))
    return ok


def scenario(
    nome: str,
    gpus: list[str],
    vendor: str,
    ram_gb: float,
    encoders: frozenset[str],
    piattaforma: str = "linux",
) -> bool:
    print(f"\n{'=' * 66}\n{nome}\n{'=' * 66}")
    ok = True
    totale = int(ram_gb * GB)
    with (
        patch.object(ms, "_read_hardware", return_value=gpus),
        patch.object(ms, "_validate", side_effect=lambda rec, d: rec),
        # `_video_vendor` ora privilegia macOS: senza patchare la piattaforma,
        # questo tool girato su un Mac darebbe KO su tutti gli scenari GPU.
        patch.object(ms.sys, "platform", piattaforma),
        # machine_setup importa `ffmpeg_encoders` con `from hardware import`:
        # va patchato DOVE VIENE USATO (ms), non nel modulo hardware, altrimenti
        # l'encoder "simulato" resta quello vero della macchina dello sviluppatore.
        patch.object(ms, "ffmpeg_encoders", return_value=encoders),
        patch.object(hw, "ram_total_bytes", return_value=totale),
        # La macchina SIMULATA non ha override: VIDEO_ENCODER nel .env della
        # macchina reale e' una scelta documentata, non un guasto di machine_setup.
        # Senza questo, ogni scenario GPU dava KO con un override "libx264".
        patch.dict(os.environ, {"VIDEO_ENCODER": ""}),
    ):
        args = _Args()
        ms.machine_setup(args, force=False)
        # Il controllo DA' star dentro il patch di sys.platform: fuori, su un
        # Mac reale, `_video_vendor` tornerebbe "apple" per tutti gli scenari.
        ok &= _check("famiglia GPU", ms._video_vendor(gpus), vendor)
        # Prima l'encoder veniva solo STAMPATO: un machine_setup che sceglieva
        # sempre libx264 passava il controllo. Si confronta con la scelta che
        # la stessa funzione condivisa fa per questa GPU simulata.
        ok &= _check(
            "encoder video",
            args.video_encoder,
            hw.choose_video_encoder(vendor, encoders),
        )
    # Prima si confrontava memory_tier(totale) con se' stesso: non poteva mai
    # fallire, e il ram_gb dello scenario non veniva verificato. Le soglie sono
    # quelle reali di hardware.memory_tier (<6 basso, <12 medio, poi alto).
    atteso_tier = "basso" if ram_gb < 6 else "medio" if ram_gb < 12 else "alto"
    ok &= _check("tier RAM", hw.memory_tier(totale), atteso_tier)
    print(f"   [   ] RAM: {hw.format_ram(totale)} ({hw.memory_tier(totale)})")
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
        try:
            esito = subprocess.run(
                [sys.executable, "-m", "mypy", "--platform", piattaforma, "."],
                capture_output=True,
                text=True,
                # Anche qui serve un tetto: senza, un mypy appeso blocca
                # `_debug_hardware.py` (e il passo "debug hardware" dei
                # controlli) per sempre, invece di dare un KO con motivo.
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            buono = False
            riga = "TIMEOUT: mypy non e' terminato entro 300s"
            dettagli: list[str] = []
        else:
            riga = (
                (esito.stdout or esito.stderr).strip().splitlines()[-1]
                if (esito.stdout or esito.stderr)
                else "?"
            )
            buono = esito.returncode == 0
            dettagli = (esito.stdout or "").strip().splitlines()[:8]
        ok &= buono
        print(f"   [{'OK ' if buono else 'KO '}] {piattaforma:7s}: {riga}")
        if not buono:
            for dettaglio in dettagli:
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
    # Ultima riga del vecchio controllo: confrontava tuple riscritte a mano
    # con numeri ricalcolati dalle STESSE formule, quindi non poteva mai
    # fallire anche se config.py non usava affatto quei valori. Ora si legge
    # la configurazione REALE caricata all'import.
    import config

    ram = hw.ram_total_bytes()
    tier_reale = hw.memory_tier(ram) if ram else "alto"
    wb_atteso = 4 if tier_reale == "basso" else 8
    eb_atteso = {"basso": 16, "medio": 32}.get(tier_reale, 64)
    if "WHISPER_BATCH" in os.environ or "EMBED_BATCH" in os.environ:
        print("   [   ] batch su RAM: salto (override esplicito nell'ambiente)")
        ok2 = True
    else:
        ok2 = True
        ok2 &= _check("batch whisper (RAM di questa macchina)", config.DEFAULT_WHISPER_BATCH, wb_atteso)
        ok2 &= _check("batch embedding (RAM di questa macchina)", config.DEFAULT_EMBED_BATCH, eb_atteso)
    risultati.append(("batch su RAM", ok2))

    print("\n" + "=" * 66)
    print("3. MACHINE SETUP SU MACCHINE DIVERSE")
    print("=" * 66)
    risultati += [
        (
            "NVIDIA + 32 GB",
            scenario(
                "NVIDIA desktop, 32 GB", ["NVIDIA GeForce RTX 4060"],
                "nvidia", 32, completo, "win32",
            ),
        ),
        (
            "Intel iGPU + 8 GB",
            scenario(
                "Notebook Intel Iris Xe, 8 GB", ["Intel(R) Iris(R) Xe Graphics"],
                "intel", 8, completo, "win32",
            ),
        ),
        (
            "AMD + 16 GB",
            scenario(
                "Desktop AMD Radeon RX 6600, 16 GB", ["AMD Radeon RX 6600"],
                "amd", 16, completo, "win32",
            ),
        ),
        (
            "Senza GPU + 4 GB",
            scenario(
                "Portatile UMA, 4 GB, nessuna GPU", [],
                "unknown", 4, solo_software, "win32",
            ),
        ),
        (
            "Mac con iGPU",
            scenario(
                "Mac Intel, 16 GB", ["Intel(R) Iris(R) Xe Graphics"],
                "apple", 16, completo, "darwin",
            ),
        ),
    ]

    print("\n" + "=" * 66)
    print("4. L'ENCODER SCELTO PRODUCE UN VIDEO REALE")
    print("=" * 66)
    tmp_ctx: Any = None
    try:
        from video import _build_video_ffmpeg

        tmp_ctx = tempfile.TemporaryDirectory(prefix="dbg_hw_")
        tmp = Path(tmp_ctx.name)
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
            encoder_effettivo: list[str] = []
            _build_video_ffmpeg(
                [str(png)], [2.0], aud, out, 5, 12, encoder=enc,
                encoder_out=encoder_effettivo,
            )
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", str(out)],
                capture_output=True, text=True,
            )
            # ffprobe vede "h264" anche quando l'encoder richiesto e' fallito
            # e il codice e' ripiegato su libx264: senza chiedere l'encoder
            # EFFETTIVO il controllo 6 diceva OK mentre l'accelerazione non
            # funzionava per niente. Il ripiego e' comunque un video valido:
            # si segnala come AVV (la sezione 5 verifica che funzioni), non
            # come KO, cosi' la diagnostica non boccia una macchina che produce
            # comunque il video.
            video_ok = out.exists() and out.stat().st_size > 0 and "h264" in probe.stdout
            senza_ripiego = _encoding_ok(out, probe.stdout, encoder_effettivo, enc)
            if not video_ok:
                etichetta, good = "KO ", False
            elif senza_ripiego:
                etichetta, good = "OK ", True
            else:
                etichetta, good = "AVV", True
                print(
                    f"   ⚠️  {enc} non utilizzabile a runtime: ripiegato su "
                    f"{','.join(encoder_effettivo) or 'libx264'}."
                )
            print(f"   [{etichetta}] {enc}: {out.stat().st_size} byte, {probe.stdout.strip()}")
            risultati.append((f"encoding reale {enc}", good))
    except Exception as e:  # noqa: BLE001
        print(f"   [KO ] encoding reale: {type(e).__name__}: {e}")
        risultati.append(("encoding reale", False))
    finally:
        # Ogni `controlli.py` creava due cartelle in %TEMP% e non le
        # rimuoveva mai: la diagnostica non puo' essere la causa di crescita
        # infinita del disco.
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    print("\n" + "=" * 66)
    print("5. RIPIEGO SE L'ENCODER ACCELERATO FALLISCE A RUNTIME")
    print("=" * 66)
    tmp2_ctx: Any = None
    try:
        import video

        tmp2_ctx = tempfile.TemporaryDirectory(prefix="dbg_fb_")
        tmp2 = Path(tmp2_ctx.name)
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
    finally:
        if tmp2_ctx is not None:
            tmp2_ctx.cleanup()

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
