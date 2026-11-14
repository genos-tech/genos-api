"""A deleted account leaves group and project chats (MIP-1546).

Account deletion is a soft delete: the row is anonymised, never removed, so the FK cascades that
would have dropped the person's `ChannelMember` rows never fire. They used to stay, and the
account kept appearing on rosters and mention lists as "Deleted user".

What happens to each kind of chat:

* **GM (group)**: a deleted person is not a participant of a group any more, so the row is
  soft-deleted (`is_deleted=True`), the way leaving a group writes it. The roles the membership
  wrote on the group's chat notes go with it.
* **PM (project chat)**: the chat mirrors the project's membership (`pm_channel_signals`), and a
  project member is removed by DELETING the `ProjectMembers` row, whose `post_delete` receiver
  soft-deletes the mirrored chat row and ends the roles on the chat's notes. This goes through
  that same delete rather than writing the chat row behind the project's back: a later save of
  the membership would otherwise bring the row back, and the project's own roster would still
  list the account.
* **DM and MDM**: KEPT. The other participants keep the conversation and its history, and every
  line the person wrote already reads "Deleted user". Rosters that OFFER people (the roster
  endpoints, `list_channel_members`, the mention check) hide a deleted account even where the
  row stays: `channel_scope.chat_member_rows`.

Only the account's OWN rows are ever touched: every query below is keyed on the one user.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Exists, OuterRef

from origin.models.chat.unified_models import ChannelKind, ChannelMember
from origin.models.project.prj_models import ProjectMembers


@dataclass(frozen=True)
class GroupMembershipCounts:
    """How many group / project rows belong to deleted accounts (counts only)."""

    accounts: int = 0
    group_rows: int = 0
    project_memberships: int = 0
    project_chat_rows: int = 0

    @property
    def total(self) -> int:
        return self.group_rows + self.project_memberships + self.project_chat_rows


def _live_group_rows(user_id):
    return ChannelMember.objects.filter(
        user_id=user_id, is_deleted=False, channel__kind=ChannelKind.GM
    )


def _live_project_chat_rows(user_id):
    return ChannelMember.objects.filter(
        user_id=user_id, is_deleted=False, channel__kind=ChannelKind.PM
    )


def end_group_memberships(user_id) -> GroupMembershipCounts:
    """End `user_id`'s membership of every group chat and project. DM and MDM rows stay.

    Returns what was ended. Idempotent. Each row goes through `.save()` / `.delete()` so the
    receivers run (the search ACL refresh, the change feed, the project chat mirror).
    """
    from origin.services.chat_note_access import revoke_membership_roles

    groups = 0
    projects = 0
    stray = 0
    with transaction.atomic():
        # The project membership first: its receiver soft-deletes the mirrored chat row.
        for member in list(ProjectMembers.objects.filter(attendee_id=user_id)):
            member.delete()
            projects += 1
        # A project-chat row with no project membership behind it (it was never mirrored, or the
        # membership went first): the same soft delete.
        for row in list(_live_project_chat_rows(user_id)):
            row.is_deleted = True
            row.save(update_fields=["is_deleted", "ts_updated_at"])
            revoke_membership_roles(
                user_id=user_id, channel_id=row.channel_id, joined_at=row.ts_joined_at
            )
            stray += 1
        for row in list(_live_group_rows(user_id)):
            row.is_deleted = True
            row.save(update_fields=["is_deleted", "ts_updated_at"])
            revoke_membership_roles(
                user_id=user_id, channel_id=row.channel_id, joined_at=row.ts_joined_at
            )
            groups += 1
    return GroupMembershipCounts(
        accounts=1, group_rows=groups, project_memberships=projects, project_chat_rows=stray
    )


def _pending_rows():
    """(group rows, project memberships, project-chat rows with no membership behind them) of
    accounts that are already deleted, as querysets."""
    groups = ChannelMember.objects.filter(
        user__is_deleted=True, is_deleted=False, channel__kind=ChannelKind.GM
    )
    projects = ProjectMembers.objects.filter(attendee__is_deleted=True)
    stray = ChannelMember.objects.filter(
        user__is_deleted=True, is_deleted=False, channel__kind=ChannelKind.PM
    ).exclude(
        Exists(
            ProjectMembers.objects.filter(
                project_id=OuterRef("channel__project_id"), attendee_id=OuterRef("user_id")
            )
        )
    )
    return groups, projects, stray


def _pending_user_ids() -> set:
    groups, projects, stray = _pending_rows()
    return (
        set(groups.values_list("user_id", flat=True))
        | set(projects.values_list("attendee_id", flat=True))
        | set(stray.values_list("user_id", flat=True))
    )


def pending_group_memberships() -> GroupMembershipCounts:
    """Counts of group / project rows that still belong to ALREADY-deleted accounts."""
    groups, projects, stray = _pending_rows()
    return GroupMembershipCounts(
        accounts=len(_pending_user_ids()),
        group_rows=groups.count(),
        project_memberships=projects.count(),
        project_chat_rows=stray.count(),
    )


def prune(*, apply: bool) -> GroupMembershipCounts:
    """Dry run (default) or apply `end_group_memberships` to every already-deleted account that
    still holds a group or project row. Only accounts with `is_deleted=True` are selected, so a
    live account is never passed to the helper."""
    before = pending_group_memberships()
    if not apply or not before.total:
        return before
    groups = projects = stray = 0
    user_ids = _pending_user_ids()
    for user_id in user_ids:
        done = end_group_memberships(user_id)
        groups += done.group_rows
        projects += done.project_memberships
        stray += done.project_chat_rows
    return GroupMembershipCounts(
        accounts=len(user_ids),
        group_rows=groups,
        project_memberships=projects,
        project_chat_rows=stray,
    )
