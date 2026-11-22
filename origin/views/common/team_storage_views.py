"""How many bytes of uploads a team holds, for its owner (MIP-1962).

    GET /api/v2/team/storage/?team_id=<uuid>
    -> {"teamId", "totalBytes", "kinds": {"task_attachment": {"count", "bytes"}, ...}}

Read-only measurement; no cap. Owner only, with the same 404 for "no such team", "not a member" and
"not the owner" as the team's security log. Session (JWT) only. Rows stored before sizes were
recorded count as 0 bytes until `backfill_storage_sizes` has run.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.services.storage_usage import team_storage_usage
from origin.views.common.api_key_views import _owned_team, _team_not_found
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


class TeamStorageUsageView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        team = _owned_team(request)
        if team is None:
            return _team_not_found()
        usage = team_storage_usage(team.team_id)
        return Response(
            {
                "teamId": str(team.team_id),
                "totalBytes": usage["total_bytes"],
                "kinds": usage["kinds"],
            },
            status=status.HTTP_200_OK,
        )
