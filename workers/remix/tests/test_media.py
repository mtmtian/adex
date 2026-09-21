from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from workers.remix import config, media
except ModuleNotFoundError:  # running unittest from workers/remix directly
    import config  # type: ignore[no-redef]
    import media  # type: ignore[no-redef]


class MediaFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"input")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_probe_nonzero_is_error(self) -> None:
        failed = media.subprocess.CompletedProcess(["ffprobe"], 1, "", "broken")
        with mock.patch.object(media, "_run", return_value=failed):
            with self.assertRaises(RuntimeError):
                media.probe(self.source)

    def test_normalize_rejects_short_clip_before_ffmpeg(self) -> None:
        with mock.patch.object(media, "probe", return_value={
            "duration": 1.0,
            "width": 320,
            "height": 240,
            "has_audio": False,
        }), mock.patch.object(media, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "too short"):
                media.normalize(self.source, self.root / "out.mp4", "9:16", 2.0)
            run.assert_not_called()

    def test_qc_zero_ocr_frames_is_incomplete_fail(self) -> None:
        with mock.patch.object(media, "probe", return_value={
            "duration": 1.0,
            "width": 320,
            "height": 240,
            "has_audio": False,
        }), mock.patch.object(media, "_run", return_value=media.subprocess.CompletedProcess([], 0, "", "")):
            result = media.qc_scan(self.source, self.root / "scratch")
        self.assertFalse(result["pass"])
        self.assertFalse(result["completed"])
        self.assertTrue(
            any(check["name"] == "ocr_brand_scan" and check["status"] == "FAIL" for check in result["checks"])
        )

    def test_qc_whisper_nonzero_is_incomplete_fail(self) -> None:
        old_model = config.WHISPER_MODEL
        config.WHISPER_MODEL = str(self.root / "model.bin")
        Path(config.WHISPER_MODEL).write_bytes(b"model")
        responses = [
            media.subprocess.CompletedProcess(
                [],
                0,
                (
                    '{"streams":[{"codec_type":"video","width":320,"height":240},'
                    '{"codec_type":"audio"}],"format":{"duration":"1"}}'
                ),
                "",
            ),
            media.subprocess.CompletedProcess([], 0, "", ""),
            media.subprocess.CompletedProcess([], 1, "", "whisper failed"),
        ]
        try:
            with mock.patch.object(media, "_run", side_effect=responses), mock.patch.object(
                media, "_ocr_frames", return_value=([], [])
            ):
                result = media.qc_scan(self.source, self.root / "scratch")
        finally:
            config.WHISPER_MODEL = old_model
        self.assertFalse(result["pass"])
        self.assertFalse(result["completed"])
        self.assertTrue(
            any(check["name"] == "audio_brand_scan" and check["status"] == "FAIL" for check in result["checks"])
        )

    def test_whisper_missing_json_is_error(self) -> None:
        wav = self.root / "audio.wav"
        wav.write_bytes(b"wav")
        old_model = config.WHISPER_MODEL
        config.WHISPER_MODEL = str(self.root / "model.bin")
        Path(config.WHISPER_MODEL).write_bytes(b"model")
        try:
            with mock.patch.object(
                media,
                "_run",
                return_value=media.subprocess.CompletedProcess([], 0, "", ""),
            ):
                with self.assertRaisesRegex(RuntimeError, "did not produce JSON"):
                    media.whisper_transcribe(wav)
        finally:
            config.WHISPER_MODEL = old_model

    def test_whisper_bad_json_is_error(self) -> None:
        wav = self.root / "audio.wav"
        wav.write_bytes(b"wav")
        old_model = config.WHISPER_MODEL
        config.WHISPER_MODEL = str(self.root / "model.bin")
        Path(config.WHISPER_MODEL).write_bytes(b"model")
        prefix = self.root / "audio.transcript.json"
        try:
            def fake_run(*args, **kwargs):
                prefix.write_text("{broken", encoding="utf-8")
                return media.subprocess.CompletedProcess([], 0, "", "")

            with mock.patch.object(
                media,
                "_run",
                side_effect=fake_run,
            ):
                with self.assertRaisesRegex(RuntimeError, "JSON is invalid"):
                    media.whisper_transcribe(wav)
        finally:
            config.WHISPER_MODEL = old_model

    def test_ocr_recognition_nonzero_is_error(self) -> None:
        frame = self.root / "scratch" / ".source.frame-00001.jpg"

        def fake_run(cmd, **kwargs):
            if cmd[0] == config.FFMPEG:
                frame.write_bytes(b"jpg")
                return media.subprocess.CompletedProcess(cmd, 0, "", "")
            return media.subprocess.CompletedProcess(cmd, 1, "", "tesseract failed")

        with mock.patch.object(media, "_run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "OCR recognition"):
                media._ocr_frames(self.source, self.root / "scratch", 1.0)

    def test_validate_runtime_reports_missing_tools(self) -> None:
        old = (config.FFMPEG, config.FFPROBE, config.WHISPER_CLI, config.TESSERACT, config.WHISPER_MODEL)
        config.FFMPEG = config.FFPROBE = config.WHISPER_CLI = config.TESSERACT = "definitely-missing"
        config.WHISPER_MODEL = ""
        try:
            with mock.patch.object(media.shutil, "which", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "missing media runtime"):
                    media.validate_runtime()
        finally:
            config.FFMPEG, config.FFPROBE, config.WHISPER_CLI, config.TESSERACT, config.WHISPER_MODEL = old


@unittest.skipUnless(
    os.environ.get("RUN_REAL_MEDIA_TESTS") == "1" and shutil.which("ffmpeg"),
    "set RUN_REAL_MEDIA_TESTS=1 to run local ffmpeg smoke",
)
class RealMediaSmokeTests(unittest.TestCase):
    def test_placeholder_is_a_valid_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = media.placeholder(Path(tmp) / "placeholder.mp4", "1:1", 0.5)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)


@unittest.skipUnless(os.environ.get("RUN_REAL_QC_TESTS") == "1", "set RUN_REAL_QC_TESTS=1 for real QC acceptance")
class RealQcAcceptanceTests(unittest.TestCase):
    """Opt-in checks use real tools/model; no network, provider or control plane."""

    @classmethod
    def setUpClass(cls) -> None:
        media.validate_runtime()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_video(self, *, brand: bool = False) -> Path:
        output = self.root / "ocr-fixture.mp4"
        filters = "color=c=white:s=640x360:r=10"
        if brand:
            filters += ",drawtext=text=Talkie:fontsize=64:fontcolor=black:x=(w-text_w)/2:y=(h-text_h)/2"
        result = media.subprocess.run([
            config.FFMPEG, "-y", "-f", "lavfi", "-i", filters,
            "-t", "2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return output

    def test_real_ocr_distinguishes_clean_and_brand_hit(self) -> None:
        clean = media.qc_scan(self.make_video(), self.root / "clean-qc")
        self.assertTrue(clean["completed"], clean["checks"])
        self.assertTrue(clean["pass"], clean)
        branded = media.qc_scan(self.make_video(brand=True), self.root / "brand-qc")
        self.assertTrue(branded["completed"], branded["checks"])
        self.assertFalse(branded["pass"])
        self.assertTrue(any(hit["brand"] == "Talkie" and hit["source"] == "ocr" for hit in branded["hits"]), branded)

    def test_real_whisper_and_ocr_complete_on_local_audio_fixture(self) -> None:
        video = media.placeholder(self.root / "audio-fixture.mp4", "1:1", 1)
        result = media.qc_scan(video, self.root / "qc")
        self.assertTrue(result["completed"], result["checks"])
        checks = {check["name"]: check["status"] for check in result["checks"]}
        self.assertIn(checks["audio_brand_scan"], ("PASS", "FAIL"))
        self.assertEqual(checks["ocr_brand_scan"], "PASS")

    def test_real_corrupt_whisper_model_is_incomplete_not_clean(self) -> None:
        video = media.placeholder(self.root / "audio-fixture.mp4", "1:1", 1)
        broken_model = self.root / "broken.bin"
        broken_model.write_bytes(b"invalid whisper model")
        with mock.patch.object(config, "WHISPER_MODEL", str(broken_model)):
            result = media.qc_scan(video, self.root / "qc")
        self.assertFalse(result["completed"])
        self.assertFalse(result["pass"])
        checks = {check["name"]: check["status"] for check in result["checks"]}
        self.assertEqual(checks["audio_brand_scan"], "FAIL")
        self.assertEqual(checks["ocr_brand_scan"], "PASS")

    def test_real_missing_ocr_executable_is_incomplete_not_clean(self) -> None:
        video = self.make_video()
        with mock.patch.object(config, "TESSERACT", str(self.root / "missing-tesseract")):
            result = media.qc_scan(video, self.root / "qc")
        self.assertFalse(result["completed"])
        self.assertFalse(result["pass"])
        checks = {check["name"]: check["status"] for check in result["checks"]}
        self.assertEqual(checks["ocr_brand_scan"], "FAIL")


if __name__ == "__main__":
    unittest.main()
