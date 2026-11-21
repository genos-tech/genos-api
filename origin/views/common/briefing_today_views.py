"""The "today" briefing over REST (MIP-1005, MIP-1696).

    GET /api/v2/briefing/today/?team_id=&since=&project_id=

What is due or overdue for me, what waits on me (requests, mentions), what others changed on my
things since I last looked, what to look at first, what I wait on: across every project the caller
is in. It is the MCP briefing (`services/briefing.build_briefing`), reshaped for a page, so "changed"
and "waits on me" have one definition and the page and Claude can never disagree.

* The caller's own: the member comes from the token, no call takes a user id, a team the caller
  is not in is the 404 every team-scoped endpoint gives. Access is the briefing's own
  (`LabelAccess`, `readable_chats_qs`, live membership), so a private project shows only to its
  members. JWT only: an API key has the MCP tool.
* It never moves the connected-app "since my last briefing" stamp: a window is the page's own
  `since` (an ISO time the page remembers, up to 31 days back) or, without it, the local day so far.
* One extra query turns task ids into project ids (and task-note ids into their task), because a
  link in the app needs them and the briefing names only the task. The page builds the path.
* Every English sentence the briefing builds (`latest.summary`, `reasons`) comes with the same
  facts as data (`latest.data`: a `code` and its parts; `reasonCodes`), so the page says it in the
  viewer's language and uses the sentence only for a code it does not know (MIP-1936). The MCP
  tool never gets these (`structured` is off there).
* Keys are camelCase, titles and texts are the words (the briefing's fence is removed), and every
  refusal is one `{"error": "<sentence>"}`.
"""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.services import briefing
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.common.memory_views import camel, plain
from origin.views.utils.request_validators import as_uuid
from origin.views.utils.scope_guards import require_team_member_or_response


class BriefingTodayThrottle(WindowRateThrottle):
    # The briefing reads a dozen sources: a page loads it once, then on a deliberate refresh.
    scope = "briefing_today"
    rate = "20/min"
    fail_closed = True


def _plain_all(value: Any) -> Any:
    """Every string in the payload as the words (the MCP fence removed), at any depth."""
    if isinstance(value, str):
        return plain(value)
    if isinstance(value, dict):
        return {k: _plain_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain_all(v) for v in value]
    return value


def _task_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = ("task_id", "display_id", "title", "status", "priority", "due_date", "days_until_due")
    return [{**{k: r[k] for k in keep if k in r}, "project_name": r.get("project")} for r in rows]


def _change(item: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "type",
        "task_id",
        "display_id",
        "title",
        "status",
        "project",
        "assigned_to_me",
        "note_type",
        "note_id",
        "chat_id",
        "chat_type",
        "thread_id",
        "latest_at",
        "events_total",
    )
    out = {k: item[k] for k in keep if k in item}
    # The latest word, as a person reads it: who, what, and a short quote when there is one.
    last = (item.get("events") or [{}])[-1]
    out["latest"] = {
        k: last[k] for k in ("at", "what", "summary", "by", "text", "data") if k in last
    }
    return out


def _mention(row: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "source",
        "at",
        "text",
        "from",
        "by",
        "task_id",
        "display_id",
        "title",
        "chat_id",
        "chat_type",
        "chat_title",
        "thread_id",
        "message_id",
        "note_type",
        "note_id",
    )
    return {k: row[k] for k in keep if k in row}


def _request(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in ("item_id", "type", "at", "text", "from")}


def _links(payload: dict[str, Any], ctx: ToolContext) -> None:
    """Fill `project_id` on every task row and `task_id`/`project_id` on every task-note row:
    two queries however many rows there are."""
    task_rows: list[dict[str, Any]] = [
        *payload["overdue"],
        *payload["dueToday"],
        *payload["suggestedNext"],
        *payload["blocked"],
        *payload["delegated"],
        *(c for c in payload["changed"]["items"] if c.get("task_id")),
        *(m for m in payload["mentions"] if m.get("task_id")),
    ]
    note_rows = [
        r
        for r in (*payload["mentions"], *payload["changed"]["items"])
        if r.get("note_type") == "task" and r.get("note_id")
    ]
    note_ids = {int(r["note_id"]) for r in note_rows if str(r["note_id"]).isdigit()}
    notes = {
        n["note_id"]: n
        for n in TaskNoteMaster.objects.filter(note_id__in=note_ids, team_id=ctx.team_id).values(
            "note_id", "task_id", "project_id"
        )
    }
    for r in note_rows:
        found = notes.get(int(r["note_id"])) if str(r["note_id"]).isdigit() else None
        if found:
            r["task_id"], r["project_id"] = found["task_id"], found["project_id"]
    task_ids = {r["task_id"] for r in (*task_rows, *note_rows) if r.get("task_id")}
    projects = dict(
        TaskMaster.objects.filter(task_id__in=task_ids, team_id=ctx.team_id).values_list(
            "task_id", "project_id"
        )
    )
    for r in (*task_rows, *note_rows):
        if r.get("task_id") in projects:
            r["project_id"] = projects[r["task_id"]]


class BriefingTodayView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [BriefingTodayThrottle]

    def get(self, request):
        team = as_uuid(request.query_params.get("team_id"))
        if team is None:
            return Response({"error": "`team_id` is required."}, status=status.HTTP_400_BAD_REQUEST)
        if (refused := require_team_member_or_response(request.user, str(team))) is not None:
            return refused
        ctx = ToolContext(team_id=str(team), user_id=str(request.user.id))
        since = request.query_params.get("since") or None
        try:
            data = briefing.build_briefing(
                ctx,
                window="today",
                since=since,
                project_id=request.query_params.get("project_id") or None,
                structured=True,
            )
        except ToolError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        agenda, waiting = data["agenda"], data["waiting_on_me"]
        others, changed = data["waiting_on_others"], data["changed_for_me"]
        payload = {
            "window": {
                "since": data["window"]["since"],
                "until": data["window"]["until"],
                "timeZone": data["window"]["time_zone"],
                "localDate": agenda["local_date"],
            },
            "overdue": _task_rows(agenda["overdue"]),
            "overdueCount": agenda["overdue_count"],
            "dueToday": _task_rows(agenda["due_today"]),
            "dueTodayCount": agenda["due_today_count"],
            "requests": [_request(r) for r in waiting["requests"]],
            "mentions": [_mention(m) for m in waiting["mentions"]],
            "changed": {
                "items": [_change(i) for i in changed["items"]],
                "total": changed["total"],
                "truncated": changed["truncated"],
            },
            "suggestedNext": [
                {
                    **_task_rows([r])[0],
                    "reasons": r.get("reasons", []),
                    "reason_codes": r.get("reason_codes", []),
                }
                for r in data["suggested_next"]
            ],
            "blocked": [
                {
                    "task_id": b["task_id"],
                    "display_id": b["display_id"],
                    "title": b["title"],
                    "waiting_for": b.get("waiting_for", []),
                }
                for b in others["blocked_tasks"]
            ],
            "delegated": _task_rows(others["delegated"]),
            "delegatedTotal": others["delegated_total"],
        }
        _links(payload, ctx)
        return Response(camel(_plain_all(payload)))
