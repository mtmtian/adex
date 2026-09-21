"""Compatibility helpers for the original ``assemble.py`` CLI.

New worker code should call :mod:`media` directly.  The small adapters here
keep existing scripts/imports working while inheriting media.py's strict
subprocess and duration checks.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    from . import config, media
except ImportError:  # direct script compatibility
    import config  # type: ignore[no-redef]
    import media  # type: ignore[no-redef]


DEFAULT_OUT_DIR = Path("out/assembled")
TARGET_WIDTH, TARGET_HEIGHT = config.RATIO_CANVAS["9:16"]
TARGET_FPS = config.TARGET_FPS
TARGET_AUDIO_RATE = config.TARGET_AUDIO_RATE
AUDIO_FADE_SEC = config.AUDIO_FADE_SEC
MATCH_TOLERANCE_SEC = 0.5
TIME_RANGE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—~]\s*(\d+(?:\.\d+)?)\s*(?:s|秒)?")


def set_target_canvas(width: int, height: int) -> None:
    """Retained for callers that previously changed the module canvas."""

    global TARGET_WIDTH, TARGET_HEIGHT
    if width <= 0 or height <= 0:
        raise ValueError("canvas dimensions must be positive")
    TARGET_WIDTH, TARGET_HEIGHT = width, height


def parse_time_range(text: str) -> tuple[float, float] | None:
    match = TIME_RANGE_PATTERN.search(text or "")
    return (float(match.group(1)), float(match.group(2))) if match else None


def ffprobe_has_audio(path: str | Path) -> bool:
    return bool(media.probe(path)["has_audio"])


def ffprobe_summary(path: str | Path) -> str:
    info = media.probe(path)
    return (
        f"duration={info['duration']:.3f}\nwidth={info['width']}\nheight={info['height']}\n"
        f"has_audio={'true' if info['has_audio'] else 'false'}"
    )


def load_gen_clip_map(gen_clip_args: list[str]) -> dict[tuple[float, float], Path]:
    mapping: dict[tuple[float, float], Path] = {}
    for raw in gen_clip_args:
        if "=" not in raw:
            raise RuntimeError(f"--gen-clip format must be <time-range>=<path>: {raw!r}")
        time_part, path_part = raw.split("=", 1)
        parsed = parse_time_range(time_part)
        if parsed is None:
            raise RuntimeError(f"cannot parse generated clip time range: {time_part!r}")
        path = Path(path_part).expanduser()
        if not path.is_file():
            raise RuntimeError(f"generated clip does not exist: {path}")
        mapping[parsed] = path
    return mapping


def find_gen_clip(
    gen_clip_map: dict[tuple[float, float], Path],
    time_range_text: str,
) -> Path | None:
    target = parse_time_range(time_range_text)
    if target is None:
        return None
    for (start, end), path in gen_clip_map.items():
        if abs(start - target[0]) <= MATCH_TOLERANCE_SEC and abs(end - target[1]) <= MATCH_TOLERANCE_SEC:
            return path
    return None


def resolve_segments(
    storyboard: list[dict],
    source_path: Path | None,
    gen_clip_map: dict[tuple[float, float], Path],
) -> tuple[list[dict], list[str]]:
    """Resolve legacy storyboard entries without performing any ffmpeg work."""

    resolved: list[dict] = []
    problems: list[str] = []
    for segment in storyboard:
        time_range = segment.get("time_range", "?")
        source = segment.get("source", "generated")
        if source == "reuse":
            reuse = segment.get("reuse_cut")
            if not isinstance(reuse, dict) or "start_sec" not in reuse or "end_sec" not in reuse:
                problems.append(f"reuse segment {time_range} is missing reuse_cut")
                continue
            if source_path is None or not source_path.is_file():
                problems.append(f"reuse segment {time_range} requires an existing source file")
                continue
            start, end = float(reuse["start_sec"]), float(reuse["end_sec"])
            if end <= start:
                problems.append(f"reuse segment {time_range} has an invalid range")
                continue
            resolved.append({
                "time_range": time_range,
                "source": "reuse",
                "start_sec": start,
                "end_sec": end,
                "source_path": source_path,
            })
        elif source == "generated":
            path = find_gen_clip(gen_clip_map, str(time_range))
            if path is None:
                problems.append(f"generated segment {time_range} is missing --gen-clip")
                continue
            resolved.append({"time_range": time_range, "source": "generated", "gen_path": path})
        else:
            problems.append(f"segment {time_range} source must be reuse/generated")
    return resolved, problems


def normalize_one(
    idx: int,
    seg: dict,
    scratch: Path,
    fade_in: bool = False,
    fade_out: bool = False,
    fade_duration_hint: float | None = None,
) -> Path:
    """Normalize one resolved segment through the strict media primitive.

    Audio fades were part of the legacy assembler.  The worker's current
    contract performs fades at the generation layer; retaining these arguments
    avoids breaking old callers while keeping normalization deterministic.
    """

    del fade_in, fade_out, fade_duration_hint
    scratch.mkdir(parents=True, exist_ok=True)
    if seg.get("source") == "reuse":
        source = seg.get("source_path")
        start = float(seg.get("start_sec", 0.0))
        seconds = float(seg["end_sec"]) - start
    elif seg.get("source") == "generated":
        source = seg.get("gen_path")
        start = 0.0
        seconds = float(seg.get("duration_sec") or media.probe(source)["duration"])
    else:
        raise RuntimeError(f"unknown segment source: {seg.get('source')!r}")
    return media.normalize(source, scratch / f"seg_{idx:02d}.mp4", "9:16", seconds, start)


def assemble(paths: list[str | Path], dest: str | Path, ratio: str = "9:16") -> Path:
    return media.assemble(paths, dest, ratio)
