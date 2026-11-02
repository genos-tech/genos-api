"""Create, edit and delete a sprint: what the page and the agent share
(MIP-941, MIP-882).

A sprint's window may not overlap another live sprint of the project, and its end
may not come before its start. Ending a sprint early is editing its `end_date`:
its status (`upcoming` / `active` / `completed`) is derived from the dates, so
there is no separate "end" action, and the page has none.

A sprint is soft-deleted, and what pointed at it has to let go of it in a way
other browsers can see. A task has no sprint of its own: it is in its
milestone's, so the milestones and then their tasks leave together. Both are
written so the change reaches a browser that already holds the rows. A browser
syncs "rows changed since my checkpoint", and a queryset `.update()` skips
`auto_now`, so `ts_updated_at` is stamped by hand (MIP-936 had the same bug for
a milestone delete).
"""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.services.sprint_status import (
    SPRINT_STATUS_ACTIVE,
    SPRINT_STATUS_ARCHIVED,
    SPRINT_STATUS_COMPLETED,
    SPRINT_STATUS_UPCOMING,
)
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.signals.task_signals import update_tasks_recorded


class SprintRefused(Exception):
    """The change can't be made. `status` is the HTTP status the page answers."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


_STATUSES = {
    SPRINT_STATUS_UPCOMING,
    SPRINT_STATUS_ACTIVE,
    SPRINT_STATUS_COMPLETED,
    SPRINT_STATUS_ARCHIVED,
}


def _overlapping(project_id, start: date, end: date, *, exclude: int | None = None):
    qs = Sprint.objects.filter(project_id=project_id, is_deleted=False).filter(
        Q(start_date__lte=end) & Q(end_date__gte=start)
    )
    return qs.exclude(sprint_id=exclude) if exclude is not None else qs


def create_sprint(*, project: ProjectMaster, name: str, start: date, end: date) -> Sprint:
    """An ad-hoc sprint, numbered after every sprint the project has ever had
    (deleted ones included: the unique constraint counts them)."""
    if end < start:
        raise SprintRefused("start_date / end_date must be valid ISO dates with end >= start.", 400)
    if _overlapping(project.project_id, start, end).exists():
        raise SprintRefused("Sprint window overlaps an existing sprint.", 409)
    with transaction.atomic():
        max_seq = (
            Sprint.objects.filter(project=project)
            .order_by("-sequence_number")
            .values_list("sequence_number", flat=True)
            .first()
            or 0
        )
        return Sprint.objects.create(
            project=project,
            team=project.team,
            name=name,
            sequence_number=max_seq + 1,
            start_date=start,
            end_date=end,
            status=SPRINT_STATUS_UPCOMING,
            is_auto_generated=False,
        )


def update_sprint(
    sprint: Sprint,
    *,
    name: str | None = None,
    start: date | None = None,
    end: date | None = None,
    status: str | None = None,
) -> Sprint:
    """Change the fields that are given; `None` leaves one alone."""
    if name is not None:
        sprint.name = name
    if start is not None:
        sprint.start_date = start
    if end is not None:
        sprint.end_date = end
    if sprint.end_date < sprint.start_date:
        raise SprintRefused("end_date must be >= start_date.", 400)
    if status in _STATUSES:
        sprint.status = status
    if _overlapping(
        sprint.project_id, sprint.start_date, sprint.end_date, exclude=sprint.sprint_id
    ).exists():
        raise SprintRefused("Updated window overlaps another sprint.", 409)
    sprint.save()
    return sprint


def delete_sprint(sprint: Sprint) -> int:
    """Soft-delete `sprint`, and return how many milestones left it.

    Idempotent: deleting a deleted sprint finds nothing left to detach.
    """
    sprint.is_deleted = True
    sprint.save(update_fields=["is_deleted", "ts_updated_at"])

    # Milestones fall back into "No sprint / Backlog", so the frontend doesn't
    # try to render them inside a now-invisible sprint.
    detached = MilestoneMaster.objects.filter(sprint=sprint).update(
        sprint=None, ts_updated_at=timezone.now()
    )
    # The tasks in those milestones were in this sprint by inheritance; the
    # recorded update leaves a history row on each and stamps them for browsers.
    update_tasks_recorded(
        TaskMaster.objects.filter(sprint=sprint), cascade="sprint_delete", sprint=None
    )
    invalidate_project_tasks_cache(sprint.team_id, sprint.project_id)
    return detached
