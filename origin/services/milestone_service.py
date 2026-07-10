"""Milestone domain helpers shared by the REST views and agent tools.

Extracted from `origin/views/task/milestone_views.py` so the agent's
composite write tools (`create_task_plan`) can create milestones with
the exact same backing-task invariants the UI path enforces, without
importing a DRF view. The view keeps thin aliases to these functions,
so its behavior (and the PATCH/mention paths that call the sync
helpers) is unchanged.

The invariant these helpers guard: **a milestone is a task.** Every
`MilestoneMaster` owns a backing `TaskMaster` row (`is_milestone=True`);
the project task table renders that row, sub-tasks nest under it via
`parent_task_id`, and the multi-assignee join is mirrored onto its
single `assignee` column. Creating or mutating a milestone outside
these helpers risks drifting the two representations apart.
"""

from datetime import date, datetime

from origin.models.common.user_models import CustomUser
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster


def parse_iso_date(value):
    """Coerce ISO-string / date / datetime / None into `date` or `None`.

    Django's `DateField` normally accepts ISO strings on save, but the
    in-memory instance returned from `objects.create(...)` keeps whatever
    Python value was passed in. That's fine for the database round-trip
    but trips serializers that call `.isoformat()`. Parse on the way in
    so the in-memory value is always a `date`.
    """

    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def ensure_backing_task(milestone: MilestoneMaster) -> TaskMaster:
    """Return the milestone's backing TaskMaster row, creating it if needed.

    A milestone is conceptually a task with `is_milestone=True`. We keep
    metadata (sprint linkage, multi-assignees, etc.) on `MilestoneMaster`
    but reuse all task plumbing (comments, notes, attachments, body,
    sub-tasks via `parent_task_id`) by giving each milestone a backing
    TaskMaster row. Legacy milestones get one created on first access.
    """

    if milestone.task_id is not None:
        # Defensive: keep is_milestone in sync if a backing task exists
        # but somehow lost the flag (data drift / hand edits).
        if not milestone.task.is_milestone:
            milestone.task.is_milestone = True
            milestone.task.save(update_fields=["is_milestone", "ts_updated_at"])
        return milestone.task

    backing = TaskMaster.objects.create(
        team=milestone.team,
        project=milestone.project,
        sprint=milestone.sprint,
        milestone=milestone,
        title=milestone.title or "Untitled milestone",
        content=milestone.description,
        status=milestone.status or "Open",
        status_code=milestone.status_code,
        priority=milestone.priority,
        priority_code=milestone.priority_code,
        effort_level=milestone.effort_level,
        effort_level_code=milestone.effort_level_code,
        start_date=milestone.start_date,
        due_date=milestone.due_date,
        tags=milestone.tags,
        links=milestone.links,
        reporter_id=milestone.reporter_id,
        assignee_id=milestone.reporter_id,
        is_milestone=True,
        is_init_task=False,
    )
    milestone.task = backing
    milestone.save(update_fields=["task", "ts_updated_at"])
    return backing


def sync_backing_task(milestone: MilestoneMaster) -> None:
    """Mirror milestone fields onto the backing task so the table view
    (which renders the backing task row) stays in sync with the
    milestone's authoritative values."""

    backing = ensure_backing_task(milestone)
    backing.title = milestone.title or backing.title
    backing.content = milestone.description
    backing.status = milestone.status or backing.status
    backing.status_code = milestone.status_code
    backing.priority = milestone.priority
    backing.priority_code = milestone.priority_code
    backing.effort_level = milestone.effort_level
    backing.effort_level_code = milestone.effort_level_code
    backing.start_date = milestone.start_date
    backing.due_date = milestone.due_date
    backing.tags = milestone.tags
    # Mirror the milestone's external links onto the backing task so
    # callers that read the task row directly (table view, sprint
    # board, dashboards) stay consistent with the milestone preview.
    backing.links = milestone.links
    backing.sprint = milestone.sprint
    backing.milestone = milestone
    backing.is_milestone = True
    backing.is_deleted = milestone.is_deleted
    # Reporter mirrors the milestone's reporter so the table row's
    # reporter chip stays consistent with the milestone preview.
    if milestone.reporter_id is not None:
        backing.reporter_id = milestone.reporter_id
    # Pick a deterministic single assignee from the milestone's multi
    # assignee table (oldest first). This keeps the milestone preview's
    # single-assignee UX in sync with the table row's assignee column.
    # Falls back to the reporter when no explicit assignees are set.
    first_assignee_id = (
        MilestoneAssignees.objects.filter(milestone=milestone)
        .order_by("ts_created_at", "id")
        .values_list("user_id", flat=True)
        .first()
    )
    backing.assignee_id = first_assignee_id or milestone.reporter_id
    backing.save()


def sync_milestone_assignees(milestone: MilestoneMaster, assignee_ids) -> None:
    """Reconcile the multi-assignee join table to exactly `assignee_ids`."""
    # `CustomUser.id` is a UUID, so keep ids as strings here. We
    # normalize to `str(...)` so comparisons with `user_id` (which
    # comes back from the ORM as a `UUID`) match consistently.
    normalized = {str(uid) for uid in assignee_ids if uid not in (None, "")}
    existing = {str(a.user_id): a for a in MilestoneAssignees.objects.filter(milestone=milestone)}
    existing_ids = set(existing.keys())

    to_add = normalized - existing_ids
    to_remove = existing_ids - normalized

    if to_add:
        users = list(CustomUser.objects.filter(id__in=list(to_add)))
        MilestoneAssignees.objects.bulk_create(
            [
                MilestoneAssignees(
                    milestone=milestone,
                    team=milestone.team,
                    user=u,
                )
                for u in users
            ],
            ignore_conflicts=True,
        )
    if to_remove:
        MilestoneAssignees.objects.filter(
            milestone=milestone, user_id__in=list(to_remove)
        ).delete()


def create_milestone(
    project,
    *,
    reporter_id,
    title: str,
    description_blocks=None,
    status: str = "Open",
    status_code=None,
    priority=None,
    priority_code=None,
    effort_level=None,
    effort_level_code=None,
    start_date=None,
    due_date=None,
    sprint=None,
    tags=None,
    links=None,
    assignee_ids=(),
) -> MilestoneMaster:
    """Create a milestone with its backing task and assignee rows.

    The create-half of `MilestoneView.post`, minus request parsing.
    The caller owns the surrounding transaction and the
    `invalidate_project_tasks_cache` call (the backing task is a new
    row in the project task table, so any cached listing is stale
    after this commits).
    """
    milestone = MilestoneMaster.objects.create(
        team=project.team,
        project=project,
        sprint=sprint,
        reporter_id=reporter_id,
        title=title,
        description=description_blocks,
        status=status or "Open",
        status_code=status_code,
        priority=priority,
        priority_code=priority_code,
        effort_level=effort_level,
        effort_level_code=effort_level_code,
        start_date=parse_iso_date(start_date),
        due_date=parse_iso_date(due_date),
        tags=tags,
        links=links,
    )
    ensure_backing_task(milestone)
    if assignee_ids:
        sync_milestone_assignees(milestone, assignee_ids)
    return milestone
