"""Two-factor sign-in with an authenticator app (MIP-2239, AUTH_SECURITY_PLAN §4).

Opt-in, for password sign-ins only. The rules this module keeps, each one a trap the plan
names:

**The second factor is checked before any token exists.** `POST /signin/` with a right
password for a person who has two-factor on answers `mfa_required` and a *pending token*,
and sets no cookie and mints no access or refresh token. `POST /signin/mfa/` then takes the
pending token and a code, and only that mints the pair.

**The pending token authenticates nothing.** It is not a JWT: it is a `django.core.signing`
blob (`payload:timestamp:signature`) under its own salt, so `JWTAuthentication` cannot parse
it and no endpoint accepts it as a bearer token. A JWT with a `purpose` claim would be one
missing check away from a full session; this cannot be, because nothing reads it but
`read_pending_token`. It lives five minutes, and it dies with the password it was issued for.

**Wrong codes lock the account, not the address.** Rotating IPs does not help a guesser. Five
wrong second factors in a row lock the row for 15 minutes, and each further wrong one doubles
the lock (24 hours at most); a right one clears it. While locked, nothing is checked at all, so
a locked row gives a guesser no answer. A code is accepted once: the last used time step is
kept and an equal or earlier step is a replay.

**Recovery codes** are ten single-use codes, hashed, shown once. Spending one is a single
`UPDATE ... WHERE used_at IS NULL`, so two requests cannot both spend it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.utils import timezone

from origin.models.common.mfa_models import (
    RECOVERY_CODE_COUNT,
    RECOVERY_CODE_LENGTH,
    MFARecoveryCode,
    UserMFA,
    generate_recovery_code,
    hash_recovery_code,
    normalize_recovery_code,
)
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.user_models import CustomUser
from origin.services import crypto, totp
from origin.services.security_events import record_security_event

logger = logging.getLogger(__name__)

ISSUER = "Genos"
LOCK_AFTER = 5
LOCK_BASE = timedelta(minutes=15)
LOCK_MAX = timedelta(hours=24)
PENDING_MAX_AGE_SECONDS = 300
_PENDING_SALT = "origin.mfa.pending.v1"


class MfaError(Exception):
    """Base for what a caller turns into an answer."""


class NotEligible(MfaError):
    """This account signs in without a password (Google, GitHub), so it has no first factor."""


class AlreadyEnabled(MfaError):
    pass


class NotEnabled(MfaError):
    pass


class NoSetupInProgress(MfaError):
    pass


class InvalidCode(MfaError):
    pass


class Locked(MfaError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("locked")
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Status:
    enabled: bool
    eligible: bool
    enabled_at: object | None
    recovery_codes_remaining: int


def is_eligible(user: CustomUser) -> bool:
    """Two-factor is for accounts that sign in with a password."""
    return bool(user.has_usable_password()) and user.primary_auth_provider == "email"


def _confirmed(user) -> UserMFA | None:
    return UserMFA.objects.filter(user=user, confirmed_at__isnull=False).first()


def is_enabled(user) -> bool:
    return UserMFA.objects.filter(user=user, confirmed_at__isnull=False).exists()


def status_for(user) -> Status:
    mfa = _confirmed(user)
    remaining = (
        MFARecoveryCode.objects.filter(user=user, used_at__isnull=True).count() if mfa else 0
    )
    return Status(
        enabled=mfa is not None,
        eligible=is_eligible(user),
        enabled_at=mfa.confirmed_at if mfa else None,
        recovery_codes_remaining=remaining,
    )


def _lock_duration(failed_attempts: int) -> timedelta:
    doublings = max(0, failed_attempts - LOCK_AFTER)
    return min(LOCK_BASE * (2 ** min(doublings, 12)), LOCK_MAX)


def _retry_after(mfa: UserMFA, now) -> int:
    return max(1, int((mfa.locked_until - now).total_seconds()))


def _fail(mfa: UserMFA, now) -> None:
    """Count one wrong second factor and lock when it is the fifth (or later) in a row."""
    mfa.failed_attempts += 1
    if mfa.failed_attempts >= LOCK_AFTER:
        mfa.locked_until = now + _lock_duration(mfa.failed_attempts)
    mfa.save(update_fields=["failed_attempts", "locked_until", "ts_updated_at"])


def _succeed(mfa: UserMFA, *also_changed: str) -> None:
    """A right second factor: clear the count and the lock, and save what the caller set."""
    mfa.failed_attempts = 0
    mfa.locked_until = None
    mfa.save(update_fields=["failed_attempts", "locked_until", "ts_updated_at", *also_changed])


def _new_recovery_codes(user) -> list[str]:
    """Replace every recovery code with a fresh set. Returns the plaintext, once."""
    MFARecoveryCode.objects.filter(user=user).delete()
    codes = [generate_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    MFARecoveryCode.objects.bulk_create(
        [MFARecoveryCode(user=user, code_hash=hash_recovery_code(c)) for c in codes]
    )
    return codes


# --------------------------------------------------------------------------- #
# Setting up                                                                  #
# --------------------------------------------------------------------------- #


def begin_setup(user: CustomUser) -> tuple[str, str]:
    """Start (or restart) setup. Returns (secret, otpauth URI), shown once, to the QR code.

    The row stays unconfirmed, so nothing about signing in changes until `confirm_setup`.
    A restart keeps the failure count, so starting over does not clear a lock.
    """
    if not is_eligible(user):
        raise NotEligible
    secret = totp.generate_secret()
    with transaction.atomic():
        mfa = UserMFA.objects.select_for_update().filter(user=user).first()
        if mfa is not None and mfa.is_enabled:
            raise AlreadyEnabled
        if mfa is None:
            UserMFA.objects.create(user=user, secret_encrypted=crypto.encrypt(secret))
        else:
            mfa.secret_encrypted = crypto.encrypt(secret)
            mfa.save(update_fields=["secret_encrypted", "ts_updated_at"])
    return secret, totp.provisioning_uri(secret, user.email, ISSUER)


def confirm_setup(user: CustomUser, code: str, *, request=None) -> list[str]:
    """Turn two-factor on after a first valid code. Returns the recovery codes, once."""
    refused: str | None = None
    codes: list[str] = []
    with transaction.atomic():
        mfa = UserMFA.objects.select_for_update().filter(user=user).first()
        if mfa is None:
            raise NoSetupInProgress
        if mfa.is_enabled:
            raise AlreadyEnabled
        now = timezone.now()
        if mfa.locked_until and mfa.locked_until > now:
            raise Locked(_retry_after(mfa, now))
        step = totp.match_step(crypto.decrypt(mfa.secret_encrypted), code)
        if step is None:
            _fail(mfa, now)
            refused = "setup_code"
        else:
            mfa.confirmed_at = now
            mfa.last_used_step = step
            _succeed(mfa, "confirmed_at", "last_used_step")
            codes = _new_recovery_codes(user)
    if refused:
        record_security_event(
            SecurityAction.MFA_FAILED,
            request=request,
            actor=user,
            metadata={"stage": refused},
        )
        raise InvalidCode
    record_security_event(
        SecurityAction.MFA_ENABLED, request=request, actor=user, metadata={"method": "totp"}
    )
    return codes


def disable(user: CustomUser, *, request=None) -> None:
    """Turn two-factor off and forget the secret and every recovery code."""
    with transaction.atomic():
        mfa = UserMFA.objects.select_for_update().filter(user=user, confirmed_at__isnull=False)
        if not mfa.exists():
            raise NotEnabled
        MFARecoveryCode.objects.filter(user=user).delete()
        UserMFA.objects.filter(user=user).delete()
    record_security_event(SecurityAction.MFA_DISABLED, request=request, actor=user)


def regenerate_recovery_codes(user: CustomUser, *, request=None) -> list[str]:
    with transaction.atomic():
        if (
            not UserMFA.objects.select_for_update()
            .filter(user=user, confirmed_at__isnull=False)
            .exists()
        ):
            raise NotEnabled
        codes = _new_recovery_codes(user)
    record_security_event(SecurityAction.MFA_RECOVERY_REGENERATED, request=request, actor=user)
    return codes


# --------------------------------------------------------------------------- #
# Signing in                                                                  #
# --------------------------------------------------------------------------- #


def issue_pending_token(user: CustomUser) -> str:
    """The proof that the password step passed. Not a JWT, and good for nothing else."""
    return signing.dumps(
        {"u": str(user.pk), "f": _password_fingerprint(user)},
        salt=_PENDING_SALT,
        compress=False,
    )


def _password_fingerprint(user) -> str:
    """A keyed digest of the stored password verifier, so the token dies when it changes."""
    return signing.salted_hmac(_PENDING_SALT, user.password or "").hexdigest()[:24]


def read_pending_token(token) -> CustomUser | None:
    """The user a live pending token was issued for, or None.

    None for a malformed, forged, expired or foreign-salt token, for a user who is gone or
    inactive or no longer has two-factor on, and for one whose password changed since.
    """
    if not isinstance(token, str) or not token or len(token) > 1024:
        return None
    try:
        payload = signing.loads(token, salt=_PENDING_SALT, max_age=PENDING_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    user = CustomUser.objects.filter(pk=payload.get("u")).first()
    if user is None or not user.is_active:
        return None
    if payload.get("f") != _password_fingerprint(user):
        return None
    if not is_enabled(user):
        return None
    return user


def verify_second_factor(user: CustomUser, code: str, *, request=None) -> str:
    """Accept an authenticator code or a recovery code. Returns "totp" or "recovery".

    Raises `Locked` (nothing was checked), `InvalidCode` (counted, and locks at the fifth in a
    row), or `NotEnabled`.
    """
    method: str | None = None
    with transaction.atomic():
        mfa = (
            UserMFA.objects.select_for_update()
            .filter(user=user, confirmed_at__isnull=False)
            .first()
        )
        if mfa is None:
            raise NotEnabled
        now = timezone.now()
        if mfa.locked_until and mfa.locked_until > now:
            raise Locked(_retry_after(mfa, now))
        if not isinstance(code, str):
            code = ""
        normalized = normalize_recovery_code(code)
        if len(normalized) == RECOVERY_CODE_LENGTH:
            spent = MFARecoveryCode.objects.filter(
                user=user, code_hash=hash_recovery_code(normalized), used_at__isnull=True
            ).update(used_at=now)
            if spent == 1:
                method = "recovery"
        else:
            step = totp.match_step(crypto.decrypt(mfa.secret_encrypted), code)
            if step is not None and step > mfa.last_used_step:
                mfa.last_used_step = step
                method = "totp"
        if method is None:
            _fail(mfa, now)
        else:
            _succeed(mfa, "last_used_step")
    if method is None:
        record_security_event(
            SecurityAction.MFA_FAILED, request=request, actor=user, metadata={"stage": "signin"}
        )
        raise InvalidCode
    if method == "recovery":
        remaining = MFARecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
        record_security_event(
            SecurityAction.MFA_RECOVERY_USED,
            request=request,
            actor=user,
            metadata={"remaining": remaining},
        )
    return method
