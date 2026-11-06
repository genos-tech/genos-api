"""Ending and describing a member's connected AI apps (MIP-1077 / MIP-1079)."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMaster
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


def member_teams(user) -> list[TeamMaster]:
    """The teams this person owns or belongs to right now (not deleted, not left)."""
    return list(
        TeamMaster.objects.filter(is_deleted=False)
        .filter(Q(owner=user) | Q(team_members__attendee=user, team_members__is_deleted=False))
        .distinct()
        .order_by("team_name")
    )


def prune_left_teams(connection: McpConnection) -> bool:
    """Drop from the connection the teams the person is no longer in; True if any went.

    A call is already refused for a team they left (membership is checked live). Dropping it
    for good means that rejoining later does not hand the app the team back without asking:
    consent was for the team as it was. The default moves to a team still granted."""
    live = {str(t.team_id) for t in member_teams(connection.user)}
    kept = [t for t in connection.team_ids if t in live]
    if kept == list(connection.team_ids):
        return False
    connection.team_ids = kept
    fields = ["team_ids", "ts_updated_at"]
    if kept and str(connection.default_team_id) not in kept:
        connection.default_team_id = kept[0]
        fields.append("default_team")
    connection.save(update_fields=fields)
    record_security_event(
        SecurityAction.MCP_UPDATED,
        actor=connection.user,
        team_id=connection.default_team_id,
        target_type="mcp_connection",
        target_id=connection.id,
        target_label=connection.application.name or connection.application.client_id,
        metadata={"reason": "left_team", "teams": len(kept)},
    )
    return True
