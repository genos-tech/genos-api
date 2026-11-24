"""Password rules shared by signup and reset (MIP-2238).

Two parts:

* `PwnedPasswordValidator`: a Django password validator that asks the
  Have I Been Pwned range API whether the password is in a known breach. It
  uses k-anonymity: only the first 5 hex characters of the password's SHA-1
  leave the server, never the password, the full hash, the email or any other
  personal data. The reply (every suffix sharing that prefix) is compared here.
  It is OFF unless `PASSWORD_BREACH_CHECK` is on, and it **fails open**: a
  timeout, an error status or a network failure logs one line and accepts the
  password, because a third-party outage must never stop anyone signing up.

* `check_password_policy` / `rejection_payload`: run Django's whole
  `AUTH_PASSWORD_VALIDATORS` chain and turn a refusal into stable machine codes,
  so the frontend can show the message in the member's own language.
"""

import hashlib
import logging

import requests
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/"
HIBP_TIMEOUT_SECONDS = 2.0
BREACHED_CODE = "password_breached"

# Codes the frontend has a message for. Anything else is shown as "too common".
KNOWN_CODES = (
    "password_too_short",
    "password_too_common",
    "password_entirely_numeric",
    "password_too_similar",
    BREACHED_CODE,
)


class PwnedPasswordValidator:
    """Refuse a password that appears in a known breach (k-anonymity lookup)."""

    def validate(self, password, user=None):
        if not getattr(settings, "PASSWORD_BREACH_CHECK", False):
            return
        digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        prefix, suffix = digest[:5], digest[5:]
        try:
            resp = requests.get(
                HIBP_RANGE_URL + prefix,
                timeout=HIBP_TIMEOUT_SECONDS,
                # Asks the service to pad replies so their size does not hint at the prefix.
                headers={"Add-Padding": "true", "User-Agent": "genos-password-check"},
                allow_redirects=False,
            )
            if resp.status_code != 200:
                logger.warning("Breached-password check skipped: HTTP %s", resp.status_code)
                return
            body = resp.text
        except Exception as exc:  # fail open: type only, never the password or the hash
            logger.warning("Breached-password check skipped: %s", type(exc).__name__)
            return
        for line in body.splitlines():
            found, _, count = line.partition(":")
            # A padded reply carries fake rows with a count of 0: they are not matches.
            if found.strip().upper() == suffix and count.strip() not in ("", "0"):
                raise ValidationError(
                    "This password has appeared in a data breach. Choose another.",
                    code=BREACHED_CODE,
                )

    def get_help_text(self):
        return "Your password must not have appeared in a known data breach."


def check_password_policy(password, user=None) -> list[tuple[str, str]]:
    """Run every configured validator. Returns (code, English text) per refusal ([] = accepted)."""
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        return [(e.code or "password_too_common", " ".join(e.messages)) for e in exc.error_list]
    return []


def public_code(code: str) -> str:
    """A code the frontend has a message for (the field's own `min_length` is 'too short')."""
    if code == "min_length":
        return "password_too_short"
    return code if code in KNOWN_CODES else "password_too_common"


def rejection_payload(errors) -> dict:
    """Response fields for a refused password: a stable code, the reasons as codes, and the
    English text. `errors` are DRF `ErrorDetail`s (they carry `.code`)."""
    return {
        "code": "password_rejected",
        "password_errors": [public_code(getattr(e, "code", "")) for e in errors],
    }
