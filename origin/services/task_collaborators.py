"""Shared helper for the `TaskMaster.collaborators` M2M.

Used by both the task endpoints (`TaskMasterView`) and the milestone
endpoints (a milestone stores its collaborators on its backing task
row, same as custom field values). Kept out of the DRF serializer so
the `fields="__all__"` serializer doesn't also try to own the relation.
"""

from __future__ import annotations

import uuid

from origin.models.common.user_models import CustomUser


def sync_task_collaborators(task, raw_ids) -> None:
    """Replace a task's `collaborators` M2M from a list of user ids.

    Contract mirrors tags / custom_field_values:
      - `None` (key absent) is a no-op — leave the current set untouched.
      - a list (including `[]`) is a wholesale replace.

    `CustomUser.id` is a UUID, so each candidate is coerced and anything
    that isn't a well-formed UUID is dropped BEFORE the `id__in` query — a
    single malformed value would otherwise raise ValidationError and 500
    the whole save. Valid-but-unknown ids then fall out of the existence
    filter (dropped, not errored) — same philosophy as orphaned
    custom-field option refs.
    """
    if raw_ids is None:
        return
    candidate_ids = []
    for x in raw_ids:
        if x in (None, ""):
            continue
        try:
            candidate_ids.append(uuid.UUID(str(x)))
        except (ValueError, AttributeError, TypeError):
            continue
    valid = list(CustomUser.objects.filter(id__in=candidate_ids).values_list("id", flat=True))
    task.collaborators.set(valid)
