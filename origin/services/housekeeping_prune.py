"""Housekeeping prunes for credentials, invites and ops rows (MIP-1787).

Each *set* below is rows that have outlived their purpose and that nothing live depends on.
`prune_housekeeping` (the command) counts them by default and deletes only with `--apply`.

What is deliberately NOT here: OAuth clients (`prune_oauth_clients`), security events
(`prune_security_events`), schedule reports and AI app notices have their own prunes.

Every set only ever matches a row that is dead by its own state AND past a grace period, so a
clock that is a little off, or a prune that runs late, can never touch a live thing:

* an access token / grant that has not expired, a refresh token that is neither revoked nor
  past its lifetime;
* a connection or API key that is not revoked (an API key past its expiry counts as dead);
* a pending invite that has not expired, or one that expired recently;
* a push subscription that registered or re-registered recently;
* an alert that is still open.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from apis.mcp_oauth_settings import REFRESH_TOKEN_SECONDS

log = logging.getLogger(__name__)

# Rows a delete batch removes at a time, and the pause between batches: a prune must never hold a
# long lock on a table the app is using.
BATCH_SIZE = 500
BATCH_SLEEP_SECONDS = 0.2

# An OAuth access token or authorization code is useless the moment it expires; 30 days after that
# is only a margin for clock skew and for a person to look at a support question.
OAUTH_EXPIRED_GRACE_DAYS = 30
# A refresh token is dead when it was revoked, or when it is older than its lifetime
# (`REFRESH_TOKEN_SECONDS`, the library does not store an expiry). Either way 30 days of margin.
OAUTH_REFRESH_GRACE_DAYS = 30
# A revoked MCP connection or API key (and an API key that expired without being revoked) keeps
# its owner's choices and key metadata; it can never work again. 90 days is long enough to answer
# "what was that key" after the fact.
CREDENTIAL_REVOKED_GRACE_DAYS = 90
# An accepted or revoked invite has done its job; it still holds the invitee's address.
INVITE_DONE_GRACE_DAYS = 90
# A pending invite past `expires_at` cannot be accepted. Kept 30 days so a person who clicked it
# late can be told it expired, then it goes.
INVITE_EXPIRED_PENDING_GRACE_DAYS = 30
# The app re-registers a browser's subscription on every load, which touches `ts_updated_at`;
# 90 days without that means the browser is gone (a gone endpoint is removed at once on 404/410).
PUSH_IDLE_DAYS = 90
# A resolved alert is history; its `detail` can hold an address. Open alerts are never touched.
OPS_ALERT_RESOLVED_GRACE_DAYS = 90


def _days(n: int) -> timedelta:
    return timedelta(days=n)


def _refresh_dead_q(now) -> Q:
    return Q(revoked__lt=now - _days(OAUTH_REFRESH_GRACE_DAYS)) | Q(
        created__lt=now - timedelta(seconds=REFRESH_TOKEN_SECONDS) - _days(OAUTH_REFRESH_GRACE_DAYS)
    )


def oauth_refresh_tokens(now) -> QuerySet:
    from oauth2_provider.models import get_refresh_token_model  # noqa: PLC0415

    return get_refresh_token_model().objects.filter(_refresh_dead_q(now))


def oauth_access_tokens(now) -> QuerySet:
    """Expired for the grace period AND not the access token of a refresh token still alive."""
    from oauth2_provider.models import get_access_token_model, get_refresh_token_model  # noqa: PLC0415

    live_refresh = (
        get_refresh_token_model()
        .objects.filter(access_token=OuterRef("pk"))
        .exclude(_refresh_dead_q(now))
    )
    return (
        get_access_token_model()
        .objects.filter(expires__lt=now - _days(OAUTH_EXPIRED_GRACE_DAYS))
        .exclude(Exists(live_refresh))
    )


def oauth_grants(now) -> QuerySet:
    from oauth2_provider.models import get_grant_model  # noqa: PLC0415

    return get_grant_model().objects.filter(expires__lt=now - _days(OAUTH_EXPIRED_GRACE_DAYS))


def mcp_connections(now) -> QuerySet:
    from origin.models.common.mcp_connection_models import McpConnection  # noqa: PLC0415

    return McpConnection.objects.filter(revoked_at__lt=now - _days(CREDENTIAL_REVOKED_GRACE_DAYS))


def api_keys(now) -> QuerySet:
    from origin.models.common.api_key_models import ApiKey  # noqa: PLC0415

    cutoff = now - _days(CREDENTIAL_REVOKED_GRACE_DAYS)
    return ApiKey.objects.filter(
        Q(revoked_at__lt=cutoff) | Q(revoked_at__isnull=True, expires_at__lt=cutoff)
    )


def team_invites(now) -> QuerySet:
    from origin.models.common.invite_models import TeamInvite  # noqa: PLC0415

    done_cutoff = now - _days(INVITE_DONE_GRACE_DAYS)
    return TeamInvite.objects.filter(
        Q(
            status__in=["accepted", "revoked"],
            ts_accepted_at__isnull=False,
            ts_accepted_at__lt=done_cutoff,
        )
        | Q(
            status__in=["accepted", "revoked"],
            ts_accepted_at__isnull=True,
            ts_updated_at__lt=done_cutoff,
        )
        | Q(status="pending", expires_at__lt=now - _days(INVITE_EXPIRED_PENDING_GRACE_DAYS))
    )


def push_subscriptions(now) -> QuerySet:
    from origin.models.common.notification_models import PushSubscription  # noqa: PLC0415

    return PushSubscription.objects.filter(ts_updated_at__lt=now - _days(PUSH_IDLE_DAYS))


def ops_alerts(now) -> QuerySet:
    from origin.models.common.ops_models import OpsAlert  # noqa: PLC0415

    return OpsAlert.objects.filter(
        resolved_at__isnull=False, resolved_at__lt=now - _days(OPS_ALERT_RESOLVED_GRACE_DAYS)
    )


def erasure_events(now) -> QuerySet:
    """The ids-only erasure log past backup retention plus a margin (MIP-2249)."""
    from origin.models.common.erasure_models import RETENTION_DAYS, ErasureEvent  # noqa: PLC0415

    return ErasureEvent.objects.filter(erased_at__lt=now - _days(RETENTION_DAYS))


@dataclass(frozen=True)
class PruneSet:
    name: str
    queryset: Callable[..., QuerySet]


# Order matters on apply: refresh tokens go before the access tokens they point at.
SETS: tuple[PruneSet, ...] = (
    PruneSet("oauth_refresh_tokens", oauth_refresh_tokens),
    PruneSet("oauth_access_tokens", oauth_access_tokens),
    PruneSet("oauth_grants", oauth_grants),
    PruneSet("mcp_connections", mcp_connections),
    PruneSet("api_keys", api_keys),
    PruneSet("team_invites", team_invites),
    PruneSet("push_subscriptions", push_subscriptions),
    PruneSet("ops_alerts", ops_alerts),
    PruneSet("erasure_events", erasure_events),
)
BY_NAME = {s.name: s for s in SETS}


def count(name: str, now=None) -> int:
    return BY_NAME[name].queryset(now or timezone.now()).count()


def delete_in_batches(name: str, now=None, sleep: float = BATCH_SLEEP_SECONDS) -> int:
    """Delete one set a batch at a time; returns the number removed. Re-evaluates the match each
    batch, so a row that turned live meanwhile is left alone, and a second run finds nothing."""
    now = now or timezone.now()
    qs = BY_NAME[name].queryset(now)
    model = qs.model
    total = 0
    while True:
        pks = list(qs.order_by("pk").values_list("pk", flat=True)[:BATCH_SIZE])
        if not pks:
            return total
        deleted, _ = model.objects.filter(pk__in=pks).filter(pk__in=qs.values("pk")).delete()
        total += len(pks)
        if len(pks) < BATCH_SIZE:
            return total
        time.sleep(sleep)
