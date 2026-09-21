"""Runtime configuration for the standalone remix worker.

This module is deliberately boring: importing it only reads environment
variables and defines constants.  Tool discovery and validation happen in
``validate_runtime`` in :mod:`media`, so a worker can import the module in a
minimal build image without an import-time exit or warning.
"""

from __future__ import annotations

import os
import math


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def _float_env(name: str, default: float) -> float:
    try:
        value = float(_env(name, str(default)))
        return value if math.isfinite(value) and value > 0 else default
    except ValueError:
        return default


# Executable names are intentionally PATH based.  Deployments may override
# each one with an absolute path through the corresponding *_BIN variable.
FFMPEG = _env("FFMPEG_BIN", "ffmpeg")
FFPROBE = _env("FFPROBE_BIN", "ffprobe")
WHISPER_CLI = _env("WHISPER_BIN", "whisper-cli")
TESSERACT = _env("TESSERACT_BIN", "tesseract")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "")
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE", "auto")

# OCR defaults to one frame per second.  Keep the interval configurable for
# slower workers while rejecting non-positive values at use time.
try:
    OCR_INTERVAL_SEC = float(_env("OCR_INTERVAL_SEC", "1.0"))
except ValueError:
    OCR_INTERVAL_SEC = 1.0

# Subprocesses are bounded so a corrupt input or hung model cannot hold a job
# lease forever.  Individual operations can override these in tests.
SUBPROCESS_TIMEOUT_SEC = _float_env("REMIX_SUBPROCESS_TIMEOUT_SEC", 120.0)
WHISPER_TIMEOUT_SEC = _float_env("REMIX_WHISPER_TIMEOUT_SEC", 300.0)
OCR_TIMEOUT_SEC = _float_env("REMIX_OCR_TIMEOUT_SEC", 120.0)

# Worker-side generation guardrails.  The control-plane remains authoritative,
# but these constants prevent an accidentally configured worker from making an
# unbounded paid request.
SEEDANCE_MAX_CLIPS_PER_RUN = 3
SEEDANCE_MAX_DURATION_SEC = 12

UPLOAD_MAX_BYTES = 100 * 1024 * 1024
REFERENCE_MAX_BYTES = 50 * 1024 * 1024
REFERENCE_MIN_SEC = 2.0
REFERENCE_MAX_SEC = 15.0

TARGET_FPS = 30
TARGET_AUDIO_RATE = 48_000
AUDIO_FADE_SEC = 0.03

# Brand variants seen in ASR/OCR output.  Keep canonical names and common
# spacing/misrecognition variants together so scanner.py can stay generic.
BRANDS = [
    "PolyBuzz",
    "Polybus",
    "Poly Buzz",
    "Talkie",
    "Talky",
    "Emochi",
    "Emoji Chi",
    "Honey",
    "Character.AI",
    "Character AI",
    "Loopit",
    "Loop It",
    "Aippy",
    "Rezona",
    "Sekai",
]


RATIO_CANVAS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}


def canvas_for_ratio(ratio: str) -> tuple[int, int]:
    """Return the target canvas or raise ``ValueError`` for an unknown ratio."""

    try:
        return RATIO_CANVAS[ratio]
    except KeyError as exc:
        raise ValueError(f"unsupported ratio: {ratio!r}") from exc
