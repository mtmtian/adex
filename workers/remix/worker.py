#!/usr/bin/env python3
"""One bounded Cloud Run Job execution. Dry runs never contact Adex or Ark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

if __package__:
    from .client import AdexClient, ArkClient, RetryableError, StaleClaimError, WorkerError, download
    from . import media
else:
    from client import AdexClient, ArkClient, RetryableError, StaleClaimError, WorkerError, download
    import media

RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}
MAX_SEGMENTS = 64


def positive_number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise WorkerError(f"Invalid {name}")
    return float(value)


def plan_job(job: dict) -> list[dict]:
    if not isinstance(job, dict) or not isinstance(job.get("brief"), dict):
        raise WorkerError("Invalid job/brief")
    tier = job.get("tier", "t0_5")
    brief = job.get("brief", {})
    if tier not in ("t0_5", "t1", "t2") or brief.get("ratio") not in RATIOS:
        raise WorkerError("Unsupported tier or ratio")
    prompt = brief.get("seedance2Prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise WorkerError("Missing or oversized generation prompt")
    raw = job.get("segmentPlan") if tier == "t2" else brief.get("storyboard")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SEGMENTS:
        raise WorkerError("Invalid storyboard/segment plan")
    plan, elapsed, previous_end = [], 0.0, 0.0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise WorkerError("Invalid segment")
        if tier == "t2":
            start, end = item.get("start"), item.get("end")
            if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < previous_end:
                raise WorkerError("Invalid segment start/order")
            positive_number(end, "segment end")
            if end <= start or item.get("action") not in ("reuse", "remake", "drop"):
                raise WorkerError("Invalid segment action/bounds")
            previous_end = end
            if item["action"] == "drop":
                continue
            seconds, action = end - start, item["action"]
        else:
            seconds = positive_number(item.get("seconds"), "beat duration")
            start, end, action = elapsed, elapsed + seconds, "remake"
            elapsed = end
        description = item.get("description", "")
        if not isinstance(description, str) or len(description) > 16000:
            raise WorkerError("Invalid beat description")
        if seconds > 120 or (action == "remake" and seconds > 10):
            raise WorkerError("Generated beats must be <=10 seconds; split the storyboard first")
        plan.append({"index": index, "seconds": seconds, "start": start, "end": end,
                     "action": action, "role": item.get("role", action),
                     "prompt": prompt + " " + description,
                     "generationSeconds": max(3, math.ceil(seconds)) if action == "remake" else 0})
    if not plan or sum(s["seconds"] for s in plan) > 120:
        raise WorkerError("Empty or oversized output timeline")
    if tier in ("t1", "t2"):
        refs = job.get("refs")
        if not isinstance(refs, list) or not refs or not isinstance(refs[0], dict) or not isinstance(refs[0].get("url"), str):
            raise WorkerError("Reference video is required for this tier")
    return plan


def token_estimate(segment: dict) -> int:
    # Conservative reservation at 720p/24fps. Actual usage is checked after polling.
    return math.ceil(1280 * 720 * 24 * segment["generationSeconds"] / 1024)


class Lease:
    def __init__(self, client: AdexClient, job: dict, interval: float = 30):
        self.client, self.job, self.interval = client, job, interval
        self.stop_event = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.client.report(self.job, heartbeat=True)
            except Exception as exc:
                self.error = exc
                return

    def check(self):
        if self.error:
            raise self.error

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=100)
        if self.thread.is_alive():
            raise RetryableError("Heartbeat did not stop")


def checkpoint(client, job, beats, **fields):
    client.report(job, beats=beats, **fields)


def check_remaining_budget(plan, beats, max_tokens):
    total = 0
    for segment, beat in zip(plan, beats):
        if segment["action"] != "remake":
            continue
        cost = beat.get("costTokens", beat.get("reservedTokens", token_estimate(segment)))
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise WorkerError("Invalid persisted token cost")
        total += cost
    if total > max_tokens:
        raise WorkerError("Generation budget exceeded by persisted or actual usage")


def run_job(job: dict, client, ark, media, root: Path, *, max_clips: int,
            max_tokens: int, poll_seconds: float = 10, poll_timeout: float = 900,
            lease=None, allow_local_downloads: bool = False) -> dict:
    plan = plan_job(job)
    generated = [s for s in plan if s["action"] == "remake"]
    reservation = sum(token_estimate(s) for s in generated)
    if len(generated) > max_clips or reservation > max_tokens:
        raise WorkerError("Generation budget exceeded before submission")
    stored = job.get("beats") or []
    if not isinstance(stored, list) or any(not isinstance(x, dict) for x in stored):
        raise WorkerError("Invalid persisted checkpoint")
    previous = {s["index"]: s for s in stored if isinstance(s.get("index"), int)}
    beats = []
    for segment in plan:
        identity = {"segment": segment, "ratio": job["brief"]["ratio"], "tier": job["tier"],
                    "refs": job.get("refs") if job["tier"] in ("t1", "t2") else None}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        old = previous.get(segment["index"])
        if old and old.get("fingerprint") != fingerprint:
            raise WorkerError("Checkpoint does not match current plan; manual reconciliation required")
        beats.append(dict(old) if old else {"index": segment["index"], "role": segment["role"],
                                          "seconds": segment["seconds"], "fingerprint": fingerprint,
                                          "status": "pending"})
    check_remaining_budget(plan, beats, max_tokens)
    checkpoint(client, job, beats, status="running")
    source = None
    source_seconds = 0.0
    if job["tier"] in ("t1", "t2"):
        source = root / "source.mp4"
        download(job["refs"][0]["url"], source, allow_local=allow_local_downloads)
        source_seconds = media.probe(source)["duration"]
        if job["tier"] == "t2" and any(s["end"] > source_seconds + 0.05 for s in plan):
            raise WorkerError("Segment plan extends beyond actual source duration")
    paths = []
    ratio = job["brief"]["ratio"]
    for segment, beat in zip(plan, beats):
        if lease:
            lease.check()
        check_remaining_budget(plan, beats, max_tokens)
        index = segment["index"]
        local = root / f"clip-{index}.mp4"
        if beat.get("status") == "done" and beat.get("videoUrl"):
            download(beat["videoUrl"], local, allow_local=allow_local_downloads)
            metadata = media.probe(local)
            if abs(metadata["duration"] - segment["seconds"]) > 0.15:
                raise WorkerError("Persisted clip duration mismatch")
            paths.append(local)
            continue
        if segment["action"] == "reuse":
            media.normalize(source, local, ratio, segment["seconds"], start=segment["start"])
        else:
            # A persisted submission intent without an ID is ambiguous: never create again.
            if beat.get("status") == "submitting" and not beat.get("taskId"):
                raise WorkerError("Uncertain paid submission; reconcile provider task before retrying")
            if not beat.get("taskId"):
                reference = None
                if job["tier"] == "t1":
                    if source_seconds < 2:
                        raise WorkerError("Reference video is too short")
                    start = min(segment["start"], max(0.0, source_seconds - 2))
                    end = min(source_seconds, start + max(2, min(15, segment["seconds"])))
                    ref_clip = media.cut_reference(source, root / f"ref-{index}.mp4", start, end)
                    reference = client.upload(job, ref_clip, purpose="reference", index=index)
                beat.update(status="submitting", reservedTokens=token_estimate(segment))
                checkpoint(client, job, beats)
                if lease:
                    lease.check()
                # Any lost response leaves the persisted intent. No automatic create retry.
                task = ark.create(segment["prompt"], ratio, segment["generationSeconds"], reference)
                task_id = task.get("id")
                if not isinstance(task_id, str) or not task_id:
                    raise WorkerError("Provider submission outcome unknown; manual reconciliation required")
                beat.update(status="generating", taskId=task_id)
                checkpoint(client, job, beats)
            deadline = time.monotonic() + poll_timeout
            while True:
                if lease:
                    lease.check()
                task = ark.get(beat["taskId"])
                status = task.get("status")
                if status == "succeeded":
                    break
                if status in ("failed", "cancelled", "expired"):
                    raise WorkerError("Provider task failed; automatic regeneration is disabled")
                if status not in ("queued", "running"):
                    raise WorkerError("Unknown provider task state")
                if time.monotonic() >= deadline:
                    raise RetryableError("Provider polling deadline reached; checkpoint retained")
                time.sleep(poll_seconds)
            url = (task.get("content") or {}).get("video_url") or (task.get("output") or {}).get("video_url")
            if not isinstance(url, str):
                raise RetryableError("Provider video URL not available yet")
            usage = (task.get("usage") or {}).get("completion_tokens")
            if isinstance(usage, int) and not isinstance(usage, bool) and usage >= 0:
                beat["costTokens"] = usage
                beat["costSource"] = "provider"
            else:
                beat["costTokens"] = beat.get("reservedTokens", token_estimate(segment))
                beat["costSource"] = "estimate"
            spent = sum(x.get("costTokens", x.get("reservedTokens", 0)) for x in beats)
            checkpoint(client, job, beats, costTokens=spent)
            remaining = sum(token_estimate(s) for s, b in zip(plan, beats) if b["status"] == "pending")
            if spent + remaining > max_tokens:
                raise WorkerError("Actual usage exceeds remaining generation budget")
            raw = root / f"raw-{index}.mp4"
            download(url, raw, allow_local=allow_local_downloads)
            media.normalize(raw, local, ratio, segment["seconds"])
        if lease:
            lease.check()
        beat.update(videoUrl=client.upload(job, local, purpose="clip", index=index), status="done")
        checkpoint(client, job, beats)
        paths.append(local)
    checkpoint(client, job, beats, status="assembling")
    final = media.assemble(paths, root / "final.mp4", ratio)
    if lease:
        lease.check()
    checkpoint(client, job, beats, status="qc")
    qc = media.qc_scan(final, root / "qc")
    if qc.get("completed") is not True or not isinstance(qc.get("pass"), bool):
        checkpoint(client, job, beats, qcReport=qc)
        raise WorkerError("QC did not complete")
    metadata = media.probe(final)
    expected = sum(s["seconds"] for s in plan)
    if abs(metadata["duration"] - expected) > 0.2:
        raise WorkerError("Final video duration differs from plan")
    actual = {"width": metadata["width"], "height": metadata["height"], "durationSec": metadata["duration"]}
    cost = sum(x.get("costTokens", 0) for x in beats)
    checkpoint(client, job, beats, qcReport=qc, costTokens=cost)
    if lease:
        lease.check()
    url = client.upload(job, final)
    result = {"status": "succeeded", "beats": beats, "qcReport": qc,
              "costTokens": cost, "outputUrl": url, "media": actual}
    client.report(job, **result)
    return result


def run_simulation(job: dict, media, out_dir: Path) -> dict:
    """Render an explicit local fixture without any control-plane side effects.

    Simulation deliberately has no client arguments: it cannot claim, upload,
    heartbeat, or report a production job by construction.
    """
    plan = plan_job(job)
    out_dir.mkdir(parents=True, exist_ok=True)
    clips = [
        media.placeholder(out_dir / f"{segment['index']}.mp4", job["brief"]["ratio"], segment["seconds"])
        for segment in plan
    ]
    final = media.assemble(clips, out_dir / "SIMULATION-NOT-FOR-REVIEW.mp4", job["brief"]["ratio"])
    return {
        "status": "simulated",
        "segments": plan,
        "reservedTokens": sum(token_estimate(segment) for segment in plan),
        "output": str(final),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run one real Adex job (paid API)")
    parser.add_argument("--job-file", type=Path, help="Offline fixture for plan/simulation; never contacts Adex")
    parser.add_argument("--simulate", action="store_true", help="Render offline placeholders; never mark as production ready")
    parser.add_argument("--base-url", default=os.environ.get("ADEX_BASE_URL"))
    parser.add_argument("--job-id")
    parser.add_argument("--max-clips", type=int, default=int(os.environ.get("REMIX_MAX_CLIPS", "3")))
    parser.add_argument("--max-tokens", type=int, default=int(os.environ.get("REMIX_MAX_TOKENS", "0")))
    parser.add_argument("--out-dir", type=Path, default=Path("out/remix-simulation"))
    args = parser.parse_args(argv)
    if args.execute:
        if args.job_file or args.simulate or os.environ.get("WORKER_ENABLE_EXECUTION") != "1":
            parser.error("Execution needs WORKER_ENABLE_EXECUTION=1 and cannot use offline flags")
        if not args.base_url or args.max_tokens <= 0 or not 1 <= args.max_clips <= MAX_SEGMENTS:
            parser.error("Execution requires base URL and explicit positive token/clip limits")
    elif not args.job_file:
        parser.error("Default is offline: supply --job-file, or explicitly enable --execute")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.execute:
        job = json.loads(args.job_file.read_text())
        if args.simulate:
            result = run_simulation(job, media, args.out_dir)
            print(json.dumps(result))
        else:
            plan = plan_job(job)
            print(json.dumps({"mode": "offline", "segments": plan,
                              "reservedTokens": sum(token_estimate(x) for x in plan)}))
        return 0
    secret, key = os.environ.get("WORKER_WEBHOOK_SECRET"), os.environ.get("ARK_API_KEY")
    if not secret or not key:
        raise WorkerError("WORKER_WEBHOOK_SECRET and ARK_API_KEY must be injected at runtime")
    media.validate_runtime()
    client = AdexClient(args.base_url, secret)
    ark = ArkClient(key, os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
                    os.environ.get("ARK_MODEL", "doubao-seedance-2-0-260128"))
    job = client.claim(args.job_id)
    if job is None:
        print("No eligible job")
        return 0
    try:
        if job.get("protocolVersion") != 2:
            raise WorkerError("Deploy the v2 control plane before enabling this worker")
        with tempfile.TemporaryDirectory(prefix="adex-remix-") as folder:
            with Lease(client, job, interval=min(30, job.get("leaseSeconds", 1800) / 3)) as lease:
                result = run_job(job, client, ark, media, Path(folder), max_clips=args.max_clips,
                                 max_tokens=args.max_tokens, lease=lease)
        print(json.dumps({"status": result["status"], "qcPass": result["qcReport"]["pass"]}))
        return 0
    except StaleClaimError:
        print("Lease lost; no further writes", file=sys.stderr)
        return 1
    except RetryableError:
        print("Transient failure; persistent checkpoint retained for stale-lease recovery", file=sys.stderr)
        return 1
    except Exception as exc:
        # WorkerError messages are fixed summaries; never persist raw media/OS errors.
        try:
            summary = str(exc) if isinstance(exc, WorkerError) else f"Media runtime failed ({type(exc).__name__})"
            client.report(job, status="failed", error=summary)
        except (WorkerError, OSError):
            pass
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Worker failed ({type(error).__name__})", file=sys.stderr)
        sys.exit(1)
