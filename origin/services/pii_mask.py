"""Mask the email addresses in a short label, for rows that outlive the user.

`mask_email("kenji@gmail.com")` is `k***@gmail.com`: the first character
of the local part, three stars, and the whole domain. An admin reading
the audit log can still tell whose action it was in most cases and which
provider, but a database dump no longer hands over a list of addresses
(MIP-765).

This is for labels, not for logs: `apis.log_redaction.redact` replaces an
email with `[EMAIL]` and also rewrites anything key-shaped, which would
mangle the API-key labels the audit log writes on purpose (`ci (gnos_ab12)`).
It also has no way to keep the first character.

A leaf module on purpose: migration 0207 imports it, and a migration must
not depend on settings, models or middleware that may change after it.
"""

from __future__ import annotations

import re

# One definition of "an email" for the whole label: an address inside other
# text (`GitHub: k@x.com`) is masked in place, the text around it is kept.
# Anchored at the start of a run, so a long run with no `@` isn't retried
# from every position (see the same note in apis.log_redaction).
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"(?P<local>[A-Za-z0-9._%+-]+)@(?P<domain>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
)

STARS = "***"


def _mask_one(match: re.Match) -> str:
    # Always the same number of stars, so the label doesn't reveal how
    # long the local part was. A one-character local part keeps its
    # character: nothing else could be shown, and `***@x.com` would make
    # two different senders look the same.
    return f"{match.group('local')[0]}{STARS}@{match.group('domain')}"


def mask_email(text: str) -> str:
    """`text` with every email address masked. Text with none is returned
    unchanged, so it is safe to run over a label that is a project name, an
    API-key label or a `provider:id`."""
    if not text or "@" not in text:
        return text
    return _EMAIL.sub(_mask_one, text)


_LOCAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
_ADDRESS_MAX = 254


def is_address(text: str) -> bool:
    """Whether the whole of `text` is one email address, nothing around it.

    The same shape `_EMAIL` masks, checked without a regex: this runs on text a
    visitor typed before signing in, so its cost must not depend on how the text
    is built (a regex over `%%%%…` was flagged as polynomial)."""
    if not text or len(text) > _ADDRESS_MAX or text.count("@") != 1:
        return False
    local, domain = text.split("@")
    labels = domain.split(".")
    tld = labels[-1]
    return (
        bool(local)
        and all(c in _LOCAL_CHARS for c in local)
        and len(labels) >= 2
        and all(label and all(c in _LABEL_CHARS for c in label) for label in labels)
        and len(tld) >= 2
        and tld.isascii()
        and tld.isalpha()
    )


def has_email(text: str) -> bool:
    """Whether `text` still holds an unmasked email address.

    A masked one (`k***@gmail.com`) is not matched: `*` is outside the
    local-part class, so masking is idempotent and this is the whole check
    the backfill needs to find the rows left to do.
    """
    return bool(text) and "@" in text and _EMAIL.search(text) is not None
