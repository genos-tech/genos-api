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


def prune_user_connections(user) -> int:
    """`prune_left_teams` for every live connection of `user`, now (MIP-1371).

    A member who is removed from a team keeps their connected AI apps, and a call into
    the team is refused anyway, because membership is checked live. Pruning at the moment of
    removal rather than "within the hour, on the next call" makes the connection stop
    listing the team at once, so the team's own view of its connections is true. Returns how
    many connections changed."""
    changed = 0
    for connection in McpConnection.objects.filter(user=user, revoked_at__isnull=True):
        if prune_left_teams(connection):
            changed += 1
    return changed


def drop_team(connection: McpConnection, team_id, *, request=None) -> bool:
    """Take ONE team out of a connection (a manager of that team ending its access); True if
    that left the connection with no team and so ended it altogether.

    The person's access to their other teams is untouched: the team's owner or editor has
    authority over their own team, not over the person's whole app. If the team was the
    connection's default, the default moves to a team still granted."""
    team_id = str(team_id)
    kept = [t for t in connection.team_ids if t != team_id]
    if not kept:
        revoke_connection(connection)
        return True
    connection.team_ids = kept
    fields = ["team_ids", "ts_updated_at"]
    if str(connection.default_team_id) == team_id:
        connection.default_team_id = kept[0]
        fields.append("default_team")
    connection.save(update_fields=fields)
    record_security_event(
        SecurityAction.MCP_UPDATED,
        request=request,
        team_id=team_id,
        target_type="mcp_connection",
        target_id=connection.id,
        target_label=connection.application.name or connection.application.client_id,
        metadata={"reason": "removed_by_team", "teams": len(kept)},
    )
    return False


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
