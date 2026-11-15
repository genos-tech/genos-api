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
"""

from __future__ import annotations

from origin.models.task.task_models import TaskMaster
from origin.views.utils.scope_guards import is_project_member


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
