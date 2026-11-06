"""A member's connection of one app to their Genos over MCP (MIP-1077).

One row per (person, client app): the thing Settings → Connected apps lists and the thing
revoking removes. Ken's decision (2026-11-06): ONE connection per person covers the teams they
choose at consent, with a default team. A call works in one team (the default, or one it names),
only inside the teams granted here, and only while the person is still in them.

The tokens are django-oauth-toolkit's own; they point at the app and the person, and this row
says what that pair is allowed to do. So the access a token has is read from HERE on every
request, never copied into the token: revoking the connection ends every token at once, and
narrowing its teams takes effect on the next call.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from origin.models.common.api_key_models import SCOPE_CHOICES, SCOPE_READ
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class McpConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="mcp_connections")
    # The client: a registered app (DCR) or one identified by its metadata document (CIMD).
    application = models.ForeignKey(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        on_delete=models.CASCADE,
        related_name="mcp_connections",
    )
    # "read" or "write": what the member allowed, the same two words an API key uses.
    scope = models.CharField(max_length=16, choices=SCOPE_CHOICES, default=SCOPE_READ)
    # A destroying tool needs this on top of write scope (MIP-723). Off unless the member ticked it.
    allow_destructive = models.BooleanField(default=False)
    # The team a call works in when it names none. Always one of `team_ids`.
    default_team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
        related_name="+",
    )
    # The teams the member chose, as team id strings. A list, not a relation: membership is
    # checked against the live team on every call, and a team that is gone just stops matching.
    team_ids = models.JSONField(default=list)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "application"], name="uniq_mcp_connection_per_app"
            )
        ]
        indexes = [models.Index(fields=["user", "revoked_at"])]
        ordering = ["-ts_created_at"]

    def __str__(self) -> str:
        return f"{self.application.name or self.application.client_id} for {self.user_id}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def revoke(self) -> None:
        """End the connection. Every token it issued stops working on its next use."""
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "ts_updated_at"])
