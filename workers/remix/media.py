"""Fail-closed media primitives used by the remix worker.

The worker deliberately keeps all ffmpeg/ffprobe/whisper/tesseract calls in
this module.  Every subprocess has a timeout; a non-zero exit, malformed
output, or missing artifact becomes an explicit failure instead of an empty
result that could be mistaken for a clean creative.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:  # package import (tests/embedding)
    from . import config, scanner
except ImportError:  # direct ``python workers/remix/worker.py`` execution
    import config  # type: ignore[no-redef]
    import scanner  # type: ignore[no-redef]


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
MAX_TEXT_BYTES = 64 * 1024


def _timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("subprocess timeout must be positive")
    return value


def _run(
    cmd: list[str],
    *,
    timeout: float | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command with bounded execution and captured text output."""

    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_timeout(timeout or config.SUBPROCESS_TIMEOUT_SEC),
            check=False,
            cwd=str(cwd) if cwd is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {exc.timeout}s: {cmd[0]}") from exc
    except UnicodeError as exc:
        raise RuntimeError(f"command output was not valid UTF-8: {cmd[0]}") from exc
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"unable to run {cmd[0]}: {exc}") from exc


run = _run


def _require_success(cp: subprocess.CompletedProcess[str], label: str) -> None:
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"{label} failed (exit {cp.returncode}): {detail[-2000:]}")


def _path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_file():
        raise RuntimeError(f"media file does not exist: {path}")
    return path


def _probe_json(path: Path) -> dict[str, Any]:
    cp = _run([
        config.FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ])
    _require_success(cp, f"ffprobe {path}")
    try:
        value = json.loads(cp.stdout or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"ffprobe returned a non-object for {path}")
    return value


# Upstream scripts imported these names directly.  Keep aliases at module
# scope while routing all work through the strict implementations above.
ffprobe_json = _probe_json


def _number(value: Any, name: str, *, default: float | None = None) -> float:
    if value in (None, ""):
        if default is not None:
            return default
        raise RuntimeError(f"ffprobe field {name} is missing")
    if isinstance(value, bool):
        raise RuntimeError(f"ffprobe field {name} is invalid: {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"ffprobe field {name} is invalid: {value!r}") from exc
    if not math.isfinite(result):
        raise RuntimeError(f"ffprobe field {name} is not finite: {result}")
    if result < 0:
        raise RuntimeError(f"ffprobe field {name} is negative: {result}")
    return result


def probe(path: str | Path) -> dict[str, Any]:
    """Return the stable media metadata contract consumed by ``worker.py``."""

    source = _path(path)
    data = _probe_json(source)
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError(f"ffprobe streams missing for {source}")

    video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError(f"media has no video stream: {source}")
    width_value = _number(video.get("width"), "width")
    height_value = _number(video.get("height"), "height")
    if not width_value.is_integer() or not height_value.is_integer():
        raise RuntimeError(f"media dimensions are not integers for {source}")
    width = int(width_value)
    height = int(height_value)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"media dimensions are invalid for {source}: {width}x{height}")

    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    duration_value = fmt.get("duration")
    if duration_value in (None, ""):
        duration_value = video.get("duration")
    duration = _number(duration_value, "duration")
    if duration <= 0:
        raise RuntimeError(f"media duration is not positive for {source}")

    rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
    fps = None
    if isinstance(rate, str) and "/" in rate:
        num, den = rate.split("/", 1)
        try:
            if float(den):
                fps = float(num) / float(den)
        except ValueError:
            fps = None

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams),
        "fps": fps,
    }


def _canvas(ratio: str) -> tuple[int, int]:
    try:
        return config.canvas_for_ratio(ratio)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _scale_filter(ratio: str) -> str:
    width, height = _canvas(ratio)
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,fps=30"
    )


def _temp_output(dest: Path) -> Path:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"cannot create output directory: {dest.parent}") from exc
    return dest.with_name(f".{dest.name}.part.mp4")


def _commit(temp: Path, dest: Path) -> Path:
    try:
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise RuntimeError(f"ffmpeg did not produce a non-empty output: {dest}")
        temp.replace(dest)
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"cannot commit media output: {dest}") from exc
    return dest


def normalize(
    source: str | Path,
    dest: str | Path,
    ratio: str,
    seconds: float,
    start: float = 0.0,
) -> Path:
    """Trim and normalize a clip to the target canvas and a real audio track.

    Missing audio is filled with deterministic stereo silence only after
    ``probe`` confirms that the input has no audio stream.  A source shorter
    than the requested exact duration is rejected; it is never silently
    accepted as a shorter generated clip.
    """

    source_path = _path(source)
    dest_path = Path(dest)
    try:
        length = float(seconds)
        offset = float(start)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("seconds and start must be numbers") from exc
    if length <= 0 or offset < 0:
        raise RuntimeError("seconds must be positive and start cannot be negative")
    metadata = probe(source_path)
    if offset + length > float(metadata["duration"]) + 0.001:
        raise RuntimeError(
            f"clip is too short for exact trim: need {offset + length:.3f}s, "
            f"source is {metadata['duration']:.3f}s"
        )
    width, height = _canvas(ratio)
    temp = _temp_output(dest_path)
    temp.unlink(missing_ok=True)
    cmd = [
        config.FFMPEG,
        "-y",
        "-ss",
        f"{offset:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{length:.3f}",
    ]
    if metadata["has_audio"]:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += [
            "-f",
            "lavfi",
            "-t",
            f"{length:.3f}",
            "-i",
            f"anullsrc=r={config.TARGET_AUDIO_RATE}:cl=stereo",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    cmd += [
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={config.TARGET_FPS}",
        "-t",
        f"{length:.3f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(config.TARGET_FPS),
        "-c:a",
        "aac",
        "-ar",
        str(config.TARGET_AUDIO_RATE),
        "-ac",
        "2",
        str(temp),
    ]
    try:
        cp = _run(cmd)
        _require_success(cp, f"normalize {source_path}")
        result = _commit(temp, dest_path)
        # A post-encode probe catches encoders that accepted -t but emitted a
        # materially short stream.  Small container rounding is tolerated.
        encoded = probe(result)
        if float(encoded["duration"]) < length - 0.20:
            raise RuntimeError(
                f"normalized clip is too short: expected {length:.3f}s, "
                f"got {encoded['duration']:.3f}s"
            )
        return result
    finally:
        temp.unlink(missing_ok=True)


def placeholder(dest: str | Path, ratio: str, seconds: float) -> Path:
    """Create a zero-cost color clip with stereo silence for dry runs."""

    width, height = _canvas(ratio)
    try:
        length = float(seconds)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("seconds must be a number") from exc
    if length <= 0:
        raise RuntimeError("seconds must be positive")
    dest_path = Path(dest)
    temp = _temp_output(dest_path)
    temp.unlink(missing_ok=True)
    cmd = [
        config.FFMPEG,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={config.TARGET_FPS}",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={config.TARGET_AUDIO_RATE}:cl=stereo",
        "-t",
        f"{length:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        str(config.TARGET_AUDIO_RATE),
        "-ac",
        "2",
        str(temp),
    ]
    try:
        cp = _run(cmd)
        _require_success(cp, "placeholder")
        return _commit(temp, dest_path)
    finally:
        temp.unlink(missing_ok=True)


def assemble(paths: list[str | Path], dest: str | Path, ratio: str) -> Path:
    """Concatenate normalized clips through ffmpeg's filter graph."""

    if not paths:
        raise RuntimeError("cannot assemble an empty clip list")
    clips = [_path(path) for path in paths]
    width, height = _canvas(ratio)
    for clip in clips:
        metadata = probe(clip)
        if not metadata["has_audio"]:
            raise RuntimeError(f"clip has no audio stream after normalization: {clip}")

    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    for index in range(len(clips)):
        filter_parts.append(
            f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={config.TARGET_FPS},setpts=PTS-STARTPTS[v{index}]"
        )
        filter_parts.append(f"[{index}:a:0]aresample={config.TARGET_AUDIO_RATE},asetpts=PTS-STARTPTS[a{index}]")
        concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
    filter_parts.append("".join(concat_inputs) + f"concat=n={len(clips)}:v=1:a=1[outv][outa]")

    dest_path = Path(dest)
    temp = _temp_output(dest_path)
    temp.unlink(missing_ok=True)
    cmd = [config.FFMPEG, "-y"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    cmd += [
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(config.TARGET_FPS),
        "-c:a",
        "aac",
        "-ar",
        str(config.TARGET_AUDIO_RATE),
        "-ac",
        "2",
        str(temp),
    ]
    try:
        cp = _run(cmd)
        _require_success(cp, "assemble")
        return _commit(temp, dest_path)
    finally:
        temp.unlink(missing_ok=True)


def cut_reference(
    source: str | Path,
    dest: str | Path,
    start: float,
    end: float,
) -> Path:
    """Cut an exact MP4 reference segment satisfying Ark's 2–15s/<50MiB limits."""

    source_path = _path(source)
    try:
        begin, finish = float(start), float(end)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("start and end must be numbers") from exc
    duration = finish - begin
    if not (config.REFERENCE_MIN_SEC <= duration <= config.REFERENCE_MAX_SEC):
        raise RuntimeError("reference duration must be between 2 and 15 seconds")
    metadata = probe(source_path)
    if begin < 0 or finish > float(metadata["duration"]) + 0.001:
        raise RuntimeError("reference range is outside the source duration")
    dest_path = Path(dest)
    if dest_path.suffix.lower() != ".mp4":
        raise RuntimeError("reference output must use the .mp4 extension")
    temp = _temp_output(dest_path)
    temp.unlink(missing_ok=True)
    cmd = [
        config.FFMPEG,
        "-y",
        "-ss",
        f"{begin:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{duration:.3f}",
    ]
    if metadata["has_audio"]:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += [
            "-f",
            "lavfi",
            "-t",
            f"{duration:.3f}",
            "-i",
            f"anullsrc=r={config.TARGET_AUDIO_RATE}:cl=stereo",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    cmd += [
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        str(config.TARGET_AUDIO_RATE),
        "-ac",
        "2",
        str(temp),
    ]
    try:
        cp = _run(cmd)
        _require_success(cp, "cut reference")
        result = _commit(temp, dest_path)
        if result.stat().st_size >= config.REFERENCE_MAX_BYTES:
            raise RuntimeError("reference output is 50 MiB or larger")
        encoded = probe(result)
        if float(encoded["duration"]) < duration - 0.20:
            raise RuntimeError("reference output is materially shorter than requested")
        return result
    finally:
        temp.unlink(missing_ok=True)


def _brand_hit_rows(source: str, text: str, *, ts: float | None = None) -> list[dict[str, Any]]:
    rows = []
    for hit in scanner.find_brand_hits(text):
        row: dict[str, Any] = {
            "source": source,
            "brand": hit.brand,
            "matched_text": hit.matched_text,
            "text": text,
        }
        if ts is not None:
            row["ts"] = ts
        rows.append(row)
    return rows


def _extract_audio(video: Path, wav: Path) -> None:
    cp = _run([
        config.FFMPEG,
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        str(wav),
    ], timeout=config.OCR_TIMEOUT_SEC)
    _require_success(cp, "audio extraction")
    if not wav.is_file() or wav.stat().st_size <= 0:
        raise RuntimeError("audio extraction produced no WAV")


def extract_audio_wav(video: str | Path, wav_path: str | Path) -> bool:
    _extract_audio(_path(video), Path(wav_path))
    return True


def _whisper(wav: Path, scratch: Path) -> list[dict[str, Any]]:
    if not config.WHISPER_MODEL:
        raise RuntimeError("WHISPER_MODEL is not configured")
    model_path = Path(config.WHISPER_MODEL).expanduser()
    if not model_path.is_file():
        raise RuntimeError(f"Whisper model does not exist: {model_path}")
    prefix = scratch / f"{wav.stem}.transcript"
    json_path = Path(str(prefix) + ".json")
    json_path.unlink(missing_ok=True)
    cp = _run([
        config.WHISPER_CLI,
        "-m",
        str(model_path),
        "-l",
        config.WHISPER_LANGUAGE,
        "--no-gpu",
        "-oj",
        "-of",
        str(prefix),
        str(wav),
    ], timeout=config.WHISPER_TIMEOUT_SEC)
    try:
        if cp.returncode != 0:
            _require_success(cp, "whisper")
        if not json_path.is_file():
            raise RuntimeError("whisper did not produce JSON")
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("whisper JSON is invalid") from exc
        if not isinstance(data, dict) or not isinstance(data.get("transcription"), list):
            raise RuntimeError("whisper JSON lacks a transcription list")
        segments: list[dict[str, Any]] = []
        for item in data["transcription"]:
            if not isinstance(item, dict) or not isinstance(item.get("offsets"), dict):
                raise RuntimeError("whisper JSON contains an invalid segment")
            offsets = item["offsets"]
            start = _number(offsets.get("from"), "whisper offsets.from") / 1000.0
            end = _number(offsets.get("to"), "whisper offsets.to") / 1000.0
            text = item.get("text")
            if not isinstance(text, str):
                raise RuntimeError("whisper JSON segment text is invalid")
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise RuntimeError("whisper JSON segment text is oversized")
            segments.append({"start": start, "end": end, "text": text.strip()})
        return segments
    finally:
        json_path.unlink(missing_ok=True)


def whisper_transcribe(wav_path: str | Path) -> list[dict[str, Any]]:
    wav = _path(wav_path)
    return _whisper(wav, wav.parent)


def _ocr_frames(video: Path, scratch: Path, duration: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scratch.mkdir(parents=True, exist_ok=True)
    interval = config.OCR_INTERVAL_SEC
    if interval <= 0:
        raise RuntimeError("OCR_INTERVAL_SEC must be positive")
    prefix = scratch / f".{video.stem}.frame-"
    pattern = str(prefix) + "%05d.jpg"
    for stale in scratch.glob(f".{video.stem}.frame-*.jpg"):
        stale.unlink(missing_ok=True)
    cp = _run([
        config.FFMPEG,
        "-y",
        "-i",
        str(video),
        "-vf",
        f"fps=1/{interval:g}",
        "-q:v",
        "3",
        pattern,
    ], timeout=config.OCR_TIMEOUT_SEC)
    _require_success(cp, "OCR frame extraction")
    frame_paths = sorted(scratch.glob(f".{video.stem}.frame-*.jpg"))
    if not frame_paths:
        raise RuntimeError("OCR frame extraction produced zero frames")
    rows: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    try:
        for index, frame in enumerate(frame_paths):
            # Passing a basename with cwd avoids platform-specific failures
            # in some Leptonica builds when opening files under temp roots.
            ocr = _run(
                [config.TESSERACT, frame.name, "stdout"],
                timeout=config.OCR_TIMEOUT_SEC,
                cwd=frame.parent,
            )
            _require_success(ocr, f"OCR recognition frame {index + 1}")
            text = " ".join((ocr.stdout or "").split())
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise RuntimeError(f"OCR recognition frame {index + 1} returned oversized text")
            ts = min(index * interval, max(0.0, duration - 0.001))
            rows.append({"ts": ts, "text": text})
            hits.extend(_brand_hit_rows("ocr", text, ts=ts))
    finally:
        for frame in frame_paths:
            frame.unlink(missing_ok=True)
    return rows, hits


def qc_scan(path: str | Path, scratch: str | Path) -> dict[str, Any]:
    """Run audio + OCR brand checks and return an explicit completion status.

    The function converts operational exceptions into FAIL checks so callers
    can safely report the result without accidentally treating a missing
    output as a clean scan.  ``completed`` is true only when every applicable
    tool actually completed; a no-audio input is explicitly not applicable.
    """

    video = Path(path)
    work = Path(scratch)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "pass": False,
            "completed": False,
            "hits": [],
            "checks": [{"name": "scratch", "check": "scratch", "status": "FAIL", "detail": str(exc)}],
        }
    checks: list[dict[str, Any]] = []
    hits: list[dict[str, Any]] = []
    completed = True
    try:
        metadata = probe(video)
    except Exception as exc:  # noqa: BLE001 - fail closed into the report
        return {
            "pass": False,
            "completed": False,
            "hits": [],
            "checks": [{"name": "probe", "check": "probe", "status": "FAIL", "detail": str(exc)}],
        }

    if not metadata["has_audio"]:
        checks.append({
            "name": "audio_brand_scan",
            "status": "not_applicable",
            "detail": "ffprobe confirmed that the input has no audio stream",
        })
    else:
        wav = work / f".{video.stem}.wav"
        try:
            _extract_audio(video, wav)
            segments = _whisper(wav, work)
            for segment in segments:
                hits.extend(_brand_hit_rows("audio", segment["text"], ts=segment["start"]))
            checks.append({
                "name": "audio_brand_scan",
                "status": "FAIL" if any(hit["source"] == "audio" for hit in hits) else "PASS",
                "detail": f"{len(segments)} transcript segments scanned",
            })
        except Exception as exc:  # noqa: BLE001 - report an explicit failure
            completed = False
            checks.append({"name": "audio_brand_scan", "status": "FAIL", "detail": str(exc)})
        finally:
            wav.unlink(missing_ok=True)

    try:
        _rows, ocr_hits = _ocr_frames(video, work, float(metadata["duration"]))
        hits.extend(ocr_hits)
        checks.append({
            "name": "ocr_brand_scan",
            "status": "FAIL" if ocr_hits else "PASS",
            "detail": f"{len(_rows)} frames scanned at {config.OCR_INTERVAL_SEC:g}s interval",
        })
    except Exception as exc:  # noqa: BLE001 - report an explicit failure
        completed = False
        checks.append({"name": "ocr_brand_scan", "status": "FAIL", "detail": str(exc)})

    # Keep the legacy ``check`` spelling alongside the clearer ``name`` key;
    # both are harmless in the bounded JSON envelope and ease old report
    # consumers during rollout.
    for check in checks:
        check.setdefault("check", check.get("name"))
    return {
        "pass": completed and not hits and all(check["status"] in {"PASS", "not_applicable"} for check in checks),
        "completed": completed,
        "hits": hits,
        "checks": checks,
    }


def validate_runtime() -> None:
    """Fail before claiming a job when media tools or the Whisper model are absent."""

    missing: list[str] = []
    for name, executable in (
        ("ffmpeg", config.FFMPEG),
        ("ffprobe", config.FFPROBE),
        ("whisper-cli", config.WHISPER_CLI),
        ("tesseract", config.TESSERACT),
    ):
        found = executable if Path(executable).is_file() else shutil.which(executable)
        if not found:
            missing.append(f"{name} ({executable})")
    if not config.WHISPER_MODEL:
        missing.append("WHISPER_MODEL")
    elif not Path(config.WHISPER_MODEL).expanduser().is_file():
        missing.append(f"Whisper model ({config.WHISPER_MODEL})")
    if missing:
        raise RuntimeError("missing media runtime dependencies: " + ", ".join(missing))


validate_tools = validate_runtime
qc = qc_scan
