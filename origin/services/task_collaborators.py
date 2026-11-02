"""Shared helper for the `TaskMaster.collaborators` M2M.

Used by both the task endpoints (`TaskMasterView`) and the milestone
endpoints (a milestone stores its collaborators on its backing task
row, same as custom field values). Kept out of the DRF serializer so
the `fields="__all__"` serializer doesn't also try to own the relation.
"""

from __future__ import annotations

import uuid

from origin.models.common.user_models import CustomUser
from origin.views.utils.scope_guards import is_project_member, is_team_member


def may_name_person(project, user_id) -> bool:
    """May this person be put on a task in `project`?

    Its members, plus its team's members: the same rule the agent's
    `create_task` applies to an assignee, so a teammate can be assigned
    before they are added to the project. Not anyone else, because the
    assignee and the reporter are both grants (`can_access_task` lets them
    open the task) and both surface the task in that person's lists.
    """
    return is_project_member(project.project_id, user_id) or is_team_member(
        project.team_id, user_id
    )


def people_allowed_on(project, raw_ids):
    """`raw_ids` with everyone who may not be on a task in `project` left
    out. `None` stays `None`, which `sync_task_collaborators` reads as
    "leave the set alone"."""
    if raw_ids is None:
        return None
    return [raw for raw in raw_ids if raw and may_name_person(project, raw)]


def _canonical(raw):
    """`raw` as a canonical UUID string, or None when it isn't one."""
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def refuse_new_people(task, project, data) -> str | None:
    """Why a task PUT may not put the people it names on the task, or None.

    `assignee`, `reporter` and `collaborators` are each a grant: a collaborator
    joins the task's notification audience, and the assignee and the reporter
    are read grants in `can_access_task`. The POST holds all three to
    `may_name_person`; the PUT used to hold none of them to anything.

    Only people being ADDED are checked. The app re-sends the whole collaborator
    list (and the assignee and reporter) on every save, so refusing someone who
    is already on the task and has since left the team would fail every later
    save of it, and make it impossible to keep the others or to remove them.
    `project` is the project the task will be in once this save lands: a move
    is judged against its destination.
    """
    named: list[tuple[str, str | None, str]] = []  # (label, raw as sent, canonical)

    for key, current in (("assignee", task.assignee_id), ("reporter", task.reporter_id)):
        raw = data.get(key)
        if raw in (None, ""):
            continue
        canon = _canonical(raw)
        if canon is not None and canon == str(current):
            continue
        named.append((f"`{key}`", raw, canon or ""))

    raw_collaborators = data.get("collaborators")
    if isinstance(raw_collaborators, list):
        already = {str(pk) for pk in task.collaborators.values_list("id", flat=True)}
        for raw in raw_collaborators:
            if raw in (None, ""):
                continue
            canon = _canonical(raw)
            if canon is not None and canon in already:
                continue
            named.append(("`collaborators`", raw, canon or ""))

    for label, raw, canon in named:
        if project is None or not canon or not may_name_person(project, canon):
            return f"{label}: {raw!r} is not a member of this project or its team."
    return None


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
