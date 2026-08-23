#!/usr/bin/env python3
"""
Test unitari per il modulo video (motore ffmpeg concat demuxer + dispatch).

Esegui con: python -m unittest test_video -v
"""

import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

from video import (
    _build_video_ffmpeg,
    _concat_quote,
    _prepare_slides_for_concat,
    _run_ffmpeg,
    _write_concat_file,
    build_video,
)


class _FakeStdout:
    """Iterabile simile a proc.stdout per mockare Popen."""

    def __init__(self, lines: list[str]):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        pass


def _fake_popen(returncode: int, lines: list[str]) -> MagicMock:
    proc = MagicMock()
    proc.stdout = _FakeStdout(lines)
    proc.wait.return_value = returncode
    return proc


class TestConcatQuote(unittest.TestCase):
    def test_windows_path_converted_to_slashes(self):
        quoted = _concat_quote(Path(r"C:\tmp\slide 1.png"))
        self.assertTrue(quoted.startswith("'"))
        self.assertTrue(quoted.endswith("'"))
        self.assertNotIn("\\", quoted)

    def test_apostrophe_escaped(self):
        quoted = _concat_quote(Path("/tmp/it's.png"))
        self.assertIn("'\\''", quoted)


class TestWriteConcatFile(unittest.TestCase):
    def test_entries_and_last_file_repeated(self):
        entries = [(Path("/tmp/a.png"), 1.5), (Path("/tmp/b.png"), 2.0)]
        with tempfile.TemporaryDirectory() as td:
            list_path = Path(td) / "concat.txt"
            _write_concat_file(entries, list_path)
            lines = list_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "ffconcat version 1.0")
        # 2 entry x 2 righe + ripetizione finale
        self.assertEqual(len(lines), 6)
        self.assertIn("duration 1.500000", lines)
        self.assertIn("duration 2.000000", lines)
        self.assertEqual(lines[-1], lines[-3])  # ultimo file ripetuto senza duration
        self.assertNotIn("duration", lines[-1])

    def test_non_positive_duration_rejected(self):
        entries = [(Path("/tmp/a.png"), 0.0)]
        with tempfile.TemporaryDirectory() as td, self.assertRaises(ValueError):
            _write_concat_file(entries, Path(td) / "concat.txt")


class TestPrepareSlides(unittest.TestCase):
    def test_uniform_canvas_for_mixed_sizes(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            small = td_path / "small.png"
            big = td_path / "big.png"
            Image.new("RGB", (320, 200), (255, 0, 0)).save(small)
            Image.new("RGB", (640, 480), (0, 255, 0)).save(big)

            out_dir = td_path / "out"
            out_dir.mkdir()
            prepared = _prepare_slides_for_concat([str(small), str(big)], out_dir)

            self.assertEqual(len(prepared), 2)
            sizes = set()
            for p in prepared:
                with Image.open(p) as img:
                    sizes.add(img.size)
            # Tutti i segmenti devono avere la STESSA risoluzione (canvas unico)
            self.assertEqual(len(sizes), 1)
            # Il canvas è la slide adattata più grande (640x480, già pari)
            self.assertEqual(sizes.pop(), (640, 480))


class TestRunFfmpeg(unittest.TestCase):
    def test_success_with_progress(self):
        lines = [
            "out_time=00:00:05.000000",
            "out_time=00:00:10.000000",
            "progress=end",
        ]
        with patch("video.subprocess.Popen", return_value=_fake_popen(0, lines)):
            _run_ffmpeg(["ffmpeg"], 10.0)  # non deve sollevare

    def test_failure_raises_runtime_error_with_output(self):
        lines = ["out_time=00:00:01.000000", "Conversion failed!"]
        with (
            patch("video.subprocess.Popen", return_value=_fake_popen(1, lines)),
            self.assertRaises(RuntimeError) as ctx,
        ):
            _run_ffmpeg(["ffmpeg"], 10.0)
        self.assertIn("exit code 1", str(ctx.exception))
        self.assertIn("Conversion failed!", str(ctx.exception))

    def test_malformed_time_line_ignored(self):
        lines = ["out_time=not-a-time", "progress=end"]
        with patch("video.subprocess.Popen", return_value=_fake_popen(0, lines)):
            _run_ffmpeg(["ffmpeg"], 10.0)  # non deve sollevare


class TestBuildVideoFfmpeg(unittest.TestCase):
    def test_missing_audio_raises(self):
        with self.assertRaises(FileNotFoundError):
            _build_video_ffmpeg(["s.png"], [1.0], Path("inesistente.m4a"), Path("out.mp4"), 5, 4)

    def test_invokes_ffmpeg_with_concat_and_audio(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            slide = td_path / "slide.png"
            Image.new("RGB", (64, 32), (10, 20, 30)).save(slide)
            audio = td_path / "audio.m4a"
            audio.write_bytes(b"\x00" * 16)
            out = td_path / "video_finale.mp4"

            captured: dict = {}

            def fake_run(cmd, total):
                captured["cmd"] = cmd
                captured["total"] = total
                # Il file concat esiste SOLO durante la TemporaryDirectory:
                # leggerne il contenuto qui dentro (dopo viene cancellata).
                concat_arg = cmd[cmd.index("-i") + 1]
                captured["concat_text"] = Path(concat_arg).read_text(encoding="utf-8")

            with patch("video._run_ffmpeg", side_effect=fake_run):
                _build_video_ffmpeg([str(slide)], [3.5], audio, out, 5, 4)

            cmd = captured["cmd"]
            self.assertEqual(cmd[0], "ffmpeg")
            self.assertIn("concat", cmd)
            self.assertIn(str(audio), cmd)
            self.assertEqual(cmd[-1], str(out))
            self.assertEqual(captured["total"], 3.5)
            # Il file ffconcat contiene l'ultima slide ripetuta senza duration
            lines = captured["concat_text"].splitlines()
            self.assertEqual(lines[0], "ffconcat version 1.0")
            self.assertIn("duration 3.500000", lines)
            self.assertNotIn("duration", lines[-1])


class TestBuildVideoDispatch(unittest.TestCase):
    SLIDES: ClassVar[list[str]] = ["a.png", "b.png"]
    DURATIONS: ClassVar[list[float]] = [10.0, 10.0]

    def test_validation_empty(self):
        with self.assertRaises(ValueError):
            build_video([], [], "audio.m4a", Path("out.mp4"))

    def test_validation_length_mismatch(self):
        with self.assertRaises(ValueError):
            build_video(self.SLIDES, [10.0], "audio.m4a", Path("out.mp4"))

    def test_transitions_force_moviepy(self):
        with patch("video._build_video_moviepy") as mov, patch("video._build_video_ffmpeg") as ffm:
            build_video(
                self.SLIDES,
                self.DURATIONS,
                "audio.m4a",
                Path("out.mp4"),
                transition_duration=0.5,
                engine="ffmpeg",
            )
        mov.assert_called_once()
        ffm.assert_not_called()
        # Ultima durata = 10.0 + buffer 3.0 + compensazione transizioni 0.5 * (2-1) = 13.5
        args = mov.call_args.args
        self.assertAlmostEqual(args[1][-1], 10.0 + 3.0 + 0.5, places=6)

    def test_engine_moviepy_explicit(self):
        with patch("video._build_video_moviepy") as mov, patch("video._build_video_ffmpeg") as ffm:
            build_video(self.SLIDES, self.DURATIONS, "audio.m4a", Path("out.mp4"), engine="moviepy")
        mov.assert_called_once()
        ffm.assert_not_called()

    def test_engine_ffmpeg_with_path(self):
        with patch("video._build_video_moviepy") as mov, patch("video._build_video_ffmpeg") as ffm:
            build_video(self.SLIDES, self.DURATIONS, "audio.m4a", Path("out.mp4"), engine="ffmpeg")
        ffm.assert_called_once()
        mov.assert_not_called()
        # Buffer default (DEFAULT_VIDEO_BUFFER_SEC) aggiunto all'ultima durata
        args = ffm.call_args.args
        self.assertEqual(args[0], self.SLIDES)
        self.assertAlmostEqual(args[1][-1], 10.0 + 3.0, places=6)

    def test_audio_clip_without_filename_falls_back_to_moviepy(self):
        clip = MagicMock(spec=[])  # nessun attributo filename
        with patch("video._build_video_moviepy") as mov, patch("video._build_video_ffmpeg") as ffm:
            build_video(self.SLIDES, self.DURATIONS, clip, Path("out.mp4"), engine="ffmpeg")
        mov.assert_called_once()
        ffm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
