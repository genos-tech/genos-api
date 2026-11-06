"""Ending and describing a member's connected AI apps (MIP-1077 / MIP-1079)."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
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


def prunable_clients(now, days: int):
    """Client registrations nothing refers to, old enough to go (MIP-1104).

    Only the ones strangers can create (registered and metadata-document), never one made by
    hand. "Nothing refers to it" is: no connection, no token of either kind, no pending code.
    A metadata document's row also has to be past its cache expiry (no expiry counts as past)."""
    Application = get_application_model()

    def uses(model):
        return Exists(model.objects.filter(application=OuterRef("pk")))

    return (
        Application.objects.filter(
            registration_source__in=[
                Application.RegistrationSource.DCR,
                Application.RegistrationSource.CIMD,
            ],
            created__lt=now - timedelta(days=days),
        )
        .filter(
            Q(registration_source=Application.RegistrationSource.DCR)
            | Q(cimd_expires_at__isnull=True)
            | Q(cimd_expires_at__lt=now)
        )
        .exclude(
            uses(McpConnection)
            | uses(get_access_token_model())
            | uses(get_refresh_token_model())
            | uses(get_grant_model())
        )
    )


def prune_clients(rows) -> int:
    """Delete the given client rows; returns how many went."""
    deleted, _per_model = rows.delete()
    return deleted
