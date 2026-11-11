#!/usr/bin/env python3
"""Il video: audio copiato, riuso del render, fotogrammi solo dei problemi.

Tre risparmi che non cambiano il risultato:

- l'audio viene COPIATO nel video quando la sorgente e' gia' un MP4/AAC
  (niente ricodifica lossy, e 10-25s in meno);
- il video gia' renderizzato viene RIUSATO se slide, tempi, audio e parametri
  sono gli stessi (71s risparmiati a ogni rilancio senza modifiche);
- i fotogrammi della verifica si conservano SOLO per i segmenti sbagliati:
  sono gli unici da guardare.

Esegui con: python -m unittest test_video_riuso -v
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from PIL import Image

from video import (
    _audio_copy_args,
    build_video,
    frame_consistency_check,
)
from video_riuso import chiave_video, segna_video, video_riutilizzabile


class TestAudioCopiato(unittest.TestCase):
    """L'audio non viene ricodificato quando la sorgente e' gia' MP4/AAC."""

    def test_sorgente_mp4_viene_copiata(self) -> None:
        self.assertEqual(_audio_copy_args(Path("podcast.m4a")), ["-c:a", "copy"])
        self.assertEqual(_audio_copy_args(Path("podcast.MP4")), ["-c:a", "copy"])

    def test_altri_formati_restano_aac(self) -> None:
        self.assertEqual(
            _audio_copy_args(Path("podcast.wav")), ["-c:a", "aac", "-b:a", "160k"]
        )
        self.assertEqual(
            _audio_copy_args(Path("podcast.mp3")), ["-c:a", "aac", "-b:a", "160k"]
        )


class TestChiaveVideo(unittest.TestCase):
    """L'impronta del video cambia se cambia QUALSIASI cosa che lo determina."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.slide = self.base / "s1.png"
        Image.new("RGB", (320, 240), (10, 20, 30)).save(self.slide)
        self.audio = self.base / "a.m4a"
        self.audio.write_bytes(b"audio fittizio")

    def _chiave(self, **kwargs: object) -> str:
        base: dict[str, object] = {
            "slide_files": [str(self.slide)],
            "durations": [10.0],
            "audio_path": self.audio,
            "fps": 5,
            "transition_duration": 0.0,
            "engine": "ffmpeg",
            "encoder": "libx264",
        }
        base.update(kwargs)
        return chiave_video(**base)  # type: ignore[arg-type]

    def test_la_chiave_e_stabile_a_input_uguali(self) -> None:
        self.assertEqual(self._chiave(), self._chiave())

    def test_cambia_se_cambia_una_slide(self) -> None:
        prima = self._chiave()
        Image.new("RGB", (320, 240), (200, 10, 10)).save(self.slide)
        self.assertNotEqual(prima, self._chiave())

    def test_cambia_se_cambia_un_tempo(self) -> None:
        self.assertNotEqual(self._chiave(), self._chiave(durations=[11.0]))

    def test_cambia_se_cambia_l_audio(self) -> None:
        self.assertNotEqual(self._chiave(), self._chiave(audio_path=self.base / "b.m4a"))

    def test_cambia_se_cambia_l_encoder(self) -> None:
        self.assertNotEqual(self._chiave(), self._chiave(encoder="h264_nvenc"))

    def test_cambia_se_cambia_il_motore(self) -> None:
        self.assertNotEqual(self._chiave(), self._chiave(engine="moviepy"))


class TestRiusoVideo(unittest.TestCase):
    """Il video gia' renderizzato si riusa solo se la sua chiave coincide."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.slide = self.base / "s1.png"
        Image.new("RGB", (320, 240), (10, 20, 30)).save(self.slide)
        self.audio = self.base / "a.m4a"
        self.audio.write_bytes(b"audio fittizio")
        self.output = self.base / "video.mp4"
        self.output.write_bytes(b"video fittizio")
        self.chiave = chiave_video(
            [str(self.slide)], [10.0], self.audio, 5, 0.0, "ffmpeg", "libx264"
        )

    def test_dopo_la_scrittura_il_video_si_riusa(self) -> None:
        segna_video(self.output, self.chiave, cache_dir=self.base)
        self.assertTrue(video_riutilizzabile(self.output, self.chiave, cache_dir=self.base))

    def test_una_chiave_diversa_non_si_riusa(self) -> None:
        segna_video(self.output, self.chiave, cache_dir=self.base)
        self.assertFalse(video_riutilizzabile(self.output, "altra", cache_dir=self.base))

    def test_se_il_file_e_stato_tocccato_non_si_riusa(self) -> None:
        segna_video(self.output, self.chiave, cache_dir=self.base)
        # Il file di output e' stato spostato o modificato: non e' piu' il video
        # che abbiamo scritto, quindi si rifa.
        self.output.write_bytes(b"video modificato")
        self.assertFalse(video_riutilizzabile(self.output, self.chiave, cache_dir=self.base))

    def test_build_video_non_rifà_se_si_puo_riusare(self) -> None:
        with (
            mock.patch("video_riuso.CACHE_DIR", self.base),
            mock.patch("video._build_video_ffmpeg") as ffmpeg,
        ):
            segna_video(self.output, self.chiave, cache_dir=self.base)
            build_video(
                [str(self.slide)],
                [10.0],
                str(self.audio),
                self.output,
                fps=5,
                threads=4,
                engine="ffmpeg",
                encoder="libx264",
            )
            ffmpeg.assert_not_called()


class TestFotogrammiSoloDeiProblemi(unittest.TestCase):
    """Nella cartella dell'utente finisce solo il fotogramma da guardare."""

    @staticmethod
    def _immagine(width: int, height: int, verticale: bool) -> Image.Image:
        """Immagine NON uniforme: su un colore piatto il vettore dei grigi ha
        norma zero e la similarita' vale 0 per definizione."""
        img = Image.new("L", (width, height), 0)
        if verticale:
            img.paste(255, (0, 0, width // 2, height))
        else:
            img.paste(255, (0, 0, width, height // 2))
        return img

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg non disponibile")
    def test_i_fotogrammi_si_conservano_solo_per_i_segmenti_sbagliati(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # Il video mostra una slide a meta' bianca (verticale).
            mostrata = tmp / "mostrata.png"
            self._immagine(320, 240, True).save(mostrata)
            altra = tmp / "altra.png"
            self._immagine(320, 240, False).save(altra)
            video = tmp / "v.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-loop", "1", "-i", str(mostrata),
                    "-t", "4", "-r", "5", str(video),
                ],
                check=True,
                timeout=60,
            )
            frames = tmp / "frames"

            coerente = frame_consistency_check(video, [(1, 0.0, 2.0)], [str(mostrata)], frames)
            self.assertEqual(coerente["coherent"], 1, "la slide del video deve essere riconosciuta")
            self.assertEqual(
                list(frames.glob("*.png")), [], "un segmento corretto non lascia fotogrammi"
            )

            sbagliato = frame_consistency_check(video, [(1, 0.0, 2.0)], [str(altra)], frames)
            self.assertEqual(len(cast("list[dict[str, object]]", sbagliato["mismatches"])), 1)
            self.assertEqual(
                len(list(frames.glob("*.png"))),
                1,
                "il fotogramma del segmento sbagliato resta: e' la prova da guardare",
            )


if __name__ == "__main__":
    unittest.main()
