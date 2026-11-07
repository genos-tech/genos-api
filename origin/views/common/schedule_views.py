"""Settings > Schedules: a member's own schedules over REST (MIP-1150).

    GET    /api/v2/schedules/?team_id=            the schedules, the jobs they can run, the limit
    POST   /api/v2/schedules/                     create one (`team_id` in the body)
    PATCH  /api/v2/schedules/<id>/?team_id=       change, pause (`enabled: false`) or resume one
    DELETE /api/v2/schedules/<id>/?team_id=       delete one
    POST   /api/v2/schedules/<id>/run/?team_id=   run one now (once a minute at most)

The same service as the MCP tools (`services/schedules.py`), so a schedule made here and one
made from Claude are the same rows with the same rules, and the words come from the same place
(`services/schedule_text.py`). Only the member's own, in the team they name, and JWT only: an
API key does not manage schedules. A schedule that is someone else's, in another team or not
an id at all is the same 404. Every refusal is one `{"error": "<sentence>"}` the page shows as
written.
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedule_defaults, schedule_text, schedules
from origin.services.throttles import WindowRateThrottle
from origin.services.user_time import user_zone
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import as_uuid


class SchedulesThrottle(WindowRateThrottle):
    scope = "schedules"
    rate = "120/min"
    fail_closed = True


def _error(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response({"error": message}, status=code)


# The recurrence and content fields a request may carry, camelCase on the wire.
_FIELDS = {
    "name": "name",
    "job": "job",
    "params": "params",
    "frequency": "frequency",
    "timeOfDay": "time_of_day",
    "weekdays": "weekdays",
    "monthDay": "month_day",
    "runDate": "run_date",
    "deliverPush": "deliver_push",
}


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    return {service: body[wire] for wire, service in _FIELDS.items() if wire in body}


def _row(sched, zone) -> dict[str, Any]:
    """The service's words, with the camelCase keys the rest of Settings uses."""
    p = schedule_text.present(sched, zone)
    out: dict[str, Any] = {
        "id": p["schedule_id"],
        "name": p["name"],
        "job": p["job"],
        "jobTitle": p["job_title"],
        "params": p["params"],
        "when": p["when"],
        "frequency": p["frequency"],
        "timeOfDay": p["time_of_day"],
        "enabled": p["enabled"],
        "deliverPush": p["deliver_push"],
        "nextRun": p["next_run"],
        "lastRun": p["last_run"],
        "lastStatus": p["last_status"],
    }
    for wire, key in (
        ("weekdays", "weekdays"),
        ("monthDay", "month_day"),
        ("runDate", "run_date"),
        ("pausedReason", "paused_reason"),
        ("lastError", "last_error"),
        ("lastResult", "last_result"),
        ("problem", "problem"),
    ):
        if key in p:
            out[wire] = p[key]
    return out


class _Base(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [SchedulesThrottle]

    def _ctx(self, request, team_id: Any) -> ToolContext | Response:
        """The member and the team they name; a team that is not a UUID or that they are not
        in is refused by the service as the same "not found" an unknown schedule gets."""
        team = as_uuid(team_id)
        if team is None:
            return _error("`team_id` is required.")
        return ToolContext(team_id=str(team), user_id=str(request.user.id))

    @staticmethod
    def _run(call):
        try:
            return call(), None
        except schedules.ScheduleNotFound as exc:
            return None, _error(str(exc), status.HTTP_404_NOT_FOUND)
        except schedules.ScheduleError as exc:
            return None, _error(str(exc))


class ScheduleListView(_Base):
    def get(self, request):
        ctx = self._ctx(request, request.query_params.get("team_id"))
        if isinstance(ctx, Response):
            return ctx
        # A member's first list in their default team creates the suggested schedules
        # (MIP-1178). Never raises, and only for a live member of this team.
        schedule_defaults.seed_if_default_team(request.user, ctx.team_id)
        rows, problem = self._run(lambda: schedules.list_schedules(ctx))
        if problem:
            return problem
        zone = user_zone(request.user)
        return Response(
            {
                "timeZone": zone.key,
                "limit": schedules.MAX_SCHEDULES_PER_MEMBER,
                "jobs": schedule_text.job_catalog(),
                "schedules": [_row(s, zone) for s in rows],
                # What the member does not have of the two defaults: one step to (re-)add.
                "suggestions": [
                    {
                        "key": d["key"],
                        "name": d["name"],
                        "job": d["job"],
                        "params": d["params"],
                        "frequency": d["frequency"],
                        "timeOfDay": d["time_of_day"],
                    }
                    for d in schedule_defaults.suggestions_for(rows)
                ],
            }
        )

    def post(self, request):
        body = request.data
        ctx = self._ctx(request, body.get("team_id"))
        if isinstance(ctx, Response):
            return ctx
        fields = _changes(body)
        sched, problem = self._run(
            lambda: schedules.create(
                ctx,
                name=fields.get("name"),
                job=fields.get("job"),
                params=fields.get("params"),
                frequency=fields.get("frequency"),
                time_of_day=fields.get("time_of_day"),
                weekdays=fields.get("weekdays"),
                month_day=fields.get("month_day"),
                run_date=fields.get("run_date"),
                deliver_push=bool(fields.get("deliver_push", False)),
            )
        )
        if problem:
            return problem
        return Response({"schedule": _row(sched, user_zone(request.user))}, status=201)


class ScheduleDetailView(_Base):
    def patch(self, request, schedule_id):
        body = request.data
        ctx = self._ctx(request, request.query_params.get("team_id"))
        if isinstance(ctx, Response):
            return ctx
        changes = _changes(body)
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return _error("`enabled` must be true or false.")
        if not changes and enabled is None:
            return _error("Nothing to change.")

        def apply():
            sched = None
            if changes:
                sched = schedules.update(ctx, schedule_id, **changes)
            if enabled is not None:
                sched = schedules.set_enabled(ctx, schedule_id, enabled)
            return sched

        sched, problem = self._run(apply)
        if problem:
            return problem
        return Response({"schedule": _row(sched, user_zone(request.user))})

    def delete(self, request, schedule_id):
        ctx = self._ctx(request, request.query_params.get("team_id"))
        if isinstance(ctx, Response):
            return ctx
        _, problem = self._run(lambda: schedules.delete(ctx, schedule_id))
        if problem:
            return problem
        return Response(status=status.HTTP_204_NO_CONTENT)


class ScheduleRunView(_Base):
    def post(self, request, schedule_id):
        ctx = self._ctx(request, request.query_params.get("team_id"))
        if isinstance(ctx, Response):
            return ctx
        out, problem = self._run(lambda: schedules.run_now(ctx, schedule_id))
        if problem:
            return problem
        return Response(out)
