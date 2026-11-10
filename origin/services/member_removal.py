"""What has to follow when an owner or editor removes someone (MIP-1371).

Taking the membership row away is the easy part (`team_membership.remove_team_member`
for a team, deleting the `ProjectMembers` row for a project, soft-deleting the
`ChannelMember` for a group chat). Three things are left behind unless somebody
tidies them, and these functions are that tidying, kept apart from the views so the
team, project and chat removals and the agent tool do exactly the same thing:

* **open work still in their name.** The person decides, at removal, whether their open
  tasks and milestones are unassigned (the confirm dialog ticks it by default). Closed and
  deleted tasks keep their assignee: that is history. Unassigning goes through
  `.save()` / `.delete()` per row, not a bulk `.update()`, so the activity feed and the
  other receivers see it like any edit.
* **requests they had pending.** A join request they sent to the team, or to one of its
  projects, would otherwise sit in an owner's inbox and, if approved, put the removed
  person back.
* **search.** Task chunks carry the assignee and reporter in their ACL (a person can
  find a task they are on even outside its project), so a removed person kept finding
  those tasks. The projects they appear on are marked for an ACL refresh, and the chunker
  stops listing a reporter or assignee who is no longer on the team
  (`active_team_member_ids`).
"""

from __future__ import annotations

import logging
from typing import Iterable

from django.db import transaction
from django.db.models import Q

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers

log = logging.getLogger(__name__)

# `InboxItems.item_type`s of the join requests a person sends.
_JOIN_REQUEST_TYPES = (1, 2, 3)  # team, project, group chat
_PROJECT_JOIN = 2


def open_assigned_tasks(team_id, user_id, *, project_id=None):
    """The person's tasks that are still open: not closed and not deleted. (A milestone's own
    assignees are separate rows, `open_assigned_milestones`.)"""
    from origin.models.task.task_models import TaskMaster
    from origin.services.task_blocking import CLOSED_STATUSES

    qs = TaskMaster.objects.filter(team_id=team_id, assignee_id=user_id, is_deleted=False).exclude(
        status__in=CLOSED_STATUSES
    )
    if project_id is not None:
        qs = qs.filter(project_id=project_id)
    return qs


def open_assigned_milestones(team_id, user_id, *, project_id=None):
    """`MilestoneAssignees` rows of the person on milestones that are still open."""
    from origin.models.task.milestone_models import MilestoneAssignees
    from origin.services.task_blocking import CLOSED_STATUSES

    qs = (
        MilestoneAssignees.objects.filter(
            user_id=user_id, milestone__team_id=team_id, milestone__is_deleted=False
        )
        .exclude(milestone__task__status__in=CLOSED_STATUSES)
        .select_related("milestone")
    )
    if project_id is not None:
        qs = qs.filter(milestone__project_id=project_id)
    return qs


def count_open_work(team_id, user_id, *, project_id=None) -> dict[str, int]:
    """What the confirm dialog names: how many open tasks and milestones the person holds."""
    return {
        "tasks": open_assigned_tasks(team_id, user_id, project_id=project_id).count(),
        "milestones": open_assigned_milestones(team_id, user_id, project_id=project_id).count(),
    }


def unassign_open_work(team_id, user_id, *, project_id=None) -> dict[str, int]:
    """Take the person off their open tasks and milestones. Returns how many of each."""
    tasks = 0
    with transaction.atomic():
        for task in open_assigned_tasks(team_id, user_id, project_id=project_id):
            task.assignee = None
            task.save(update_fields=["assignee", "ts_updated_at"])
            tasks += 1
        milestones = 0
        for row in list(open_assigned_milestones(team_id, user_id, project_id=project_id)):
            row.delete()
            milestones += 1
    return {"tasks": tasks, "milestones": milestones}


def withdraw_join_requests(team_id, user_id, *, project_id=None) -> int:
    """Close the join requests the person sent that are still pending, so approving one
    later cannot put them back. For a team removal that is every request into the team
    (the team itself, its projects, its group chats); for a project removal only the
    requests for that project. Returns how many."""
    qs = InboxItems.objects.filter(
        team_id=team_id,
        sender_id=user_id,
        request_status="pending",
        item_type__in=_JOIN_REQUEST_TYPES,
    )
    if project_id is not None:
        # The id was stored as a number by some senders and a string by others.
        qs = qs.filter(item_type=_PROJECT_JOIN).filter(
            Q(item_optionals__project_id=project_id) | Q(item_optionals__project_id=str(project_id))
        )
    return qs.update(request_status="rejected")


def mark_search_acl_dirty(team_id, user_id) -> None:
    """Ask search to refresh the projects whose tasks the person is on.

    Their membership rows already mark the projects they WERE members of; this covers the
    others: a project where they only appeared as assignee or reporter. Best-effort, like
    every ACL mark."""
    from origin.models.task.task_models import TaskMaster
    from origin.search_engine.acl_dirty import mark_projects_dirty

    ids = (
        TaskMaster.objects.filter(team_id=team_id, is_deleted=False)
        .filter(Q(assignee_id=user_id) | Q(reporter_id=user_id))
        .exclude(project_id__isnull=True)
        .values_list("project_id", flat=True)
        .distinct()
    )
    mark_projects_dirty(list(ids))


def clean_up_after_removal(team_id, user_id, *, project_id=None, unassign: bool) -> dict[str, int]:
    """Everything that follows a removal. `project_id` narrows it to one project."""
    out = {"tasks": 0, "milestones": 0}
    if unassign:
        out = unassign_open_work(team_id, user_id, project_id=project_id)
    withdraw_join_requests(team_id, user_id, project_id=project_id)
    mark_search_acl_dirty(team_id, user_id)
    if project_id is None:
        # Leaving the TEAM: their connected AI apps stop listing it now, not within the hour.
        from origin.models.common.user_models import CustomUser
        from origin.services.mcp_connections import prune_user_connections

        user = CustomUser.objects.filter(id=user_id).first()
        if user is not None:
            prune_user_connections(user)
    return out


def active_team_member_ids(team_id, candidates: Iterable) -> set[str]:
    """Which of `candidates` are current members of the team (or its owner).

    The search ACL lists a task's assignee and reporter even outside the project; that
    stays, but only for someone still on the team."""
    from origin.models.common.team_models import TeamMaster

    ids = {str(c) for c in candidates if c}
    if not ids:
        return set()
    live = {
        str(a)
        for a in TeamMembers.objects.filter(
            team_id=team_id, attendee_id__in=ids, is_deleted=False
        ).values_list("attendee_id", flat=True)
    }
    owner = TeamMaster.objects.filter(team_id=team_id).values_list("owner_id", flat=True).first()
    if owner is not None and str(owner) in ids:
        live.add(str(owner))
    return live
