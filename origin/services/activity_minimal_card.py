"""The card a teammate who cannot open a task gets of an @mention of them in it (MIP-1593).

A direct @mention of a team member who is not on the project is kept: it is how they find out
the thing exists, and the sender is told to add them (`nonMemberMentions`, MIP-1579). What they
are shown of the task is this module's rule. The stored row keeps everything (the title the
sockets service files, the project's name, the display id), because it is judged again whenever
it is read: the activity feed, `list_my_unread` and the briefing hold it to `can_access_task`'s
rule and give a reader who cannot open the task nothing of it (MIP-1560). The three places that
rendered it WITHOUT that judgement are the ones that call this:

  * the live `activity.created` payload (the writer's response, relayed by genos-sockets),
  * the web push,
  * the notification email and the daily digest's line (they share the push's spec).

For such a recipient they show a minimal card: who mentioned them, that it was in a task they
cannot open, the mentioned comment's own text (the sender wrote it to them), and no task title,
project name, task id, display id or status; the push and the email carry no link into the project.
(The live row keeps the project chat's and the messages' opaque ids: they open nothing for them.)

Who can open the task is `can_access_task`'s rule, in batch form: the project's member or owner,
the assignee, the reporter. It is asked once for all the rows of a write, never per row.

Rows covered: an @mention whose host is a task body (surface 5, `meta.taskId`) or a task comment
(the project-chat mirror, `message.task_id`). A task note's mention (surface 7) is a note's
rule, not a task's, and is not changed here.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from origin.models.chat.unified_models import ActivityType
from origin.services.v3_activity import SURFACE_TASK_BODY

logger = logging.getLogger(__name__)

# The push's and the email's link for a card with nothing to open: the task workspace's root,
# the fallback `webpush_dispatch._task_url` already uses when a row names no task.
MINIMAL_URL = "/workspace/tasks"

# What the live row says in place of the task's title.
MINIMAL_FIRST_LINE = "Mentioned in a task you can't open"

# What a message embedded in an activity row (`MessageSerializer`) says of its task. What stays
# is opaque: the chat's and the messages' ids, `seq`, the comment's number on its task
# (`metadata.taskCommentId`). None of those opens anything for them (the chat, the message and
# the task all answer 404) and the app labels the row from them, so they are not blanked.
_TASK_KEYS_OF_A_MESSAGE = ("taskId", "displayId", "taskStatus", "taskCommentCount")
# What of an activity's `meta` and a message's `metadata` is not about the task.
_KEPT_META = ("tsSent", "isComment", "via")


def minimal_title(actor_name: str | None) -> str:
    return f"{actor_name or 'Someone'} mentioned you in a task you can't open"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_NO_TASK = object()


def _task_of(act) -> Any:
    """The task id an @mention row is about, None when its host names none that parses (the
    row is then judged unreadable, as the feed does), or `_NO_TASK` for a row this does not
    cover."""
    if getattr(act, "activity_type", None) != ActivityType.MENTION:
        return _NO_TASK
    if act.surface_type is not None:
        if act.surface_type != SURFACE_TASK_BODY:
            return _NO_TASK
        meta = act.meta if isinstance(act.meta, dict) else {}
        return _as_int(meta.get("taskId"))
    if not act.message_id:
        return _NO_TASK
    task_id = getattr(act.message, "task_id", None)
    return _NO_TASK if task_id is None else _as_int(task_id)


def _candidates(activities: Iterable[Any]) -> dict[Any, tuple[str, int | None]]:
    """`{activity id: (recipient id, task id)}` for the rows this covers. No query for a row
    whose message is already loaded (every producer builds its rows with it)."""
    out: dict[Any, tuple[str, int | None]] = {}
    for act in activities:
        if act is None:
            continue
        task = _task_of(act)
        if task is not _NO_TASK:
            out[act.id] = (str(act.recipient_id), task)
    return out


def task_reader_pairs(
    recipient_ids: Iterable[Any], task_ids: Iterable[Any]
) -> set[tuple[str, int]]:
    """The `(recipient id, task id)` pairs `can_access_task` says yes to: the project's member
    or owner, the assignee or the reporter while still on the task's team (MIP-1592: the
    project's team, else the task's own), and the task is not soft-deleted (MIP-1566). Three
    queries, five when a holder who is not on the project is among the pairs, however many
    recipients and tasks."""
    from origin.models.project.prj_models import ProjectMaster, ProjectMembers
    from origin.models.task.task_models import TaskMaster
    from origin.views.utils.scope_guards import active_team_pairs

    who = {str(r) for r in recipient_ids if r}
    wanted = {t for t in (_as_int(i) for i in task_ids) if t is not None}
    if not who or not wanted:
        return set()
    tasks = list(
        TaskMaster.objects.filter(task_id__in=wanted, is_deleted=False).values(
            "task_id", "project_id", "assignee_id", "reporter_id", "team_id", "project__team_id"
        )
    )
    projects = {t["project_id"] for t in tasks if t["project_id"] is not None}
    on_project: set[tuple[str, int]] = set()
    if projects:
        on_project = {
            (str(attendee), project)
            for attendee, project in ProjectMembers.objects.filter(
                attendee_id__in=who, project_id__in=projects
            ).values_list("attendee_id", "project_id")
        } | {
            (str(owner), project)
            for owner, project in ProjectMaster.objects.filter(
                owner_id__in=who, project_id__in=projects
            ).values_list("owner_id", "project_id")
        }
    # The holders who are not on the project are readers only while they are on the task's team.
    held: list[tuple[str, dict]] = []
    for t in tasks:
        holders = {str(t["assignee_id"]), str(t["reporter_id"])}
        held += [(r, t) for r in who if r in holders and (r, t["project_id"]) not in on_project]
    on_team = active_team_pairs(
        {r for r, _ in held}, {t["project__team_id"] or t["team_id"] for _, t in held}
    )
    out: set[tuple[str, int]] = {
        (r, t["task_id"]) for t in tasks for r in who if (r, t["project_id"]) in on_project
    }
    out |= {
        (r, t["task_id"])
        for r, t in held
        if (r, str(t["project__team_id"] or t["team_id"])) in on_team
    }
    return out


def minimal_card_activity_ids(activities: Iterable[Any]) -> set:
    """The ids, among `activities`, of the task @mentions whose recipient cannot open the task.

    A fixed number of queries (none when no row is a task mention, three otherwise)."""
    candidates = _candidates(activities)
    if not candidates:
        return set()
    readable = task_reader_pairs(
        {r for r, _ in candidates.values()},
        {t for _, t in candidates.values() if t is not None},
    )
    return {
        act_id
        for act_id, (recipient, task) in candidates.items()
        if task is None or (recipient, task) not in readable
    }


def minimal_card_ids_failing_closed(activities: Iterable[Any]) -> set:
    """`minimal_card_activity_ids`, for a caller that must not raise: when the check fails, every
    task @mention is treated as a stranger's, so a failure shows less and never more."""
    activities = [a for a in activities if a is not None]
    try:
        return minimal_card_activity_ids(activities)
    except Exception:  # noqa: BLE001 — a notification path never breaks its writer
        logger.warning("minimal-card check failed; showing the minimal card", exc_info=True)
        try:
            return set(_candidates(activities))
        except Exception:  # noqa: BLE001
            return {a.id for a in activities}


def minimal_wire(data: dict) -> dict:
    """One serialized activity row (`ActivitySerializer`), as a stranger to the task reads it."""
    out = dict(data)
    meta = out.get("meta") if isinstance(out.get("meta"), dict) else {}
    kept = {k: meta[k] for k in _KEPT_META if k in meta}
    kept["noAccess"] = True
    message = out.get("message")
    if isinstance(message, dict):
        message = dict(message)
        for key in _TASK_KEYS_OF_A_MESSAGE:
            if key in message:
                message[key] = None
        out["message"] = message
    else:
        kept["firstLineContent"] = MINIMAL_FIRST_LINE
    out["meta"] = kept
    return out


def activities_wire(activities: Iterable[Any]) -> list:
    """`ActivitySerializer(activities, many=True).data`, with a stranger to the task shown the
    minimal card. For the response a write hands the sockets service to relay."""
    from origin.serializers.chat.unified_serializers import ActivitySerializer

    rows = [a for a in activities if a is not None]
    data = ActivitySerializer(rows, many=True).data
    minimal = minimal_card_ids_failing_closed(rows)
    if not minimal:
        return data
    return [minimal_wire(d) if row.id in minimal else d for row, d in zip(rows, data)]
