"""Mask secrets and email addresses in every log line (MIP-622).

Logs leave our systems: Railway keeps them, and anything it keeps can be
read without breaching us. So the formatters run their finished line,
traceback included, through `redact()`. That catches a value however it got
there: an f-string, `%r` of a dict, a third-party exception's message, a
header someone dumped while debugging.

It masks values by their shape, so it's a net, not the fix. Call sites
still shouldn't log an email or content in the first place; this only
stops the ones nobody caught. What it masks:

  * email addresses, plain or URL-encoded;
  * JWTs (three base64url segments, the first starting `eyJ`);
  * Genos API keys (`gnos_…`) and the providers' prefixed keys;
  * the credential after `Bearer` / `ApiKey` / `Basic`;
  * the value of a `name=…` / `name: …` / `"name": "…"` pair whose name
    says it's secret (`password`, `token`, `secret`, `authorization`, …);
  * any other run of 32+ base64url characters mixing upper case, lower
    case and digits: what a generated key looks like, and what a path,
    a snake_case name, a UUID or a hex hash never does;
  * the token in an unsubscribe link (`unsubscribe/<token>/`): a signed,
    never-expiring payload that holds the user's id, and that Django and the
    throttles write whenever the path of a refused request is logged
    (MIP-1788). Rewritten to the route's template, so the line still says which
    route was hit.

A client address is not masked by shape (a version number looks the same):
call sites do not log one. Where a line needs to tell callers apart,
`ip_marker` is a short keyed hash.

genos-sockets has a copy of this module and genos-collab a port of it; all
three are tested against the same cases in `log_redaction_cases.json`.
"""

from __future__ import annotations

import logging
import re

MASK = "[REDACTED]"
EMAIL_MASK = "[EMAIL]"

_SECRET_NAMES = (
    r"(?:access|refresh|id|auth|api|csrf|session)?[_-]?token"
    r"|password|passwd|secret|client[_-]?secret|api[_-]?key|apikey"
    r"|authorization|cookie|private[_-]?key|signature"
)

# A run of letters and nothing else: prose, not a credential.
_PLAIN_WORD = r"[A-Za-z]+(?![A-Za-z0-9._~+/=-])"

_SCHEME = re.compile(
    # Not a plain word after it ("Bearer authentication failed").
    r"\b(Bearer|ApiKey|Basic)(\s+)(?!" + _PLAIN_WORD + r")[A-Za-z0-9._~+/=-]{12,}",
    re.IGNORECASE,
)

_QUOTED = r""""(?!\[REDACTED\])[^"]*"|'(?!\[REDACTED\])[^']*'"""
_BARE = r"""(?!\[REDACTED\]|(?:Bearer|ApiKey|Basic)\s)[^\s"'&,;}\]]+"""
_NAME = r"""["']?\b(?:""" + _SECRET_NAMES + r""")\b["']?"""

# A quoted value after `=` or `:` is always a value.
_NAMED_QUOTED = re.compile(r"(" + _NAME + r"\s*[:=]\s*)(?:" + _QUOTED + ")", re.IGNORECASE)
# A bare one only in the shapes logs and URLs use. `token=abc` (a query
# string, kwargs, `key=%s`), but not `token = make()`: a traceback's source
# line. `secret: abc`, but not `api_key:<id>` (a type:id label) or
# "missing secret: set X" (prose).
_NAMED_BARE = re.compile(
    r"(" + _NAME + r"=)" + _BARE + r"|(" + _NAME + r":\s+)(?!" + _PLAIN_WORD + r")" + _BARE,
    re.IGNORECASE,
)

# `/api/v2/email/unsubscribe/<signed token>/`. The token is base64url text with
# `:` between its parts; anything up to the next `/`, query, quote or space.
_UNSUBSCRIBE = re.compile(r"(/unsubscribe/)(?!<token>)[^/\s?#\"'<>]+")

# `/h/<signed token>`: an HTML note's page URL (MIP-2228). The token is the page's
# capability for a few minutes, so it never goes to a log; the route stays visible.
_HTML_PAGE = re.compile(r"(/h/)(?!<token>)[^/\s?#\"'<>]+")

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")

_PREFIXED = re.compile(
    r"\b(?:(?:gnos|ghp|gho|ghu|ghs|ghr|github_pat|sk|rk|whsec|xox[abprs]|aks|vci)[_-]"
    r"[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{30,})"
)

_EMAIL = re.compile(
    # Only from the start of a run: unanchored, a long run with no `@` (a
    # base64 image in a payload) is retried from every position, which
    # took 50 s on 160k characters.
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+(?:@|%40)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

_LONG = re.compile(
    r"(?<![A-Za-z0-9_+-])"
    r"(?=[A-Za-z0-9_+-]*[0-9])(?=[A-Za-z0-9_+-]*[a-z])(?=[A-Za-z0-9_+-]*[A-Z])"
    r"[A-Za-z0-9_+-]{32,}={0,2}"
)


def _mask_named(match: re.Match) -> str:
    name = next(group for group in match.groups() if group is not None)
    value = match.group(0)[len(name) :]
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{name}{quote}{MASK}{quote}"


def redact(text: str) -> str:
    """`text` with every secret- or email-shaped substring masked."""
    if not text:
        return text
    # The scheme first, so the rules after it see `Bearer [REDACTED]`
    # rather than half a token.
    text = _SCHEME.sub(rf"\1\2{MASK}", text)
    text = _NAMED_QUOTED.sub(_mask_named, text)
    text = _NAMED_BARE.sub(_mask_named, text)
    text = _JWT.sub(MASK, text)
    text = _PREFIXED.sub(MASK, text)
    text = _EMAIL.sub(EMAIL_MASK, text)
    return _LONG.sub(MASK, text)


def redact_line(text: str) -> str:
    """What the formatters call: `redact`, after the rewrite only this API needs.

    The unsubscribe token goes first, to the route's template, so the line still
    says which route was hit. It is kept out of `redact` because that function,
    and its cases in `log_redaction_cases.json`, are shared with genos-sockets and
    genos-collab, which have no such route."""
    if not text:
        return text
    return redact(_HTML_PAGE.sub(r"\1<token>", _UNSUBSCRIBE.sub(r"\1<token>", text)))


def ip_marker(address: str | None) -> str:
    """Ten hex characters that stand for a client address in a log line.

    A keyed hash (`salted_hmac`, so its key is derived from `SECRET_KEY` and is not
    the key itself), so the line says "the same caller again" or "a different one"
    without holding the address, and the marker cannot be turned back into one by
    hashing all of IPv4 (MIP-1788). It changes if the key is rotated, which is
    fine: it is for reading a burst, not for joining logs."""
    from django.utils.crypto import salted_hmac  # noqa: PLC0415 — needs settings only when called

    if not address:
        return "-"
    return salted_hmac("origin.log_ip_marker", str(address), algorithm="sha256").hexdigest()[:10]


class RedactingFormatter(logging.Formatter):
    """The plain text formatter, with the finished line redacted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_line(super().format(record))
