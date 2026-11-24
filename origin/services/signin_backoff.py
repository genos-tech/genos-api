"""Per-account sign-in backoff and the password-spray alert (MIP-2237).

The per-IP throttles (`SignInBurstThrottle`, `SignInSustainedThrottle`) do not stop an
attacker who sprays from thousands of addresses: each stays under 20/min while one account
takes unlimited guesses. This module counts failures per ACCOUNT instead.

* Key. The counter is keyed on the blind index (HMAC) of the address that was typed, never
  the address, so no plaintext email reaches Redis. It is keyed on what was TYPED, not on a
  user row, so an address with no account is counted and refused exactly like a real one:
  the answer never says whether the account exists.
* Backoff. After `SIGNIN_BACKOFF_FAILURES` failures within an hour the account's sign-ins
  are refused (429 + Retry-After) for 30 s, doubling per further failure up to 15 minutes.
  A refused attempt is not a guess and does not extend the lock; only a real failure does.
* Spray alert. The first failure of an account in its hour also counts once into an hourly
  tally of distinct accounts. Reaching `SIGNIN_SPRAY_ACCOUNTS` writes `alert.signin_spray`,
  which `ops_alerts` mails to the operator. One alert per hour.
* Reset. A successful sign-in or a password reset clears the counter and the lock.
* Fails closed, as `AuthThrottle` does: with the cache unreadable, sign-in is refused for 60 s
  rather than left unlimited.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.http import JsonResponse

from origin.models.common.security_event_models import SecurityAction
from origin.services import blind_index, throttles

logger = logging.getLogger(__name__)

_WINDOW = 3600
_BASE_LOCK = 30
_MAX_LOCK = 900
_CACHE_DOWN_WAIT = 60


def _failure_limit() -> int:
    return max(1, int(getattr(settings, "SIGNIN_BACKOFF_FAILURES", 5)))


def _spray_threshold() -> int:
    return max(1, int(getattr(settings, "SIGNIN_SPRAY_ACCOUNTS", 50)))


def _account_key(email) -> str | None:
    """`None` when the box held no usable address text (the per-IP throttles still apply)."""
    if not isinstance(email, str):
        return None
    digest = blind_index.USER_EMAIL.compute(email)
    return digest or None


def _cache():
    # No breaker: this guards one low-traffic endpoint, as the fail-closed throttles do.
    return throttles.throttle_cache(breaker=False)


def refusal(wait: int) -> JsonResponse:
    response = JsonResponse(
        {"detail": f"Request was throttled. Expected available in {wait} seconds."},
        status=429,
    )
    response["Retry-After"] = str(wait)
    return response


def check(email) -> JsonResponse | None:
    """A 429 response if sign-in for this typed address is locked, else None."""
    digest = _account_key(email)
    if digest is None:
        return None
    try:
        until = _cache().get(f"signin_lock:{digest}")
    except Exception:
        logger.error("signin.backoff cache_unavailable action=refuse", exc_info=True)
        return refusal(_CACHE_DOWN_WAIT)
    if until is None:
        return None
    wait = int(until - time.time())
    return refusal(max(wait, 1)) if wait > 0 else None


def note_failure(email, request=None) -> None:
    """Count one wrong-password attempt against the typed address. Never raises."""
    digest = _account_key(email)
    if digest is None:
        return
    try:
        cache = _cache()
        key = f"signin_fail:{digest}"
        if cache.add(key, 1, _WINDOW):
            count = 1
            _note_new_account(request)
        else:
            try:
                count = cache.incr(key)
            except ValueError:  # expired between the add and the incr
                cache.set(key, 1, _WINDOW)
                count = 1
        over = count - _failure_limit()
        if over >= 0:
            lock = min(_BASE_LOCK * (2**over), _MAX_LOCK)
            cache.set(f"signin_lock:{digest}", time.time() + lock, lock)
    except Exception:
        logger.error("signin.backoff count failed", exc_info=True)


def _note_new_account(request) -> None:
    hour = int(time.time() // _WINDOW)
    cache = _cache()
    key = f"signin_spray:{hour}"
    count = 1 if cache.add(key, 1, _WINDOW * 2) else cache.incr(key)
    if count != _spray_threshold():
        return
    from origin.services.security_events import record_security_event  # noqa: PLC0415

    logger.warning(
        "security.alert action=%s accounts=%s window_seconds=%s",
        SecurityAction.ALERT_SIGNIN_SPRAY,
        count,
        _WINDOW,
    )
    record_security_event(
        SecurityAction.ALERT_SIGNIN_SPRAY,
        request=request,
        metadata={"accounts": count, "window_seconds": _WINDOW},
    )


def clear(email) -> None:
    """A successful sign-in or a password reset: forget the failures. Never raises."""
    digest = _account_key(email)
    if digest is None:
        return
    try:
        _cache().delete_many([f"signin_fail:{digest}", f"signin_lock:{digest}"])
    except Exception:
        logger.error("signin.backoff clear failed", exc_info=True)
