from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from collections import deque
from pathlib import Path
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from workers.remix import client
except ModuleNotFoundError:  # unittest launched from workers/remix
    import client  # type: ignore[no-redef]


class _ContractHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append((self.command, self.path, dict(self.headers), body))  # type: ignore[attr-defined]
        if self.server.responses:  # type: ignore[attr-defined]
            status, response, headers = self.server.responses.popleft()  # type: ignore[attr-defined]
        else:
            status, response, headers = 200, b'{"ok":true}', {"Content-Type": "application/json"}
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def log_message(self, *_args) -> None:
        return


class _LocalContract:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ContractHandler)
        self.server.responses = deque()  # type: ignore[attr-defined]
        self.server.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def queue(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.server.responses.append((status, body, headers or {"Content-Type": "application/json"}))  # type: ignore[attr-defined]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ClientContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.http = _LocalContract()
        self.addCleanup(self.http.close)
        self.secret = "test-secret"
        self.job = {"id": "job-1", "claimToken": "claim-1"}

    def test_adex_base_path_and_signature(self) -> None:
        c = client.AdexClient(self.http.base_url + "/adex", self.secret, allow_local=True)
        with mock.patch.object(client.time, "time", return_value=123):
            result = c.post("claim", {"jobId": "j"}, retry=False)
        self.assertTrue(result["ok"])
        method, path, headers, body = self.http.server.requests[0]  # type: ignore[attr-defined]
        self.assertEqual((method, path), ("POST", "/adex/api/worker/remix-jobs/claim"))
        expected = hmac.new(self.secret.encode(), b'123:{"jobId":"j"}', hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Adex-Signature"], "sha256=" + expected)

    def test_upload_purpose_is_bound_into_hmac(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "clip.mp4"
        path.write_bytes(b"video-bytes")
        self.addCleanup(path.unlink, missing_ok=True)
        self.http.queue(200, b'{"fileUrl":"http://127.0.0.1/output.mp4"}')
        c = client.AdexClient(self.http.base_url, self.secret, allow_local=True)
        with mock.patch.object(client.time, "time", return_value=200):
            result = c.upload(self.job, path, purpose="clip", index=2)
        self.assertEqual(result, "http://127.0.0.1/output.mp4")
        method, request_path, headers, body = self.http.server.requests[0]  # type: ignore[attr-defined]
        self.assertEqual(method, "POST")
        self.assertIn("purpose=clip", request_path)
        self.assertIn("index=2", request_path)
        digest = hashlib.sha256(b"video-bytes").hexdigest()
        expected = hmac.new(
            self.secret.encode(),
            f"200:job-1:claim-1:clip:2:{digest}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["X-Adex-Signature"], "sha256=" + expected)
        self.assertEqual(headers["X-Adex-Content-Sha256"], digest)
        self.assertEqual(body, b"video-bytes")

    def test_retry_transient_http_failure(self) -> None:
        self.http.queue(503, b'{"error":"busy"}')
        c = client.AdexClient(self.http.base_url, self.secret, allow_local=True)
        with mock.patch.object(client.time, "sleep") as sleep:
            self.assertTrue(c.post("report", {"jobId": "j"})["ok"])
        self.assertEqual(len(self.http.server.requests), 2)  # type: ignore[attr-defined]
        self.assertEqual(sleep.call_count, 1)

    def test_409_is_stale_and_never_retried(self) -> None:
        self.http.queue(409, b'{"error":"stale claim"}')
        c = client.AdexClient(self.http.base_url, self.secret, allow_local=True)
        with mock.patch.object(client.time, "sleep") as sleep:
            with self.assertRaises(client.StaleClaimError):
                c.post("report", {"jobId": "j"})
        self.assertEqual(len(self.http.server.requests), 1)  # type: ignore[attr-defined]
        sleep.assert_not_called()

    def test_redirect_is_rejected(self) -> None:
        self.http.queue(302, b"", {"Location": self.http.base_url + "/else"})
        c = client.AdexClient(self.http.base_url, self.secret, allow_local=True)
        with self.assertRaises(client.WorkerError):
            c.post("claim", {})
        self.assertEqual(len(self.http.server.requests), 1)  # type: ignore[attr-defined]

    def test_environment_proxy_is_ignored_for_local_contract(self) -> None:
        c = client.AdexClient(self.http.base_url, self.secret, allow_local=True)
        with mock.patch.dict(os.environ, {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
        }):
            self.assertTrue(c.post("claim", {})["ok"])

    def test_dns_is_resolved_once_and_socket_uses_that_ip(self) -> None:
        c = client.AdexClient(
            f"http://localhost:{self.http.server.server_port}",
            self.secret,
            allow_local=True,
        )
        real_connect = socket.socket.connect
        connected: list[tuple[str, int]] = []

        def record_connect(sock, address):
            connected.append(address)
            return real_connect(sock, address)

        with mock.patch.object(
            client.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", self.http.server.server_port))],
        ) as resolver, mock.patch.object(client.socket.socket, "connect", record_connect):
            self.assertTrue(c.post("claim", {})["ok"])
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(connected[0][0], "127.0.0.1")

    def test_private_dns_result_is_rejected(self) -> None:
        with mock.patch.object(
            client.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.2", 443))],
        ):
            with self.assertRaises(client.WorkerError):
                client.validate_url("https://example.com")

    def test_tls_uses_original_hostname_with_pinned_ip(self) -> None:
        context = mock.Mock()
        validated = client._ValidatedURL("https://example.com", "example.com", 443, ("93.184.216.34",), True)
        connection = client._PinnedHTTPSConnection("example.com", validated=validated, context=context)
        sock = mock.Mock()
        with mock.patch.object(client, "_connect_validated", return_value=sock) as connect:
            connection.connect()
        self.assertEqual(connect.call_args.args[:2], (("93.184.216.34",), 443))
        context.wrap_socket.assert_called_once_with(sock, server_hostname="example.com")

    def test_ark_create_does_not_retry_paid_submission(self) -> None:
        ark = client.ArkClient("key", "https://ark.example", "model")
        with mock.patch.object(client, "request_json", side_effect=client.RetryableError("network")) as request:
            with self.assertRaises(client.RetryableError):
                ark.create("prompt", "9:16", 3, None)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("retry", request.call_args.kwargs)

    def test_incomplete_read_is_retryable(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                raise http.client.IncompleteRead(b"{")

        opener = mock.Mock()
        opener.open.return_value = Response()
        validated = client._ValidatedURL("http://example", "example", 80, ("93.184.216.34",), False)
        with mock.patch.object(client, "_validate_url", return_value=validated), mock.patch.object(
            client, "_build_opener", return_value=opener
        ), mock.patch.object(client.time, "sleep"):
            with self.assertRaises(client.RetryableError):
                client.request_json("http://example", {}, {}, retry=True)
        self.assertEqual(opener.open.call_count, 3)


if __name__ == "__main__":
    unittest.main()
