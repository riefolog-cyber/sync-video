#!/usr/bin/env python3
"""Test del rilevamento GPU su Linux a cascata (P2 #16).

Esegui con: python -m unittest test_gpu_linux -v

Il problema che copre: `lspci` sta in `pciutils`, che non e' installato di
default su molte distro. Senza i fallback, una macchina NVIDIA su Linux
finiva su faster-whisper CPU SENZA avvisare: rilevamento muto, 5-10x piu'
lento. Qui ogni cascata e' simulata.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import machine_setup as ms


class TestGpuLinux(unittest.TestCase):
    """Tre fonti, in ordine di affidabilita': lspci, nvidia-smi, sysfs."""

    def _con_lspci(self, out: str) -> AbstractContextManager[Any]:
        return patch.object(ms, "_run", side_effect=lambda cmd, timeout=30: out if "lspci" in cmd[0] else "")

    def test_lspci_disponibile_va_per_primo(self) -> None:
        out = "01:00.0 VGA compatible controller: NVIDIA Corporation GA106M [GeForce RTX 3060]\n"
        with self._con_lspci(out):
            got = ms._gpus_linux()
        self.assertEqual(len(got), 1)
        self.assertIn("NVIDIA", got[0])

    def test_senza_lspci_usa_nvidia_smi(self) -> None:
        # pciutils assente: nvidia-smi risponde e la GPU viene trovata.
        def finto(cmd: list[str], timeout: int = 30) -> str:
            if cmd[0] == "nvidia-smi":
                return "GPU 0: NVIDIA GeForce RTX 4070 (UUID: GPU-abc)\n"
            return ""  # lspci assente

        with patch.object(ms, "_run", side_effect=finto):
            got = ms._gpus_linux()
        self.assertEqual(len(got), 1)
        self.assertIn("NVIDIA", got[1 - 1])

    def test_senza_lspci_e_nvidia_smi_usa_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            card = Path(d) / "card0" / "device"
            card.mkdir(parents=True)
            (card / "uevent").write_text(
                "DRIVER=i915\nPCI_ID=8086:9A49\nPCI_SUBSYS_ID=17AA:22E8\n", encoding="utf-8"
            )
            with (
                patch.object(ms, "_run", return_value=""),  # niente lspci, niente nvidia-smi
                patch("glob.glob", return_value=[str(card / "uevent")]),
            ):
                got = ms._gpus_linux()
        self.assertEqual(len(got), 1)
        self.assertIn("Intel", got[0])

    def test_sysfs_amd_e_qualcomm(self) -> None:
        for vendor_id, atteso in (("1002:1636", "AMD"), ("17CB:0308", ""), ("5143:0900", "Qualcomm")):
            with tempfile.TemporaryDirectory() as d:
                card = Path(d) / "card0" / "device"
                card.mkdir(parents=True)
                (card / "uevent").write_text(f"DRIVER=test\nPCI_ID={vendor_id}\n", encoding="utf-8")
                with (
                    patch.object(ms, "_run", return_value=""),
                    patch("glob.glob", return_value=[str(card / "uevent")]),
                ):
                    got = ms._gpus_linux()
            with self.subTest(vendor=vendor_id):
                self.assertEqual(len(got), 1)
                if atteso:
                    self.assertIn(atteso, got[0])

    def test_nessuna_gpu_restituisce_vuoto(self) -> None:
        # Non deve MAI inventare una GPU: vuoto = ripiego su CPU, che e' la
        # scelta conservativa giusta quando non si sa.
        with patch.object(ms, "_run", return_value=""), patch("glob.glob", return_value=[]):
            self.assertEqual(ms._gpus_linux(), [])

    def test_sysfs_illeggibile_non_rompe(self) -> None:
        with (
            patch.object(ms, "_run", return_value=""),
            patch("glob.glob", side_effect=OSError("permesso negato")),
        ):
            self.assertEqual(ms._gpus_linux(), [])

    def test_il_vendor_sysfs_si_classifica_come_il_nome(self) -> None:
        # Il punto di tutto: la stringa grezza deve passare _classify_gpu,
        # altrimenti il fallback sysfs non serve a nulla.
        with tempfile.TemporaryDirectory() as d:
            for indice, (vid, driver) in enumerate(
                [("8086:9A49", "i915"), ("10DE:2504", "nouveau"), ("1002:1636", "amdgpu")]
            ):
                card = Path(d) / f"card{indice}" / "device"
                card.mkdir(parents=True)
                (card / "uevent").write_text(f"DRIVER={driver}\nPCI_ID={vid}\n", encoding="utf-8")
            with (
                patch.object(ms, "_run", return_value=""),
                patch("glob.glob", return_value=[str(p / "device" / "uevent") for p in Path(d).iterdir()]),
            ):
                got = ms._gpus_linux()
        famiglie = {ms._classify_gpu(n) for n in got}
        self.assertIn("intel", famiglie)
        self.assertIn("nvidia", famiglie)
        self.assertIn("amd", famiglie)


class TestDetectGpusSuQuestraMacchina(unittest.TestCase):
    def test_non_solleva_e_ritorna_qualcosa(self) -> None:
        try:
            gpus = ms.detect_gpus()
        except Exception as e:
            self.fail(f"detect_gpus ha sollevato {type(e).__name__}: {e}")
        self.assertIsInstance(gpus, list)


if __name__ == "__main__":
    unittest.main()
