"""Real loopback HTTP verifies wire bytes independently of fake worker clients."""

import hashlib
import hmac
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

try:
    from workers.remix import client
except ModuleNotFoundError:
    import client


class HttpContractTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.reply_status = 200
        self.reply_body = {"ok": True}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.received.append((self.path, self.headers, raw))
                self.send_response(owner.reply_status)
                if owner.reply_status == 302:
                    self.send_header("Location", "/must-not-follow")
                self.end_headers()
                self.wfile.write(json.dumps(owner.reply_body).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/adex"
        self.adex = client.AdexClient(self.base, "test-only-secret", allow_local=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def assert_signed(self, headers, text):
        canonical = f"{headers['x-adex-timestamp']}:{text}".encode()
        digest = hmac.new(b"test-only-secret", canonical, hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-adex-signature"], "sha256=" + digest)

    def test_claim_and_report_exact_json_and_base_path(self):
        self.reply_body = {"job": {"id": "j", "claimToken": "token"}}
        job = self.adex.claim("j")
        self.adex.report(job, beats=[{"index": 0, "status": "done"}])
        for path, headers, raw in self.received:
            self.assertTrue(path.startswith("/adex/api/worker/remix-jobs/"))
            self.assertEqual(json.loads(raw)["protocolVersion"], 2)
            self.assert_signed(headers, raw.decode())
        self.assertEqual(json.loads(self.received[-1][2])["claimToken"], "token")

    def test_upload_binds_purpose_index_hash_and_bytes(self):
        self.reply_body = {"fileUrl": "https://example.com/clip.mp4"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mp4"
            content = b"\x00\x00\x00\x18ftypisom" + b"fixture"
            path.write_bytes(content)
            self.adex.upload({"id": "j", "claimToken": "token"}, path, purpose="reference", index=2)
        url, headers, raw = self.received[0]
        self.assertEqual(parse_qs(urlsplit(url).query), {"jobId": ["j"], "purpose": ["reference"], "index": ["2"]})
        self.assertEqual(raw, content)
        digest = hashlib.sha256(content).hexdigest()
        self.assertEqual(headers["x-adex-content-sha256"], digest)
        self.assert_signed(headers, f"j:token:reference:2:{digest}")

    def test_claim_does_not_retry_uncertain_response(self):
        self.reply_status = 503
        with self.assertRaises(client.RetryableError):
            self.adex.claim()
        self.assertEqual(len(self.received), 1)

    def test_stale_claim_and_redirect_do_not_retry(self):
        for status, error in ((409, client.StaleClaimError), (302, client.WorkerError)):
            self.received.clear()
            self.reply_status = status
            with self.assertRaises(error):
                self.adex.report({"id": "j", "claimToken": "token"}, heartbeat=True)
            self.assertEqual(len(self.received), 1)

    def test_safe_report_retries_transient_response(self):
        self.reply_status = 503
        with patch.object(client.time, "sleep"), self.assertRaises(client.RetryableError):
            self.adex.report({"id": "j", "claimToken": "token"}, heartbeat=True)
        self.assertEqual(len(self.received), 3)


if __name__ == "__main__":
    unittest.main()
