"""Turn a root task into a milestone, and a milestone back into a task (MIP-1042).

A milestone is a task row (`is_milestone=True`) plus a `MilestoneMaster` that
holds what only a milestone has: its own id, a folder, a sprint, several
assignees. So a conversion KEEPS THE ROW. The task's id, display id, body,
comments, notes, attachments, history, dependencies, custom fields and
collaborators are the same afterwards, and only the side record is added or
retired.

Two rules shape both directions:

* The parent/child tree is never changed. Tasks in a milestone hang under its row
  through `parent_task_id`; a root task's sub-tasks hang under it the same way.
  Nothing here writes `parent_task_id` or `root_task_id`. Only `milestone_id`
  and `sprint_id` of the rows below follow, exactly as `cascade_tree_position_to_subtasks`
  does when a task is filed into a milestone.
* A milestone conversion must NOT run `delete_milestone`, whose side effects
  (soft-deleting the row, re-rooting and detaching the tasks, purging the row's
  search chunks) are the opposite of this. The retired milestone is unlinked from
  the row first (`task=None`), so nothing that follows a milestone to its backing
  row can reach it again, including a later DELETE of the retired milestone.

Callers own the access checks (project membership and the object ACL). The
functions here re-check the shape of the data under a row lock and raise
`ConversionRefused` with a code the API returns.
"""

from __future__ import annotations

from django.db import transaction

from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.purge import purge_milestone
from origin.services.milestone_service import ensure_backing_task
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.services.task_project_move import collect_descendant_task_ids
from origin.signals.task_signals import update_tasks_recorded


class ConversionRefused(Exception):
    """The conversion is not allowed for this row. `code` is stable and meant
    for the client; `message` is for a person."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def convert_task_to_milestone(task_id: int) -> MilestoneMaster:
    """Make the root task `task_id` a milestone. Returns the new milestone.

    Allowed only for a ROOT task: no parent, not in a milestone, not already one.
    A sub-task, or a task that lives in a milestone, has a place in someone else's
    tree, and moving it out would be a restructure, not a conversion.
    """
    with transaction.atomic():
        try:
            row = (
                TaskMaster.objects.select_for_update(of=("self",))
                .select_related("team", "project")
                .get(task_id=task_id)
            )
        except TaskMaster.DoesNotExist:
            raise ConversionRefused("task_not_found", "Task not found.", 404) from None

        if row.is_deleted or row.status == "Deleted":
            raise ConversionRefused("task_deleted", "A deleted task can't be converted.")
        if row.is_init_task:
            raise ConversionRefused(
                "task_not_saved", "This task hasn't been saved yet, so it can't be converted."
            )
        if row.is_milestone or MilestoneMaster.objects.filter(task_id=row.task_id).exists():
            raise ConversionRefused("already_a_milestone", "This task is already a milestone.")
        if row.parent_task_id is not None:
            raise ConversionRefused(
                "not_a_root_task",
                "Only a task with no parent can become a milestone. "
                "A sub-task, or a task inside a milestone, can't.",
            )
        if row.milestone_id is not None:
            raise ConversionRefused(
                "task_in_milestone", "A task that is in a milestone can't become a milestone."
            )

        milestone = MilestoneMaster.objects.create(
            team=row.team,
            project=row.project,
            task=row,
            reporter_id=row.reporter_id,
            title=row.title,
            # The row's body is the live one (the editor writes it); the
            # milestone's copy starts from it.
            description=row.content,
            status=row.status or "Open",
            status_code=row.status_code,
            priority=row.priority,
            priority_code=row.priority_code,
            effort_level=row.effort_level,
            effort_level_code=row.effort_level_code,
            start_date=row.start_date,
            due_date=row.due_date,
            tags=row.tags,
            links=row.links,
        )
        # A task has one assignee and a milestone has several: the one becomes the first.
        if row.assignee_id is not None:
            MilestoneAssignees.objects.create(
                milestone=milestone, team=row.team, user_id=row.assignee_id
            )

        # The row itself: saved (not a queryset update) so its history records the
        # milestone link and `ts_updated_at` moves. A root task has no sprint to keep.
        row.is_milestone = True
        row.milestone = milestone
        row.sprint = None
        row.save(update_fields=["is_milestone", "milestone", "sprint", "ts_updated_at"])

        # Everything below the row joins the new milestone. The edges between them
        # stay as they are.
        below = collect_descendant_task_ids(row.task_id)
        if below:
            update_tasks_recorded(
                TaskMaster.objects.filter(task_id__in=below),
                cascade="task_to_milestone",
                milestone=milestone,
                sprint=None,
            )

    invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
    return milestone


def convert_milestone_to_task(milestone_id: int) -> tuple[TaskMaster, MilestoneMaster, int]:
    """Make the milestone an ordinary root task. Returns `(row, retired milestone,
    how many tasks stay under it as sub-tasks)`.

    The tasks in the milestone stay where they are in the tree, now as sub-tasks of
    an ordinary task (they lose the milestone and its sprint). The milestone's
    first assignee becomes the task's assignee and the others its collaborators;
    its folder and sprint go. The retired milestone remembers the task, so an old
    link can follow it.
    """
    with transaction.atomic():
        try:
            milestone = MilestoneMaster.objects.select_for_update().get(
                milestone_id=milestone_id, is_deleted=False
            )
        except MilestoneMaster.DoesNotExist:
            raise ConversionRefused("milestone_not_found", "Milestone not found.", 404) from None

        # Legacy milestones get their row on first access, as everywhere else.
        row = ensure_backing_task(milestone)
        row = TaskMaster.objects.select_for_update().get(task_id=row.task_id)
        if row.is_deleted:
            raise ConversionRefused(
                "milestone_row_deleted",
                "This milestone's task was deleted, so it can't be converted.",
            )

        assignee_ids = list(
            MilestoneAssignees.objects.filter(milestone=milestone)
            .order_by("ts_created_at", "id")
            .values_list("user_id", flat=True)
        )
        if assignee_ids:
            row.assignee_id = assignee_ids[0]
            others = [uid for uid in assignee_ids[1:] if uid != row.assignee_id]
            # Added to whoever collaborates already; never replaced.
            if others:
                row.collaborators.add(*others)
            # Someone can't be both the assignee and a collaborator.
            row.collaborators.remove(row.assignee_id)

        row.is_milestone = False
        row.milestone = None
        row.sprint = None
        row.save(update_fields=["assignee", "is_milestone", "milestone", "sprint", "ts_updated_at"])

        # The tasks that cite the milestone leave it, with its sprint; their place
        # in the tree is not touched. (The row itself is done above.)
        staying = TaskMaster.objects.filter(milestone=milestone).exclude(task_id=row.task_id)
        kept = staying.count()
        update_tasks_recorded(staying, cascade="milestone_to_task", milestone=None, sprint=None)

        # While the row is still linked, so removing each assignee is in its history.
        MilestoneAssignees.objects.filter(milestone=milestone).delete()
        milestone.is_deleted = True
        milestone.task = None
        milestone.converted_to_task = row
        milestone.sprint = None
        milestone.folder = None
        milestone.save()

    # The milestone's own entry in search is gone with it. The row stays indexed as
    # the task it now is (its change time moved, so the next ingestion pass refreshes it).
    purge_milestone(milestone.milestone_id)
    invalidate_project_tasks_cache(row.team_id, row.project_id)
    return row, milestone, kept
