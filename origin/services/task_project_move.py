"""Moving tasks — and whole milestones — between projects.

More of a task's identity is project-scoped than the `project_id` column
itself. Its `<CODE>-<n>` display id is minted per project, its notes keep
their own copy of the owning project, and the project task list is read
with `team_id` **and** `project_id`. A move that rewrites only
`project_id` therefore produces a row that belongs to no list anyone can
open — which is exactly what moving a task into an externally shared
project did: the destination project is owned by the HOST team, the row
kept the guest team, and `GetProjectTasksView` (filtering on both)
matched it under neither the source nor the destination project. The task
appeared to simply vanish.

So a move is defined here, once, over a SET of tasks, and both callers —
the task PUT and the milestone PATCH — hand over the whole sub-tree they
are moving.
"""

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskDependency, TaskMaster, claim_project_task_number
from origin.services import change_feed, member_events
from origin.services.task_number_alias import remember_numbers
from origin.views.utils.foreign_team_scope import forget_object_team
from origin.views.utils.note_role import NOTE_TYPE_TASK

logger = logging.getLogger(__name__)

# Real task hierarchies are shallow; the cap defangs any cyclic or
# corrupt `parent_task_id` chain that might exist in the wild.
DEPTH_LIMIT = 10


def collect_descendant_task_ids(root_task_id, depth_limit=DEPTH_LIMIT):
    """Every task below `root_task_id` in the `parent_task_id` chain
    (the root itself excluded)."""
    collected = set()
    frontier = {root_task_id}
    for _ in range(depth_limit):
        if not frontier:
            break
        children = set(
            TaskMaster.objects.filter(parent_task_id__in=frontier)
            .exclude(task_id__in=collected | {root_task_id})
            .values_list("task_id", flat=True)
        )
        if not children:
            break
        collected |= children
        frontier = children
    return collected


def milestone_subtree_task_ids(milestone) -> set:
    """Every task that has to move when `milestone` moves.

    A task belongs to a milestone through the explicit FK *or* by hanging
    under its backing task — `_serialize_milestone`'s rollups count both,
    so a move has to carry both. Their descendants come along for the same
    reason a moved task's sub-tasks do, and the backing task itself is in
    the set because it IS the milestone's row in the project task table.
    """
    ids = set()
    if milestone.task_id is not None:
        ids.add(milestone.task_id)
    membership = Q(milestone_id=milestone.milestone_id)
    if milestone.task_id is not None:
        membership |= Q(parent_task_id=milestone.task_id)
    ids |= set(TaskMaster.objects.filter(membership).values_list("task_id", flat=True))
    for task_id in list(ids):
        ids |= collect_descendant_task_ids(task_id)
    return {int(task_id) for task_id in ids if task_id is not None}


def live_tasks(task_ids):
    """The tasks among `task_ids` a person still sees: not soft-deleted, and not the status
    "Deleted" the app gives a task it deletes (the row stays). What the move and convert answers
    count and judge (MIP-1583, MIP-1597), as `subTasksKept` does."""
    return TaskMaster.objects.filter(task_id__in=task_ids, is_deleted=False).exclude(
        status="Deleted"
    )


def milestone_move_task_count(milestone) -> int:
    """How many tasks go with a moving milestone that a person still sees: not its own row (that
    is the milestone) and not a deleted one (it moves too, silently)."""
    return live_tasks(milestone_subtree_task_ids(milestone) - {milestone.task_id}).count()


def people_outside(destination, people) -> set[str]:
    """The ids among `people` who may not be put on a task in `destination`: not a member of the
    project, nor of its team (`may_name_person`). The bar an assignment there uses, so a move
    inside one team, the common case, has nobody outside."""
    from origin.services.task_collaborators import may_name_person  # noqa: PLC0415

    return {
        person
        for person in {str(p) for p in people if p not in (None, "")}
        if not may_name_person(destination, person)
    }


def moving_assignees(task_ids) -> dict[str, list[str]]:
    """Who is assigned to the live tasks among `task_ids`, as `{person id: [display ids]}`."""
    people: dict[str, list[str]] = {}
    for task in (
        live_tasks(task_ids)
        .filter(assignee__isnull=False)
        .select_related("project")
        .order_by("task_id")
    ):
        people.setdefault(str(task.assignee_id), []).append(task.display_id or str(task.task_id))
    return people


def reporters_outside_destination(task_ids, destination) -> int:
    """How many DIFFERENT people filed a live task among `task_ids` and are not on `destination`
    or its team (MIP-1597). A milestone's own reporter is its backing task's, which is in the set.

    A reporter is who filed the task, not someone working it, so a move is not refused for one
    (an assignee is: `milestone_manage.move_assignee_problem`, `task_move_assignee_problem`).
    The reporter keeps reading the task through `can_access_task`'s reporter clause; the move
    reports how many people that is, so the person who moves can tell them. Nobody gains.
    """
    reporters = set(live_tasks(task_ids).values_list("reporter_id", flat=True))
    return len(people_outside(destination, reporters))


def assignee_problem_sentence(subject: str, destination, parts: list[str], *, way_out: str) -> str:
    """The refusal a move gives when an assignee is not on the destination: one wording for the
    milestone and the task, so the app and the tool say the same (`way_out` is the part that
    differs: what to change)."""
    return (
        f"This {subject} can't move to {destination.project_name}: {' and '.join(parts)} "
        "is not on that project or its team, so they would keep opening tasks that are no longer "
        f"theirs to see. {way_out} Nothing was changed."
    )


def shown_refs(refs: list[str]) -> str:
    """Up to five display ids, then how many more."""
    return ", ".join(refs[:5]) + (f" and {len(refs) - 5} more" if len(refs) > 5 else "")


#: What the app's task PUT says to do instead: it takes an `assignee` in the same save.
PUT_WAY_OUT = "Reassign them first (or send `assignee` with the move)."


def task_move_assignee_problem(
    task, destination, *, assignee=None, way_out: str = PUT_WAY_OUT
) -> str | None:
    """Why `task` may not move to `destination` because of who is assigned, or None (MIP-1597).

    The single-task PUT's side of `milestone_manage.move_assignee_problem` (MIP-1583), by the same
    bar: an assignee reads the task they hold (`can_access_task`), so a task, or one of the
    sub-tasks that move with it, whose assignee is not on the destination project or its team would
    stay open to them. A move inside one team is not refused. `assignee` is the one this save sets
    (a save that sends none, or a null, keeps the stored one: `task.assignee_id`). A deleted
    sub-task moves too and is not judged. The caller must have checked that the task is going to
    another project, and that the caller is on the destination (a stranger gets a not-found
    first). `way_out` is what to do instead: the PUT's by default; `update_task` takes no assignee,
    so it names the tool that reassigns (MIP-1609).
    """
    moving = {task.task_id} | collect_descendant_task_ids(task.task_id)
    people = moving_assignees(moving - {task.task_id})
    # The task itself: the assignee it will have after this save.
    holder = assignee or task.assignee_id
    if holder not in (None, ""):
        people.setdefault(str(holder), []).insert(0, task.display_id or str(task.task_id))
    outside = people_outside(destination, people)
    refs = sorted(
        {ref for person, person_refs in people.items() if person in outside for ref in person_refs}
    )
    if not refs:
        return None
    return assignee_problem_sentence(
        "task",
        destination,
        [f"the assignee of {shown_refs(refs)}"],
        way_out=way_out,
    )


def move_report_said(report: dict) -> dict[str, int]:
    """What a tool's result says of a move's `report` (`notes_narrowed`,
    `reporters_outside_destination`): only the counts that are not zero, so a move that narrowed
    nothing answers what it always did (MIP-1583, MIP-1597; the milestone and the task tools)."""
    return {
        key: report[key]
        for key in ("notes_narrowed", "reporters_outside_destination")
        if report.get(key)
    }


def move_notice(report: dict, destination_name: str) -> str:
    """The same two counts as `move_report_said`, as an instruction to say them to the person who asked
    for the move, or "" when both are zero (MIP-1609, `update_task` only: the milestone move keeps
    its result).

    The in-app loop hands the model a result WITHOUT its `__summary__`, so a bare
    `"moved": {"notes_narrowed": 1}` was answered with "Moved X to Archive" (paid run 1), and a
    sentence at the end of the result was relayed in English but not in Japanese (run 2). The caller
    puts this in the field `tell_the_member`, first in the result, and the sentence ends on what to
    do with it."""
    narrowed = report.get("notes_narrowed", 0)
    reporters = report.get("reporters_outside_destination", 0)
    parts = []
    if narrowed:
        one = narrowed == 1
        parts.append(
            f"{narrowed} task {'note was' if one else 'notes were'} narrowed to "
            f"{destination_name}'s members: {'its author is' if one else 'their authors are'} not "
            f"on {destination_name}, so they lost their Owner role on {'it' if one else 'them'} "
            "(moving the task back to a project they are on gives it back)."
        )
    if reporters:
        one = reporters == 1
        parts.append(
            f"{reporters} {'person who filed' if one else 'people who filed'} a moved task "
            f"{'is' if one else 'are'} not on {destination_name} or its team; they keep reading "
            "what they filed."
        )
    if not parts:
        return ""
    return (
        f"{' '.join(parts)} Say so in your answer, in the language the member writes in, after "
        "saying the task moved."
    )


def move_report_clauses(report: dict, destination_name: str) -> str:
    """The end of a move tool's `__summary__` for the same counts, "" when both are zero."""
    narrowed = report.get("notes_narrowed", 0)
    reporters = report.get("reporters_outside_destination", 0)
    return (
        f"; {narrowed} task note(s) lost their author's Owner role, as the author is not "
        f"on {destination_name}"
        if narrowed
        else ""
    ) + (
        f"; {reporters} reporter(s) of moved tasks are not on {destination_name} "
        "and keep reading what they filed"
        if reporters
        else ""
    )


def relocate_tasks(
    task_ids,
    *,
    project_id,
    team_id,
    clear_sprint=False,
    clear_tags=True,
) -> int:
    """Move a set of task rows into `project_id` / `team_id`. Returns the number of task notes that
    lost their author's Owner row to it (`relocate_task_satellites`).

    For the rows that ride ALONG with a move (sub-tasks, a milestone's
    member tasks) — the ones no user re-picks anything for. The row the
    user actually edited is written by its own endpoint; call
    `relocate_task_satellites` for that one.

    `clear_tags` is on by default because tags are project-scoped rows and,
    across a team boundary, another tenant's labels: the frontend already
    clears them on the task the user moved, and nothing would ever clear
    them on the rows dragged along behind it. `custom_field_values` are
    deliberately left alone — readers resolve them against the live field
    definitions and drop unknowns, so they cost nothing where they are and
    carrying free text back is impossible once discarded.
    """
    task_ids = {int(task_id) for task_id in task_ids if task_id is not None}
    if not task_ids:
        return 0

    fields = {
        "project_id": project_id,
        "team_id": team_id,
        # Numbers are unique per project, so a rider's number can already
        # be taken in the destination. NULL is exempt from the constraint;
        # a free number is re-claimed below.
        "project_task_number": None,
        # A queryset `.update()` skips `auto_now` (and `post_save`).
        # Without this, clients that sync incrementally off `ts_updated_at`
        # never learn these rows moved.
        "ts_updated_at": timezone.now(),
    }
    if clear_sprint:
        fields["sprint_id"] = None
    if clear_tags:
        fields["tags"] = None
    from origin.signals.task_signals import update_tasks_recorded

    with transaction.atomic():
        # Their old display ids keep opening them (MIP-2497).
        remember_numbers(
            TaskMaster.objects.filter(task_id__in=task_ids, is_init_task=False).values_list(
                "project_id", "project_task_number", "task_id"
            )
        )
        update_tasks_recorded(
            TaskMaster.objects.filter(task_id__in=task_ids), cascade="project_move", **fields
        )

        # Re-fetch AFTER the bulk update so each instance carries the new
        # project — `claim_project_task_number` counts within `project_id`.
        for task in TaskMaster.objects.filter(task_id__in=task_ids):
            claim_project_task_number(task)

    return relocate_task_satellites(task_ids, project_id=project_id, team_id=team_id)


def relocate_task_satellites(task_ids, *, project_id, team_id) -> int:
    """Carry everything else keyed to a task across the boundary with it. Returns how many task
    notes lost their author's Owner row to it (the author is not on the project they went to).

    Split from `relocate_tasks` for the endpoint that writes the moved
    task's own columns through a serializer and only needs this half —
    which makes this the one function BOTH move paths run over every id
    they touch, and therefore the place for anything that has to be
    settled per moved task rather than per move.
    """
    task_ids = {int(task_id) for task_id in task_ids if task_id is not None}
    if not task_ids:
        return 0
    # Task notes denormalize the owning team and project, and the notes
    # tree is read with `team=<id>, project__in=<ids>` — so a note left
    # behind disappears from both projects' trees while still being
    # reachable from the task it hangs off.
    notes = TaskNoteMaster.objects.filter(task_id__in=task_ids)
    moved = list(notes.values_list("note_id", "team_id", "project_id"))
    moved_note_ids = [note_id for note_id, _, _ in moved]
    notes.update(project_id=project_id, team_id=team_id)
    # The Owner row of an author who is not on the project the note now lives in ends with it
    # (MIP-1493), recorded so a move to a project they ARE on writes it back (MIP-1583); the notes
    # are in the new project's search scope, whose ACL is refreshed.
    from origin.search_engine.acl_dirty import mark_projects_dirty
    from origin.services.note_role_revoke import (
        restore_task_roles_on_move,
        revoke_task_roles_on_move,
    )

    note_moves = [(note_id, old_project) for note_id, _, old_project in moved]
    narrowed = revoke_task_roles_on_move(moves=note_moves, target_project_id=project_id)
    restored = restore_task_roles_on_move(moves=note_moves, target_project_id=project_id)
    if narrowed or restored:
        mark_projects_dirty([project_id])
    # The `.update()` fires no `post_save`: tell the change feed by hand
    # (MIP-970), for the team the notes left and the one they joined.
    for old_team_id in {old for _, old, _ in moved} | {team_id}:
        change_feed.bump_notes(old_team_id, task=True)
    _settle_dependencies(task_ids, team_id=team_id)
    # A collaborator is told about a task only while they can open it (MIP-1567); in the
    # destination that is its members and owner, and the task's own assignee and reporter.
    from origin.services.task_collaborator_revoke import drop_unreadable_collaborators

    drop_unreadable_collaborators(task_ids)
    # A move is the one thing in the product that changes which team an
    # object belongs to, and the request-scoping layer caches that answer
    # for five minutes on the premise that nothing does. Left stale, it
    # hides the move from exactly the requests that have to know about it
    # — see `forget_object_team`. Both kinds of row this function has just
    # rewritten are cached under that premise.
    for task_id in task_ids:
        forget_object_team("task", task_id)
    for note_id in moved_note_ids:
        forget_object_team("note", note_id, NOTE_TYPE_TASK)
    return narrowed


def _settle_dependencies(task_ids, *, team_id) -> None:
    """Keep dependency edges within one team after a move.

    `TaskDependency` allows cross-project edges but not cross-team ones
    (see the model), and denormalizes the team so listings can scope
    cheaply. A cross-TEAM move therefore invalidates every edge touching
    the moved rows: the surviving ones get the new team, and the ones that
    would now straddle two teams are dropped rather than left pointing at
    a task the other side can never load. Same-team moves — the common
    case — lose nothing.
    """
    touching = Q(blocker_task_id__in=task_ids) | Q(blocked_task_id__in=task_ids)
    edges = list(
        TaskDependency.objects.filter(touching).values("id", "blocker_task_id", "blocked_task_id")
    )
    if not edges:
        return
    endpoints = {edge["blocker_task_id"] for edge in edges} | {
        edge["blocked_task_id"] for edge in edges
    }
    teams = dict(TaskMaster.objects.filter(task_id__in=endpoints).values_list("task_id", "team_id"))
    straddling = [
        edge["id"]
        for edge in edges
        if str(teams.get(edge["blocker_task_id"])) != str(teams.get(edge["blocked_task_id"]))
    ]
    if straddling:
        logger.info(
            "task move dropped %d cross-team dependency edge(s) for tasks=%s",
            len(straddling),
            sorted(task_ids),
        )
        # The move dropped these, the member did not choose to remove them: not their ledger's
        # `dependency_remove`.
        with member_events.not_recorded():
            TaskDependency.objects.filter(id__in=straddling).delete()
    TaskDependency.objects.filter(touching).exclude(id__in=straddling).update(team_id=team_id)
