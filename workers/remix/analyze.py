"""Strict adapters for the upstream offline analysis helpers.

The production worker uses :mod:`media`; these names are kept for old batch
scripts and tests that imported ``analyze.py`` directly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

try:
    from . import config, media
except ImportError:  # direct script compatibility
    import config  # type: ignore[no-redef]
    import media  # type: ignore[no-redef]


VIDEO_EXTS = media.VIDEO_EXTS
OCR_INTERVAL_SEC = config.OCR_INTERVAL_SEC


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a media command with the shared timeout/error handling."""

    return media._run(cmd, **kwargs)


def ffprobe_json(path: str | Path) -> dict:
    return media._probe_json(media._path(path))


def get_duration_sec(probe: dict) -> float:
    fmt = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    raw = fmt.get("duration")
    if raw in (None, ""):
        for stream in probe.get("streams", []):
            if isinstance(stream, dict) and stream.get("duration") not in (None, ""):
                raw = stream["duration"]
                break
    if raw in (None, ""):
        raise RuntimeError("ffprobe did not provide a duration")
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid duration: {raw!r}") from exc
    if duration <= 0:
        raise RuntimeError(f"duration is not positive: {duration}")
    return duration


def extract_audio_wav(video: str | Path, wav_path: str | Path) -> bool:
    media._extract_audio(media._path(video), Path(wav_path))
    return True


def whisper_transcribe(wav_path: str | Path) -> list[dict]:
    wav = media._path(wav_path)
    return media._whisper(wav, wav.parent)


def analyze_one(video: str | Path, scratch: str | Path) -> dict[str, Any]:
    """Analyze one file, raising on any applicable tool failure."""

    source = media._path(video)
    work = Path(scratch)
    work.mkdir(parents=True, exist_ok=True)
    metadata = media.probe(source)
    transcript: list[dict] = []
    if metadata["has_audio"]:
        wav = work / f".{source.stem}.wav"
        try:
            media._extract_audio(source, wav)
            transcript = media._whisper(wav, work)
        finally:
            wav.unlink(missing_ok=True)
    ocr_rows, ocr_hits = media._ocr_frames(source, work, float(metadata["duration"]))
    return {
        "file": source.name,
        "metadata": metadata,
        "transcript": transcript,
        "ocr_frames": ocr_rows,
        "transcript_brand_hits": [
            {**hit, "start": seg["start"], "end": seg["end"]}
            for seg in transcript
            for hit in media._brand_hit_rows("audio", seg["text"])
        ],
        "ocr_brand_hits": ocr_hits,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Strict remix media analysis")
    parser.add_argument("video_dir", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    video_dir = args.video_dir.resolve()
    if not video_dir.is_dir():
        raise SystemExit(f"素材目录不存在: {video_dir}")
    out = (args.out or Path("out") / video_dir.name).resolve()
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / ".scratch"
    files = sorted(p for p in video_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not files:
        raise SystemExit(f"目录下没有视频文件: {video_dir}")
    results = [analyze_one(path, scratch) for path in files]
    (out / "analysis.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"analysis.json -> {out / 'analysis.json'} ({len(results)} files)")


if __name__ == "__main__":
    main()
