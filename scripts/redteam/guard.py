"""The only way the red-team harness sends a request (MIP-2267).

Every request goes through `LocalClient`, which refuses any target that is not plain http(s) on
`localhost` / `127.0.0.1` / `::1`, refuses credentials embedded in the URL, lookalike hosts
(`127.0.0.1.evil.example`, `localhost.example`, `0x7f.0.0.1`), and never follows a redirect (a
local server answering 302 to a public host must not move the run off the machine).

Standard library only, so it runs in any python:3.11 container next to the target.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class NotLocal(ValueError):
    """The URL is not the local target."""


def check_local(url: str) -> str:
    """Return `url` if it is a local http(s) URL; raise NotLocal otherwise."""
    if not isinstance(url, str) or not url:
        raise NotLocal("empty url")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise NotLocal(f"scheme {parts.scheme!r} refused")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise NotLocal("credentials in the url refused")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise NotLocal(f"host {host!r} is not local")
    try:
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError as exc:
        raise NotLocal(f"bad port: {exc}") from None
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # surface the 3xx to the caller instead of following it


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


class LocalClient:
    """A tiny http client bound to one local base URL."""

    def __init__(self, base: str, timeout: float = 30.0):
        self.base = check_local(base).rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))

    def request(
        self,
        method,
        path,
        *,
        token=None,
        auth=None,
        json_body=None,
        data=None,
        headers=None,
        query=None,
    ):
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = self.base + (path if path.startswith("/") else "/" + path)
        if query:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode(query, doseq=True)
        check_local(url)
        hdrs = {"Accept": "application/json", "User-Agent": "genos-redteam-local"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        if auth:
            hdrs["Authorization"] = auth
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = data if isinstance(data, bytes) else data.encode()
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=body, method=method.upper(), headers=hdrs)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read() if exc.fp else b"", dict(exc.headers or {}))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            return Response(0, str(exc).encode(), {})
