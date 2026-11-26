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

import os
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
    _tentativi_encoding,
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
        altra = self.base / "b.m4a"
        altra.write_bytes(b"audio diverso, stessa dimensione!")
        self.assertNotEqual(self._chiave(), self._chiave(audio_path=altra))

    def test_stessi_byte_in_altro_percorso_non_cambiano_la_chiave(self) -> None:
        copia = self.base / "copia.m4a"
        copia.write_bytes((self.base / "a.m4a").read_bytes())
        self.assertEqual(self._chiave(), self._chiave(audio_path=copia))

    def test_audio_stesso_con_estensione_diversa_cambia_la_chiave(self) -> None:
        wav = self.base / "a.wav"
        wav.write_bytes((self.base / "a.m4a").read_bytes())
        self.assertNotEqual(self._chiave(), self._chiave(audio_path=wav))

    def test_cambia_se_cambia_l_encoder(self) -> None:
        self.assertNotEqual(self._chiave(), self._chiave(encoder="h264_nvenc"))

    def test_cambia_se_cambia_il_motore(self) -> None:
        self.assertNotEqual(self._chiave(), self._chiave(engine="moviepy"))

    def test_cambia_se_cambiano_i_thread(self) -> None:
        # I thread finiscono nei flag di libx264: un video con un altro numero
        # di thread non e' garantito identico.
        self.assertNotEqual(self._chiave(), self._chiave(threads=4))

    def test_stessi_input_percorso_diverso(self) -> None:
        # Spostare/rinominare la cartella delle slide non cambia il video:
        # l'impronta usa l'indice e il contenuto, non il percorso.
        altra = self.base / "sotto" / "s1.png"
        altra.parent.mkdir()
        altra.write_bytes(self.slide.read_bytes())
        self.assertEqual(self._chiave(), self._chiave(slide_files=[str(altra)]))

    def test_cambia_se_cambia_la_risoluzione_o_il_buffer(self) -> None:
        # Risoluzione e buffer non devono mancare dall'impronta: cambiandoli
        # cambia il video (il buffer allunga l'ultima durata).
        originale = self._chiave()
        self.assertNotEqual(originale, self._chiave(video_res=(1280, 720)))
        self.assertNotEqual(originale, self._chiave(video_buffer_sec=5.0))

    def test_i_default_seguono_la_configurazione(self) -> None:
        # I parametri non passati esplicitamente vanno letti dalla configurazione
        # del modulo, cosi' chi cambia VIDEO_RES/VIDEO_BUFFER_SEC cambia chiave.
        originale = self._chiave()
        with (
            mock.patch("video_riuso.DEFAULT_VIDEO_RES", (800, 600)),
            mock.patch("video_riuso.DEFAULT_VIDEO_BUFFER_SEC", 9.0),
        ):
            self.assertNotEqual(originale, self._chiave())


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

    def test_riuso_riporta_l_encoder_effettivo_non_quello_richiesto(self) -> None:
        # La prima run aveva chiesto la GPU ma era ripiegata su CPU: la
        # seconda, che riusa il file, deve dire "libx264", non "h264_nvenc".
        segna_video(self.output, self.chiave, cache_dir=self.base, encoder="libx264")
        out_enc: list[str] = ["h264_nvenc"]
        self.assertTrue(
            video_riutilizzabile(
                self.output, self.chiave, cache_dir=self.base, encoder_out=out_enc
            )
        )
        self.assertEqual(out_enc, ["libx264"])

    def test_una_chiave_diversa_non_si_riusa(self) -> None:
        segna_video(self.output, self.chiave, cache_dir=self.base)
        self.assertFalse(video_riutilizzabile(self.output, "altra", cache_dir=self.base))

    def test_se_il_file_e_stato_tocccato_non_si_riusa(self) -> None:
        segna_video(self.output, self.chiave, cache_dir=self.base)
        # Il file di output e' stato spostato o modificato: non e' piu' il video
        # che abbiamo scritto, quindi si rifa.
        self.output.write_bytes(b"video modificato")
        self.assertFalse(video_riutilizzabile(self.output, self.chiave, cache_dir=self.base))

    def test_touch_senza_modifiche_non_invalida_il_riuso(self) -> None:
        segna_video(self.output, self.chiave, cache_dir=self.base)
        st = self.output.stat()
        # Antivirus/OneDrive/touch: cambia SOLO il momento di modifica.
        os.utime(self.output, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.assertNotEqual(self.output.stat().st_mtime_ns, st.st_mtime_ns)
        self.assertTrue(video_riutilizzabile(self.output, self.chiave, cache_dir=self.base))

    def test_modifica_stessa_dimensione_non_si_riusa(self) -> None:
        segna_video(self.output, self.chiave, cache_dir=self.base)
        # Stessa lunghezza ma contenuto diverso: decide l'hash, non il mtime.
        nuovo = b"video alterato"
        self.assertEqual(len(nuovo), len(b"video fittizio"))
        self.output.write_bytes(nuovo)
        # Su filesystem con risoluzione grossolana due scritture ravvicinate
        # possono avere lo STESSO mtime: in quel caso il percorso rapido non
        # confronta il contenuto e il test diventa flaky. Forzare un mtime
        # diverso garantisce che si eserciti proprio il confronto dell'hash.
        st = self.output.stat()
        os.utime(self.output, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        self.assertFalse(video_riutilizzabile(self.output, self.chiave, cache_dir=self.base))

    def test_build_video_non_rifà_se_si_puo_riusare(self) -> None:
        with (
            mock.patch("video_riuso.CACHE_DIR", self.base),
            mock.patch("video._build_video_ffmpeg") as ffmpeg,
        ):
            # La chiave deve descrivere la stessa run: build_video usa
            # threads=4, quindi anche l'impronta del video gia' scritto.
            chiave_con_threads = chiave_video(
                [str(self.slide)], [10.0], self.audio, 5, 0.0, "ffmpeg", "libx264", 4
            )
            segna_video(self.output, chiave_con_threads, cache_dir=self.base)
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


class TestRiusoNonToccaIlVideo(unittest.TestCase):
    """Una run identica non riscrive il video, quindi non crea un backup inutile."""

    def test_video_riusato_non_crea_backup(self) -> None:
        from unittest import mock

        import video

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            slide = base / "s1.png"
            slide.write_bytes(b"png finto")
            audio = base / "a.m4a"
            audio.write_bytes(b"audio finto")
            out = base / "video_finale.mp4"
            out.write_bytes(b"vecchio video")

            with (
                mock.patch.object(video, "video_riutilizzabile", return_value=True),
                mock.patch.object(video, "_salva_video_precedente") as salva,
                mock.patch.object(video, "_build_video_ffmpeg") as build,
            ):
                video.build_video([str(slide)], [10.0], audio, out)

            salva.assert_not_called()
            build.assert_not_called()
            self.assertEqual(out.read_bytes(), b"vecchio video")


class TestVideoPrecedente(unittest.TestCase):
    """Il video che viene sovrascritto non va perso."""

    def test_il_video_esistente_viene_conservato(self) -> None:
        from video import _salva_video_precedente

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "video_finale.mp4"
            out.write_bytes(b"vecchio video")
            _salva_video_precedente(out)
            copia = Path(tmp) / "video_finale (precedente).mp4"
            self.assertTrue(copia.exists())
            self.assertEqual(copia.read_bytes(), b"vecchio video")

    def test_senza_video_non_crea_nulla(self) -> None:
        from video import _salva_video_precedente

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "video_finale.mp4"
            _salva_video_precedente(out)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class TestTentativiEncoding(unittest.TestCase):
    """Le reti di sicurezza dell'encoding, nell'ordine in cui si provano."""

    def test_audio_copiato_ritenta_anche_in_aac(self) -> None:
        # Si cambia PRIMA l'audio: riprovare la copia con libx264 dopo un
        # errore chiaramente audio sprecava un encoding intero.
        self.assertEqual(
            _tentativi_encoding("h264_nvenc", True, True),
            [
                ("h264_nvenc", True),
                ("h264_nvenc", False),
                ("libx264", True),
                ("libx264", False),
            ],
        )

    def test_encoder_software_con_audio_aac_un_solo_tentativo(self) -> None:
        self.assertEqual(
            _tentativi_encoding("libx264", False, False), [("libx264", False)]
        )

    def test_encoder_accelerato_con_audio_aac_non_ritenta_la_copia(self) -> None:
        self.assertEqual(
            _tentativi_encoding("h264_qsv", True, False),
            [("h264_qsv", False), ("libx264", False)],
        )


if __name__ == "__main__":
    unittest.main()
