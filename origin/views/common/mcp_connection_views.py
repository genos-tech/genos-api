"""Settings > Connected apps: what a member has connected over MCP, and ending it (MIP-1079).

    GET    /api/v2/mcp/connections/        the member's live connections
    PATCH  /api/v2/mcp/connections/<id>/   change permissions or teams
    DELETE /api/v2/mcp/connections/<id>/   disconnect (every token ends at once)

Only the member's own, JWT only (an API key must not be able to widen or end an app's access),
and a connection that is someone else's, revoked or unknown is the same 404. NOT dark: a
member must be able to see and end what they connected even while OAuth is switched off (the
list is simply empty until anything was ever connected).

A change takes effect on the app's next call: its access is read from the connection each time,
never copied into the token, so narrowing teams or turning off deleting needs no new sign-in.
"""

from __future__ import annotations

from django.db import transaction
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMembers
from origin.services import mcp_connections, mcp_oauth
from origin.services.member_roles import can_remove_member, resolve_team_role
from origin.services.security_events import record_security_event
from origin.services.throttles import WindowRateThrottle
from origin.views.common.api_key_views import _managed_team
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.common.mcp_consent_views import _problem, _teams_of


class McpConnectionsThrottle(WindowRateThrottle):
    scope = "mcp_connections"
    fail_closed = True


def _serialize(connection: McpConnection, teams: dict[str, str]) -> dict:
    """`teams` is id → name for the teams the member is in now: a team they left stops
    showing (and stops working) without anyone editing the connection."""
    live = [t for t in connection.team_ids if t in teams]
    default = str(connection.default_team_id)
    return {
        "id": str(connection.id),
        "client": mcp_oauth.client_summary(connection.application),
        "scope": connection.scope,
        "allowDestructive": connection.allow_destructive,
        "defaultTeamId": default if default in teams else None,
        "teamIds": live,
        "teams": [{"id": t, "name": teams[t]} for t in live],
        "lastUsedAt": connection.last_used_at,
        "createdAt": connection.ts_created_at,
    }


class _Base(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [McpConnectionsThrottle]

    @staticmethod
    def _team_names(user) -> dict[str, str]:
        return {str(t.team_id): t.team_name for t in _teams_of(user)}


class McpConnectionListView(_Base):
    def get(self, request):
        teams = self._team_names(request.user)
        rows = McpConnection.objects.filter(
            user=request.user, revoked_at__isnull=True
        ).select_related("application")
        return Response(
            {
                "connections": [_serialize(c, teams) for c in rows],
                # Every team the member is in now, so the page can offer to add one.
                "availableTeams": [{"id": t, "name": n} for t, n in teams.items()],
            }
        )


class McpConnectionDetailView(_Base):
    def _mine(self, request, connection_id) -> McpConnection:
        connection = (
            McpConnection.objects.filter(
                id=connection_id, user=request.user, revoked_at__isnull=True
            )
            .select_related("application")
            .first()
        )
        if connection is None:
            raise Http404
        return connection

    def patch(self, request, connection_id):
        connection = self._mine(request, connection_id)
        body = request.data
        if not any(k in body for k in ("scopes", "team_ids", "default_team_id")):
            return _problem("invalid_request", "Nothing to change.")
        teams = self._team_names(request.user)

        scope, destructive = connection.scope, connection.allow_destructive
        if "scopes" in body:
            granted, why = mcp_oauth.check_scopes(body.get("scopes"), list(mcp_oauth.SCOPES))
            if why:
                return _problem("invalid_request", why)
            scope = "write" if "mcp:write" in granted else "read"
            destructive = "mcp:destructive" in granted

        team_ids = list(connection.team_ids)
        if "team_ids" in body:
            team_ids, why = mcp_oauth.check_team_ids(body.get("team_ids"))
            if why:
                return _problem("invalid_request", why)
        if not all(t in teams for t in team_ids):
            return _problem("invalid_teams", "You can only choose teams you belong to.")
        default = str(body.get("default_team_id", connection.default_team_id))
        if default not in team_ids:
            return _problem("invalid_request", "The default team must be one of the chosen teams.")

        with transaction.atomic():
            connection.scope = scope
            connection.allow_destructive = destructive
            connection.team_ids = team_ids
            connection.default_team_id = default
            connection.save()
            record_security_event(
                SecurityAction.MCP_UPDATED,
                actor=request.user,
                team_id=connection.default_team_id,
                target_type="mcp_connection",
                target_id=connection.id,
                target_label=connection.application.name or connection.application.client_id,
                metadata={
                    "scope": scope,
                    "allow_destructive": destructive,
                    "teams": len(team_ids),
                },
            )
        return Response(_serialize(connection, teams))

    def delete(self, request, connection_id):
        mcp_connections.revoke_connection(self._mine(request, connection_id))
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- The team's own view of the connected AI apps in it (MIP-1371) -----------------------


def _team_connection_row(connection: McpConnection, team_id: str, revocable: bool) -> dict:
    user = connection.user
    return {
        "id": str(connection.id),
        "userId": str(user.id),
        "userName": user.username,
        "userEmail": user.email,
        "client": mcp_oauth.client_summary(connection.application),
        "scope": connection.scope,
        "allowDestructive": connection.allow_destructive,
        "isDefaultTeam": str(connection.default_team_id) == team_id,
        "otherTeams": max(len(connection.team_ids) - 1, 0),
        "lastUsedAt": connection.last_used_at,
        "createdAt": connection.ts_created_at,
        "revocable": revocable,
    }


class TeamMcpConnectionListView(_Base):
    """GET /api/v2/team/mcp-connections/?team_id= — every connected AI app that can work in
    the team, whoever connected it (owner or editor). Who may END each is `revocable`."""

    def get(self, request):
        team, role = _managed_team(request)
        if team is None:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
        team_id = str(team.team_id)
        live = {
            str(m)
            for m in TeamMembers.objects.filter(team=team, is_deleted=False).values_list(
                "attendee_id", flat=True
            )
        } | {str(team.owner_id)}
        rows = (
            McpConnection.objects.filter(revoked_at__isnull=True, team_ids__contains=[team_id])
            .select_related("application", "user")
            .order_by("user__username", "-ts_created_at")
        )
        return Response(
            {
                "connections": [
                    _team_connection_row(
                        c,
                        team_id,
                        str(c.user_id) == str(request.user.id)
                        or can_remove_member(role, resolve_team_role(team, c.user_id)),
                    )
                    for c in rows
                    if str(c.user_id) in live
                ]
            }
        )


class TeamMcpConnectionDetailView(_Base):
    """DELETE /api/v2/team/mcp-connections/<id>/?team_id= — end an app's access to THIS team.

    Their other teams are untouched; an app left with no team ends. The owner may do it for
    anyone, an editor only for a viewer or guest (`can_remove_member`), and anyone for their
    own (the Settings page already does that). Same 404 for an unknown connection, another
    team's, or a team the caller does not manage."""

    def delete(self, request, connection_id):
        team, role = _managed_team(request)
        if team is None:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
        team_id = str(team.team_id)
        connection = (
            McpConnection.objects.filter(
                id=connection_id, revoked_at__isnull=True, team_ids__contains=[team_id]
            )
            .select_related("application", "user")
            .first()
        )
        if connection is None:
            raise Http404
        if str(connection.user_id) != str(request.user.id) and not can_remove_member(
            role, resolve_team_role(team, connection.user_id)
        ):
            return Response(
                {"error": "Only the team owner can end an editor's connection."},
                status=status.HTTP_403_FORBIDDEN,
            )
        with transaction.atomic():
            ended = mcp_connections.drop_team(connection, team_id, request=request)
        return Response({"id": str(connection.id), "ended": ended}, status=status.HTTP_200_OK)
