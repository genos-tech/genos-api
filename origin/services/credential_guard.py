"""Detection that acts (MIP-2259): read volume per credential, and pausing a credential on a burst.

Two detectors feed one reaction.

- **Read volume.** A sliding one-minute window of calls and response characters per API key,
  connected app (MCP connection) and user, counted for the public API and MCP only. One credential can
  pull about 10 MB of text a minute over MCP (400 calls times 24k characters), and until now nothing
  watched that. Passing a threshold writes `alert.read_volume`, which opens an `OpsAlert` that
  `ops_alert_tick` mails to the operator, like the export and refused-burst alerts.
- **Refused burst.** `security_events.note_forbidden` calls `on_burst` when a credential reaches
  `SECURITY_FORBIDDEN_BURST` refusals in a minute.

**The reaction is a switch**, `CREDENTIAL_AUTOSUSPEND_MODE`:

- `off` (default): nothing is paused. The read-volume alert still fires (it only emails).
- `shadow`: the credential is NOT paused; the alert row says `would_suspend: true`, so the thresholds
  can be tuned against real traffic first.
- `enforce`: the key or connection is paused (`suspended_at`), a `credential.suspended` event is
  written, and the owner re-enables it in Settings (`resume`), which writes `credential.resumed`.

A suspension is reversible and is not a revocation: nothing about the credential is deleted, and a
revoked one is never touched. Only a key or a connected app is paused; a person's own browser session
is not (the sign-in throttles and the alerts cover it).

Every function here never raises: a detector must not turn a request into a 500. With the throttle
cache down nothing is counted.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from origin.models.common.security_event_models import SecurityAction
from origin.services import throttles

logger = logging.getLogger(__name__)

WINDOW = 60

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"

REASON_READ_VOLUME = "read_volume"
REASON_FORBIDDEN_BURST = "forbidden_burst"


def mode() -> str:
    value = str(getattr(settings, "CREDENTIAL_AUTOSUSPEND_MODE", MODE_OFF) or MODE_OFF).lower()
    return value if value in (MODE_SHADOW, MODE_ENFORCE) else MODE_OFF


def _int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def calls_threshold() -> int:
    return _int("SECURITY_READ_CALLS_PER_MINUTE", 300)


def chars_threshold() -> int:
    return _int("SECURITY_READ_CHARS_PER_MINUTE", 3_000_000)


def user_multiplier() -> int:
    """A person's own limit is this many times a credential's: they may run several."""
    return max(1, _int("SECURITY_READ_USER_MULTIPLIER", 2))


# ── the sliding window ──────────────────────────────────────────────────


def _bump(cache, key: str, amount: int) -> None:
    if not cache.add(key, amount, WINDOW * 3):
        cache.incr(key, amount)


def window_total(subject: str, metric: str, *, now: float | None = None) -> int:
    """The estimate for the last minute: this bucket plus the share of the previous one that is
    still inside the window. Read only."""
    now = time.time() if now is None else now
    bucket = int(now // WINDOW)
    cache = throttles.throttle_cache()
    cur = int(cache.get(f"secread:{subject}:{metric}:{bucket}") or 0)
    prev = int(cache.get(f"secread:{subject}:{metric}:{bucket - 1}") or 0)
    carried = 1 - ((now % WINDOW) / WINDOW)
    return cur + int(prev * carried)


def record_read(
    subject: str, calls: int, chars: int, *, now: float | None = None
) -> tuple[int, int]:
    """Add one request to the subject's window and return (calls, chars) for the last minute."""
    now = time.time() if now is None else now
    bucket = int(now // WINDOW)
    cache = throttles.throttle_cache()
    _bump(cache, f"secread:{subject}:calls:{bucket}", calls)
    if chars:
        _bump(cache, f"secread:{subject}:chars:{bucket}", chars)
    return window_total(subject, "calls", now=now), window_total(subject, "chars", now=now)


def _breach(subject: str, calls: int, chars: int, factor: int) -> dict | None:
    over_calls = calls >= calls_threshold() * factor
    over_chars = chars >= chars_threshold() * factor
    if not (over_calls or over_chars):
        return None
    return {
        "subject": subject,
        "calls": calls,
        "chars": chars,
        "calls_limit": calls_threshold() * factor,
        "chars_limit": chars_threshold() * factor,
        "window_seconds": WINDOW,
    }


def _once(key: str, seconds: int) -> bool:
    try:
        return bool(throttles.throttle_cache().add(key, 1, seconds))
    except Exception:
        return False


# ── the reaction ────────────────────────────────────────────────────────


def _suspendable(credential) -> bool:
    """A key or connected app that is live and not already paused."""
    if credential is None or not hasattr(credential, "audit_ref"):
        return False
    row = _row(credential)
    return row is not None and row.revoked_at is None and row.suspended_at is None


def _row(credential):
    """The model row behind a credential (an API key is its own row; an OAuth credential wraps a
    connection)."""
    return getattr(credential, "connection", credential)


def suspend(credential, reason: str, *, request=None, detail: dict | None = None) -> bool:
    """Pause the credential. True when it was paused now. Reversible: `resume`."""
    row = _row(credential)
    try:
        with transaction.atomic():
            updated = (
                type(row)
                .objects.filter(pk=row.pk, revoked_at__isnull=True, suspended_at__isnull=True)
                .update(suspended_at=timezone.now(), suspended_reason=reason)
            )
        if not updated:
            return False
        row.suspended_at = timezone.now()
        row.suspended_reason = reason
    except Exception:
        logger.exception("credential suspend failed")
        return False
    _write_event(
        SecurityAction.CREDENTIAL_SUSPENDED,
        credential,
        request,
        {"reason": reason, **(detail or {})},
    )
    logger.warning(
        "security.alert action=%s %s reason=%s",
        SecurityAction.CREDENTIAL_SUSPENDED,
        credential.audit_ref(),
        reason,
    )
    return True


def resume(row, *, request=None, actor=None) -> bool:
    """Re-enable a paused key or connection (the owner's action). True if it was paused."""
    updated = (
        type(row)
        .objects.filter(pk=row.pk, suspended_at__isnull=False)
        .update(suspended_at=None, suspended_reason="")
    )
    if not updated:
        return False
    row.suspended_at = None
    row.suspended_reason = ""
    _write_event(SecurityAction.CREDENTIAL_RESUMED, row, request, {}, actor=actor)
    # A fresh start: the window that tripped it must not trip it again at once.
    try:
        _clear_window(row)
    except Exception:
        pass
    return True


def _clear_window(row) -> None:
    ref = _ref(row)
    cache = throttles.throttle_cache()
    bucket = int(time.time() // WINDOW)
    cache.delete_many(
        [f"secread:{ref}:{m}:{b}" for m in ("calls", "chars") for b in (bucket, bucket - 1)]
    )


def _ref(row) -> str:
    if hasattr(row, "audit_ref"):
        return row.audit_ref()
    return f"oauth:{row.id}"


def _write_event(action, credential, request, metadata, *, actor=None) -> None:
    from origin.services.security_events import record_security_event  # noqa: PLC0415

    row = _row(credential)
    oauth = getattr(credential, "kind", "api_key") != "api_key" or not hasattr(row, "key_hash")
    team_id = getattr(row, "team_id", None) if not oauth else getattr(row, "default_team_id", None)
    label = getattr(credential, "label", "") or getattr(row, "name", "") or ""
    record_security_event(
        action,
        request=request,
        actor=actor or row.user,
        api_key=row if not oauth else None,
        team_id=team_id,
        target_type="mcp_connection" if oauth else "api_key",
        target_id=row.id,
        target_label=label[:254],
        metadata={"credential": "oauth" if oauth else "api_key", **metadata},
    )


def _alert_read_volume(
    credential_or_user_ref: str, breach: dict, request, would_suspend: bool
) -> None:
    from origin.services.security_events import record_security_event  # noqa: PLC0415

    logger.warning(
        "security.alert action=%s %s calls=%s chars=%s window_seconds=%s",
        SecurityAction.ALERT_READ_VOLUME,
        credential_or_user_ref,
        breach["calls"],
        breach["chars"],
        WINDOW,
    )
    record_security_event(
        SecurityAction.ALERT_READ_VOLUME,
        request=request,
        metadata={
            "caller": credential_or_user_ref,
            "calls": breach["calls"],
            "chars": breach["chars"],
            "calls_limit": breach["calls_limit"],
            "chars_limit": breach["chars_limit"],
            "window_seconds": WINDOW,
            "mode": mode(),
            "would_suspend": would_suspend,
            "path": getattr(request, "path", ""),
        },
    )


# ── entry points ────────────────────────────────────────────────────────


def note_read(request, credential, chars: int) -> None:
    """Count one successful read by a key or connected app, for the credential and for its user.
    Alert on a breach; in `enforce` mode, pause the credential. Never raises."""
    try:
        subject = credential.audit_ref()
        calls, total = record_read(subject, 1, chars)
        breach = _breach(subject, calls, total, 1)
        user = getattr(credential, "user", None)
        user_breach = None
        if user is not None and getattr(user, "pk", None):
            u_subject = f"user:{user.pk}"
            u_calls, u_chars = record_read(u_subject, 1, chars)
            user_breach = _breach(u_subject, u_calls, u_chars, user_multiplier())
        if breach and _once(f"secreadalert:{subject}", WINDOW * 5):
            would = _suspendable(credential)
            _alert_read_volume(subject, breach, request, would and mode() != MODE_OFF)
            if would and mode() == MODE_ENFORCE:
                suspend(
                    credential,
                    REASON_READ_VOLUME,
                    request=request,
                    detail={
                        "calls": breach["calls"],
                        "chars": breach["chars"],
                    },
                )
        if user_breach and _once(f"secreadalert:{user_breach['subject']}", WINDOW * 5):
            _alert_read_volume(user_breach["subject"], user_breach, request, False)
    except Exception:
        logger.exception("security.read_volume count failed")


def on_burst(credential, request, refused: int) -> None:
    """A credential reached the refused-request threshold: pause it when the switch is `enforce`.
    A burst is already alerted by the caller. Never raises."""
    try:
        if credential is None or mode() == MODE_OFF or not _suspendable(credential):
            return
        if mode() == MODE_SHADOW:
            logger.warning(
                "security.shadow would suspend %s reason=%s refused=%s",
                credential.audit_ref(),
                REASON_FORBIDDEN_BURST,
                refused,
            )
            return
        suspend(credential, REASON_FORBIDDEN_BURST, request=request, detail={"refused": refused})
    except Exception:
        logger.exception("security.burst suspend failed")


# ── middleware ──────────────────────────────────────────────────────────

_PUBLIC_PREFIX = "/api/public/"


class ReadVolumeMiddleware:
    """Counts what a key or connected app reads: every successful GET of the public API, and every
    MCP request. The size is the response body's length in characters' worth of bytes; a streamed
    response has none and counts as a call only."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if request.path.startswith(_PUBLIC_PREFIX) and 200 <= response.status_code < 300:
                from origin.services.security_events import _on_mcp  # noqa: PLC0415
                from origin.views.public.credentials import credential_of  # noqa: PLC0415

                is_read = request.method in ("GET", "HEAD") or (
                    request.method == "POST" and _on_mcp(request.path.rstrip("/"))
                )
                credential = credential_of(request) if is_read else None
                if credential is not None:
                    size = 0 if getattr(response, "streaming", False) else len(response.content)
                    note_read(request, credential, size)
        except Exception:
            logger.exception("security.read_volume middleware failed")
        return response
