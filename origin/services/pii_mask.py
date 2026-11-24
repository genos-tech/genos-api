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

from collections.abc import Iterator

STARS = "***"

_LOCAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DOMAIN_CHARS = _LABEL_CHARS | {"."}
_ADDRESS_MAX = 254


def _domain_end(text: str, start: int) -> int | None:
    """Where the domain that begins at `start` ends, or None if there is none.

    A domain is dot-separated labels of letters, digits and `-`, and its last
    label starts with two or more letters (the top-level domain). The match
    ends after those letters, so `x@b.com1` is the address `x@b.com` followed
    by a stray `1`. The longest such domain wins.
    """
    stop = start
    while stop < len(text) and text[stop] in _DOMAIN_CHARS:
        stop += 1
    # Labels up to the first empty one (`..`, a leading or a trailing dot).
    offsets = []
    pos = start
    for label in text[start:stop].split("."):
        if not label:
            break
        offsets.append(pos)
        pos += len(label) + 1
    # The first label is the host part; the top-level domain is a later one.
    for first in reversed(offsets[1:]):
        end = first
        while end < stop and text[end] in _LETTERS:
            end += 1
        if end - first >= 2:
            return end
    return None


def _addresses(text: str) -> Iterator[tuple[int, int, int]]:
    """`(local_start, at, end)` of each address in `text`, left to right.

    Linear in the length of the text, whatever it holds: each `@` looks back
    over its own run of local-part characters and forward over its own run of
    domain characters, and neither run crosses another `@`. It is a hand-written
    scan and not a regular expression because this runs on labels that
    visitors influence, and a regex over `%%%%...` was flagged as polynomial.
    The local part is the whole run before the `@` (an address is not taken
    from the middle of a run) and addresses do not overlap.
    """
    done = 0
    at = text.find("@")
    while at != -1:
        start = at
        while start > 0 and text[start - 1] in _LOCAL_CHARS:
            start -= 1
        if done <= start < at:
            end = _domain_end(text, at + 1)
            if end is not None:
                yield start, at, end
                done = end
        at = text.find("@", at + 1)


def mask_email(text: str) -> str:
    """`text` with every email address masked. Text with none is returned
    unchanged, so it is safe to run over a label that is a project name, an
    API-key label or a `provider:id`."""
    if not text or "@" not in text:
        return text
    # Always the same number of stars, so the label doesn't reveal how
    # long the local part was. A one-character local part keeps its
    # character: nothing else could be shown, and `***@x.com` would make
    # two different senders look the same.
    out = []
    last = 0
    for start, at, end in _addresses(text):
        out.append(text[last:start])
        out.append(f"{text[start]}{STARS}{text[at:end]}")
        last = end
    out.append(text[last:])
    return "".join(out)


def is_address(text: str) -> bool:
    """Whether the whole of `text` is one email address, nothing around it.

    The same shape `mask_email` masks, checked without a regex: this runs on text a
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
    return bool(text) and "@" in text and next(_addresses(text), None) is not None
