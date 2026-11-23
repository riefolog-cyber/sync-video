#!/usr/bin/env python3
"""Il riepilogo per lezione (`riepilogo.md`).

Il referto JSON resta tecnico; qui si verifica che la pagina leggibile dica le
cose giuste: quante pagine, quante ancore, la sezione piu' corta, da dove
vengono slide e voce, e cosa guardare a mano. Non deve MAI interrompere la run,
nemmeno con un report malformato.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline_report import print_timing
from riepilogo import riepilogo_markdown, rigenera, scrivi_riepilogo


def _report(**extra: object) -> dict[str, object]:
    report: dict[str, object] = {
        "versione": "1.2.3",
        "segments": [
            {"slide": 1, "start": 0.0, "end": 120.0, "duration": 120.0},
            {"slide": 2, "start": 120.0, "end": 150.0, "duration": 30.0},
        ],
        "anchors": {
            "transitions": 1,
            "anchored": 1,
            "unanchored_slides": [],
            "mentions_off": [],
        },
        "notes": {"tempi": {"ocr": 8, "trascrizione": 207, "sincronizzazione": 3}},
    }
    report.update(extra)
    return report


class TestRiepilogo(unittest.TestCase):
    def test_dice_pagine_ancore_e_pagina_piu_corta(self) -> None:
        testo = riepilogo_markdown(_report(), "video_finale.mp4")
        self.assertIn("| Pagine | 2 |", testo)
        self.assertIn("| Ancore usate | 1 su 1 |", testo)
        self.assertIn("| Pagina più breve | 2 (30s) |", testo)
        self.assertIn("video_finale.mp4", testo)

    def test_senza_dubbi_lo_dice(self) -> None:
        testo = riepilogo_markdown(_report())
        self.assertIn("Niente: tutti i confini sono quelli annunciati a voce.", testo)

    def test_i_dubbi_sono_elencati_con_i_due_tempi(self) -> None:
        report = _report(
            anchors={
                "transitions": 1,
                "anchored": 1,
                "unanchored_slides": [],
                "mentions_off": [{"slide": 2, "mention": 60.0, "declared": 75.0}],
            }
        )
        testo = riepilogo_markdown(report)
        self.assertIn("slide 2: annunciata a 1m00s ma mostrata da 1m15s", testo)
        self.assertIn("in ritardo", testo)

    def test_una_sola_pagina_non_parla_di_confini(self) -> None:
        # Con una pagina sola non ci sono confini: dire "stimati dal contenuto"
        # sarebbe una diagnosi inventata.
        report = _report(
            segments=[{"slide": 1, "start": 0.0, "end": 60.0, "duration": 60.0}],
            anchors={"transitions": 0, "anchored": 0, "unanchored_slides": []},
        )
        testo = riepilogo_markdown(report)
        self.assertIn("Una sola pagina", testo)
        self.assertNotIn("stimati dal contenuto", testo)

    def test_dice_se_la_trascrizione_e_stata_ripresa(self) -> None:
        testo = riepilogo_markdown(_report(notes={"ripresa_da": 720.0}))
        self.assertIn("| Trascrizione ripresa da | 12m00s |", testo)

    def test_le_note_stanno_in_cima_nel_report_vero(self) -> None:
        # `_build_sync_report` fa `report.update(notes)`: le note sono in cima,
        # non sotto "notes". Se il riepilogo le cerca solo annidate, tempi e
        # ripresa non compaiono mai in un report vero.
        report = _report(ripresa_da=600.0, tempi={"ocr": 8, "trascrizione": 207})
        testo = riepilogo_markdown(report)
        self.assertIn("| Trascrizione ripresa da | 10m00s |", testo)
        self.assertIn("trascrizione 207s", testo)

    def test_dry_run_dice_che_il_video_non_c_e(self) -> None:
        testo = riepilogo_markdown(_report(), "video_finale.mp4", dry_run=True)
        self.assertIn("non creato** (dry-run)", testo)

    def test_dice_quando_il_video_non_c_e_piu(self) -> None:
        # Un riepilogo che nomina un video assente fa credere che esista: e'
        # successo davvero (file cancellato dopo la run).
        testo = riepilogo_markdown(_report(), "video_finale.mp4", video_presente=False)
        self.assertIn("assente dalla cartella", testo)
        self.assertNotIn("assente dalla cartella", riepilogo_markdown(_report(), "video_finale.mp4"))

    def test_rigenerando_dice_se_il_video_manca(self) -> None:
        # La stessa informazione passando dal disco: la rigenerazione guarda il
        # file, non si fida del nome. (La pipeline no: scrive il riepilogo
        # PRIMA di renderizzare, quindi li' il video non c'e' ancora.)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cache = base / "cache"
            cache.mkdir()
            (cache / "sync_report.json").write_text(json.dumps(_report()), encoding="utf-8")
            video = base / "video_finale.mp4"
            self.assertEqual(rigenera(cache, video), 0)
            self.assertIn("assente dalla cartella", (base / "riepilogo.md").read_text(encoding="utf-8"))

    def test_dice_da_dove_vengono_slide_e_voce(self) -> None:
        testo = riepilogo_markdown(
            _report(sorgenti={"pdf": "lezione 3.pdf", "audio": "voce.m4a"})
        )
        self.assertIn("slide da `lezione 3.pdf`", testo)
        self.assertIn("voce da `voce.m4a`", testo)

    def test_un_report_malformato_non_esplode(self) -> None:
        # La promessa e' "mai fatale": dopo il video, un traceback qui
        # rovinerebbe una run gia' riuscita.
        rotto = {
            "segments": [{"slide": "x", "start": "a", "end": "b", "duration": "c"}],
            "anchors": ["non", "un", "dict"],
            "notes": "nemmeno",
        }
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video_finale.mp4"
            video.write_bytes(b"x")
            self.assertIsNone(scrivi_riepilogo(rotto, video))

    def test_scrive_il_file_accanto_al_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "video_finale.mp4"
            video.write_bytes(b"x")
            path = scrivi_riepilogo(_report(), video)
            self.assertIsNotNone(path)
            assert path is not None  # per il type-checker
            self.assertEqual(path.name, "riepilogo.md")
            self.assertTrue(path.exists())
            self.assertIn("Lezione del", path.read_text(encoding="utf-8"))


class TestRigenera(unittest.TestCase):
    """`--solo-riepilogo`: la pagina dal report, senza pipeline."""

    def test_riscrive_dal_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "sync_report.json").write_text(
                json.dumps(_report()), encoding="utf-8"
            )
            video = base / "video_finale.mp4"
            video.write_bytes(b"x")
            self.assertEqual(rigenera(base, video), 0)
            self.assertTrue((base / "riepilogo.md").exists())

    def test_senza_report_esce_con_uno(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(rigenera(base, base / "video_finale.mp4"), 1)


class TestSoloRiepilogoDaRigaDiComando(unittest.TestCase):
    """`--solo-riepilogo` passa dal vero main, non solo da `rigenera`. """

    def _esegui(self, base: Path) -> int:
        import main as m

        vecchia = m.CACHE_DIR
        m.CACHE_DIR = base
        try:
            with self.assertRaises(SystemExit) as ctx:
                m.main(["--solo-riepilogo", "--output", str(base / "video_finale.mp4")])
        finally:
            m.CACHE_DIR = vecchia
        codice = ctx.exception.code
        assert codice is not None
        return int(codice)

    def test_scrive_il_file_ed_esce_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "sync_report.json").write_text(json.dumps(_report()), encoding="utf-8")
            (base / "video_finale.mp4").write_bytes(b"x")
            self.assertEqual(self._esegui(base), 0)
            self.assertTrue((base / "riepilogo.md").exists())

    def test_senza_report_esce_uno(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._esegui(Path(tmp)), 1)


class TestEncoderNelRiepilogo(unittest.TestCase):
    """La riga Hardware deve dire la verità sull'encoder usato per il video."""

    def _hardware(self, encoder: str | None) -> str:
        with self.assertLogs("slide2video", level="INFO") as logs:
            print_timing(
                1.0, 2.0, 3.0, 0.0, 0.0, 5.0, 11.0,
                history_fn=lambda *_a: None,
                video_encoder=encoder,
            )
        # assertLogs restituisce righe GIÀ formattate (str), non LogRecord.
        return "\n".join(str(r) for r in logs.output)

    def test_encoder_gpu_dichiarato(self) -> None:
        self.assertIn("video h264_nvenc (GPU)", self._hardware("h264_nvenc"))

    def test_encoder_cpu_dichiarato(self) -> None:
        self.assertIn("video libx264 (CPU)", self._hardware("libx264"))

    def test_auto_non_e_gpu(self) -> None:
        # Con --no-auto-setup restava "auto": il vecchio confronto
        # `!= "libx264"` stampava "video auto (GPU)" senza alcuna prova.
        out = self._hardware("auto")
        self.assertIn("video auto (non verificato)", out)
        self.assertNotIn("(GPU)", out)

    def test_encoder_sconosciuto_non_e_gpu(self) -> None:
        out = self._hardware("boh")
        self.assertIn("(non verificato)", out)
        self.assertNotIn("(GPU)", out)


class TestAutoEncoderSenzaAutoSetup(unittest.TestCase):
    """`--no-auto-setup` non deve lasciare `encoder="auto"` a video.py."""

    def test_auto_viene_risolto_dall_hardware(self) -> None:
        import main

        args = SimpleNamespace(video_encoder="auto")
        with (
            patch("main.detect_gpus", return_value=["NVIDIA GeForce RTX 3060"]),
            patch("main.ffmpeg_encoders", return_value=frozenset({"h264_nvenc"})),
            patch.dict("os.environ", {"VIDEO_ENCODER": ""}),
            self.assertLogs("slide2video", level="INFO"),
        ):
            risolto = main._risolvi_encoder_video(args)
        self.assertEqual(risolto, "h264_nvenc")
        self.assertEqual(args.video_encoder, "h264_nvenc")

    def test_senza_gpu_resta_libx264(self) -> None:
        import main

        args = SimpleNamespace(video_encoder="auto")
        with (
            patch("main.detect_gpus", return_value=[]),
            patch("main.ffmpeg_encoders", return_value=frozenset({"h264_nvenc"})),
            patch.dict("os.environ", {"VIDEO_ENCODER": ""}),
            self.assertLogs("slide2video", level="INFO"),
        ):
            risolto = main._risolvi_encoder_video(args)
        self.assertEqual(risolto, "libx264")

    def test_scelta_esplicita_non_viene_toccata(self) -> None:
        import main

        args = SimpleNamespace(video_encoder="h264_qsv")
        self.assertEqual(main._risolvi_encoder_video(args), "h264_qsv")
        self.assertEqual(args.video_encoder, "h264_qsv")


if __name__ == "__main__":
    unittest.main()
