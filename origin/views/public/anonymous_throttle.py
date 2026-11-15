"""A per-address limit on MCP requests that carry no valid credential (MIP-1367, phase 1).

`PublicApiThrottle` counts per credential, so a request with no credential, or with one that is
refused, was never counted at all: 150 anonymous POSTs and 150 with a malformed `ApiKey` all got
their 401 in under a second, each one costing a header parse and, for a key-shaped value, a hash
and a lookup. This is the cap on that.

**What counts.** A request whose authentication FAILED (`NotAuthenticated` or
`AuthenticationFailed`), counted when the 401 is built (`McpView.handle_exception`). Nothing else:

* a request that authenticates is never counted here, whatever address it comes from. Claude's own
  connectors and a company's proxy put many people behind one address, so counting every request
  per address would let one person's runaway client lock their colleagues out. Only the failures
  are counted, so the only thing that can exhaust a bucket is requests that were going to be
  refused anyway.
* the OAuth endpoints have their own limits (`oauth_views.py`) and are other views.
* DRF would not run a throttle for an anonymous request at all (authentication and permission
  refuse it first), which is why the count is made where the refusal is.

**The rate.** `THROTTLE_MCP_ANONYMOUS`, default 300 per minute per address. A client doing
everything right makes ONE refused call (`WWW-Authenticate` points it at the OAuth metadata), then
signs in, then calls with a token; one that is refreshing an expired token makes one more. Even
an address shared by a whole office, or by a connector's egress, stays far below 300 a minute in
refused calls, and the token endpoint a refusal sends clients to is itself capped at 120 a minute.
It sits next to the class, not in `apis/settings.py`, because a `settings.py` edit starts the
Vertex-billed agent evals on a PR.

**The address.** `BaseThrottle.get_ident`, as every throttle here resolves it: behind Railway
`NUM_PROXIES = 1` makes it the LAST `X-Forwarded-For` entry, the one the proxy wrote, so a
client-supplied entry (earlier in the list) can neither pick another person's bucket nor move
itself out of its own. An IPv6 address is bucketed by its /64, because one subscriber holds a
whole /64 and could otherwise start a fresh bucket for every request.

**Failure.** A fixed window with one `INCR` per refused request. With the cache unreachable it
allows (and says so in the log), like the other availability limits: this is a limit on noise, not
a security control (see `PublicApiThrottle`); the key hash is the control.

Only a count and a scope are logged, never the address, a header or a body.
"""

from __future__ import annotations

import ipaddress
import os
import time

from rest_framework.throttling import BaseThrottle

from origin.services.throttles import (
    CACHE_ERRORS,
    WindowRateThrottle,
    _cache_down,
    _record_denial,
    parse_rate,
)

CATEGORY = "mcp_anonymous"
DEFAULT_RATE = "300/min"


def _configured_rate() -> str:
    """`THROTTLE_MCP_ANONYMOUS`, or the default when it is not a rate (a typo in a variable
    must not take the 401 down with it)."""
    raw = os.environ.get("THROTTLE_MCP_ANONYMOUS", "").strip()
    try:
        parse_rate(raw)
    except (ValueError, KeyError, IndexError):
        return DEFAULT_RATE
    return raw


def bucket_for(request) -> str:
    """The address this request counts under: see "The address" above."""
    raw = (BaseThrottle().get_ident(request) or "").strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return "unknown"
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class McpAnonymousThrottle(WindowRateThrottle):
    scope = CATEGORY
    rate = _configured_rate()

    def allow_request(self, request, view=None):
        ident = bucket_for(request)
        now = time.time()
        window = int(now // self.duration)
        key = f"throttle:{self.scope}:{ident}:{window}"
        self._cache_was_down = False
        try:
            count = self._hit(key, 1)
        except CACHE_ERRORS as exc:
            return _cache_down(self, request, exc)
        if count <= self.num_requests:
            return True
        self._wait = (window + 1) * self.duration - now
        _record_denial(self.scope, request, "anonymous")
        return False
