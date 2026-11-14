"""Shared helper for the `TaskMaster.collaborators` M2M.

Used by both the task endpoints (`TaskMasterView`) and the milestone
endpoints (a milestone stores its collaborators on its backing task
row, same as custom field values). Kept out of the DRF serializer so
the `fields="__all__"` serializer doesn't also try to own the relation.
"""

from __future__ import annotations

import uuid

from origin.models.common.member_event_models import (
    KIND_COLLABORATOR_ADD,
    KIND_COLLABORATOR_REMOVE,
    OBJECT_TASK,
)
from origin.models.common.user_models import CustomUser
from origin.services import member_events
from origin.views.utils.scope_guards import (
    is_project_member,
    is_team_member,
    task_reader_ids,
)


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


def may_collaborate(project, user_id, readers=()) -> bool:
    """May this person be made a COLLABORATOR of a task in `project`?

    Only someone who can open the task: a member (or the owner) of the project, or one of
    `readers`, the ids that hold the task as its assignee or reporter. That is
    `can_access_task`'s rule (MIP-1567). A collaborator joins the task's notification
    audience, and a person told about a task they cannot open is told about something they
    cannot follow (the activity feed already withholds it from them, MIP-1560). A teammate who
    is not on the project is therefore not accepted here, although `may_name_person` lets them
    be assigned. Adding them to the project first is what lets them in.
    """
    if user_id is None:
        return False
    if str(user_id) in {str(r) for r in readers if r}:
        return True
    return is_project_member(project.project_id, user_id)


def people_allowed_on(project, raw_ids, readers=()):
    """`raw_ids` with everyone who may not be on a task in `project` left
    out. `None` stays `None`, which `sync_task_collaborators` reads as
    "leave the set alone". `readers` is the assignee and reporter the task has
    (see `may_collaborate`)."""
    if raw_ids is None:
        return None
    return [raw for raw in raw_ids if raw and may_collaborate(project, raw, readers)]


def notifiable_collaborator_ids(task_id) -> set[str]:
    """The task's collaborators who can open it, as strings: who a notice about the task may
    reach (MIP-1567).

    A collaborator told about a task they cannot open is told about something they cannot
    follow, and the activity feed already withholds such a row from them (MIP-1560). People
    listed before the rule held (a teammate who was never on the project, or who has left it)
    stay in the list and are skipped here, at send time. Two queries for the task and its
    collaborators plus two for the readers, however many collaborators there are.
    """
    from origin.models.task.task_models import TaskMaster

    if task_id is None:
        return set()
    row = (
        TaskMaster.objects.filter(task_id=task_id)
        .values("project_id", "assignee_id", "reporter_id")
        .first()
    )
    if row is None:
        return set()
    collaborators = {
        str(pk)
        for pk in TaskMaster.collaborators.through.objects.filter(
            taskmaster_id=task_id
        ).values_list("customuser_id", flat=True)
    }
    return task_reader_ids(row["project_id"], row["assignee_id"], row["reporter_id"], collaborators)


def _canonical(raw):
    """`raw` as a canonical UUID string, or None when it isn't one."""
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        return None


def refuse_unreachable_people(project, people) -> str | None:
    """Why the people named can't be put on something in `project`, or None.

    `people` is `(label, raw ids as sent, ids already there)` triples, each with an
    optional fourth item: the rule for that group, `(project, canonical id) -> bool`
    (`may_name_person` when left out; a collaborator list passes `may_collaborate`).
    Someone already there is not judged again (see `refuse_new_people`); anyone else
    has to pass the rule, and a malformed id is refused rather than dropped. The message
    names the label and the id.
    """
    for label, raws, already, *rule in people:
        check = rule[0] if rule else may_name_person
        for raw in raws:
            if raw in (None, ""):
                continue
            canon = _canonical(raw)
            if canon is not None and canon in already:
                continue
            if project is None or canon is None or not check(project, canon):
                if check is may_name_person:
                    return f"{label}: {raw!r} is not a member of this project or its team."
                return (
                    f"{label}: {raw!r} is not a member of this project, so they could not "
                    "open the task. Add them to the project first."
                )
    return None


def refuse_new_people(task, project, data) -> str | None:
    """Why a task PUT may not put the people it names on the task, or None.

    `assignee`, `reporter` and `collaborators` are each a grant: a collaborator
    joins the task's notification audience, and the assignee and the reporter
    are read grants in `can_access_task`. The assignee and the reporter are held to
    `may_name_person` (a member of the project or its team); a collaborator to
    `may_collaborate`, which wants someone who can open the task (MIP-1567).

    Only people being ADDED are checked. The app re-sends the whole collaborator
    list (and the assignee and reporter) on every save, so refusing someone who
    is already on the task and has since left the team would fail every later
    save of it, and make it impossible to keep the others or to remove them.
    `project` is the project the task will be in once this save lands: a move
    is judged against its destination.
    """
    people = []
    for key, current in (("assignee", task.assignee_id), ("reporter", task.reporter_id)):
        people.append((f"`{key}`", [data.get(key)], {str(current)}))
    raw_collaborators = data.get("collaborators")
    if isinstance(raw_collaborators, list):
        already = {str(pk) for pk in task.collaborators.values_list("id", flat=True)}
        # The assignee and the reporter the task will have once this save lands.
        holders = [data.get(key) or current for key, current in (
            ("assignee", task.assignee_id), ("reporter", task.reporter_id))]

        def rule(project, canon):
            return may_collaborate(project, canon, holders)

        people.append(("`collaborators`", raw_collaborators, already, rule))
    return refuse_unreachable_people(project, people)


def refuse_new_milestone_people(milestone, project, data) -> str | None:
    """`refuse_new_people` for a milestone create (`milestone` is None) or
    PATCH (MIP-915): the people it names in `assignee_ids`, `reporter_id` and
    `collaborators`, judged against `project`, the one it will be in.

    A milestone's assignees are readers (`milestone_acl_user_ids`,
    `LabelAccess.milestone_readable`), and its collaborators and reporter ride
    on its backing task, where they are the same grants as on any task. People
    already on the milestone are not judged again.
    """
    assignees = data.get("assignee_ids")
    collaborators = data.get("collaborators")
    already_assigned: set[str] = set()
    already_collaborating: set[str] = set()
    already_reporter: set[str] = set()
    if milestone is not None:
        already_assigned = {
            str(pk) for pk in milestone.milestone_assignees.values_list("user_id", flat=True)
        }
        already_reporter = {str(milestone.reporter_id)}
        if milestone.task_id is not None:
            already_collaborating = {
                str(pk) for pk in milestone.task.collaborators.values_list("id", flat=True)
            }
    people = [("`reporter_id`", [data.get("reporter_id")], already_reporter)]
    if isinstance(assignees, list):
        people.append(("`assignee_ids`", assignees, already_assigned))
    if isinstance(collaborators, list):
        # A milestone's collaborators ride on its backing task: they have to be able to open it.
        people.append(("`collaborators`", collaborators, already_collaborating, may_collaborate))
    return refuse_unreachable_people(project, people)


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
    # The acting member's ledger notes who was added and who was taken off (MIP-1085):
    # nothing else saves it. Only when something can be written for them, so a member
    # who opted out costs no extra query.
    actor = member_events.acting_user_id()
    before: set[str] | None = None
    if member_events.recording_activity_for(actor):
        before = {str(pk) for pk in task.collaborators.values_list("id", flat=True)}
    task.collaborators.set(valid)
    if before is not None:
        after = {str(pk) for pk in valid}
        for kind, people in (
            (KIND_COLLABORATOR_ADD, sorted(after - before)),
            (KIND_COLLABORATOR_REMOVE, sorted(before - after)),
        ):
            member_events.record_activity(
                user_id=actor,
                team_id=task.team_id,
                kind=kind,
                items=[(OBJECT_TASK, str(task.task_id), person) for person in people],
            )
