import copy
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__:
    from . import client, worker
else:
    import client
    import worker


def fixture(tier="t0_5"):
    return {"id": "test-job", "claimToken": "test-claim", "attempt": 1,
            "protocolVersion": 2, "tier": tier, "beats": [],
            "brief": {"ratio": "9:16", "seedance2Prompt": "Original product scene",
                      "storyboard": [{"role": "hook", "seconds": 3, "description": "first shot"},
                                     {"role": "scene", "seconds": 3, "description": "second shot"},
                                     {"role": "end-card", "seconds": 2, "description": "last shot"}]},
            "refs": [{"url": "https://example.com/source.mp4", "kind": "video"}],
            "segmentPlan": [{"start": 0, "end": 3, "action": "reuse"},
                            {"start": 3, "end": 6, "action": "remake"},
                            {"start": 6, "end": 8, "action": "drop"}]}


class FakeAdex:
    def __init__(self):
        self.beats = []
        self.reports = []
        self.uploads = []
        self.fail_after_task = False

    def report(self, job, **fields):
        if self.fail_after_task and fields.get("beats") and any(b.get("taskId") for b in fields["beats"]):
            self.fail_after_task = False
            raise client.RetryableError("checkpoint unavailable")
        self.reports.append(copy.deepcopy(fields))
        if "beats" in fields:
            self.beats = copy.deepcopy(fields["beats"])
        return {"ok": True}

    def upload(self, job, path, *, purpose="output", index=0):
        self.uploads.append((purpose, index))
        return f"https://example.com/{purpose}/{index}.mp4"


class FakeArk:
    def __init__(self):
        self.calls = []
        self.polls = []
        self.fail_create = False
        self.fail_poll_once = False

    def create(self, prompt, ratio, seconds, reference):
        self.calls.append((prompt, ratio, seconds, reference))
        if self.fail_create:
            raise client.RetryableError("unknown outcome")
        return {"id": f"task-{len(self.calls)}"}

    def get(self, task_id):
        self.polls.append(task_id)
        if self.fail_poll_once:
            self.fail_poll_once = False
            raise client.RetryableError("temporary network failure")
        return {"status": "succeeded", "content": {"video_url": "https://example.com/clip.mp4"},
                "usage": {"completion_tokens": 100}}


class FakeMedia:
    def __init__(self):
        self.durations = {}
        self.qc_completed = True
        self.qc_pass = True
        self.ref_windows = []

    def probe(self, path):
        return {"width": 1080, "height": 1920, "duration": self.durations.get(str(path), 10), "has_audio": True}

    def normalize(self, source, dest, ratio, seconds, start=0):
        dest.write_bytes(b"fake media")
        self.durations[str(dest)] = seconds
        return dest

    def assemble(self, paths, dest, ratio):
        self.durations[str(dest)] = sum(self.durations[str(p)] for p in paths)
        dest.write_bytes(b"fake final")
        return dest

    def cut_reference(self, source, dest, start, end):
        self.ref_windows.append((start, end))
        dest.write_bytes(b"fake ref")
        return dest

    def qc_scan(self, path, scratch):
        return {"completed": self.qc_completed, "pass": self.qc_pass, "hits": [], "checks": []}


class FakeSimulationMedia:
    def __init__(self):
        self.placeholders = []
        self.assembled = None

    def placeholder(self, path, ratio, seconds):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
        self.placeholders.append((path, ratio, seconds))
        return path

    def assemble(self, paths, dest, ratio):
        dest.write_bytes(b"simulation")
        self.assembled = (list(paths), dest, ratio)
        return dest


def fake_download(url, path, **kwargs):
    path.write_bytes(b"fixture")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FakeAdex()
        self.ark = FakeArk()
        self.media = FakeMedia()
        self.download = patch.object(worker, "download", side_effect=fake_download)
        self.download.start()
        self.addCleanup(self.download.stop)

    def run_job(self, job=None, **kwargs):
        options = {"max_clips": 3, "max_tokens": 1_000_000, "poll_seconds": 0}
        options.update(kwargs)
        return worker.run_job(job or fixture(), self.client, self.ark, self.media, self.root, **options)

    def test_three_beat_t0_5_end_to_end(self):
        result = self.run_job()
        self.assertEqual(len(self.ark.calls), 3)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["media"]["durationSec"], 8)
        self.assertEqual(result["costTokens"], 300)
        self.assertEqual(self.client.uploads, [("clip", 0), ("clip", 1), ("clip", 2), ("output", 0)])
        self.assertTrue(all(b["videoUrl"].startswith("https://") for b in result["beats"]))

    def test_budget_rejects_before_paid_submission(self):
        for settings in ({"max_clips": 2}, {"max_tokens": 1}):
            with self.assertRaisesRegex(client.WorkerError, "budget"):
                self.run_job(**settings)
        self.assertEqual(self.ark.calls, [])

    def test_uncertain_submission_never_automatically_repeats(self):
        self.ark.fail_create = True
        with self.assertRaises(client.RetryableError):
            self.run_job()
        job = fixture()
        job["beats"] = self.client.beats
        self.assertEqual(job["beats"][0]["status"], "submitting")
        self.ark.fail_create = False
        with self.assertRaisesRegex(client.WorkerError, "Uncertain paid"):
            self.run_job(job)
        self.assertEqual(len(self.ark.calls), 1)

    def test_checkpoint_failure_after_submit_does_not_resubmit(self):
        self.client.fail_after_task = True
        with self.assertRaises(client.RetryableError):
            self.run_job()
        job = fixture()
        job["beats"] = self.client.beats
        with self.assertRaisesRegex(client.WorkerError, "Uncertain paid"):
            self.run_job(job)
        self.assertEqual(len(self.ark.calls), 1)

    def test_poll_failure_resumes_existing_task(self):
        self.ark.fail_poll_once = True
        with self.assertRaises(client.RetryableError):
            self.run_job()
        job = fixture()
        job["beats"] = self.client.beats
        self.assertEqual(job["beats"][0]["taskId"], "task-1")
        self.run_job(job)
        self.assertEqual(len(self.ark.calls), 3)
        self.assertEqual(self.ark.polls[:2], ["task-1", "task-1"])

    def test_completed_clips_resume_without_regeneration(self):
        result = self.run_job()
        job = fixture()
        job["beats"] = result["beats"]
        self.run_job(job)
        self.assertEqual(len(self.ark.calls), 3)
        self.assertEqual(self.client.uploads.count(("clip", 0)), 1)

    def test_cold_restart_after_assembly_failure_reuses_persisted_clips(self):
        with patch.object(self.media, "assemble", side_effect=client.RetryableError("interrupted")):
            with self.assertRaises(client.RetryableError):
                self.run_job()
        job = fixture()
        job.update(beats=copy.deepcopy(self.client.beats), attempt=2, claimToken="new-claim")
        self.assertTrue(all(beat["status"] == "done" for beat in job["beats"]))
        self.temp.cleanup()
        self.assertFalse(self.root.exists())

        # Only persisted artifacts/checkpoints survive; all process-local state is new.
        resumed_client, resumed_ark, resumed_media = FakeAdex(), FakeArk(), FakeMedia()
        durations = {beat["videoUrl"]: beat["seconds"] for beat in job["beats"]}

        def restore_clip(url, path, **kwargs):
            fake_download(url, path, **kwargs)
            resumed_media.durations[str(path)] = durations[url]

        with tempfile.TemporaryDirectory() as folder, patch.object(worker, "download", side_effect=restore_clip):
            result = worker.run_job(job, resumed_client, resumed_ark, resumed_media, Path(folder),
                                    max_clips=3, max_tokens=1_000_000, poll_seconds=0)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["costTokens"], 300)
        self.assertEqual(resumed_ark.calls, [])
        self.assertEqual(resumed_ark.polls, [])
        self.assertEqual(resumed_client.uploads, [("output", 0)])

    def test_missing_persisted_clip_never_falls_back_to_paid_regeneration(self):
        result = self.run_job()
        job = fixture()
        job["beats"] = result["beats"]
        self.client = FakeAdex()
        self.ark = FakeArk()
        with patch.object(worker, "download", side_effect=client.WorkerError("Media download rejected (404)")):
            with self.assertRaises(client.WorkerError):
                self.run_job(job)
        self.assertEqual(self.ark.calls, [])
        self.assertEqual(self.client.uploads, [])
        self.assertFalse(any(row.get("status") == "succeeded" for row in self.client.reports))

    def test_resumed_cost_is_checked_before_any_submission(self):
        self.ark.fail_poll_once = True
        with self.assertRaises(client.RetryableError):
            self.run_job()
        job = fixture()
        job["beats"] = self.client.beats
        job["beats"][0]["costTokens"] = 999_999
        with self.assertRaisesRegex(client.WorkerError, "budget"):
            self.run_job(job)
        self.assertEqual(len(self.ark.calls), 1)

    def test_changed_plan_never_reuses_old_checkpoint(self):
        result = self.run_job()
        job = fixture()
        job["beats"] = result["beats"]
        job["brief"]["seedance2Prompt"] = "Different product"
        with self.assertRaisesRegex(client.WorkerError, "does not match"):
            self.run_job(job)
        self.assertEqual(len(self.ark.calls), 3)

    def test_changed_ratio_never_reuses_old_checkpoint(self):
        result = self.run_job()
        job = fixture()
        job["beats"] = result["beats"]
        job["brief"]["ratio"] = "16:9"
        with self.assertRaisesRegex(client.WorkerError, "does not match"):
            self.run_job(job)
        self.assertEqual(len(self.ark.calls), 3)

    def test_t1_uses_uploaded_per_beat_reference_not_source(self):
        self.run_job(fixture("t1"))
        self.assertEqual(self.media.ref_windows, [(0, 3), (3, 6), (6, 8)])
        self.assertTrue(all(c[3].startswith("https://example.com/reference/") for c in self.ark.calls))

    def test_t1_reference_upload_failure_does_not_submit_source_url_to_ark(self):
        with patch.object(self.client, "upload", side_effect=client.RetryableError("reference upload failed")) as upload:
            with self.assertRaises(client.RetryableError):
                self.run_job(fixture("t1"))
        self.assertEqual(upload.call_args.kwargs, {"purpose": "reference", "index": 0})
        self.assertEqual(self.ark.calls, [])
        self.assertFalse(any(row.get("status") == "succeeded" for row in self.client.reports))

    def test_t2_only_generates_remake(self):
        result = self.run_job(fixture("t2"))
        self.assertEqual(len(self.ark.calls), 1)
        self.assertEqual(result["media"]["durationSec"], 6)

    def test_qc_incomplete_never_uploads_final_or_succeeds(self):
        self.media.qc_completed = False
        with self.assertRaisesRegex(client.WorkerError, "QC did not complete"):
            self.run_job()
        self.assertNotIn(("output", 0), self.client.uploads)
        self.assertFalse(any(r.get("status") == "succeeded" for r in self.client.reports))
        self.assertFalse(self.client.reports[-1]["qcReport"]["completed"])

    def test_brand_hits_retained_for_human_review(self):
        self.media.qc_pass = False
        self.assertFalse(self.run_job()["qcReport"]["pass"])

    def test_stale_claim_stops_before_generation(self):
        class LostLease:
            def check(self):
                raise client.StaleClaimError("lost")
        with self.assertRaises(client.StaleClaimError):
            self.run_job(lease=LostLease())
        self.assertEqual(self.ark.calls, [])

    def test_invalid_durations(self):
        for seconds in (0, -1, float("nan"), float("inf"), True, 11):
            job = fixture()
            job["brief"]["storyboard"][0]["seconds"] = seconds
            with self.assertRaises(client.WorkerError):
                worker.plan_job(job)

    def test_default_offline_does_not_contact_network(self):
        path = self.root / "job.json"
        path.write_text(json.dumps(fixture()))
        with patch.object(client.urllib.request, "build_opener") as opener:
            self.assertEqual(worker.main(["--job-file", str(path)]), 0)
            opener.assert_not_called()

    def test_simulation_isolated_from_production_clients_and_success_reporting(self):
        path = self.root / "job.json"
        path.write_text(json.dumps(fixture()))
        with patch.dict(os.environ, {
            "ADEX_BASE_URL": "https://production.example/adex",
            "WORKER_WEBHOOK_SECRET": "injected-test-secret",
            "ARK_API_KEY": "injected-test-key",
        }), patch.object(worker, "AdexClient") as adex, patch.object(worker, "ArkClient") as ark, \
                patch.object(worker, "run_job") as run_job, patch.object(client.urllib.request, "build_opener") as opener:
            self.assertEqual(worker.main([
                "--job-file", str(path), "--simulate", "--out-dir", str(self.root / "simulation"),
            ]), 0)
        adex.assert_not_called()
        ark.assert_not_called()
        run_job.assert_not_called()
        opener.assert_not_called()

    def test_simulation_uses_explicit_fixture_sandbox_without_upload_or_succeeded_report(self):
        simulation_media = FakeSimulationMedia()
        job = fixture()
        result = worker.run_simulation(job, simulation_media, self.root / "simulation")
        self.assertEqual(result["status"], "simulated")
        self.assertNotEqual(result["status"], "succeeded")
        self.assertEqual(len(simulation_media.placeholders), 3)
        self.assertIsNotNone(simulation_media.assembled)
        self.assertTrue(result["output"].endswith("SIMULATION-NOT-FOR-REVIEW.mp4"))

    def test_execution_needs_double_opt_in(self):
        with patch.dict(os.environ, {"WORKER_ENABLE_EXECUTION": "0"}):
            with self.assertRaises(SystemExit):
                worker.parse_args(["--execute", "--base-url", "https://example.com", "--max-tokens", "1000000"])


class ClientTests(unittest.TestCase):
    def test_hmac_exact_body(self):
        c = client.AdexClient("https://example.com/adex", "testing-only")
        with patch.object(client.time, "time", return_value=100):
            headers = c.signature('{"jobId":"j"}')
        expected = hmac.new(b"testing-only", b'100:{"jobId":"j"}', hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-adex-signature"], "sha256=" + expected)

    def test_refuse_private_destinations(self):
        for url in ("http://metadata.google.internal/", "file:///etc/passwd", "https://x:y@example.com",
                    "https://127.0.0.1/", "https://[::1]/"):
            with self.assertRaises(client.WorkerError):
                client.validate_url(url)
        with patch.object(client.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(client.WorkerError):
                client.validate_url("https://example.com")


if __name__ == "__main__":
    unittest.main()
