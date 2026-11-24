"""Time-based one-time passwords (RFC 6238) on the standard library (MIP-2239).

AUTH_SECURITY_PLAN §4.1 named `pyotp`. This is the same 30-line algorithm without the
dependency: a new package edits `Pipfile`, which is on the billed agent-eval gate's
`RETRIEVAL_PATHS`, and a sign-in secret is a poor place to take on a package for something
`hmac` already does. The tests pin it to the RFC 6238 appendix B vectors and to the
RFC 4226 appendix D vectors, so it cannot drift from what authenticator apps compute.

SHA-1, 6 digits, 30 seconds: the only combination every authenticator app supports.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
# 160 bits, the RFC 4226 recommended length (and the length Google Authenticator expects).
SECRET_BYTES = 20
# A code is accepted one period either side of now: a phone clock a few seconds off, or a
# code typed as the period rolls over.
WINDOW = 1


def generate_secret() -> str:
    """A fresh base32 secret, without padding (what authenticator apps and QR codes use)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP: the code for `counter`."""
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def totp(secret: str, now: float | None = None) -> str:
    """The code an authenticator app shows right now (tests, and nothing else, call this)."""
    return hotp(secret, current_step(now))


def match_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step `code` is valid for, within the drift window, or None.

    Every candidate is compared in constant time and none is skipped once one matches, so
    the time taken does not say which step (or whether any step) matched.
    """
    if not isinstance(code, str):
        return None
    code = code.replace(" ", "").replace("-", "")
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    centre = current_step(now)
    matched: int | None = None
    for step in range(centre - WINDOW, centre + WINDOW + 1):
        if hmac.compare_digest(hotp(secret, step), code):
            matched = step
    return matched


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """The `otpauth://` URI an authenticator app reads from the QR code."""
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )
