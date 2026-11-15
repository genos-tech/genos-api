"""How big a request the MCP endpoint reads (MIP-1367, phase 1).

Nothing in Genos set a request body limit for MCP. Django's own
`DATA_UPLOAD_MAX_MEMORY_SIZE` (2,621,440 bytes) applied, and above it the answer was an HTML
"Bad Request (400)" plus a `RequestDataTooBig` ERROR traceback in the logs: opaque to a model or
a person, and noisy for whoever watches the logs. This module makes the limit explicit and the
refusal readable.

The check runs in `McpView.dispatch`, BEFORE authentication and before anything parses the body,
and it looks at the `Content-Length` header only: the body is never read to measure it.

* `MCP_MAX_REQUEST_BYTES` defaults to 2,621,440, today's limit, so a request that worked before
  works the same and one that failed before still fails, now as HTTP 413 with a JSON-RPC error
  (`-32600`, `id: null`) that says what to do. The env var can LOWER the limit. Raising it above
  Django's setting changes nothing: the effective limit is the smaller of the two, because Django
  would refuse the body anyway.
* A POST with `Transfer-Encoding` and no `Content-Length` (a chunked body) is answered 411. Django's
  WSGI request reads a body only as far as `Content-Length` says, so a chunked body arrives as an
  empty one and the call used to be dropped without a word (an authenticated `tools/call` answered
  202, as for a notification). Nothing of it is read, so it cannot use memory, but it must not be
  ignored silently either. No client of ours sends one: they all send a `Content-Length`.
* `ECHO_ID_MAX_BYTES`: a refusal that has to echo the JSON-RPC `id` (the 401 for no credential, the
  503 when MCP is off) used to parse the whole body for it, so an anonymous 1 MB body was parsed
  for nothing. Above this size the id is not echoed (`null`): no real call is that large before it
  authenticates.

Logs carry sizes only, never content, an address or a credential.
"""

from __future__ import annotations

import json
import logging
import os

from django.conf import settings
from django.http import HttpResponse

from origin.views.public.mcp import protocol

log = logging.getLogger(__name__)

DEFAULT_MAX_REQUEST_BYTES = 2_621_440  # Django's DATA_UPLOAD_MAX_MEMORY_SIZE default: 2.5 MiB.
ECHO_ID_MAX_BYTES = 64 * 1024


def _env_int(name: str, default: int) -> int:
    """A positive integer from the environment; anything else is the default (a typo must
    not switch the guard off or make every request too large)."""
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


MCP_MAX_REQUEST_BYTES = _env_int("MCP_MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES)


def declared_length(request) -> int | None:
    """The `Content-Length` the client declared, or None when absent or not a number.
    Django treats those the same way (as no body)."""
    raw = request.META.get("CONTENT_LENGTH")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def effective_limit() -> int:
    """The smaller of ours and Django's: above Django's the body is refused regardless."""
    limit = MCP_MAX_REQUEST_BYTES
    django_limit = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", None)
    if django_limit is not None:
        limit = min(limit, django_limit)
    return limit


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):g} MiB"
    if n >= 1024:
        return f"{n / 1024:g} KiB"
    return f"{n} bytes"


def too_big_to_echo_id(request) -> bool:
    """True when the body is too large to parse just to read an `id` out of it."""
    length = declared_length(request)
    return length is not None and length > ECHO_ID_MAX_BYTES


def _refusal(status: int, message: str) -> HttpResponse:
    error = protocol.JsonRpcError(protocol.INVALID_REQUEST, message, http_status=status)
    return HttpResponse(
        json.dumps(error.as_response(None), separators=(",", ":")),
        status=status,
        content_type="application/json",
    )


def refusal_for(request) -> HttpResponse | None:
    """The answer for a request that must not be read, or None to carry on."""
    if request.method != "POST":
        return None
    length = declared_length(request)
    if length is None:
        if request.META.get("HTTP_TRANSFER_ENCODING"):
            log.info("mcp.request_refused reason=no_content_length")
            return _refusal(
                411,
                "The request body has no Content-Length (it was sent chunked). "
                "Send the whole request as one body with a Content-Length header.",
            )
        return None
    limit = effective_limit()
    if length <= limit:
        return None
    log.info("mcp.request_refused reason=too_large bytes=%d limit=%d", length, limit)
    return _refusal(
        413,
        f"The request is {length} bytes; the limit is {human_size(limit)}. "
        "Send less text per call.",
    )
