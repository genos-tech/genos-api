"""Remove the sample data a team was made with, in one action (MIP-1706).

`GET  /api/v2/team/sample-data/?team_id=` the preview: what would go and what stays, and why.
`POST /api/v2/team/sample-data/` `{team_id}` removes exactly what the preview lists.

Owner only. An editor can manage a team, but deleting projects, chats and notes in bulk is the
destructive kind of action the owner keeps (`member_roles`: "Only they may do destructive
things"). A person outside the team (or an external guest, who has no team row) gets the same 404
as a team that does not exist; a member who is not the owner gets 403 and no preview, so a
viewer is not shown the names of what an owner could remove.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.team_models import TeamMaster
from origin.services import sample_data
from origin.services.member_roles import OWNER, resolve_team_role
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_team_member


def _require_team_owner(request, team_id):
    """`(team, None)` for the team's owner, else `(None, Response)`."""
    if not team_id:
        return None, Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    except (DjangoValidationError, ValueError):
        team = None
    if team is None or not is_team_member(team.team_id, request.user.id):
        return None, Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    if resolve_team_role(team, request.user.id) != OWNER:
        return None, Response(
            {"error": "Only the team owner can remove the sample data."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return team, None


class SampleDataView(AuthenticatedAPIView):
    def get(self, request):
        team, error = _require_team_owner(request, request.query_params.get("team_id"))
        if error is not None:
            return error
        return Response(sample_data.build_plan(team).as_dict(), status=status.HTTP_200_OK)

    def post(self, request):
        team, error = _require_team_owner(request, request.data.get("team_id"))
        if error is not None:
            return error
        result = sample_data.execute(team, request=request)
        return Response(result.as_dict(), status=status.HTTP_200_OK)
