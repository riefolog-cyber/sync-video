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
from typing import ClassVar
from unittest import mock

from machine_setup import (
    _CPU_FALLBACK,
    _apply,
    _classify_gpu,
    _fingerprint,
    _is_stale,
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

    def test_qualcomm_adreno_falls_back_to_cpu(self):
        # Windows ARM / Snapdragon X: nessuna accelerazione disponibile.
        rec = recommend(["Qualcomm(R) Adreno(TM) X1-45 GPU"])
        self.assertEqual(rec["transcriber"], "whisper")
        self.assertEqual(rec["whisper_device"], "cpu")
        self.assertIn("Qualcomm", rec["reason"])

    def test_mixed_qualcomm_and_intel_intel_wins(self):
        # Macchina con iGPU Intel E Adreno (es. laptop con GPU discreta
        # Qualcomm): vince la iGPU Intel (OpenVINO), come su x64.
        rec = recommend(["Qualcomm(R) Adreno(TM) X1-45 GPU", "Intel(R) Iris(R) Xe Graphics"])
        self.assertEqual(rec["transcriber"], "openvino")
        self.assertIn(rec["openvino_device"], ("GPU", "CPU"))


class TestClassifyGpu(unittest.TestCase):
    def test_qualcomm_adreno(self):
        self.assertEqual(_classify_gpu("Qualcomm(R) Adreno(TM) X1-45 GPU"), "qualcomm")

    def test_snapdragon(self):
        self.assertEqual(_classify_gpu("Snapdragon(R) X Elite GPU"), "qualcomm")

    def test_intel_still_classified(self):
        self.assertEqual(_classify_gpu("Intel(R) Iris(R) Xe Graphics"), "intel")

    def test_nvidia_still_classified(self):
        self.assertEqual(_classify_gpu("NVIDIA GeForce RTX 4060"), "nvidia")


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
        ):
            machine_setup(args, force=True)
        self.assertEqual(args.transcriber, "whisper")
        saved = _read_config()
        self.assertEqual(saved["transcriber"], "whisper")
        self.assertEqual(saved["whisper_device"], "cuda")

    def test_first_run_records_machine_fingerprint(self):
        # Senza l'impronta, la config riusata su un'altra macchina imporrebbe un
        # motore pensato per hardware diverso (es. OpenVINO su una GPU NVIDIA).
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["Intel(R) Iris(R) Xe Graphics"]),
            mock.patch("machine_setup._provision", side_effect=lambda rec, d: rec),
        ):
            machine_setup(args, force=True)
        saved = _read_config()
        self.assertEqual(saved["fingerprint"], _fingerprint())
        self.assertEqual(saved["gpus"], ["Intel(R) Iris(R) Xe Graphics"])

    def test_provision_failure_falls_back_to_cpu(self):
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["Intel(R) Iris(R) Xe Graphics"]),
            mock.patch("machine_setup._provision", return_value=dict(_CPU_FALLBACK)),
        ):
            machine_setup(args, force=True)
        self.assertEqual(args.transcriber, "whisper")
        self.assertEqual(args.whisper_device, "cpu")


class TestFingerprint(_TempConfigMixin, unittest.TestCase):
    """La config salvata vale solo per la macchina per cui' e' stata fatta."""

    _REC: ClassVar[dict] = {
        "transcriber": "openvino",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "openvino_device": "GPU",
        "reason": "test",
    }

    def _write(self, fingerprint):
        rec = dict(self._REC)
        if fingerprint is not None:
            rec["fingerprint"] = fingerprint
        self.config_path.write_text(json.dumps(rec), encoding="utf-8")

    def test_matching_fingerprint_is_reused_without_detecting(self):
        self._write(_fingerprint())
        args = _FakeArgs()
        with mock.patch("machine_setup.detect_gpus") as det:
            machine_setup(args, force=False)
            det.assert_not_called()
        self.assertEqual(args.transcriber, "openvino")

    def test_fingerprint_of_another_machine_triggers_redetection(self):
        # Cartella clonata da un PC Intel+iGPU a uno NVIDIA: la config salvata
        # non deve sopravvivere, altrimenti OpenVINO verrebbe riusato su una
        # macchina che non ha quella iGPU, senza avviso.
        self._write("Darwin/arm64")
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["NVIDIA GeForce RTX 4060"]),
            mock.patch("machine_setup._provision", side_effect=lambda rec, d: rec),
        ):
            machine_setup(args, force=False)
        self.assertEqual(args.transcriber, "whisper")
        self.assertEqual(args.whisper_device, "cuda")
        # La config riscritta porta l'impronta della macchina corrente.
        self.assertEqual(_read_config()["fingerprint"], _fingerprint())

    def test_legacy_config_without_fingerprint_is_accepted(self):
        # File scritto da una versione precedente: non c'e' modo di sapere da
        # dove arriva, quindi non si invalida (l'utente puo' usare --force-setup).
        self._write(None)
        args = _FakeArgs()
        with mock.patch("machine_setup.detect_gpus") as det:
            machine_setup(args, force=False)
            det.assert_not_called()
        self.assertEqual(args.transcriber, "openvino")

    def test_is_stale_helper(self):
        self.assertFalse(_is_stale({}))
        self.assertFalse(_is_stale({"fingerprint": _fingerprint()}))
        self.assertTrue(_is_stale({"fingerprint": "Something/else"}))


if __name__ == "__main__":
    unittest.main()
