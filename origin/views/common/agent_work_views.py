"""`GET /api/v2/agent/work/?team_id=&days=&project_id=&agent=&user_id=&kind=&limit=&cursor=` (MIP-1613).

What the AI apps did in one team, for the Agent Activity tab: totals by agent, who used which agent,
the work by project, the decisions recorded, and the viewer's connected apps. The rules are in
`services/agent_work`. A viewer outside the team gets the sibling endpoints' 404.

Query: `days` 1 to 31 (default 7); `agent` one of claude, chatgpt, api_key, other_app, person;
`kind` one of task_created, task_status, task_updated, task_comment, note, decision; `limit` 1 to
200 (default 50, larger is clamped); `cursor` the `next_cursor` of the previous page (opaque; an
offset into the newest-first list, at most 2000).
"""

from __future__ import annotations

import uuid

from rest_framework import status
from rest_framework.response import Response

from origin.services import agent_work
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import require_team_member_or_response


def _bad(message: str) -> Response:
    return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)


def _int(raw, default=None):
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise agent_work.BadFilter(raw) from None


class AgentWorkView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        if not team_id:
            return _bad("team_id is required.")
        if res := require_team_member_or_response(request.user, team_id):
            return res
        try:
            days = _int(request.GET.get("days"), agent_work.DEFAULT_DAYS)
            if not 1 <= days <= agent_work.MAX_DAYS:
                raise agent_work.BadFilter("days")
            project_id = _int(request.GET.get("project_id"))
            agent = request.GET.get("agent") or None
            if agent is not None and agent not in agent_work.AGENTS:
                raise agent_work.BadFilter("agent")
            kind = request.GET.get("kind") or None
            if kind is not None and kind not in agent_work.KINDS:
                raise agent_work.BadFilter("kind")
            user_id = request.GET.get("user_id") or None
            if user_id is not None:
                user_id = str(uuid.UUID(user_id))
            limit = _int(request.GET.get("limit"), agent_work.DEFAULT_LIMIT)
            limit = max(1, min(limit, agent_work.MAX_LIMIT))
            offset = _int(request.GET.get("cursor"), 0)
            if not 0 <= offset <= agent_work.MAX_OFFSET:
                raise agent_work.BadFilter("cursor")
        except (agent_work.BadFilter, ValueError, TypeError, AttributeError):
            return _bad("Invalid query: days, project_id, agent, kind, user_id, limit or cursor.")

        filters = agent_work.Filters(
            days=days, project_id=project_id, agent=agent, user_id=user_id, kind=kind
        )
        payload = agent_work.build(
            viewer_id=request.user.id,
            team_id=team_id,
            f=filters,
            offset=offset,
            limit=limit,
        )
        return Response(payload, status=status.HTTP_200_OK)
