"""Outbound requests to URLs a user typed (MIP-717).

Two call sites take a destination from a user: webhook delivery
(`webhook_delivery.py`) and the link-title fetch behind task links
(`views/utils/extract_page_title_view.py`). Every other outbound call
targets a host we chose. A user-chosen URL is a request made from inside
the private network — Postgres, Redis, OpenSearch (no auth today), the
sockets and collab services, a cloud metadata endpoint — so both sites
share this one guard.

## What counts as public

`is_public_ip` wants `is_global`, which refuses ranges a list of
`is_private` / `is_loopback` / … misses: 100.64.0.0/10 (shared / CGNAT
space, used for internal networks by some hosts) was the one the webhook
guard let through. Site-local `fec0::/10` is deprecated but not private
to the stdlib, so it is refused by name. IPv6 forms that carry an IPv4
address (`::ffff:…`, NAT64 `64:ff9b::…`, 6to4 `2002:…`, Teredo) are all
private or reserved to the stdlib already; the tests list each one, so a
Python release that classified them differently would fail CI.

## Why the fetch connects to an IP, not to the URL

Checking a hostname and then handing the URL to `requests` resolves it
twice. A name with a short TTL can answer publicly for the check and
privately for the connection (DNS rebinding). `fetch_public` resolves
once, checks every address, and connects to the checked IP itself,
sending the real name as Host and as the TLS SNI and certificate name.
Redirects are followed by hand, each hop checked the same way.

`post_public` does the same for a webhook delivery, which POSTs a signed body
and must not follow redirects at all. Before MIP-819 the webhook path checked
the host (`is_public_host`) and then handed the URL to `requests.post`, which
resolved it a second time: exactly the gap above, and the alert CodeQL raised
on `webhook_delivery.py` (`py/full-ssrf`).
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3

ALLOWED_SCHEMES = ("http", "https")
# Only the web's own ports. Anything else on a public host is far more
# often a service than a page, and a port scanner is what this guards
# against.
ALLOWED_PORTS = {"http": 80, "https": 443}


class OutboundUrlError(ValueError):
    """The URL is not a legal outbound destination."""


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


def resolve_public(host: str) -> list[str]:
    """Every address `host` resolves to, or `OutboundUrlError`.

    ALL of them must be public, not any: a name with a public and a
    private record would otherwise be connected to whichever the
    resolver happened to list first.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise OutboundUrlError("The host does not resolve.")
    addresses: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise OutboundUrlError("The host does not resolve to a public address.")
        if not is_public_ip(ip):
            raise OutboundUrlError("The host does not resolve to a public address.")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise OutboundUrlError("The host does not resolve.")
    return addresses


def is_public_host(host: str) -> bool:
    try:
        resolve_public(host)
    except OutboundUrlError:
        return False
    return True


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int
    path: str


def _parse(url: str, schemes: tuple[str, ...]) -> _Target:
    if not isinstance(url, str) or not url:
        raise OutboundUrlError("A URL is required.")
    if len(url) > 2048:
        raise OutboundUrlError("The URL is too long.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise OutboundUrlError("The URL is not valid.")
    scheme = parts.scheme.lower()
    if scheme not in schemes:
        raise OutboundUrlError(f"Only {' and '.join(schemes)} URLs are allowed.")
    if not parts.hostname:
        raise OutboundUrlError("The URL has no host.")
    try:
        # The ASCII (IDNA) form is what DNS, SNI and Host all want.
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise OutboundUrlError("The URL's host is not valid.")
    if parts.username or parts.password:
        raise OutboundUrlError("The URL must not contain credentials.")
    if port is not None and port != ALLOWED_PORTS[scheme]:
        raise OutboundUrlError("Only the standard web ports are allowed.")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return _Target(scheme, host, ALLOWED_PORTS[scheme], path)


def _pinned_pool(target: _Target, deadline: float) -> tuple[urllib3.HTTPConnectionPool, str]:
    """A connection pool pinned to ONE checked address, and the Host header.

    Resolves `target.host` once, requires every answer to be public, and
    connects to the first. The certificate is still verified against the
    name the user typed (SNI and hostname both), so pinning the address does
    not weaken TLS. Shared by `fetch_public` and `post_public` so the two
    cannot drift apart on the part that matters.
    """
    ip = resolve_public(target.host)[0]
    host_header = target.host if ":" not in target.host else f"[{target.host}]"
    remaining = max(deadline - time.monotonic(), 0.1)
    common = dict(
        host=ip,
        port=target.port,
        timeout=urllib3.Timeout(connect=remaining, read=remaining),
        retries=False,
        maxsize=1,
    )
    if target.scheme == "https":
        pool: urllib3.HTTPConnectionPool = urllib3.HTTPSConnectionPool(
            **common,
            # The connection goes to the checked IP; the certificate
            # is still verified against the name the user typed.
            server_hostname=target.host,
            assert_hostname=target.host,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
        )
    else:
        pool = urllib3.HTTPConnectionPool(**common)
    return pool, host_header


@dataclass(frozen=True)
class FetchResult:
    url: str  # the final URL, after redirects
    status: int
    content_type: str
    body: bytes  # at most `max_bytes`; the rest is never read


def fetch_public(
    url: str,
    *,
    timeout: float = 5.0,
    max_bytes: int = 512 * 1024,
    max_redirects: int = 3,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """GET a user-supplied URL without letting it reach a private address.

    `timeout` bounds the whole call, redirects and body included, not
    each socket read: a server dribbling a byte per read timeout would
    otherwise hold a worker indefinitely.

    Raises `OutboundUrlError` for a refused destination and
    `urllib3.exceptions.HTTPError` (or `OSError`) for a network failure.
    The messages of the former are safe to show the user; the latter
    are not, so callers log them and answer generically.
    """
    deadline = time.monotonic() + timeout
    for _ in range(max_redirects + 1):
        target = _parse(url, ALLOWED_SCHEMES)
        pool, host_header = _pinned_pool(target, deadline)
        try:
            resp = pool.urlopen(
                "GET",
                target.path,
                headers={**(headers or {}), "Host": host_header},
                redirect=False,
                preload_content=False,
                assert_same_host=False,
            )
            try:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise OutboundUrlError("The page redirected nowhere.")
                    url = urljoin(url, location)
                    continue
                body = b""
                while len(body) < max_bytes:
                    if time.monotonic() > deadline:
                        raise OutboundUrlError("The page took too long to load.")
                    chunk = resp.read(min(16384, max_bytes - len(body)), decode_content=True)
                    if not chunk:
                        break
                    body += chunk
                return FetchResult(
                    url=url,
                    status=resp.status,
                    content_type=resp.headers.get("Content-Type", ""),
                    body=body,
                )
            finally:
                resp.release_conn()
                resp.close()
        finally:
            pool.close()
    raise OutboundUrlError("The page redirected too many times.")


def post_public(
    url: str,
    body: bytes,
    *,
    headers: dict[str, str],
    timeout: float = 10.0,
) -> int:
    """POST `body` to a user-supplied https URL; returns the HTTP status.

    For webhook delivery. The destination is resolved ONCE, every address must
    be public, and the connection goes to that checked address (certificate
    verified against the typed name), so a hostname that answers publicly for a
    check and privately for a connection cannot reach the private network.

    **Redirects are not followed.** A 3xx is returned as a status, not a hop: a
    redirect to a private address would defeat every check above, and a webhook
    endpoint that moved should be updated by its owner.

    https only, and the standard port only, as for `fetch_public`. The reply is
    never read: only the status matters, and a customer's endpoint must not be
    able to make a worker buffer an unbounded body.

    Raises `OutboundUrlError` for a refused destination (its message is safe to
    show the person who configured the webhook) and `urllib3` / `OSError` for a
    network failure (log it; do not show it).
    """
    deadline = time.monotonic() + timeout
    target = _parse(url, ("https",))
    pool, host_header = _pinned_pool(target, deadline)
    try:
        resp = pool.urlopen(
            "POST",
            target.path,
            body=body,
            headers={**headers, "Host": host_header},
            redirect=False,
            preload_content=False,
            assert_same_host=False,
        )
        try:
            return resp.status
        finally:
            resp.release_conn()
            resp.close()
    finally:
        pool.close()
