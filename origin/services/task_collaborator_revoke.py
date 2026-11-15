"""A person who leaves a project stops being a collaborator on its tasks (MIP-1567).

A collaborator is told about a task (`task_collaborators.notifiable_collaborator_ids`), and only
someone who can open the task is: a member of its project, or its assignee or reporter
(`can_access_task`). Joining the project is what made a collaborator a reader; leaving it takes
that back, the way it takes back the Owner row an author wrote on the project's task notes
(`note_role_revoke.revoke_task_author_roles`, the same `ProjectMembers` `post_delete` hook).

What goes, and what does not:

  * the collaborator rows of the leaver on the tasks of that project; a milestone's collaborators
    ride on its backing task, which is one of them;
  * NOT where they are the task's assignee or reporter: that stays a grant of its own
    (`can_access_task`), and the row is theirs to keep or clear;
  * nothing when they are still on the project (its owner has no `ProjectMembers` row, so the
    row that went is not what made them a reader) or when the project is gone.

Coming back does not restore the rows: unlike an author's Owner row there is no record of who
collaborated before, and being told about a task again is a choice for the task's people.

Two more places take the same rows off, by the same rule (`can_access_task`, in batch form):

  * a task moved to another project (`task_project_move.relocate_task_satellites`, which every
    move path runs): the collaborators who are not on the destination, except the assignee and the
    reporter, which the rule counts as readers;
  * the cleanup of rows listed before the rule held: `manage.py prune_unreadable_task_collaborators`.
"""

from __future__ import annotations

from collections import defaultdict

from origin.models.task.task_models import TaskMaster
from origin.views.utils.scope_guards import is_project_member, project_reader_ids


def drop_collaborator_on_project_leave(*, user_id, project_id) -> int:
    """`user_id` left `project_id`: take them off the collaborators of its tasks, except where
    they are the assignee or the reporter. Returns how many rows went."""
    if not user_id or not project_id or is_project_member(project_id, user_id):
        return 0
    rows = TaskMaster.collaborators.through.objects.filter(
        customuser_id=user_id, taskmaster__project_id=project_id
    ).exclude(taskmaster__assignee_id=user_id)
    rows = rows.exclude(taskmaster__reporter_id=user_id)
    deleted, _ = rows.delete()
    return deleted


def unreadable_collaborator_rows(task_ids=None) -> list[tuple[int, int]]:
    """`(row pk, task id)` of the collaborator rows whose person cannot open their task, for
    `task_ids` or, left out, for every task.

    The rule is `can_access_task`'s (project member or owner, assignee, reporter), decided for
    all rows in a fixed number of queries: one for the rows with their task's project, assignee
    and reporter, and two per distinct project. A task in no project keeps only its assignee and
    reporter. Soft-deleted tasks are judged like any other: the rule does not look at that.
    """
    rows = TaskMaster.collaborators.through.objects.values_list(
        "pk",
        "taskmaster_id",
        "customuser_id",
        "taskmaster__project_id",
        "taskmaster__assignee_id",
        "taskmaster__reporter_id",
    )
    if task_ids is not None:
        rows = rows.filter(taskmaster_id__in=[t for t in task_ids if t is not None])
    rows = list(rows)
    people_by_project: dict = defaultdict(set)
    for _pk, _task, person, project_id, _assignee, _reporter in rows:
        if project_id is not None:
            people_by_project[project_id].add(str(person))
    readers = {
        project_id: project_reader_ids(project_id, people)
        for project_id, people in people_by_project.items()
    }
    out = []
    for pk, task_id, person, project_id, assignee_id, reporter_id in rows:
        who = str(person)
        if who in {str(assignee_id), str(reporter_id)} or who in readers.get(project_id, ()):
            continue
        out.append((pk, task_id))
    return out


def drop_unreadable_collaborators(task_ids=None) -> int:
    """Take the collaborators who cannot open their task off `task_ids` (every task when left
    out). Returns how many rows went."""
    rows = unreadable_collaborator_rows(task_ids)
    if not rows:
        return 0
    deleted, _ = TaskMaster.collaborators.through.objects.filter(
        pk__in=[pk for pk, _task in rows]
    ).delete()
    return deleted
