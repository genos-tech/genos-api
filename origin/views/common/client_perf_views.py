"""Interaction timings the browser measured (MIP-957).

The server can time a request, not what the person waited for: the time
from a click to the first paint, to the page going quiet, how long the
main thread was blocked on the way. The browser measures those and posts
them here in small batches, and each accepted sample becomes ONE log line
on `origin.perf`, beside the server's own `perf.request` lines (MIP-956),
so the two can be read against each other.

Contract:

    POST /api/v2/client-perf/

    {"samples": [{"kind": "task_switch", "paint_ms": 120, "settle_ms": 1500,
                  "blocked_ms": 900, "longest_ms": 250, "requests": 11,
                  "window_ms": 3200}],
     "ctx": {"cores": 8, "homes_mounted": 2, "viewport_w": 1500,
             "build": "abc1234"}}

    Response (202): {"accepted": <n>, "dropped": <m>}

    perf.client kind=task_switch paint_ms=120 settle_ms=1500 blocked_ms=900 longest_ms=250 requests=11 window_ms=3200 cores=8 homes=2 vw=1500 build=abc1234

What is accepted, and what is not:

  * `kind` must be one of `KINDS`; a sample with another kind, one that is
    not an object, or one with a number that is negative or not a number
    (a string, a boolean, a list) is DROPPED and counted in `dropped`. It
    is not an error: a client one release ahead of the server sends a kind
    this one has not heard of, and must not be told off for it.
  * Numbers are whole milliseconds or counts: floats are rounded, and
    anything above 600000 is clamped to it. `null` and a missing field
    both mean "nothing to report" and are logged as `-`.
  * `ctx` is optional and describes the page the batch came from. Its
    numbers are clamped (`cores` 0..256, `homes_mounted` 0..3,
    `viewport_w` 0..20000) and `build` keeps only `[A-Za-z0-9._-]`, at
    most 40 characters. One `ctx` applies to every sample in the batch.
  * More than 20 samples, a body that is not a JSON object, a missing or
    non-list `samples`, or a body over 8 KB is a 400. The size is read
    from Content-Length before anything parses the body, which is also
    before authentication, so an oversized body costs nothing to refuse.

It touches no tenant data, so it needs no team or project scope: it is
`signed_in` in `origin/tests/route_access.py`. It writes nothing to the
database, and the log line carries none of the request except the fields
above: no user, no team, no path, no header. Every value is a number, an
allow-listed word or a sanitised token, so a line cannot be forged by
what a client sends. Signed in only, and rate limited per user
(`ClientPerfThrottle`, `THROTTLE_CLIENT_PERF`, 30/min).
"""

from __future__ import annotations

import logging
import math
import re

from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser
from rest_framework.response import Response

from origin.services.throttles import ClientPerfThrottle, with_defaults
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

log = logging.getLogger("origin.perf")

KINDS = frozenset(
    {
        "task_open",
        "task_switch",
        "note_open",
        "note_switch",
        "project_switch",
        "service_switch",
        "idle_minute",
    }
)
SAMPLE_FIELDS = (
    "paint_ms",
    "settle_ms",
    "blocked_ms",
    "longest_ms",
    "requests",
    "window_ms",
)
CTX_LIMITS = {"cores": 256, "homes_mounted": 3, "viewport_w": 20000}

MAX_SAMPLES = 20
MAX_BODY_BYTES = 8 * 1024
MAX_VALUE = 600_000
MAX_BUILD_CHARS = 40

_BUILD_JUNK = re.compile(r"[^A-Za-z0-9._-]")
_DROP = object()


def _is_number(value) -> bool:
    """A JSON number. `True` is an `int` to Python and not one to JSON."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not (isinstance(value, float) and not math.isfinite(value))


def _sample_number(value):
    """`None` for null, `_DROP` for anything that is not a number of
    milliseconds or a count (the whole sample is then dropped), else the
    whole number, at most `MAX_VALUE`."""
    if value is None:
        return None
    if not _is_number(value) or value < 0:
        return _DROP
    return min(round(value), MAX_VALUE)


def _ctx_number(value, upper: int):
    """A page-context number clamped to 0..`upper`; `None` when it is not
    one. A bad `ctx` field costs the batch nothing, only that field."""
    if not _is_number(value):
        return None
    return max(0, min(round(value), upper))


def _sample(raw) -> dict | None:
    """The sample's fields, or None when it is to be dropped."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    # `isinstance` first: an unhashable kind (a list) must not raise on `in`.
    if not isinstance(kind, str) or kind not in KINDS:
        return None
    sample = {"kind": kind}
    for field in SAMPLE_FIELDS:
        value = _sample_number(raw.get(field))
        if value is _DROP:
            return None
        sample[field] = value
    return sample


def _ctx(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    ctx = {key: _ctx_number(raw.get(key), upper) for key, upper in CTX_LIMITS.items()}
    build = raw.get("build")
    ctx["build"] = None
    if isinstance(build, str):
        # Strip first, then cut: 40 characters of what is left.
        ctx["build"] = _BUILD_JUNK.sub("", build)[:MAX_BUILD_CHARS] or None
    return ctx


def _text(value) -> str:
    return "-" if value is None else str(value)


def _log_sample(sample: dict, ctx: dict) -> None:
    log.info(
        "perf.client kind=%s paint_ms=%s settle_ms=%s blocked_ms=%s longest_ms=%s "
        "requests=%s window_ms=%s cores=%s homes=%s vw=%s build=%s",
        sample["kind"],
        _text(sample["paint_ms"]),
        _text(sample["settle_ms"]),
        _text(sample["blocked_ms"]),
        _text(sample["longest_ms"]),
        _text(sample["requests"]),
        _text(sample["window_ms"]),
        _text(ctx["cores"]),
        _text(ctx["homes_mounted"]),
        _text(ctx["viewport_w"]),
        _text(ctx["build"]),
    )


def _declared_length(request) -> int:
    """The body's Content-Length. Django reads no more than this from the
    socket, so it bounds what parsing can cost; no header reads as empty."""
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return 0


def _bad_request(message: str) -> ParseError:
    return ParseError({"error": message})


class ClientPerfView(AuthenticatedAPIView):
    parser_classes = [JSONParser]
    throttle_classes = with_defaults(ClientPerfThrottle)

    def initial(self, request, *args, **kwargs):
        # Before `super().initial()`, which parses the body (the team
        # alignment looks in it), so an oversized one is never read.
        if _declared_length(request) > MAX_BODY_BYTES:
            raise _bad_request(f"Request body is over {MAX_BODY_BYTES} bytes")
        super().initial(request, *args, **kwargs)

    def post(self, request):
        body = request.data
        if not isinstance(body, dict):
            raise _bad_request("Body must be a JSON object")
        samples = body.get("samples")
        if not isinstance(samples, list):
            raise _bad_request("samples must be a list")
        if len(samples) > MAX_SAMPLES:
            raise _bad_request(f"At most {MAX_SAMPLES} samples per request")

        ctx = _ctx(body.get("ctx"))
        accepted = [sample for sample in map(_sample, samples) if sample is not None]
        for sample in accepted:
            _log_sample(sample, ctx)
        return Response(
            {"accepted": len(accepted), "dropped": len(samples) - len(accepted)},
            status=status.HTTP_202_ACCEPTED,
        )
