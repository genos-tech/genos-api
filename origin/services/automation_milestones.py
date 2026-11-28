"""Automation rules on milestones (MIP-2521).

The engine (`automation_engine`) queues, matches and records; this is what differs for a
milestone. A milestone's values live on `MilestoneMaster` (its backing task row only mirrors
them), and a rule that moves one moves its whole sub-tree through
`milestone_manage.update_milestone`, exactly as a move by hand does: the sprint and folder go,
and every assignee in the sub-tree is checked against the destination.

Because one hit moves many tasks, two limits apply that a task rule does not need: a sub-tree
above `MOVE_TASK_CAP` tasks is not moved by a rule at all, and a milestone takes fewer applied
runs per hour than a task.

Imports of `milestone_manage` and `milestone_service` are inside the functions: both reach the
task signals, which import the engine.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import automation_rules as rules
from origin.services.task_assignment_notice import notify_milestone_assigned
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.services.task_project_move import DEPTH_LIMIT

#: The most tasks (not counting the milestone itself) a rule moves with a milestone. A bigger one
#: is moved by hand: a rule runs unwatched, a move cannot be undone (new ids, cleared sprint),
#: and it runs inside the request that made the change.
MOVE_TASK_CAP = 200
#: Applied runs one milestone may have inside `automation_engine.RATE_WINDOW`.
RATE_LIMIT = 2
#: Milestones one request acts on before it answers or ends; the schedule tick takes the rest.
#: One of them can be a move of `MOVE_TASK_CAP` tasks.
INLINE_LIMIT = 2


def lock(task_id) -> MilestoneMaster | None:
    """The live milestone `task_id` backs, its row locked: a move by hand locks the same row
    first, so the two cannot interleave."""
    return (
        MilestoneMaster.objects.select_for_update(of=("self",))
        .select_related("project", "task", "task__project")
        .filter(task_id=task_id, is_deleted=False)
        .first()
    )


def assignee_ids_of(milestone) -> list[str]:
    return [
        str(pk)
        for pk in MilestoneAssignees.objects.filter(milestone=milestone).values_list(
            "user_id", flat=True
        )
    ]


def subject_of(milestone, assignee_ids=None) -> rules.MilestoneSubject:
    if assignee_ids is None:
        assignee_ids = assignee_ids_of(milestone)
    return rules.MilestoneSubject(milestone, milestone.task, assignee_ids)


def subtree_ids(milestone, *, stop_above: int | None = None) -> set[int]:
    """The tasks that go with `milestone` (its backing row left out), as
    `milestone_subtree_task_ids` finds them but one query per level, and no further once there
    are more than `stop_above`."""
    membership = Q(milestone_id=milestone.milestone_id)
    if milestone.task_id is not None:
        membership |= Q(parent_task_id=milestone.task_id)
    found = set(TaskMaster.objects.filter(membership).values_list("task_id", flat=True))
    found.discard(milestone.task_id)
    # The backing row's children are members above; its own descendants are theirs.
    frontier = set(found)
    for _ in range(DEPTH_LIMIT):
        if not frontier or (stop_above is not None and len(found) > stop_above):
            break
        frontier = set(
            TaskMaster.objects.filter(parent_task_id__in=frontier).values_list("task_id", flat=True)
        )
        frontier -= found | {milestone.task_id}
        found |= frontier
    return found


def move_problem(rule, milestone) -> str | None:
    """Why `rule` may not move this milestone, whoever the rule is, or None. The project-level
    guards (team, privacy, readers, owner) are `automation_engine.rule_problem`'s."""
    from origin.services.milestone_manage import UNSET, move_assignee_problem  # noqa: PLC0415

    ids = subtree_ids(milestone, stop_above=MOVE_TASK_CAP)
    if len(ids) > MOVE_TASK_CAP:
        return "too_many_tasks"
    # A task filed under the milestone from another project (old data) would be carried along,
    # and the privacy check compares this project's readers only.
    if TaskMaster.objects.filter(task_id__in=ids).exclude(project_id=milestone.project_id).exists():
        return "move_not_allowed"
    if move_assignee_problem(
        milestone,
        rule.destination_project,
        assignee_ids=[str(rule.set_assignee_id)] if rule.set_assignee_id else UNSET,
        # Read once, level by level: the dry run asks this of many milestones.
        task_ids=ids,
    ):
        return "assignee_not_on_destination"
    return None


def apply(rule, milestone: MilestoneMaster) -> str:
    """Do the rule's actions on the locked milestone. "" or "no_change". The caller has set the
    rule's owner as the writer and switched capture off."""
    from origin.services.milestone_manage import UNSET, update_milestone  # noqa: PLC0415 - cycle

    fields: dict = {}
    if rule.set_status:
        custom = rule.set_custom_status
        if milestone.status != rule.set_status or (
            custom is not None and milestone.custom_status_id != custom.status_id
        ):
            fields["status"] = rule.set_status
            fields["custom_status"] = custom
    assignee_ids = UNSET
    if rule.set_assignee_id and assignee_ids_of(milestone) != [str(rule.set_assignee_id)]:
        assignee_ids = [str(rule.set_assignee_id)]

    destination = rule.destination_project if rule.moves_task else None
    tag_names: dict = {}
    if destination is not None:
        # A move clears tags (they are the old project's): put back, by name, the ones the
        # destination has, as a task rule does.
        moving = subtree_ids(milestone)
        tag_names = {
            row.task_id: rules.tag_names_of(row)
            for row in TaskMaster.objects.filter(task_id__in=moving).only("task_id", "tags")
        }
        milestone_tags = rules.tag_names_of(milestone)
    elif rule.add_tag:
        have = {t.casefold() for t in rules.tag_names_of(milestone)}
        tag = rules.find_tag(milestone.project_id, rule.add_tag)
        if tag is not None and tag["tagName"].casefold() not in have:
            fields["tags"] = [*(milestone.tags or []), tag]

    if destination is None and not fields and assignee_ids is UNSET:
        return "no_change"

    source = (milestone.team_id, milestone.project_id)
    added = update_milestone(
        milestone,
        fields=fields,
        assignee_ids=assignee_ids,
        project_move=destination,
    )
    if destination is not None:
        folded = rules.catalogue(milestone.project_id)
        wanted = [*milestone_tags, *([rule.add_tag] if rule.add_tag else [])]
        if tags := _remap(wanted, milestone.project_id, folded):
            milestone.tags = tags
            milestone.save(update_fields=["tags", "ts_updated_at"])
        # The backing row mirrors the milestone's. Read fresh: the copy `milestone.task` holds
        # is from before the move, and saving it whole would write the old project back.
        tag_names[milestone.task_id] = wanted
        for row in TaskMaster.objects.filter(task_id__in=tag_names):
            if tags := _remap(tag_names[row.task_id], milestone.project_id, folded):
                row.tags = tags
                row.save(update_fields=["tags", "ts_updated_at"])

    if added:
        # After the commit: a run that is rolled back has told nobody.
        actor_id = rule.owner_id
        transaction.on_commit(
            lambda: notify_milestone_assigned(milestone, actor_id=actor_id, added_user_ids=added)
        )
    target = (milestone.team_id, milestone.project_id)
    _invalidate(source, target)
    # And once more when it is committed: a read in between would cache what was there before.
    transaction.on_commit(lambda: _invalidate(source, target))
    return ""


def _invalidate(*places) -> None:
    for team_id, project_id in set(places):
        invalidate_project_tasks_cache(team_id, project_id)


def _remap(names: list[str], project_id, folded: dict) -> list[dict]:
    """The project's tags that carry these names; a name it lacks is dropped."""
    out: list[dict] = []
    for name in names:
        tag = rules.find_tag(project_id, name, folded)
        if tag is not None and tag not in out:
            out.append(tag)
    return out
