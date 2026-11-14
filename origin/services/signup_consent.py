"""Explicit consent to the Terms of Service and Privacy Policy at signup.

A new account is only created through the signup view (email) or the first
OAuth sign-in when the person agreed on the signup screen. The server records
when, and which text they agreed to, as evidence: `CustomUser.terms_accepted_at`
and `CustomUser.terms_version`.

Only those two creation paths enforce this. Accounts the server makes itself
(demo visitor, project system users, management commands) and rows tests build
with `create_user` are not people signing up, so they carry no acceptance.
"""

from __future__ import annotations

from django.utils import timezone

# The text the person agreed to. Bump it when the Terms or the Privacy Policy
# change in a way that needs fresh acceptance; accounts keep the version they
# accepted, so the record stays true. The value is the server's, never the
# client's: a client cannot claim to have accepted a text we did not show.
TERMS_VERSION = "2026-11-draft"

# Stable machine-readable code for a refused signup. The frontend keys its
# message off this string, so renaming it is a breaking API change.
TERMS_NOT_ACCEPTED_CODE = "terms_not_accepted"

# The OAuth callback reports failures to the frontend as `?error=<reason>`.
TERMS_REQUIRED_OAUTH_REASON = "terms_required"

_TRUE_STRINGS = {"true", "1"}


def is_accepted(value) -> bool:
    """Whether a request's `accepted_terms` is an explicit yes.

    Only JSON `true` and the strings "true"/"1" (form-encoded bodies) count.
    Missing, false, null, "false", "0", "", 0 and anything else do not: a
    consent is never inferred from a value that merely exists.
    """
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in _TRUE_STRINGS


def acceptance_fields(accepted_at=None, version=None) -> dict:
    """The `CustomUser` fields that record an acceptance.

    Defaults to now and the current `TERMS_VERSION`. The OAuth flow passes
    the moment and version carried through the provider round trip instead.
    """
    return {
        "terms_accepted_at": accepted_at or timezone.now(),
        "terms_version": version or TERMS_VERSION,
    }
