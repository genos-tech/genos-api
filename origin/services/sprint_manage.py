"""Delete a sprint: what the page and the agent share (MIP-941).

A sprint is soft-deleted, and what pointed at it has to let go of it in a way
other browsers can see. A task has no sprint of its own: it is in its
milestone's, so the milestones and then their tasks leave together. Both are
written so the change reaches a browser that already holds the rows. A browser
syncs "rows changed since my checkpoint", and a queryset `.update()` skips
`auto_now`, so `ts_updated_at` is stamped by hand (MIP-936 had the same bug for
a milestone delete).
"""

from __future__ import annotations

from django.utils import timezone

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.signals.task_signals import update_tasks_recorded


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
