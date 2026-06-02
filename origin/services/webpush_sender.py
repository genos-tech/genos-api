"""Sends a single Web Push via VAPID, and prunes dead subscriptions.

`pywebpush` is imported lazily so the rest of Django keeps working if the
dependency isn't installed yet (the dispatch simply no-ops with a warning)
— installing it + setting the VAPID env vars "turns on" delivery.

Designed to be safe to call from a worker thread: it takes plain fields
(not an ORM instance) and only touches the DB to prune a gone endpoint.
"""

import json
import logging

from django.conf import settings
from django.db import close_old_connections

from origin.models.common.notification_models import PushSubscription

logger = logging.getLogger(__name__)


def vapid_configured() -> bool:
    return bool(
        getattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY", "")
        and getattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY", "")
    )


def send_web_push(
    *, subscription_id, endpoint: str, p256dh: str, auth: str, payload: dict
) -> None:
    """Best-effort single push. Never raises — push is fire-and-forget.

    Runs in a worker thread (see webpush_dispatch), so it manages its own
    DB connection lifecycle: pool threads aren't request threads, so Django
    won't auto-close the connection the prune-on-410 opens. Bracketing with
    `close_old_connections()` clears a stale connection on entry and closes
    this run's on exit, preventing a connection leak / stale-connection
    errors under load.
    """
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("[webpush] pywebpush not installed; skipping push")
        return

    if not vapid_configured():
        logger.warning("[webpush] VAPID keys not configured; skipping push")
        return

    close_old_connections()
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": p256dh, "auth": auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{settings.WEBPUSH_VAPID_ADMIN_EMAIL}"},
            timeout=10,
        )
    except WebPushException as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in (404, 410):
            # Endpoint permanently gone — prune so we stop trying.
            PushSubscription.objects.filter(pk=subscription_id).delete()
            logger.info("[webpush] pruned dead subscription %s (%s)", subscription_id, status_code)
        else:
            logger.warning("[webpush] send failed (%s): %s", status_code, exc)
    except Exception as exc:  # noqa: BLE001 — a push must never break the caller
        logger.warning("[webpush] unexpected send error: %s", exc)
    finally:
        # Close this thread's connection so it isn't leaked / reused stale.
        close_old_connections()
