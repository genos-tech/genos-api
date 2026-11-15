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

Rows covered: an @mention whose host is a task body (surface 5, `meta.taskId`), a task comment
(the project-chat mirror, `message.task_id`) or a NOTE (surfaces 6, 7 and 8, `meta.noteId`,
MIP-1598, MIP-1599). A note is judged by the NOTE's rule (`note_role.get_effective_role`, in batch
form `note_reader_pairs`), not the task's: the assignee who is not on the project opens the task
and not its note, so a note mention shows them the minimal card, in which the note's title, the
task's display id, the chat's ids and a link naming any of them are gone. The rule per kind of
note: a task note, an explicit grant or a project member row; a personal note, its owner, an
explicit grant or a reader of the team folder it is filed in; a chat note, an explicit grant or
a member of its chat by the chat rule (`chat_note_member_q`).

A comment mention is a row of the task's project chat, which the recipient may not be able to open
although they can open the TASK (the assignee or reporter who is not on the project). The mention
is about the task, so what that person is shown follows the task's rule too: the feed keeps the
row (`task_readable_comment_mention_ids`) and the push and the email link to the task, not to a
chat that answers 404 (`task_links`). Nothing else of the chat is opened by that.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.db.models import Q

from origin.models.chat.unified_models import ActivityType
from origin.services.v3_activity import (
    SURFACE_CHAT_NOTE,
    SURFACE_PERSONAL_NOTE,
    SURFACE_TASK_BODY,
    SURFACE_TASK_NOTE,
)

logger = logging.getLogger(__name__)

# The push's and the email's link for a card with nothing to open: the task workspace's root,
# the fallback `webpush_dispatch._task_url` already uses when a row names no task.
MINIMAL_URL = "/workspace/tasks"

# The same for a note: the notes workspace's root, the fallback `webpush_dispatch._note_url` uses.
MINIMAL_NOTE_URL = "/workspace/notes"

# What the live row says in place of the task's title (or the note's).
MINIMAL_FIRST_LINE = "Mentioned in a task you can't open"
MINIMAL_NOTE_FIRST_LINE = "Mentioned in a note you can't open"

# What a message embedded in an activity row (`MessageSerializer`) says of its task. What stays
# is opaque: the chat's and the messages' ids, `seq`, the comment's number on its task
# (`metadata.taskCommentId`). None of those opens anything for them (the chat, the message and
# the task all answer 404) and the app labels the row from them, so they are not blanked.
_TASK_KEYS_OF_A_MESSAGE = ("taskId", "displayId", "taskStatus", "taskCommentCount")
# What of an activity's `meta` and a message's `metadata` is not about the task.
_KEPT_META = ("tsSent", "isComment", "via")


def minimal_title(actor_name: str | None, surface_type: int | None = None) -> str:
    what = "note" if surface_type in NOTE_SURFACES else "task"
    return f"{actor_name or 'Someone'} mentioned you in a {what} you can't open"


def minimal_url(surface_type: int | None) -> str:
    return MINIMAL_NOTE_URL if surface_type in NOTE_SURFACES else MINIMAL_URL


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_NO_TARGET = object()
_TASK = "task"

# The note surfaces, and the note type each is (`note_role.NOTE_TYPE_*`).
_NOTE_TYPE = {SURFACE_PERSONAL_NOTE: 1, SURFACE_TASK_NOTE: 2, SURFACE_CHAT_NOTE: 3}
NOTE_SURFACES = frozenset(_NOTE_TYPE)


def _target_of(act) -> Any:
    """What an @mention row is about, as `(kind, id)` (`kind` is "task", or a note type: 1
    personal, 2 task note, 3 chat note), the id None
    when its host names none that parses (the row is then judged unreadable, as the feed does),
    or `_NO_TARGET` for a row this does not cover."""
    if getattr(act, "activity_type", None) != ActivityType.MENTION:
        return _NO_TARGET
    if act.surface_type is not None:
        meta = act.meta if isinstance(act.meta, dict) else {}
        if act.surface_type == SURFACE_TASK_BODY:
            return (_TASK, _as_int(meta.get("taskId")))
        if act.surface_type in _NOTE_TYPE:
            return (_NOTE_TYPE[act.surface_type], _as_int(meta.get("noteId")))
        return _NO_TARGET
    if not act.message_id:
        return _NO_TARGET
    task_id = getattr(act.message, "task_id", None)
    return _NO_TARGET if task_id is None else (_TASK, _as_int(task_id))


def _candidates(activities: Iterable[Any]) -> dict[Any, tuple[str, Any, int | None]]:
    """`{activity id: (recipient id, kind, task or note id)}` for the rows this covers. No query
    for a row whose message is already loaded (every producer builds its rows with it)."""
    out: dict[Any, tuple[str, Any, int | None]] = {}
    for act in activities:
        if act is None:
            continue
        target = _target_of(act)
        if target is not _NO_TARGET:
            out[act.id] = (str(act.recipient_id), target[0], target[1])
    return out


def task_reader_pairs(
    recipient_ids: Iterable[Any], task_ids: Iterable[Any]
) -> set[tuple[str, int]]:
    """The `(recipient id, task id)` pairs `can_access_task` says yes to: the project's member
    or owner, the assignee or the reporter while still on the task's team (MIP-1592: the
    project's team, else the task's own), and the task is not soft-deleted (MIP-1566). Three
    queries, five when a holder who is not on the project is among the pairs, however many
    recipients and tasks.

    The ONE place this rule is written for the mention card, the activity feed's gate and the
    comment-mention row (MIP-1598); `tests/test_mention_followups.py` pins it equal to
    `can_access_task` over every kind of person, so a change to the rule there shows up here."""
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
    """The ids, among `activities`, of the task and note @mentions whose recipient cannot open
    the task (the task's rule) or the note (the note's rule).

    A fixed number of queries: none when no row is such a mention, at most five for the task
    rows and, for each type of note present, at most seven (`note_reader_pairs`), however many
    rows, recipients and notes."""
    candidates = _candidates(activities)
    if not candidates:
        return set()
    from origin.views.utils.note_role import note_reader_pairs

    readable: dict[Any, set] = {}
    for kind in {k for _, k, _ in candidates.values()}:
        wanted = [(r, i) for r, k, i in candidates.values() if k == kind]
        who, things = {r for r, _ in wanted}, {i for _, i in wanted}
        readable[kind] = (
            task_reader_pairs(who, things)
            if kind == _TASK
            else note_reader_pairs(who, kind, things)
        )
    return {
        act_id
        for act_id, (recipient, kind, target) in candidates.items()
        if target is None or (recipient, target) not in readable[kind]
    }


# A comment mention: the comment is mirrored into the project chat as a reply carrying its number
# (`metadata.taskCommentId`), the same test `unread_digest` uses for "a comment on the task". A
# mention in any other message of that chat (a reply under the task card, a plain chat message) is
# the chat's content and is never judged by the task's rule.
COMMENT_MENTION_Q = Q(
    activity_type=ActivityType.MENTION,
    surface_type__isnull=True,
    message__task_id__isnull=False,
    message__metadata__has_key="taskCommentId",
)


def _is_comment_mention(act) -> bool:
    if getattr(act, "activity_type", None) != ActivityType.MENTION:
        return False
    if act.surface_type is not None or not act.message_id:
        return False
    message = act.message
    metadata = getattr(message, "metadata", None)
    return getattr(message, "task_id", None) is not None and (
        isinstance(metadata, dict) and "taskCommentId" in metadata
    )


def task_readable_comment_mention_ids(user_id: Any, rows, *, limit: int) -> set:
    """Among the comment-mention rows in `rows` (a queryset of the caller's own `Activity` rows in
    chats they cannot read), the ids whose task `user_id` can open: the activity feed keeps them
    (MIP-1598). The mention is about the task, so the task's rule decides, and it is the card's
    (`task_reader_pairs`). One query for the newest `limit` candidates and three for the rule."""
    candidates = tuple(
        rows.filter(COMMENT_MENTION_Q)
        .order_by("-ts_created_at", "-id")
        .values_list("id", "message__task_id")[:limit]
    )
    if not candidates:
        return set()
    readable = task_reader_pairs([user_id], {task for _, task in candidates})
    return {act_id for act_id, task in candidates if (str(user_id), _as_int(task)) in readable}


def task_links(activities: Iterable[Any], *, skip: Iterable[Any] = ()) -> dict:
    """`{activity id: link to the task}` for the comment mentions whose recipient can open the
    task and not the project chat it is mirrored into (a project chat is read by a project member
    row: the assignee or reporter who is not on the project, or the project's owner with no row,
    cannot open it). The push and the email would link to that chat, which answers 404 for them.

    `skip`: the ids already shown the minimal card (they link to nothing). Two queries however
    many rows, none when no row is a comment mention."""
    from origin.models.project.prj_models import ProjectMembers
    from origin.models.task.task_models import TaskMaster

    skip = set(skip)
    rows = {
        a.id: (str(a.recipient_id), _as_int(a.message.task_id))
        for a in activities
        if a is not None and a.id not in skip and _is_comment_mention(a)
    }
    if not rows:
        return {}
    projects = dict(
        TaskMaster.objects.filter(
            task_id__in={t for _, t in rows.values() if t is not None}, is_deleted=False
        ).values_list("task_id", "project_id")
    )
    on_project = {
        (str(attendee), project)
        for attendee, project in ProjectMembers.objects.filter(
            attendee_id__in={r for r, _ in rows.values()},
            project_id__in={p for p in projects.values() if p is not None},
        ).values_list("attendee_id", "project_id")
    }
    return {
        act_id: f"/workspace/tasks/project/{projects[task]}/task/{task}"
        for act_id, (recipient, task) in rows.items()
        if projects.get(task) is not None and (recipient, projects[task]) not in on_project
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


def task_links_failing_open(activities: Iterable[Any], *, skip: Iterable[Any] = ()) -> dict:
    """`task_links`, for a caller that must not raise. A failure gives no link override: the
    notification keeps the link it had, to the chat, which opens nothing for someone who cannot
    read it (it shows nothing more than before)."""
    activities = [a for a in activities if a is not None]
    try:
        return task_links(activities, skip=skip)
    except Exception:  # noqa: BLE001 — a notification path never breaks its writer
        logger.warning("task-link check failed; keeping the chat link", exc_info=True)
        return {}


def minimal_wire(data: dict) -> dict:
    """One serialized activity row (`ActivitySerializer`), as a stranger to the task or the note
    reads it."""
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
        kept["firstLineContent"] = (
            MINIMAL_NOTE_FIRST_LINE
            if out.get("surfaceType") in NOTE_SURFACES
            else MINIMAL_FIRST_LINE
        )
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
