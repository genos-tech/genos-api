"""A reference for each request, shared by its log lines and its error answer.

An error answer can't carry its exception: the text names tables,
columns, Stripe and provider internals and the values that broke them.
`error_response` logs that text with the request's reference and hands
the client the reference instead, so a user who reports "it says
ref 3f2a…" leads straight to the log line (MIP-622).

`RequestIdMiddleware` sets the reference before any view runs; the JSON
log formatter stamps it on every line as `request_id`. Work a request
hands to another thread (the agent's streaming worker) gets it with
`bind_request_id`.

Imported by the log formatter, which Django loads before the apps, so
the Django and DRF imports below are deferred.
"""

from __future__ import annotations

import contextvars
import logging
import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rest_framework.response import Response

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

log = logging.getLogger("origin.errors")

GENERIC_MESSAGE = "Something went wrong on our side."

# Google's error status ("RESOURCE_EXHAUSTED", "UNAVAILABLE"): a code
# word, unlike the message beside it.
_STATUS_WORD = re.compile(r"[A-Z][A-Z_]{2,40}")


def current_request_id() -> str | None:
    return _request_id.get()


def bind_request_id(request_id: str | None):
    """Carry a request's reference onto a thread it started; returns the
    token for `_request_id.reset`. A bare thread starts with no context."""
    return _request_id.set(request_id)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def describe_exception(exc: BaseException) -> str:
    """An exception's type, HTTP status and status word ("429
    RESOURCE_EXHAUSTED"): what a client or a model may hear about a
    failure, and what the eval runner reads to tell an outage from a bad
    answer. Not its text, which can be a provider's response body, a SQL
    statement or an email address."""
    parts = [type(exc).__name__]
    response = getattr(exc, "response", None)
    for status in (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(response, "status_code", None),
    ):
        if isinstance(status, int) and not isinstance(status, bool):
            parts.append(str(status))
            break
    word = getattr(exc, "status", None)
    if isinstance(word, str) and _STATUS_WORD.fullmatch(word):
        parts.append(word)
    return " ".join(parts)


def describe_upstream(resp) -> str:
    """A third-party HTTP failure for the log: the status and the error's
    code word (Google's `status`/`reason`, GitHub's documentation slug),
    never its body, which quotes our request back (MIP-622)."""
    code = ""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — not JSON, nothing to name
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("status") or ""
            errors = err.get("errors")
            if not code and isinstance(errors, list) and errors and isinstance(errors[0], dict):
                code = errors[0].get("reason") or ""
        elif isinstance(err, str):
            code = err
        if not code:
            url = body.get("documentation_url")
            if isinstance(url, str):
                code = url.rstrip("/").rsplit("/", 1)[-1].split("#")[-1]
    code = str(code)[:60] if isinstance(code, (str, int)) else ""
    return f"status={resp.status_code} code={code}" if code else f"status={resp.status_code}"


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


class RequestIdMiddleware:
    """Give each request its reference, and say it in the `X-Request-ID`
    response header."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = new_request_id()
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-ID"] = request_id
        return response


def log_failure(what: str, exc: BaseException | None = None, *, level: int = logging.ERROR) -> str:
    """Log `what` (and `exc`'s traceback) under this request's reference,
    and return the reference."""
    reference = current_request_id() or new_request_id()
    log.log(level, "%s (ref %s)", what, reference, exc_info=exc)
    return reference


def error_response(
    what: str,
    exc: BaseException | None = None,
    *,
    status: int = 500,
    message: str = GENERIC_MESSAGE,
    level: int = logging.ERROR,
) -> Response:
    """An error answer that says nothing about `exc` but where to find it.

    `message` is for the user, so it must be text we wrote."""
    from rest_framework.response import Response  # noqa: PLC0415, F811

    reference = log_failure(what, exc, level=level)
    return Response({"error": message, "reference": reference}, status=status)


def server_error(request, *args, **kwargs):
    """`handler500`: the unhandled exception is already logged by
    `django.request`, under this reference; Django's own page would be
    HTML, and in DEBUG a traceback."""
    from django.http import JsonResponse  # noqa: PLC0415

    reference = current_request_id() or new_request_id()
    return JsonResponse({"error": GENERIC_MESSAGE, "reference": reference}, status=500)
