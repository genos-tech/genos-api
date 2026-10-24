"""Write the security audit log (MIP-725).

`record_security_event` is the one writer of `SecurityEvent`. Call it
at the point an action is known to have happened (or been refused),
with whatever the call site knows; it fills in the rest from the active
request — IP, user agent, and whether the caller came in through the
app, an API key, MCP or the agent.

**It never raises.** An audit row that can't be written must not turn a
successful sign-in or export into a 500, so a failure logs at ERROR and
the action goes ahead. Every row is also logged as a `security.event`
line, so the Railway logs hold a copy even when the insert failed.

**Alerts.** Two anomalies write an `alert.*` row and log a
`security.alert` WARNING:

- every data export, since one export is a whole account's data leaving;
- a burst of refused requests from one caller: `SECURITY_FORBIDDEN_BURST`
  403s within a minute (`ForbiddenBurstMiddleware`). That is what
  walking through someone else's ids looks like.

Every `alert.*` row also opens an `OpsAlert`, which `ops_alert_tick`
mails to `OPS_ALERT_EMAIL` (MIP-772). The WARNING line stays, as the
copy in the logs; search them for `security.alert`. WARNING rather than
ERROR because ERROR on this logger is what the cron tripwire treats as
a failed run.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.throttling import BaseThrottle

from origin.middleware import current_user
from origin.models.common.security_event_models import (
    SecurityAction,
    SecurityEvent,
    SecuritySource,
)
from origin.services import throttles
from origin.services.pii_mask import mask_email

logger = logging.getLogger(__name__)

# MCP is an API-key route like the rest of the public API; only the path
# tells it apart.
_MCP_PATH = "/api/public/v1/mcp"

_LABEL_MAX = 254
_UA_MAX = 512


def retention_days() -> int:
    """How long events are kept: `SECURITY_EVENT_RETENTION_DAYS`, never
    under the floor. A trail that is gone by the time a breach is noticed
    is no trail."""
    return max(
        int(getattr(settings, "SECURITY_EVENT_RETENTION_DAYS", 365)),
        int(getattr(settings, "SECURITY_EVENT_RETENTION_MIN_DAYS", 90)),
    )


def prune_before():
    return timezone.now() - timedelta(days=retention_days())


def client_ip(request) -> str | None:
    """The caller's address, resolved the way the throttles resolve it
    (`NUM_PROXIES` makes it the address Railway's proxy saw, not one a
    client wrote into X-Forwarded-For). None unless it is one address."""
    if request is None:
        return None
    raw = BaseThrottle().get_ident(request) or ""
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


def _api_key(request):
    from origin.models.common.api_key_models import ApiKey  # noqa: PLC0415

    key = getattr(request, "auth", None)
    return key if isinstance(key, ApiKey) else None


def _source(request, key=None) -> tuple[str, str | None]:
    """(`SecuritySource`, api key id) for the request."""
    via = getattr(current_user._state, "via", None)
    if via is not None and via.get("kind") == "agent":
        return SecuritySource.AGENT, None
    if request is None:
        return SecuritySource.SYSTEM, None
    key = key or _api_key(request)
    if key is not None:
        path = (getattr(request, "path", "") or "").rstrip("/")
        return (SecuritySource.MCP if path == _MCP_PATH else SecuritySource.API_KEY), str(key.id)
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return SecuritySource.APP, None
    return SecuritySource.ANONYMOUS, None


def _label(user) -> str:
    if user is None:
        return ""
    return str(getattr(user, "email", "") or getattr(user, "pk", ""))[:_LABEL_MAX]


def _log_actor(event) -> str:
    """Who to name in the log line: the user id, or the label when it isn't
    an email. A failed sign-in has no user, and its label is the address
    that was typed; the row keeps it, and the line's event id leads there
    (MIP-622)."""
    if event.actor_id:
        return str(event.actor_id)
    label = event.actor_label or ""
    if not label:
        return "-"
    return "[EMAIL]" if "@" in label else label


def user_label(user_id) -> str:
    """A target user's email, captured now because the row keeps no FK
    and deletion anonymises the address. Empty if it can't be read."""
    from django.contrib.auth import get_user_model  # noqa: PLC0415

    try:
        email = get_user_model().objects.filter(pk=user_id).values_list("email", flat=True).first()
    except Exception:
        return ""
    return str(email or "")[:_LABEL_MAX]


def record_security_event(
    action: str,
    *,
    request=None,
    actor=None,
    actor_label: str = "",
    api_key=None,
    team_id=None,
    target_type: str = "",
    target_id="",
    target_label: str = "",
    metadata: dict | None = None,
) -> SecurityEvent | None:
    """Append one event. Returns it, or None if it couldn't be written.

    `request` defaults to the one `CurrentUserMiddleware` holds, and
    `actor` to the acting user, so most call sites pass only the action,
    the team and the target. Pass `actor` when the request's user isn't
    the one acting: a sign-in, where nobody is authenticated yet.
    `actor_label` is for when there is no user at all, such as the
    address a failed sign-in tried. Both labels are stored with any email
    in them masked (`k***@gmail.com`, `pii_mask.mask_email`), here and
    not at the call sites: the row outlives the user, and a dump of this
    table must not hand over addresses (MIP-765). `api_key` is for the authenticator,
    which runs before DRF has put the key on the request; anywhere else
    it is read from `request.auth`.
    """
    try:
        if request is None:
            request = getattr(current_user._state, "request", None)
        if actor is None:
            user = getattr(request, "user", None) if request is not None else None
            if user is not None and getattr(user, "is_authenticated", False):
                actor = user
            else:
                actor = current_user.get_current_user()
        source, api_key_id = _source(request, api_key)
        if source == SecuritySource.ANONYMOUS and actor is not None:
            # Not authenticated, but it proved who it was another way: a
            # password, an OAuth round trip, a reset token, the cookie
            # being signed out. That is a person using the app.
            source = SecuritySource.APP
        meta = getattr(request, "META", None) or {}
        # A savepoint, so a failed insert can't poison a transaction the
        # caller is in the middle of.
        with transaction.atomic():
            event = SecurityEvent.objects.create(
                action=action,
                actor_id=getattr(actor, "pk", None),
                actor_label=mask_email(actor_label or _label(actor))[:_LABEL_MAX],
                team_id=team_id or None,
                target_type=target_type,
                target_id=str(target_id or "")[:64],
                target_label=mask_email(str(target_label or ""))[:_LABEL_MAX],
                source=source,
                api_key_id=api_key_id,
                ip=client_ip(request),
                user_agent=str(meta.get("HTTP_USER_AGENT") or "")[:_UA_MAX],
                metadata=metadata or {},
            )
    except Exception:
        logger.exception("security.event write failed action=%s", action)
        return None

    logger.info(
        "security.event id=%s action=%s actor=%s team=%s target=%s:%s source=%s ip=%s",
        event.event_id,
        event.action,
        _log_actor(event),
        event.team_id or "-",
        event.target_type or "-",
        event.target_id or "-",
        event.source,
        event.ip or "-",
    )
    if action == SecurityAction.EXPORT:
        _alert(SecurityAction.ALERT_EXPORT, event, {"summary": "account data exported"})
    elif action.startswith("alert."):
        _page(event)
    return event


def _page(event: SecurityEvent) -> None:
    from origin.services import ops_alerts  # noqa: PLC0415 — imports models

    ops_alerts.raise_security_alert(event)


def _alert(action: str, cause: SecurityEvent, metadata: dict) -> None:
    """Write the alert row for `cause` and log the WARNING that is the
    alert itself. The line goes out even if the row can't be written."""
    logger.warning(
        "security.alert action=%s actor=%s source=%s ip=%s cause=%s %s",
        action,
        _log_actor(cause),
        cause.source,
        cause.ip or "-",
        cause.event_id,
        " ".join(f"{k}={v}" for k, v in metadata.items()),
    )
    try:
        with transaction.atomic():
            alert = SecurityEvent.objects.create(
                action=action,
                actor_id=cause.actor_id,
                # Masked again, not copied as stored: a cause written before
                # MIP-765 (or before its backfill ran) still holds the address.
                actor_label=mask_email(cause.actor_label),
                team_id=cause.team_id,
                target_type="security_event",
                target_id=str(cause.event_id),
                source=cause.source,
                api_key_id=cause.api_key_id,
                ip=cause.ip,
                user_agent=cause.user_agent,
                metadata=metadata,
            )
    except Exception:
        logger.exception("security.alert write failed action=%s", action)
        return
    _page(alert)


def _first_in_window(key: str, seconds: int) -> bool:
    """True the first time `key` is seen in `seconds`. Counts in the
    throttle cache behind its breaker; True with the cache down, so an
    outage records too much rather than nothing."""
    try:
        return bool(throttles.throttle_cache().add(key, 1, seconds))
    except throttles.CACHE_ERRORS as exc:
        if not isinstance(exc, throttles.CacheDown):
            throttles._trip_breaker()
        return True
    except Exception:
        return True


# ── API-key use ─────────────────────────────────────────────────────────

_KEY_USE_WINDOW = 3600


def _key_label(key) -> str:
    return f"{key.name} ({key.prefix})"


def note_api_key_used(request, key) -> None:
    """Record that `key` made a request, once per key, IP and hour.

    A row per request would be the public API's write cost doubled. Once
    an hour per address answers what a leak needs answered: was the key
    used, when, and from where. A new address is recorded by the first
    request that comes from it.

    `request` is the Django request: this runs inside DRF's
    authentication, where reading the DRF request's `.auth` would start
    authentication over again.
    """
    ip = client_ip(request)
    hour = int(time.time() // _KEY_USE_WINDOW)
    if not _first_in_window(f"seckeyuse:{key.id}:{ip}:{hour}", _KEY_USE_WINDOW * 2):
        return
    record_security_event(
        SecurityAction.API_KEY_USED,
        request=request,
        actor=key.user,
        api_key=key,
        team_id=key.team_id,
        target_type="api_key",
        target_id=key.id,
        target_label=_key_label(key),
        metadata={"path": request.path, "method": request.method},
    )


def note_api_key_rejected(request, key, reason: str) -> None:
    """Record a refused API key, once per key (or, for a key we don't
    know, per address), reason and hour.

    A known key that was revoked or expired is the interesting case:
    that is a leaked key being tried. An unknown one is recorded too, by
    address only; nothing of the presented value is stored.
    """
    ip = client_ip(request)
    hour = int(time.time() // _KEY_USE_WINDOW)
    who = f"key:{key.id}" if key is not None else f"ip:{ip}"
    if not _first_in_window(f"seckeyrej:{who}:{reason}:{hour}", _KEY_USE_WINDOW * 2):
        return
    record_security_event(
        SecurityAction.API_KEY_REJECTED,
        request=request,
        actor=key.user if key is not None else None,
        api_key=key,
        team_id=key.team_id if key is not None else None,
        target_type="api_key" if key is not None else "",
        target_id=key.id if key is not None else "",
        target_label=_key_label(key) if key is not None else "",
        metadata={"reason": reason, "path": request.path, "method": request.method},
    )


# ── 403 bursts ──────────────────────────────────────────────────────────

_BURST_WINDOW = 60


def forbidden_burst_threshold() -> int:
    return int(getattr(settings, "SECURITY_FORBIDDEN_BURST", 20))


def _caller(request) -> str | None:
    """Whom a refusal counts against: the API key, else the user.
    Anonymous refusals aren't counted; the per-IP auth throttles cover
    those."""
    key = _api_key(request)
    if key is not None:
        return f"key:{key.id}"
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return f"user:{user.pk}"
    return None


def note_forbidden(request) -> None:
    """Count one 403 for the caller; alert when the count reaches the
    threshold.

    Fixed one-minute windows in the throttle cache. The alert fires on
    the request that reaches the threshold exactly, so a scan of a
    thousand ids is one alert per minute, not a thousand. With the cache
    down nothing is counted, and the request is unaffected.
    """
    who = _caller(request)
    if who is None:
        return
    key = f"secburst:{who}:{int(time.time() // _BURST_WINDOW)}"
    try:
        cache = throttles.throttle_cache()
        count = 1 if cache.add(key, 1, _BURST_WINDOW * 2) else cache.incr(key)
    except throttles.CACHE_ERRORS as exc:
        if not isinstance(exc, throttles.CacheDown):
            throttles._trip_breaker()
        return
    except Exception:
        return
    if count != forbidden_burst_threshold():
        return
    logger.warning(
        "security.alert action=%s %s refused=%s window_seconds=%s path=%s",
        SecurityAction.ALERT_FORBIDDEN_BURST,
        who,
        count,
        _BURST_WINDOW,
        request.path,
    )
    record_security_event(
        SecurityAction.ALERT_FORBIDDEN_BURST,
        request=request,
        metadata={
            "refused": count,
            "window_seconds": _BURST_WINDOW,
            "path": request.path,
            "method": request.method,
        },
    )


class ForbiddenBurstMiddleware:
    """Counts every 403 the app answers, wherever it came from.

    A middleware, not a DRF exception handler, because most refusals
    here are an inline `Response(status=403)` and MCP's are JSON-RPC
    errors; neither goes through the exception handler. It reads
    `request.user` and `request.auth` after the view, once DRF has
    mirrored the authenticated caller onto the Django request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code == 403:
            note_forbidden(request)
        return response
