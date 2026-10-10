#!/usr/bin/env python3
"""Test del rilevamento hardware (P2 #14).

Esegui con: python -m unittest test_hardware -v

Nessun download, nessuna rete, nessun encoder lanciato: i probe che
dipendono dalla macchina sono simulati, quelli puri sono verificati per
definizione.
"""

from __future__ import annotations

import json
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import hardware as hw


class TestMemoryTier(unittest.TestCase):
    """Le soglie: sotto 6 GB il batch scende, sotto 12 e' ancora accettabile."""

    def test_classificazione(self) -> None:
        gb = 1024**3
        casi = [
            (None, "ignoto"),
            (0, "ignoto"),
            (4 * gb, "basso"),
            (5_900 * 1024**2, "basso"),
            (6 * gb, "medio"),
            (8 * gb, "medio"),
            (11_900 * 1024**2, "medio"),
            (12 * gb, "alto"),
            (64 * gb, "alto"),
        ]
        for valore, atteso in casi:
            with self.subTest(gb=valore):
                self.assertEqual(hw.memory_tier(valore), atteso)

    def test_nessun_dato_ammette_nessun_crash(self) -> None:
        self.assertEqual(hw.memory_tier(None), "ignoto")

    def test_format_ram(self) -> None:
        self.assertEqual(hw.format_ram(None), "sconosciuta")
        self.assertEqual(hw.format_ram(0), "sconosciuta")
        self.assertTrue(hw.format_ram(16 * 1024**3).startswith("16.0 GB"))


class TestChooseVideoEncoder(unittest.TestCase):
    """Il punto delicato: `ffmpeg -encoders` elenca quello che e' COMPILATO,
    non quello che funziona. Un encoder accelerato presente nella lista ma
    senza device compatibile fallisce solo a runtime."""

    tutti = frozenset({"libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"})

    def test_nvidia_va_su_nvenc(self) -> None:
        self.assertEqual(hw.choose_video_encoder("nvidia", self.tutti), "h264_nvenc")

    def test_intel_va_su_qsv(self) -> None:
        self.assertEqual(hw.choose_video_encoder("intel", self.tutti), "h264_qsv")

    def test_amd_va_su_amf(self) -> None:
        self.assertEqual(hw.choose_video_encoder("amd", self.tutti), "h264_amf")

    def test_apple_va_su_videotoolbox(self) -> None:
        self.assertEqual(hw.choose_video_encoder("apple", self.tutti), "h264_videotoolbox")

    def test_nessuna_gpu_resta_sul_software(self) -> None:
        # Anche se ffmpeg ha tutto compilato, senza GPU nota non si sceglie
        # un encoder accelerato: fallirebbe a runtime.
        for vendor in ("unknown", "qualcomm", ""):
            with self.subTest(vendor=vendor):
                self.assertEqual(hw.choose_video_encoder(vendor, self.tutti), "libx264")

    def test_amd_non_va_a_cercare_qsv(self) -> None:
        # h264_qsv e' nella lista di quasi ogni build ffmpeg, ma su una GPU
        # AMD non funziona: non deve essere scelto come ripiego nemmeno
        # quando la build non ha AMF (le build senza AMF sono comuni).
        self.assertEqual(hw.choose_video_encoder("amd", frozenset({"libx264", "h264_qsv"})), "libx264")

    def test_intel_puo_ripiegare_su_amf(self) -> None:
        # Con Intel e AMD insieme vince 'intel' nella precedenza, e AMF puo'
        # legittimamente funzionare sulla dGPU AMD.
        self.assertEqual(hw.choose_video_encoder("intel", frozenset({"libx264", "h264_amf"})), "h264_amf")

    def test_se_l_accelerato_non_c_e_ripiega_su_libx264(self) -> None:
        # Build ffmpeg minimale: solo libx264.
        self.assertEqual(hw.choose_video_encoder("nvidia", frozenset({"libx264"})), "libx264")
        self.assertEqual(hw.choose_video_encoder("intel", frozenset()), "libx264")

    def test_rispetta_l_ordine_di_precedenza(self) -> None:
        # NVENC prima di AMF: su una macchina con entrambe, la dGPU NVIDIA
        # vince (stessa precedenza di machine_setup.recommend).
        solo_amf_e_libx = frozenset({"libx264", "h264_amf"})
        self.assertEqual(hw.choose_video_encoder("nvidia", solo_amf_e_libx), "h264_amf")


class TestEncoderArgs(unittest.TestCase):
    def test_ogni_encoder_conosciuto_ha_i_suoi_flag(self) -> None:
        for enc, attesi in (
            ("libx264", ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage"]),
            ("h264_qsv", ["-c:v", "h264_qsv"]),
            ("h264_nvenc", ["-c:v", "h264_nvenc"]),
            ("h264_amf", ["-c:v", "h264_amf"]),
            ("h264_videotoolbox", ["-c:v", "h264_videotoolbox"]),
        ):
            with self.subTest(enc=enc):
                got = hw.encoder_args(enc)
                self.assertEqual(got[:2], ["-c:v", enc])
                for flag in attesi:
                    self.assertIn(flag, got)

    def test_encoder_sconosciuto_ricade_sul_software(self) -> None:
        # Non deve mai passare un encoder inesistente a ffmpeg.
        self.assertEqual(hw.encoder_args("h264_non_esiste"), hw.encoder_args("libx264"))
        self.assertEqual(hw.encoder_args(""), hw.encoder_args("libx264"))

    def test_vaapi_escluso(self) -> None:
        # VAAPI richiede il filtro hwupload e -vaapi_device: percorsi fragili,
        # esclusi di proposito.
        self.assertNotIn("h264_vaapi", hw._ENCODER_ARGS)
        self.assertNotIn("vaapi", " ".join(str(v) for v in hw._ENCODER_BY_VENDOR.values()))

    def test_restituisce_copia(self) -> None:
        # Il chiamante potrebbe modificarlo: non deve corrompere il modulo.
        primo = hw.encoder_args("libx264")
        primo.append("--roba")
        self.assertNotIn("--roba", hw.encoder_args("libx264"))


class TestEncoderInList(unittest.TestCase):
    """Parsing dell'output di `ffmpeg -encoders`."""

    OUTPUT = """
Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D av1_vaapi            AV1 (VAAPI) (codec av1)
 V....D libx264              libx264 H.264 / AVC (codec h264)
 V..... h264_amf             AMD AMF H.264 Encoder (codec h264)
 V..... h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V..... h264_qsv             H.264 / AVC (codec h264)
"""

    def test_trova_gli_encoder(self) -> None:
        for nome in ("libx264", "h264_amf", "h264_nvenc", "h264_qsv", "av1_vaapi"):
            self.assertTrue(hw._encoder_in_list(self.OUTPUT, nome), nome)

    def test_non_trova_quello_che_non_c_e(self) -> None:
        for nome in ("h264_videotoolbox", "hevc_nvenc", "h264_inesistente", "libx265"):
            self.assertFalse(hw._encoder_in_list(self.OUTPUT, nome), nome)

    def test_nessun_falso_positivo_su_prefissi(self) -> None:
        # 'libx264rgb' contiene 'libx264': il confronto e' per colonna esatta.
        out = " V..... libx264rgb       libx264 RGB (codec h264)"
        self.assertFalse(hw._encoder_in_list(out, "libx264"))
        self.assertTrue(hw._encoder_in_list(out, "libx264rgb"))

    def test_output_vuoto(self) -> None:
        self.assertFalse(hw._encoder_in_list("", "libx264"))


class TestRamProbes(unittest.TestCase):
    def test_totale_positivo_ulla_macchina_di_test(self) -> None:
        totale = hw.ram_total_bytes()
        # Non deve mai sollevare. Se il probe funziona, il numero e' plausibile.
        if totale is not None:
            self.assertGreater(totale, 0)

    def test_totale_e_disponibile_coerenti(self) -> None:
        totale, disponibile = hw.ram_total_bytes(), hw.ram_available_bytes()
        if totale is not None and disponibile is not None:
            self.assertLessEqual(disponibile, totale)


class TestEncoderCache(unittest.TestCase):
    """La cache su disco e' un'ottimizzazione: se fallisce, si riprova."""

    def _cache(self, tmp: Path, payload: dict) -> None:
        (tmp / "hardware.json").write_text(json.dumps(payload), encoding="utf-8")

    def _con_cache(self, tmp: Path) -> AbstractContextManager[Any]:
        return patch.object(hw, "HARDWARE_CACHE", tmp / "hardware.json")

    def _con_fingerprint(self, valore: str) -> AbstractContextManager[Any]:
        return patch.object(hw, "_cache_fingerprint", return_value=valore)

    def test_scrive_e_rilegge(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d, self._con_cache(Path(d)), self._con_fingerprint("X/Y"):
            hw._write_encoder_cache(frozenset({"libx264", "h264_qsv"}))
            self.assertEqual(hw._read_encoder_cache(), frozenset({"libx264", "h264_qsv"}))

    def test_impronta_diversa_ignora_la_cache(self) -> None:
        # Progetto clonato su un'altra macchina: la cache vale per quella.
        import tempfile

        with tempfile.TemporaryDirectory() as d, self._con_cache(Path(d)):
            (Path(d) / "hardware.json").write_text(
                json.dumps({"fingerprint": "ALTRA/MACCHINA", "ts": 9e9, "encoders": ["h264_nvenc"]}),
                encoding="utf-8",
            )
            with self._con_fingerprint("QUESTA/MACCHINA"):
                self.assertIsNone(hw._read_encoder_cache())

    def test_cache_scaduta_ignorata(self) -> None:
        # L'hardware non cambia, ma ffmpeg puo' essere aggiornato: dopo una
        # settimana si rileva.
        import tempfile

        with tempfile.TemporaryDirectory() as d, self._con_cache(Path(d)), self._con_fingerprint("X/Y"):
            (Path(d) / "hardware.json").write_text(
                json.dumps({"fingerprint": "X/Y", "ts": 0, "encoders": ["h264_nvenc"]}),
                encoding="utf-8",
            )
            self.assertIsNone(hw._read_encoder_cache())

    def test_cache_corrotta_ignorata(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d, self._con_cache(Path(d)):
            (Path(d) / "hardware.json").write_text("{non e' json", encoding="utf-8")
            self.assertIsNone(hw._read_encoder_cache())

    def test_contenuto_malformato_ignorato(self) -> None:
        import tempfile

        for payload in ({"fingerprint": "X/Y", "ts": 9e9}, {"fingerprint": "X/Y", "ts": 9e9, "encoders": [1, 2]}):
            with tempfile.TemporaryDirectory() as d, self._con_cache(Path(d)), self._con_fingerprint("X/Y"):
                (Path(d) / "hardware.json").write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload):
                    self.assertIsNone(hw._read_encoder_cache())

    def test_il_probe_usa_la_cache(self) -> None:
        # Se la cache c'e', ffmpeg non viene lanciato.
        import tempfile

        with tempfile.TemporaryDirectory() as d, self._con_cache(Path(d)), self._con_fingerprint("X/Y"):
            (Path(d) / "hardware.json").write_text(
                json.dumps({"fingerprint": "X/Y", "ts": 9e9, "encoders": ["h264_qsv"]}),
                encoding="utf-8",
            )
            with patch.object(hw.subprocess, "run", side_effect=AssertionError("ffmpeg non doveva partire")):
                self.assertEqual(hw.ffmpeg_encoders(), frozenset({"h264_qsv"}))

    def test_ffmpeg_assente_ritorna_vuoto(self) -> None:
        import tempfile

        with (
            tempfile.TemporaryDirectory() as d,
            self._con_cache(Path(d)),
            patch.object(hw.subprocess, "run", side_effect=FileNotFoundError),
        ):
            self.assertEqual(hw.ffmpeg_encoders(), frozenset())

    def test_refresh_ignora_la_cache(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d, self._con_cache(Path(d)), self._con_fingerprint("X/Y"):
            (Path(d) / "hardware.json").write_text(
                json.dumps({"fingerprint": "X/Y", "ts": 9e9, "encoders": ["vecchio"]}),
                encoding="utf-8",
            )
            with patch.object(hw.subprocess, "run", return_value=type("R", (), {"stdout": " V..... libx264  x"})()):
                got = hw.ffmpeg_encoders(refresh=True)
            self.assertEqual(got, frozenset({"libx264"}))

    def test_cache_non_scrivibile_non_rompe(self) -> None:
        import tempfile

        with (
            tempfile.TemporaryDirectory() as d,
            self._con_cache(Path(d)),
            # Parent non scrivibile (lettura sola): deve essere silenzioso.
            patch.object(Path, "write_text", side_effect=OSError("read-only")),
        ):
            hw._write_encoder_cache(frozenset({"libx264"}))  # non deve sollevare


class TestProfileSummary(unittest.TestCase):
    def test_riga_leggibile(self) -> None:
        riga = hw.profile_summary("h264_qsv")
        self.assertIn("RAM", riga)
        self.assertIn("h264_qsv", riga)

    def test_libx264_dicce_che_su_cpu(self) -> None:
        self.assertIn("CPU", hw.profile_summary("libx264"))
        self.assertNotIn("h264_qsv", hw.profile_summary("libx264"))

    def test_niente_encoder_usa_libx264(self) -> None:
        self.assertIn("libx264", hw.profile_summary(None))


class TestNessunaDipendenzaEsterna(unittest.TestCase):
    """hardware.py deve restare importabile senza il progetto gia' caricato:
    e' il modulo che config.py importa in fase di import."""

    def test_non_importa_il_progetto(self) -> None:
        sorgente = Path(hw.__file__).read_text(encoding="utf-8")
        for modulo in ("config", "main", "semantic_sync", "video", "machine_setup"):
            with self.subTest(modulo=modulo):
                # Solo hardware.py può citarli nei docstring: controlliamo
                # le righe di import vere e proprie.
                for riga in sorgente.splitlines():
                    if riga.startswith(("import ", "from ")) and modulo in riga:
                        self.fail(f"hardware.py importa {modulo}: crea un ciclo")

    def test_usa_solo_solibra_standard(self) -> None:
        # psutil NON deve comparire: e' la trappola che aveva fatto fallire
        # in silenzio il probe dei core fisici.
        sorgente = Path(hw.__file__).read_text(encoding="utf-8")
        for riga in sorgente.splitlines():
            if riga.startswith(("import ", "from ")) and "psutil" in riga:
                self.fail("hardware.py non deve dipendere da psutil")


if __name__ == "__main__":
    unittest.main()
