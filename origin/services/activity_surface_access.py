"""Which channel-less activity rows may their recipient still open? (MIP-1560)

An activity row of a task body (surface 5) or of a personal / task / chat note (6 / 7 / 8) has no
channel, so the chat rule of MIP-1513 never judged it, yet its `meta` (title, first line of the
text, display id, project id) is content of the task or note it points at. Once the recipient has
been taken off the project, lost the grant, or left the chat of a chat note, they can no longer
open that thing, so the feed must not hand back its `meta`.

The rule per row is the one the product applies to open the thing itself, not a new one:

  * 5 task body   `scope_guards.can_access_task`: on the project (member or owner), the assignee,
                  or the reporter. A task that no longer exists is not readable.
  * 6 / 7 / 8     `note_role.get_effective_role` (an explicit grant, project membership for a
                  task note, the chat's readers for a chat note, a team folder for a personal
                  note), in its batch form `readable_note_ids`. A personal note's owner reads it
                  (`acl.may_read_note`: the owner is a personal note's whole ACL); a chat note's
                  and a task note's author who left is a stranger.

A row that does not say which task / note it is about gets no benefit of the doubt: it is
unreadable. Everything is asked in a fixed number of queries per surface type, never per row;
`tests/test_activity_feed_surface_access.py` pins the batch equal to the per-row rules.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services.v3_activity import (
    SURFACE_CHAT_NOTE,
    SURFACE_PERSONAL_NOTE,
    SURFACE_TASK_BODY,
    SURFACE_TASK_NOTE,
)
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    readable_note_ids,
)

# The surfaces this module judges, and the note type each note surface is.
COVERED_SURFACES = (SURFACE_TASK_BODY, SURFACE_PERSONAL_NOTE, SURFACE_TASK_NOTE, SURFACE_CHAT_NOTE)
_NOTE_TYPE = {
    SURFACE_PERSONAL_NOTE: NOTE_TYPE_PERSONAL,
    SURFACE_TASK_NOTE: NOTE_TYPE_TASK,
    SURFACE_CHAT_NOTE: NOTE_TYPE_CHAT,
}


def _as_int(value):
    """`int(value)` for an int or a digit string, else None (a bool is not an id)."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _readable_tasks(user_id, task_ids: set[int]) -> set[int]:
    """The subset of `task_ids` `can_access_task(task_id, user_id)` says yes to: the assignee,
    the reporter, the project's owner, or a project member. Three queries."""
    me = str(user_id)
    tasks = list(
        TaskMaster.objects.filter(task_id__in=task_ids).values(
            "task_id", "project_id", "assignee_id", "reporter_id"
        )
    )
    projects = {t["project_id"] for t in tasks if t["project_id"] is not None}
    on_project = set(
        ProjectMembers.objects.filter(attendee=user_id, project_id__in=projects).values_list(
            "project_id", flat=True
        )
    ) | set(
        ProjectMaster.objects.filter(owner=user_id, project_id__in=projects).values_list(
            "project_id", flat=True
        )
    )
    return {
        t["task_id"]
        for t in tasks
        if me in {str(t["assignee_id"]), str(t["reporter_id"])} or t["project_id"] in on_project
    }


def _readable_notes(user_id, surface: int, note_ids: set[int]) -> set[int]:
    """The subset of `note_ids` (all of one note surface) the user can open now."""
    owned: set[int] = set()
    if surface == SURFACE_PERSONAL_NOTE:
        owned = set(
            PersonalNoteMaster.objects.filter(note_id__in=note_ids, owner=user_id).values_list(
                "note_id", flat=True
            )
        )
    return owned | readable_note_ids(user_id, _NOTE_TYPE[surface], note_ids - owned)


def unreadable_surface_activity_ids(user_id, rows: Iterable[tuple]) -> set:
    """The ids, among `rows` (`(activity_id, surface_type, meta)`), of the task-body and note
    rows whose task / note `user_id` can no longer open. Other surfaces are never judged.

    A missing `user_id` reads nothing, so every covered row comes back unreadable.
    """
    keyed: dict[int, list[tuple]] = defaultdict(list)  # surface -> [(activity_id, thing id)]
    for activity_id, surface, meta in rows:
        if surface not in COVERED_SURFACES:
            continue
        meta = meta if isinstance(meta, dict) else {}
        key = "taskId" if surface == SURFACE_TASK_BODY else "noteId"
        keyed[surface].append((activity_id, _as_int(meta.get(key))))

    unreadable: set = set()
    for surface, items in keyed.items():
        wanted = {thing for _, thing in items if thing is not None}
        if user_id is None or not wanted:
            readable: set[int] = set()
        elif surface == SURFACE_TASK_BODY:
            readable = _readable_tasks(user_id, wanted)
        else:
            readable = _readable_notes(user_id, surface, wanted)
        unreadable |= {aid for aid, thing in items if thing is None or thing not in readable}
    return unreadable
