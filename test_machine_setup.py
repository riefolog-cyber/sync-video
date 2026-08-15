#!/usr/bin/env python3
"""
Test unitari per `machine_setup` (rilevamento hardware al primo avvio).
Non tocca la rete né pip: usa GPU finte per verificare classificazione,
raccomandazione, provisioning (mockato) e persistenza.

Esegui con: python -m unittest test_machine_setup -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from machine_setup import (
    _CPU_FALLBACK,
    _apply,
    _read_config,
    machine_setup,
    recommend,
)


class _FakeArgs:
    def __init__(self, transcriber="auto"):
        self.transcriber = transcriber
        self.whisper_device = "cpu"
        self.whisper_compute_type = "int8"
        self.openvino_device = "GPU"
        self.openvino_model_dir = str(Path(tempfile.gettempdir()) / "whisper_openvino_small")


class _TempConfigMixin:
    """Reindirizza MACHINE_CONFIG_PATH su un file temporaneo per i test."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp_dir.name) / "machine_setup.json"
        patcher = mock.patch("machine_setup.MACHINE_CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp_dir.cleanup)


class TestRecommend(unittest.TestCase):
    def test_nvidia_wins(self):
        rec = recommend(["Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 4060"])
        self.assertEqual(rec["transcriber"], "whisper")
        self.assertEqual(rec["whisper_device"], "cuda")

    def test_intel_igpu(self):
        rec = recommend(["Intel(R) Iris(R) Xe Graphics"])
        self.assertEqual(rec["transcriber"], "openvino")
        self.assertIn(rec["openvino_device"], ("GPU", "CPU"))

    def test_amd_falls_back_to_cpu(self):
        rec = recommend(["AMD Radeon RX 6600"])
        self.assertEqual(rec["transcriber"], "whisper")
        self.assertEqual(rec["whisper_device"], "cpu")

    def test_no_gpu(self):
        rec = recommend([])
        self.assertEqual(rec["transcriber"], "whisper")
        self.assertEqual(rec["whisper_device"], "cpu")


class TestApply(unittest.TestCase):
    def test_auto_sets_transcriber(self):
        args = _FakeArgs(transcriber="auto")
        rec = {
            "transcriber": "openvino",
            "openvino_device": "GPU",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
        }
        _apply(args, rec)
        self.assertEqual(args.transcriber, "openvino")
        self.assertEqual(args.openvino_device, "GPU")

    def test_explicit_transcriber_is_respected(self):
        args = _FakeArgs(transcriber="whisper")
        rec = {
            "transcriber": "openvino",
            "openvino_device": "GPU",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
        }
        _apply(args, rec)
        self.assertEqual(args.transcriber, "whisper")
        self.assertEqual(args.openvino_device, "GPU")


class TestMachineSetup(_TempConfigMixin, unittest.TestCase):
    def test_rerun_uses_saved_config_without_provisioning(self):
        args = _FakeArgs()
        rec = {
            "transcriber": "whisper",
            "whisper_device": "cuda",
            "whisper_compute_type": "float16",
            "openvino_device": None,
            "reason": "test",
        }
        self.config_path.write_text(json.dumps(rec), encoding="utf-8")
        with mock.patch("machine_setup._provision") as prov, mock.patch("machine_setup.detect_gpus") as det:
            machine_setup(args, force=False)
            prov.assert_not_called()
            det.assert_not_called()
        self.assertEqual(args.transcriber, "whisper")
        self.assertEqual(args.whisper_device, "cuda")

    def test_first_run_provisions_and_persists(self):
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["NVIDIA GeForce RTX 4060"]),
            mock.patch("machine_setup._provision", side_effect=lambda rec, d: rec),
            mock.patch("machine_setup._update_env"),
        ):
            machine_setup(args, force=True)
        self.assertEqual(args.transcriber, "whisper")
        saved = _read_config()
        self.assertEqual(saved["transcriber"], "whisper")
        self.assertEqual(saved["whisper_device"], "cuda")

    def test_provision_failure_falls_back_to_cpu(self):
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["Intel(R) Iris(R) Xe Graphics"]),
            mock.patch("machine_setup._provision", return_value=dict(_CPU_FALLBACK)),
            mock.patch("machine_setup._update_env"),
        ):
            machine_setup(args, force=True)
        self.assertEqual(args.transcriber, "whisper")
        self.assertEqual(args.whisper_device, "cpu")


if __name__ == "__main__":
    unittest.main()
