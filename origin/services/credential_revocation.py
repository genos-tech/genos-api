"""End every credential a member has handed to an outside program, at once.

A connected AI app (OAuth) and an API key are the two ways a program acts as a member
without their password. Neither carries the password the way a browser session does, so
changing the password does not end them. This is the one function that ends both, built on
the helpers each already has (`mcp_connections.revoke_connection`,
`api_keys.revoke_api_key`), so each ended item writes exactly the security-log row an
explicit disconnect or revoke writes.

It also closes the member's open real-time sockets (genos-sockets), which no token check
reaches once they are connected.

Used by the password reset (`PasswordResetConfirmView`); a future "change password while
signed in" would call it too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.db import transaction
from django.utils import timezone

from origin.models.common.api_key_models import ApiKey
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.webhook_models import WebhookEndpoint
from origin.services.api_keys import revoke_api_key
from origin.services.mcp_connections import end_all_tokens, revoke_connection
from origin.services.realtime_push import push_user_disconnect


@dataclass(frozen=True)
class EndedCredentials:
    connections: int
    api_keys: int


def end_all_credentials(user, *, by: str, request=None) -> EndedCredentials:
    """Revoke every live connection and API key of `user`, delete every token and code.

    Only what is still live is counted and logged: a second call finds nothing and writes
    nothing. Runs in the caller's transaction (it opens its own atomic block so a failure
    here never leaves it half done)."""
    with transaction.atomic():
        connections = 0
        for connection in McpConnection.objects.select_related("application").filter(
            user=user, revoked_at__isnull=True
        ):
            revoke_connection(connection, metadata={"by": by})
            connections += 1
        # Tokens an app holds without a live connection row (a revoked or deleted one) go
        # too: nothing the member ever issued to an app may outlive the reset.
        end_all_tokens(user)

        api_keys = 0
        for key in ApiKey.objects.filter(user=user, revoked_at__isnull=True):
            if revoke_api_key(key, by=by, request=request, actor=user):
                api_keys += 1
        # A socket checks the token only when it connects: close the member's open ones too
        # (MIP-2241). Sent after commit and never raises.
        push_user_disconnect(user.pk)
    return EndedCredentials(connections=connections, api_keys=api_keys)


RECENT_WEBHOOK_DAYS = 7


def recent_webhooks(user) -> list[dict]:
    """Webhooks `user` made in the last 7 days, as `{"id", "host"}` (MIP-2290).

    A reset ends keys and connections but not webhooks, so one made during a takeover would
    survive it unseen. The reset answer lists them so the owner can remove each. The host only:
    the path and query of a webhook URL often hold the secret that authorises the sender."""
    since = timezone.now() - timedelta(days=RECENT_WEBHOOK_DAYS)
    rows = WebhookEndpoint.objects.filter(created_by=user, ts_created_at__gte=since).order_by(
        "-ts_created_at"
    )[:50]
    out = []
    for hook in rows:
        try:
            host = urlsplit(hook.url).hostname or ""
        except ValueError:
            host = ""
        out.append({"id": str(hook.id), "host": host})
    return out
