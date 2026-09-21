"""Small HTTP clients. Never retry a paid submission or follow credential redirects."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class WorkerError(Exception):
    pass


class RetryableError(WorkerError):
    pass


class StaleClaimError(WorkerError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class _ValidatedURL:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]
    https: bool
    # Preserve the URL spelling for TLS SNI/certificate checks while ``host``
    # remains normalized for connection bookkeeping.
    server_hostname: str | None = None


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_TRANSIENT_EXCEPTIONS = (
    urllib.error.URLError,
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    http.client.HTTPException,
    TimeoutError,
    OSError,
)


def _resolve_addresses(host: str, port: int, *, local: bool) -> tuple[str, ...]:
    """Resolve once and return the exact IP literals permitted for connection.

    ``urllib`` is deliberately never allowed to resolve this hostname again;
    the returned tuple is passed to the pinned connection classes below.
    """

    if host in {"127.0.0.1", "::1"}:
        if not local:
            raise WorkerError("Private/non-global network destinations are forbidden")
        return (host,)
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise RetryableError("DNS resolution failed") from exc
    addresses: list[str] = []
    for info in infos:
        try:
            candidate = str(info[4][0])
            ip = ipaddress.ip_address(candidate)
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise WorkerError("DNS returned an invalid address") from exc
        if local:
            permitted = ip.is_loopback
        else:
            permitted = ip.is_global
        if not permitted:
            raise WorkerError("Private/non-global network destinations are forbidden")
        if candidate not in addresses:
            addresses.append(candidate)
    if not addresses:
        raise RetryableError("DNS resolution returned no usable addresses")
    return tuple(addresses)


def _validate_url(url: str, *, allow_local: bool = False) -> _ValidatedURL:
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url):
        raise WorkerError("Invalid URL")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise WorkerError("Invalid URL") from exc
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise WorkerError("Invalid URL")
    host = parsed.hostname.casefold().rstrip(".")
    local = allow_local and host in _LOOPBACK_HOSTS
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise WorkerError("HTTPS required (HTTP only for explicitly enabled loopback tests)")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise WorkerError("Invalid URL port") from exc
    addresses = _resolve_addresses(host, port, local=local)
    return _ValidatedURL(
        url=url,
        host=host,
        port=port,
        addresses=addresses,
        https=parsed.scheme == "https",
        server_hostname=parsed.hostname,
    )


def validate_url(url: str, *, allow_local: bool = False) -> str:
    _validate_url(url, allow_local=allow_local)
    return url


def _connect_validated(
    addresses: tuple[str, ...],
    port: int,
    timeout: float,
    source_address=None,
) -> socket.socket:
    errors: list[OSError] = []
    for address in addresses:
        sock = None
        try:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            timeout_value = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
            sock.settimeout(timeout_value)
            if source_address is not None:
                sock.bind(source_address)
            # connect() receives the already-validated numeric IP directly;
            # unlike socket.create_connection, it cannot trigger a second DNS
            # lookup that could return a rebinding target.
            sock.connect((address, port))
            return sock
        except OSError as exc:
            if sock is not None:
                sock.close()
            errors.append(exc)
    if errors:
        raise OSError("all validated addresses failed") from errors[-1]
    raise OSError("no validated addresses")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, *, validated: _ValidatedURL, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                 source_address=None, **kwargs):
        super().__init__(host, port, timeout=timeout, source_address=source_address, **kwargs)
        self._validated = validated

    def connect(self):
        self.sock = _connect_validated(
            self._validated.addresses,
            self.port,
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, *, validated: _ValidatedURL,
                 context: ssl.SSLContext, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                 source_address=None, **kwargs):
        super().__init__(
            host,
            port,
            timeout=timeout,
            source_address=source_address,
            context=context,
            **kwargs,
        )
        self._validated = validated

    def connect(self):
        self.sock = _connect_validated(
            self._validated.addresses,
            self.port,
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()
        # Keep the original hostname for both SNI and certificate validation;
        # only the TCP peer is pinned to the already-verified IP literal.
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self._validated.server_hostname or self.host,
        )


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, validated: _ValidatedURL):
        super().__init__()
        self.validated = validated

    def http_open(self, req):
        validated = self.validated

        def factory(_host, **kwargs):
            return _PinnedHTTPConnection(validated.host, validated.port, validated=validated, **kwargs)

        return self.do_open(factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, validated: _ValidatedURL):
        context = ssl.create_default_context()
        super().__init__(context=context)
        self.validated = validated
        self.context = context

    def https_open(self, req):
        validated = self.validated

        def factory(_host, **kwargs):
            kwargs.pop("context", None)
            return _PinnedHTTPSConnection(
                validated.host,
                validated.port,
                validated=validated,
                context=self.context,
                **kwargs,
            )

        return self.do_open(factory, req, context=self.context)


def _build_opener(validated: _ValidatedURL):
    # An explicit empty ProxyHandler prevents HTTP(S)_PROXY/ALL_PROXY from
    # changing the destination between validation and connection.
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if validated.https:
        handlers.append(_PinnedHTTPSHandler(validated))
    else:
        handlers.append(_PinnedHTTPHandler(validated))
    return urllib.request.build_opener(*handlers)


def request_json(url: str, payload: dict | None, headers: dict, *, retry: bool = False,
                 allow_local: bool = False) -> dict:
    validated = _validate_url(url, allow_local=allow_local)
    opener = _build_opener(validated)
    data = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    for attempt in range(3 if retry else 1):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with opener.open(req, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise WorkerError("JSON response too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise WorkerError("Expected JSON object")
                return result
        except urllib.error.HTTPError as exc:
            # Do not log provider bodies: they can contain URLs, prompts or credentials.
            exc.close()
            if exc.code == 409:
                raise StaleClaimError("Claim conflict; stop without overwriting state") from exc
            if exc.code not in (408, 429, 500, 502, 503, 504):
                raise WorkerError(f"HTTP request rejected ({exc.code})") from exc
            error = RetryableError(f"Transient HTTP failure ({exc.code})")
        except _TRANSIENT_EXCEPTIONS as exc:
            error = RetryableError(f"Transport failure ({type(exc).__name__})")
        except (ValueError, UnicodeError) as exc:
            raise RetryableError("Invalid JSON response; outcome may be unknown") from exc
        if not retry or attempt == 2:
            raise error
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def download(url: str, dest: Path, *, max_bytes: int = 100 * 1024 * 1024,
             allow_local: bool = False) -> None:
    validated = _validate_url(url, allow_local=allow_local)
    opener = _build_opener(validated)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkerError("Unable to create media destination") from exc
    partial = dest.with_suffix(".partial")
    try:
        # Redirects are intentionally refused, including redirects to metadata services.
        with opener.open(url, timeout=60) as response:
            total = 0
            with partial.open("wb") as out:
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise WorkerError("Media download exceeds limit")
                    out.write(chunk)
        if total == 0:
            raise WorkerError("Empty media download")
        partial.replace(dest)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise WorkerError(f"Media download rejected ({exc.code})") from exc
    except _TRANSIENT_EXCEPTIONS as exc:
        raise RetryableError("Media download failed") from exc
    finally:
        partial.unlink(missing_ok=True)


class AdexClient:
    def __init__(self, base_url: str, secret: str, *, allow_local: bool = False):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.allow_local = allow_local

    def signature(self, text: str) -> dict:
        stamp = str(int(time.time()))
        digest = hmac.new(self.secret.encode(), f"{stamp}:{text}".encode(), hashlib.sha256).hexdigest()
        return {"x-adex-timestamp": stamp, "x-adex-signature": "sha256=" + digest}

    def post(self, action: str, payload: dict, *, retry: bool = True) -> dict:
        body = json.dumps(payload, separators=(",", ":"))
        return request_json(self.base_url + "/api/worker/remix-jobs/" + action, payload,
                            {**self.signature(body), "Content-Type": "application/json"},
                            retry=retry, allow_local=self.allow_local)

    def claim(self, job_id: str | None = None) -> dict | None:
        payload = {"protocolVersion": 2}
        if job_id:
            payload["jobId"] = job_id
        return self.post("claim", payload, retry=False).get("job")

    def report(self, job: dict, **fields) -> dict:
        return self.post("report", {"jobId": job["id"], "claimToken": job["claimToken"],
                                    "protocolVersion": 2, **fields})

    def upload(self, job: dict, path: Path, *, purpose: str = "output", index: int = 0) -> str:
        if purpose not in {"output", "clip", "reference"}:
            raise WorkerError("Invalid upload purpose")
        if purpose != "output" and (isinstance(index, bool) or not isinstance(index, int) or index < 0 or index > 255):
            raise WorkerError("Invalid upload index")
        cap = 50 * 1024 * 1024 if purpose == "reference" else 100 * 1024 * 1024
        try:
            if path.stat().st_size > cap:
                raise WorkerError("Upload exceeds limit")
            data = path.read_bytes()
        except WorkerError:
            raise
        except OSError as exc:
            raise WorkerError("Unable to read upload") from exc
        digest = hashlib.sha256(data).hexdigest()
        body = f"{job['id']}:{job['claimToken']}:{digest}"
        params = {"jobId": job["id"]}
        if purpose != "output":
            params.update(purpose=purpose, index=str(index))
            body = f"{job['id']}:{job['claimToken']}:{purpose}:{index}:{digest}"
        url = self.base_url + "/api/worker/remix-jobs/upload?" + urllib.parse.urlencode(params)
        validated = _validate_url(url, allow_local=self.allow_local)
        opener = _build_opener(validated)
        # Same bytes/path are safe to retry while this attempt holds the lease.
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method="POST", headers={
                **self.signature(body), "Content-Type": "video/mp4",
                "x-adex-claim-token": job["claimToken"], "x-adex-content-sha256": digest,
            })
            try:
                with opener.open(req, timeout=180) as response:
                    raw = response.read(65537)
                    if len(raw) > 65536:
                        raise RetryableError("Upload response too large")
                    parsed = json.loads(raw)
                    value = parsed.get("fileUrl") if isinstance(parsed, dict) else None
                    if not isinstance(value, str) or not value:
                        raise RetryableError("Upload response missing fileUrl")
                    return value
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code == 409:
                    raise StaleClaimError("Upload lost its claim") from exc
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise WorkerError(f"Upload rejected ({exc.code})") from exc
            except _TRANSIENT_EXCEPTIONS + (ValueError, RetryableError):
                pass
            if attempt < 2:
                time.sleep(2 ** attempt)
        raise RetryableError("Upload could not be confirmed")


class ArkClient:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}

    def create(self, prompt: str, ratio: str, seconds: int, reference: str | None) -> dict:
        content = [{"type": "text", "text": prompt}]
        if reference:
            validate_url(reference)
            content.append({"type": "video_url", "video_url": {"url": reference}, "role": "reference_video"})
        return request_json(self.base_url + "/contents/generations/tasks", {
            "model": self.model, "content": content, "ratio": ratio, "duration": seconds,
            "resolution": "720p", "generate_audio": False, "watermark": False,
        }, self.headers)  # Deliberately no retries: submission is billable.

    def get(self, task_id: str) -> dict:
        return request_json(self.base_url + "/contents/generations/tasks/" + urllib.parse.quote(task_id, safe=""),
                            None, self.headers, retry=True)
