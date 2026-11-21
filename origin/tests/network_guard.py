"""Fail the test that opens a real network connection, by name (MIP-1776).

Why: while stress-running the pre-fix polluter of MIP-1760 the log showed
`httpx: HTTP Request: POST https://api.openai.com/v1/embeddings "401 Unauthorized"` many times: a
mock was missing, so the test called out. It only answered 401 because the test environment carries
no key. Nothing stopped the connection itself (a test could equally POST to the live dev `sockets`
container).

What it does: `socket.socket.connect` and `connect_ex` are wrapped. While a Django test case runs, a
connection to an address that is not allowed raises `NetworkGuardError` naming the test and the
address (`api.openai.com:443`, the host name the test asked for, not just the IP). Code that
swallows the error (`except Exception`, a retry, a fallback) does not hide it: the attempt is also
recorded, and the test is failed at its end, as the MIP-1765 tripwire does.

Allowed (ALLOWED_* below, the one place to change):
  * loopback (127.0.0.0/8, ::1) and unix sockets: CI services and in-process servers;
  * the Redis and OpenSearch hosts the settings point at (`settings.REDIS_URL`,
    `settings.SEARCH_ENGINE["OPENSEARCH_HOST"]`, which default to the compose names `redis` and
    `opensearch`), which on the dev stack are containers, not loopback.
  Postgres needs no entry: libpq opens that connection in C, not through Python's `socket`.

Scope (what it does NOT see): a connection made outside a test (module import, `setUpClass`, the
runner), by C extensions (libpq), by a subprocess, or a bare DNS lookup (`getaddrinfo`); only Django
test cases (`SimpleTestCase` and subclasses), like the patch-leak tripwire.

If it fails a test of yours: mock the call (`mock.patch(...)`) instead of reaching the network. If
the test must reach a service on the dev stack, add that host to ALLOWED_HOSTS with a reason.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from urllib.parse import urlsplit

from django.conf import settings
from django.test import SimpleTestCase

# The services the tests use on purpose, as `reason: function returning the configured host`. The
# host comes from the settings (which hold the compose-name defaults when the environment does not
# set them), so a run with no REDIS_URL/OPENSEARCH_HOST exported is covered.
ALLOWED_HOSTS = {
    "cache/session tests run against the stack's Redis": lambda: urlsplit(settings.REDIS_URL).hostname,
    "search tests run against the stack's OpenSearch": lambda: settings.SEARCH_ENGINE.get(
        "OPENSEARCH_HOST"
    ),
}

_current_test = None  # the test case running now (set by the hook); None = outside a test
_violations = None  # the running test's list of refused addresses
_names = {}  # ip -> host name asked for, filled by the getaddrinfo wrapper
_allowed_ips = None  # resolved on first use


class NetworkGuardError(AssertionError):
    pass


def _allowed():
    """IPs of the allowed hosts (resolved once, with the real getaddrinfo)."""
    global _allowed_ips
    if _allowed_ips is None:
        ips = set()
        for host_of in ALLOWED_HOSTS.values():
            host = host_of()
            if not host:
                continue
            try:
                ips.update(info[4][0] for info in _real_getaddrinfo(host, None))
            except OSError:
                pass  # not resolvable: nothing to allow
        _allowed_ips = ips
    return _allowed_ips


def _is_allowed(ip):
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False  # not an IP address: refuse
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped  # ::ffff:127.0.0.1
    if addr.is_loopback:
        return True
    return ip in _allowed()


def _check(address):
    """Raise if `address` (the argument of `connect`) may not be reached."""
    test, found = _current_test, _violations  # read together: a thread may outlive its test
    if test is None or found is None or not isinstance(address, tuple) or len(address) < 2:
        return  # outside a test, or AF_UNIX (a path) / another family
    ip, port = address[0], address[1]
    if _is_allowed(ip):
        return
    shown = f"{_names.get(ip, ip)}:{port}"
    if shown != f"{ip}:{port}":
        shown += f" ({ip})"
    found.append(shown)
    raise NetworkGuardError(
        f"{test.id()} tried to open a real network connection to {shown} "
        "(MIP-1776). Mock the call. If this test must reach a dev-stack service, add its host to "
        "ALLOWED_HOSTS in origin/tests/network_guard.py."
    )


_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _getaddrinfo(host, *args, **kwargs):
    infos = _real_getaddrinfo(host, *args, **kwargs)
    if isinstance(host, str):  # remember the name behind each IP so the error can show it
        for info in infos:
            _names[info[4][0]] = host
    return infos


def _connect(self, address):
    _check(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(address)
    return _real_connect_ex(self, address)


def install():
    """Wrap `socket` once and hook every Django test case. Safe to call again."""
    if getattr(SimpleTestCase, "_network_guard", False):
        return
    socket.getaddrinfo = _getaddrinfo
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    setup_and_call = SimpleTestCase._setup_and_call

    def _setup_and_call(self, result, debug=False):
        global _current_test, _violations
        outer = _current_test, _violations  # a test case run inside a test (this guard's own tests)
        found = []
        _current_test, _violations = self, found
        try:
            setup_and_call(self, result, debug)
        finally:
            _current_test, _violations = outer
            if found:
                error = NetworkGuardError(
                    f"{self.id()} tried to open a real network connection to "
                    f"{', '.join(sorted(set(found)))} (MIP-1776). The error may have been "
                    "swallowed by the code under test; mock the call, or add the host to "
                    "ALLOWED_HOSTS in origin/tests/network_guard.py if the test must reach "
                    "a dev-stack service."
                )
                if debug:
                    if sys.exc_info()[0] is None:
                        raise error
                else:
                    try:
                        raise error
                    except NetworkGuardError:
                        result.addFailure(self, sys.exc_info())

    SimpleTestCase._setup_and_call = _setup_and_call
    SimpleTestCase._network_guard = True
