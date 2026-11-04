"""`GET /api/v2/changes/`: which collections changed since I last looked (MIP-970).

The web client used to refresh nine collections once a minute (note lists, the
project list, the open project's tasks, the team task list) whether or not
anything had changed. This is the cheap question that replaces the guessing:
each collection has an opaque token that moves when, and only when, something in
it was written, and the client refetches the collections whose token moved.

    GET /api/v2/changes/?team_id=<uuid>[&project_id=<int>]

    200 {"tokens": {"tasks_project": "7f3a9c01" | null,
                    "tasks_team": "2b8e4d10",
                    "projects": "0",
                    "notes_personal": "91c0aa42",
                    "notes_task": "0",
                    "notes_chat": "5d2f7e88"}}

The contract (`tests/golden/changes_response.json`) is shared with the web
client's tests: the key set may not change on one side only.

  * Tokens are OPAQUE. The client compares them with `===` and nothing else. A
    collection nobody ever wrote reads `"0"`.
  * `tasks_project` is `null` unless `project_id` is given AND the caller is on
    that project. A project the caller cannot open never reveals that anything
    happened in it, not even by a token that moves.
  * The team-level tokens are per TEAM, not per person: they say only that
    "something in this team's notes changed", which every member of the team may
    know. The membership check below is what keeps that to members.
  * A guest team's tokens fold in its host teams' keys (see
    `change_feed.host_team_ids`), because a shared project is written under the
    host's keys.
  * The cost is a handful of indexed reads whatever the size of the workspace
    (`tests/test_changes_view.py` pins that).

`team_id` is the caller's CURRENT team and is used as sent. This view skips
`AuthenticatedAPIView`'s rewrite of `team_id` to an object's owning team
(`align_team_to_object`): for a guest looking at a host's project that rewrite
would answer with the host's team tokens instead of the guest's own, which are
the collections this client holds.

The answer is never cacheable: a token served from a cache is a change the
client never hears about.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.models.common.team_models import TeamMaster
from origin.services import change_feed
from origin.services.throttles import ChangeFeedThrottle, with_defaults
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_project_member, is_team_member


class ChangesView(AuthenticatedAPIView):
    throttle_classes = with_defaults(ChangeFeedThrottle)

    def initial(self, request, *args, **kwargs):
        # Authentication, throttles, permissions: everything the base view does
        # except rewriting `team_id` (see the module docstring).
        APIView.initial(self, request, *args, **kwargs)

    def get(self, request):
        team_id = request.GET.get("team_id")
        if not team_id:
            return Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        # Membership by the plain predicate, not `require_team_member_or_response`:
        # that guard counts a refusal toward the forbidden-burst alert (MIP-744,
        # 20 refusals a minute raises a security event). A tab left open by someone
        # just removed from the team polls this endpoint every 30-60 s from every
        # tab, which is a stale client and not a probe. `project/projects/`, which
        # this poll stands in for, asks `is_team_member` directly and never
        # counted either. The answer is the same 404 body.
        team = change_feed.norm_team(team_id)
        if (
            team is None
            or not is_team_member(team, request.user.id)
            or not TeamMaster.objects.filter(team_id=team, is_deleted=False).exists()
        ):
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
        team_id = team

        # Only a project the caller is on. Anything else (absent, malformed,
        # someone else's) is simply "no project token": never a 404 that would
        # confirm the id exists.
        project_id = change_feed.norm_project(request.GET.get("project_id"))
        if project_id is not None and not is_project_member(project_id, request.user.id):
            project_id = None

        response = Response(
            {"tokens": change_feed.tokens(team_id, project_id)}, status=status.HTTP_200_OK
        )
        response["Cache-Control"] = "no-store"
        return response
