"""Settings > What Genos remembers (the secretary's part), over REST (MIP-1088).

    GET    /api/v2/user/memory/?team_id=                  the switch, what is stored, this team's
                                                          tuning and marks, the words (cheap)
    GET    /api/v2/user/memory/top/?team_id=&limit=       what Genos has noticed: top items, why
    PUT    /api/v2/user/memory/marks/<task_id>/?team_id=  mark a task (`{"mark": ...}`)
    DELETE /api/v2/user/memory/marks/<task_id>/?team_id=  clear the mark
    PATCH  /api/v2/user/memory/tuning/?team_id=           `{"weights": {signal: level}}`
    DELETE /api/v2/user/memory/tuning/?team_id=           every signal back to normal
    DELETE /api/v2/user/memory/<what>/                    delete: importance | summaries |
                                                          events | everything (all teams)

Pausing is the existing `PATCH /api/v2/user/preferences/member-events/`.

The same service as the MCP tools (`services/memory_control.py`, `services/importance.py`), so
the page and Claude can never disagree. Only the caller's own: the member comes from the token,
no call takes a user id, a team the caller is not in is the same 404 as one that does not exist,
and a task the caller cannot open is the same 404 as one that does not exist. JWT only: an API
key has the MCP tools. Keys are camelCase (the rest of Settings); every refusal is one
`{"error": "<sentence>"}` the page shows as written, and the one a page may branch on carries a
`code` (`recording_off`).
"""

from __future__ import annotations

import re
from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.member_importance_models import MARKS
from origin.search_engine.agent.fence import unwrap
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.services import importance, memory_control
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import as_int, as_uuid
from origin.views.utils.scope_guards import require_team_member_or_response

_SNAKE = re.compile(r"_([a-z])")


def camel(value: Any) -> Any:
    """`display_id` -> `displayId`, in every dict key at any depth. Values are never touched
    (a mark is still `not_important`, a signal is still `others`)."""
    if isinstance(value, dict):
        return {
            _SNAKE.sub(lambda m: m.group(1).upper(), str(k)): camel(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [camel(v) for v in value]
    return value


def plain(value: Any) -> str:
    """A title as the task has it: the service gives the briefing's fenced text, a page
    shows the words."""
    if not isinstance(value, str):
        return ""
    inner = unwrap(value)
    return value if inner is None else inner


class MemoryThrottle(WindowRateThrottle):
    scope = "memory"
    rate = "60/min"
    fail_closed = True


class MemoryTopThrottle(WindowRateThrottle):
    # Ranking reads a few hundred tasks and a week of activity: a page loads it once.
    scope = "memory_top"
    rate = "20/min"
    fail_closed = True


def _error(message: str, http: int = status.HTTP_400_BAD_REQUEST, **extra: Any) -> Response:
    return Response({"error": message, **extra}, status=http)


class _Base(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [MemoryThrottle]

    def _ctx(self, request) -> ToolContext | Response:
        """The member and the team they name. A team that is not a UUID is a 400, one the
        member is not in (or that does not exist) is the 404 every team-scoped endpoint gives."""
        team = as_uuid(request.query_params.get("team_id"))
        if team is None:
            return _error("`team_id` is required.")
        if (refused := require_team_member_or_response(request.user, str(team))) is not None:
            return refused
        return ToolContext(team_id=str(team), user_id=str(request.user.id))

    @staticmethod
    def _refusal(exc: ToolError) -> Response:
        if isinstance(exc, importance.TaskNotFound):
            return _error(str(exc), status.HTTP_404_NOT_FOUND)
        if isinstance(exc, importance.RecordingOff):
            return _error(str(exc), status.HTTP_409_CONFLICT, code="recording_off")
        return _error(str(exc))


class MemoryOverviewView(_Base):
    def get(self, request):
        ctx = self._ctx(request)
        if isinstance(ctx, Response):
            return ctx
        out = memory_control.overview(ctx)
        for row in out["marks"]:
            row["title"] = plain(row["title"])
        return Response(camel(out))


class MemoryTopView(_Base):
    throttle_classes = [MemoryTopThrottle]

    def get(self, request):
        ctx = self._ctx(request)
        if isinstance(ctx, Response):
            return ctx
        raw = request.query_params.get("limit")
        limit = None
        if raw not in (None, ""):
            limit = as_int(raw)
            if limit is None:
                return _error(f"`limit` must be a whole number from 1 to {importance.TOP_TOOL}.")
        try:
            seen = importance.describe(ctx, limit=limit, structured=True)
        except ToolError as exc:
            return self._refusal(exc)
        if not seen["enabled"]:
            return Response({"enabled": False, "note": seen["note"], "top": [], "suggestions": []})
        for row in (*seen["top"], *seen["suggestions"]):
            row["title"] = plain(row.get("title"))
        return Response(
            camel(
                {
                    "enabled": True,
                    "history": seen["history"],
                    "top": seen["top"],
                    "suggestions": seen["suggestions"],
                }
            )
        )


class MemoryMarkView(_Base):
    def put(self, request, task_id):
        ctx = self._ctx(request)
        if isinstance(ctx, Response):
            return ctx
        mark = request.data.get("mark")
        if mark not in MARKS:
            return _error(f"`mark` must be one of: {', '.join(MARKS)}.")
        return self._set(ctx, task_id, mark)

    def delete(self, request, task_id):
        ctx = self._ctx(request)
        if isinstance(ctx, Response):
            return ctx
        return self._set(ctx, task_id, "clear")

    def _set(self, ctx: ToolContext, task_id: int, mark: str) -> Response:
        try:
            out = importance.set_mark(ctx, task_id, mark)
        except ToolError as exc:
            return self._refusal(exc)
        return Response(camel(out))


class MemoryTuningView(_Base):
    def patch(self, request):
        ctx = self._ctx(request)
        if isinstance(ctx, Response):
            return ctx
        return self._set(ctx, weights=request.data.get("weights"))

    def delete(self, request):
        ctx = self._ctx(request)
        if isinstance(ctx, Response):
            return ctx
        return self._set(ctx, reset=True)

    def _set(self, ctx: ToolContext, **change: Any) -> Response:
        try:
            levels = importance.set_levels(ctx, **change)
        except ToolError as exc:
            return self._refusal(exc)
        return Response({"tuning": memory_control.tuning(importance.Prefs({}, levels))})


class MemoryForgetView(_Base):
    def delete(self, request, what):
        # No team: the switch and the counts the page shows are the member's, in every team.
        if what not in memory_control.WHAT:
            return _error("Nothing to delete by that name.", status.HTTP_404_NOT_FOUND)
        return Response(camel(memory_control.forget(request.user.id, what)))
