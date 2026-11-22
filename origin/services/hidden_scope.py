"""Hidden private projects and group chats (MIP-1699).

A Hidden item is private AND its name must not reach anyone who is not a member: not the Join
list, not the chat search box, not a join request. Every listing path filters through THIS module
so a new one has one obvious thing to call.

Search content, the agent and MCP list tools, the public API, citations and realtime pushes already
work from membership (`agent/acl.py` never reads `is_private`), so they need nothing here.
"""

from __future__ import annotations

from django.db.models import Q

from origin.models import ChannelMember, ProjectMaster, ProjectMembers


def visible_projects_q(user_id) -> Q:
    """Projects `user_id` may see by name: every non-hidden one, plus hidden ones they belong to.

    The owner counts as a member even without a ProjectMembers row (see ProjectMasterView.put).
    """
    return (
        Q(is_hidden=False)
        | Q(owner_id=user_id)
        | Q(project_id__in=ProjectMembers.objects.filter(attendee_id=user_id).values("project_id"))
    )


def visible_channels_q(user_id) -> Q:
    """Channels `user_id` may see by name: every non-hidden one, plus hidden ones they belong to."""
    return Q(is_hidden=False) | Q(
        id__in=ChannelMember.objects.filter(user_id=user_id, is_deleted=False).values("channel_id")
    )


def project_hidden_from(project, user_id) -> bool:
    """True when `project` is hidden and `user_id` is neither its owner nor a member."""
    if not project.is_hidden:
        return False
    if project.owner_id is not None and str(project.owner_id) == str(user_id):
        return False
    return not ProjectMembers.objects.filter(
        project_id=project.project_id, attendee_id=user_id
    ).exists()


def channel_hidden_from(channel, user_id) -> bool:
    """True when `channel` is hidden and `user_id` is not an active member."""
    if not channel.is_hidden:
        return False
    return not ChannelMember.objects.filter(
        channel_id=channel.id, user_id=user_id, is_deleted=False
    ).exists()


def project_name_taken_for(team_id, name, user_id, exclude_project_id=None) -> bool:
    """True when a project `user_id` can SEE already uses `name` in the team.

    A hidden project the caller cannot see never counts: refusing its name would confirm that
    it exists (MIP-1935). Project names are therefore not unique in the database.
    """
    qs = ProjectMaster.objects.filter(team=team_id, project_name=name).filter(
        visible_projects_q(user_id)
    )
    if exclude_project_id is not None:
        qs = qs.exclude(project_id=exclude_project_id)
    return qs.exists()


def taken_project_codes(team_id) -> set[str]:
    """Every code in use in the team, hidden projects included.

    Codes prefix task display ids (MIP-123) so they must stay unique; the caller derives a free
    one silently instead of refusing, so a refusal never confirms a hidden project's code.
    """
    return set(
        ProjectMaster.objects.filter(team=team_id, code__isnull=False).values_list(
            "code", flat=True
        )
    )


def project_code_visibly_taken(team_id, code, user_id, exclude_project_id=None) -> bool:
    """True when a project `user_id` can see already uses `code` in the team."""
    qs = ProjectMaster.objects.filter(team=team_id, code=code).filter(visible_projects_q(user_id))
    if exclude_project_id is not None:
        qs = qs.exclude(project_id=exclude_project_id)
    return qs.exists()
