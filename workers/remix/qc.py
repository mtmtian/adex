"""Strict command-line QC adapter backed by :mod:`media`."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

try:
    from . import config, media
except ImportError:  # direct script compatibility
    import config  # type: ignore[no-redef]
    import media  # type: ignore[no-redef]

VIDEO_EXTS = media.VIDEO_EXTS
OCR_INTERVAL_SEC = config.OCR_INTERVAL_SEC


def scan_frames_for_brand_hits(
    video: str | Path,
    scratch: str | Path,
    interval_sec: float = OCR_INTERVAL_SEC,
) -> tuple[list[dict[str, Any]], int]:
    """Return structured OCR hits and frame count, failing on zero/failed frames."""

    old_interval = config.OCR_INTERVAL_SEC
    config.OCR_INTERVAL_SEC = interval_sec
    try:
        Path(scratch).mkdir(parents=True, exist_ok=True)
        metadata = media.probe(video)
        rows, hits = media._ocr_frames(media._path(video), Path(scratch), float(metadata["duration"]))
    finally:
        config.OCR_INTERVAL_SEC = old_interval
    return hits, len(rows)


def qc_audio(video: str | Path, scratch: str | Path) -> list[dict[str, Any]]:
    path = media._path(video)
    metadata = media.probe(path)
    if not metadata["has_audio"]:
        return [{
            "file": path.name,
            "check": "audio_brand_scan",
            "status": "not_applicable",
            "detail": "ffprobe confirmed that the input has no audio stream",
        }]
    try:
        result = media.qc_scan(path, scratch)
        check = next(item for item in result["checks"] if item["name"] == "audio_brand_scan")
        return [{"file": path.name, **check}]
    except Exception as exc:  # noqa: BLE001 - report fail-closed row
        return [{"file": path.name, "check": "audio_brand_scan", "status": "FAIL", "detail": str(exc)}]


def qc_ocr(video: str | Path, scratch: str | Path) -> list[dict[str, Any]]:
    path = media._path(video)
    try:
        result = media.qc_scan(path, scratch)
        check = next(item for item in result["checks"] if item["name"] == "ocr_brand_scan")
        return [{"file": path.name, **check}]
    except Exception as exc:  # noqa: BLE001 - report fail-closed row
        return [{"file": path.name, "check": "ocr_brand_scan", "status": "FAIL", "detail": str(exc)}]


def qc_one(video: str | Path, scratch: str | Path) -> list[dict[str, Any]]:
    result = media.qc_scan(video, scratch)
    rows = []
    for check in result.get("checks", []):
        rows.append({"file": Path(video).name, **check})
    if not result.get("completed") and not any(row.get("status") == "FAIL" for row in rows):
        rows.append({"file": Path(video).name, "name": "runtime", "status": "FAIL", "detail": "QC incomplete"})
    return rows


qc_scan = media.qc_scan


def main() -> None:
    parser = argparse.ArgumentParser(description="投放前媒体品牌预检")
    parser.add_argument("target", type=Path)
    parser.add_argument("--out", type=Path, default=Path("out/qc"))
    args = parser.parse_args()
    target = args.target.resolve()
    if target.is_dir():
        videos = sorted(path for path in target.iterdir() if path.suffix.lower() in VIDEO_EXTS)
    elif target.is_file():
        videos = [target]
    else:
        raise SystemExit(f"目标不存在: {target}")
    if not videos:
        raise SystemExit(f"没有找到视频文件: {target}")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / ".scratch"
    all_rows = [row for video in videos for row in qc_one(video, scratch)]
    report = out / "qc_report.csv"
    with report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "name", "check", "status", "detail"])
        writer.writeheader()
        writer.writerows(all_rows)
    failed = [row for row in all_rows if row.get("status") == "FAIL"]
    print(f"qc_report.csv -> {report}")
    if failed:
        for row in failed:
            print(
                f"FAIL: {row.get('file')} / {row.get('name') or row.get('check')}: {row.get('detail')}",
                file=sys.stderr,
            )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
