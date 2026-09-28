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
    _read_hardware,
    _validate,
    _write_hardware,
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
    """La run riusa i FATTI hardware, ma ricalcola e riverifica la decisione."""

    def test_saved_decision_is_ignored_and_recomputed(self):
        # Il file puo' contenere ancora le chiavi di decisione di versioni
        # precedenti: non devono influire. La GPU salvata viene ricalcolata e la
        # decisione pure.
        args = _FakeArgs()
        self.config_path.write_text(
            json.dumps(
                {
                    "fingerprint": _fingerprint(),
                    "gpus": ["Intel(R) Iris(R) Xe Graphics"],
                    "transcriber": "whisper",
                    "whisper_device": "cuda",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("machine_setup._validate", side_effect=lambda rec, d: rec) as val:
            machine_setup(args, force=False)
            val.assert_called_once()
        self.assertEqual(args.transcriber, "openvino")
        self.assertNotEqual(args.whisper_device, "cuda")

    def test_cached_gpus_skip_detection_but_not_provisioning(self):
        # Rilevare costa una subprocess: si riusa la lista. Ma il provisioning
        # (che scarica) gira solo quando l'hardware e' stato davvero rilevato.
        args = _FakeArgs()
        _write_hardware(["NVIDIA GeForce RTX 4060"])
        with (
            mock.patch("machine_setup.detect_gpus") as det,
            mock.patch("machine_setup._provision") as prov,
            mock.patch("machine_setup._validate", side_effect=lambda rec, d: rec) as val,
        ):
            machine_setup(args, force=False)
            det.assert_not_called()
            prov.assert_not_called()
            val.assert_called_once()

    def test_first_run_provisions_and_persists(self):
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["NVIDIA GeForce RTX 4060"]),
            mock.patch("machine_setup._provision", side_effect=lambda rec, d: rec),
        ):
            machine_setup(args, force=True)
        self.assertEqual(args.transcriber, "whisper")

    def test_persisted_file_holds_only_hardware_facts(self):
        # La decisione NON deve finire su disco: e' cio' che la faceva invecchiare.
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["Intel(R) Iris(R) Xe Graphics"]),
            mock.patch("machine_setup._provision", side_effect=lambda rec, d: rec),
        ):
            machine_setup(args, force=True)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["fingerprint"], _fingerprint())
        self.assertEqual(saved["gpus"], ["Intel(R) Iris(R) Xe Graphics"])
        for key in ("transcriber", "whisper_device", "whisper_compute_type", "openvino_device"):
            self.assertNotIn(key, saved)

    def test_provision_failure_falls_back_to_cpu(self):
        args = _FakeArgs()
        with (
            mock.patch("machine_setup.detect_gpus", return_value=["Intel(R) Iris(R) Xe Graphics"]),
            mock.patch("machine_setup._provision", return_value=dict(_CPU_FALLBACK)),
        ):
            machine_setup(args, force=True)
        self.assertEqual(args.transcriber, "whisper")
        self.assertEqual(args.whisper_device, "cpu")


class TestReadHardware(_TempConfigMixin, unittest.TestCase):
    """`_read_hardware` decide se la lista GPU cachata e' fidabile."""

    def test_matching_fingerprint_returns_gpus(self):
        _write_hardware(["Intel(R) Iris(R) Xe Graphics"])
        self.assertEqual(_read_hardware(), ["Intel(R) Iris(R) Xe Graphics"])

    def test_fingerprint_of_another_machine_returns_none(self):
        # Cartella clonata: la lista GPU di un altro PC non e' fidabile.
        self.config_path.write_text(
            json.dumps({"fingerprint": "Darwin/arm64", "gpus": ["Apple M2"]}), encoding="utf-8"
        )
        self.assertIsNone(_read_hardware())

    def test_missing_file_returns_none(self):
        self.assertIsNone(_read_hardware())

    def test_corrupt_file_returns_none(self):
        self.config_path.write_text("{ non json", encoding="utf-8")
        self.assertIsNone(_read_hardware())

    def test_legacy_file_without_gpus_returns_none(self):
        # File di una versione precedente: non ha la lista, quindi si rileva.
        self.config_path.write_text(json.dumps({"transcriber": "openvino"}), encoding="utf-8")
        self.assertIsNone(_read_hardware())

    def test_empty_gpu_list_is_valid(self):
        # "nessuna GPU" e' un risultato legittimo e deve sopravvivere al cache:
        # rilevare di nuovo servirebbe solo a riavere la stessa risposta.
        _write_hardware([])
        self.assertEqual(_read_hardware(), [])

    def test_non_string_gpu_entries_rejected(self):
        self.config_path.write_text(
            json.dumps({"fingerprint": _fingerprint(), "gpus": [1, 2]}), encoding="utf-8"
        )
        self.assertIsNone(_read_hardware())


class TestValidate(unittest.TestCase):
    """`_validate` e' il controllo che impedisce di morire su un device assente."""

    _CUDA: ClassVar[dict] = {
        "transcriber": "whisper",
        "whisper_device": "cuda",
        "whisper_compute_type": "float16",
        "openvino_device": None,
        "reason": "GPU NVIDIA",
    }
    _OPENVINO: ClassVar[dict] = {
        "transcriber": "openvino",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "openvino_device": "GPU",
        "reason": "iGPU Intel",
    }

    def test_cuda_without_cuda_device_falls_back_to_cpu(self):
        # Il caso reale: lista GPU stale che raccomanda CUDA dove CUDA non esiste.
        with mock.patch("machine_setup._cuda_available", return_value=False):
            out = _validate(dict(self._CUDA), None)
        self.assertEqual(out["transcriber"], "whisper")
        self.assertEqual(out["whisper_device"], "cpu")
        self.assertEqual(out["whisper_compute_type"], "int8")

    def test_cuda_with_cuda_device_is_kept(self):
        with (
            mock.patch("machine_setup._cuda_available", return_value=True),
            mock.patch("machine_setup.importlib.util.find_spec", return_value=object()),
        ):
            out = _validate(dict(self._CUDA), None)
        self.assertEqual(out["whisper_device"], "cuda")

    def test_openvino_without_runtime_falls_back_to_cpu(self):
        with mock.patch("machine_setup.importlib.util.find_spec", return_value=None):
            out = _validate(dict(self._OPENVINO), Path("C:/qualcosa/che/non/esiste"))
        self.assertEqual(out["transcriber"], "whisper")
        self.assertEqual(out["whisper_device"], "cpu")

    def test_openvino_with_missing_model_falls_back_to_cpu(self):
        with mock.patch("machine_setup.importlib.util.find_spec", return_value=object()):
            out = _validate(dict(self._OPENVINO), Path("C:/non/esiste"))
        self.assertEqual(out["transcriber"], "whisper")

    def test_openvino_without_gpu_device_demotes_to_cpu_openvino(self):
        # La iGPU c'e' ma il runtime non espone il device GPU: si usa comunque
        # OpenVINO, solo su CPU. Non e' un ripiego su faster-whisper.
        with (
            mock.patch("machine_setup.importlib.util.find_spec", return_value=object()),
            mock.patch("machine_setup.openvino_gpu_available", return_value=False),
        ):
            out = _validate(dict(self._OPENVINO), Path(tempfile.gettempdir()))
        self.assertEqual(out["transcriber"], "openvino")
        self.assertEqual(out["openvino_device"], "CPU")

    def test_valid_openvino_is_untouched(self):
        with (
            mock.patch("machine_setup.importlib.util.find_spec", return_value=object()),
            mock.patch("machine_setup.openvino_gpu_available", return_value=True),
        ):
            out = _validate(dict(self._OPENVINO), Path(tempfile.gettempdir()))
        self.assertEqual(out["transcriber"], "openvino")
        self.assertEqual(out["openvino_device"], "GPU")

    def test_cpu_recommendation_needs_no_check(self):
        rec = {
            "transcriber": "whisper",
            "whisper_device": "cpu",
            "whisper_compute_type": "int8",
            "openvino_device": None,
            "reason": "CPU",
        }
        self.assertIs(_validate(rec, None), rec)


if __name__ == "__main__":
    unittest.main()
