"""Ending and describing a member's connected AI apps (MIP-1077 / MIP-1079)."""

from __future__ import annotations

from django.db import transaction
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction
from origin.services.security_events import record_security_event


def end_tokens(user, application) -> None:
    """Delete every token and pending code this person holds for this app.

    The connection being revoked already makes the MCP endpoint refuse them (access is read
    from the connection on each call); deleting them as well means the refresh token cannot be
    used to ask for new ones, and nothing is left lying around."""
    get_refresh_token_model().objects.filter(user=user, application=application).delete()
    get_access_token_model().objects.filter(user=user, application=application).delete()
    get_grant_model().objects.filter(user=user, application=application).delete()


def revoke_connection(connection: McpConnection) -> None:
    """End one connection and every token it issued, and leave the trail."""
    with transaction.atomic():
        connection.revoke()
        end_tokens(connection.user, connection.application)
        record_security_event(
            SecurityAction.MCP_DISCONNECTED,
            actor=connection.user,
            team_id=connection.default_team_id,
            target_type="mcp_connection",
            target_id=connection.id,
            target_label=connection.application.name or connection.application.client_id,
        )
