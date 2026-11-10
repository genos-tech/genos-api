"""Create and change a project: what the page and the agent share (MIP-882).

The page creates a project in several browser steps: it signs up a system user
for the project, creates the project, joins its creator, joins the system user to
the team, and adds the people it was given. A tool has no browser, so `create_project`
does every one of those steps on the server, in one transaction: either the whole
project exists or none of it does.

The rules are the page's. A project's name is unique within its team; its code
(the prefix of its task ids) is 2 to 6 uppercase letters or digits starting with
a letter and unique within its team; a rename or a code change is for the owner or
an editor; a role is `editor` or `viewer`, never `owner` (that is a transfer).
Who may do what is checked by the caller (the page's views, the tools'
`project_access`); this module holds what is checked and written.
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable

from django.db import transaction

from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.acl_dirty import mark_projects_dirty
from origin.services.member_removal import clean_up_after_removal
from origin.services.member_roles import (
    ASSIGNABLE_ROLES,
    OWNER,
    can_manage,
    can_remove_member,
    is_assignable,
    resolve_project_role,
)
from origin.services.project_code import derive_project_code
from origin.services.security_events import record_security_event, user_label
from origin.views.utils.scope_guards import is_team_member

PROJECT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")
NAME_MAX = 120


class ProjectRefused(Exception):
    """The change can't be made. `status` is the HTTP status the page answers."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _clean_name(raw) -> str:
    name = (raw or "").strip() if isinstance(raw, str) else ""
    if not name:
        raise ProjectRefused("project_name cannot be empty.", 400)
    if len(name) > NAME_MAX:
        raise ProjectRefused(
            f"project_name is too long ({len(name)} characters; the limit is {NAME_MAX}).", 400
        )
    return name


def record_member_added(project: ProjectMaster, attendee_id, *, via: str, request=None, actor=None):
    record_security_event(
        SecurityAction.MEMBER_ADDED,
        request=request,
        actor=actor,
        team_id=project.team_id,
        target_type="user",
        target_id=attendee_id,
        target_label=user_label(attendee_id),
        metadata={"project_id": project.project_id, "project": project.project_name, "via": via},
    )


def new_system_user(team, name: str) -> CustomUser:
    """The account a project's own messages come from. It has no password and
    never signs in, and it holds no billed seat. The page names it
    `<team id>-<name with underscores>@genos.tech`; a taken address (a project of
    that name existed before) gets a short suffix instead of failing."""
    slug = re.sub(r"\s+", "_", name)
    email = f"{team.team_id}-{slug}@genos.tech"
    if CustomUser.objects.with_email(email).exists():
        email = f"{team.team_id}-{slug}-{uuid.uuid4().hex[:6]}@genos.tech"
    return CustomUser.objects.create_user(
        email=email, username=name, password=None, is_system_user=True
    )


def create_project(
    *,
    team,
    owner: CustomUser,
    name,
    is_private: bool = False,
    member_ids: Iterable = (),
    request=None,
) -> ProjectMaster:
    """A project owned by `owner`, in `team`, with `member_ids` added to it.

    Every person named must already be on the team: a project can't be used to pull
    in an outsider. Nothing is created if any of that fails.
    """
    name = _clean_name(name)
    if ProjectMaster.objects.filter(team=team, project_name=name).exists():
        raise ProjectRefused("Another project in this team already uses that name.", 409)

    wanted = []
    for raw in member_ids or ():
        member_id = str(raw)
        if member_id == str(owner.id) or member_id in wanted:
            continue
        if not is_team_member(team.team_id, member_id):
            raise ProjectRefused(f"User {member_id} is not a member of this team.", 400)
        wanted.append(member_id)

    with transaction.atomic():
        system_user = new_system_user(team, name)
        taken = set(
            ProjectMaster.objects.filter(team=team, code__isnull=False).values_list(
                "code", flat=True
            )
        )
        project = ProjectMaster.objects.create(
            team=team,
            project_name=name,
            code=derive_project_code(name, taken),
            owner=owner,
            project_system_user=system_user,
            is_private=bool(is_private),
        )
        # Plain `.create()`, never `bulk_create`: the signal that mirrors a
        # membership into the project's chat channel only fires on a save.
        ProjectMembers.objects.create(team=team, project=project, attendee=owner)
        TeamMembers.objects.get_or_create(team=team, attendee=system_user)
        for member_id in wanted:
            ProjectMembers.objects.create(team=team, project=project, attendee_id=member_id)
            record_member_added(project, member_id, via="direct", request=request, actor=owner)
    return project


def set_project_code(project: ProjectMaster, raw) -> ProjectMaster:
    code = (raw or "").strip().upper()
    if not code:
        raise ProjectRefused("Code cannot be empty.", 400)
    if not PROJECT_CODE_RE.match(code):
        raise ProjectRefused(
            "Code must be 2-6 uppercase letters/digits, starting with a letter.", 400
        )
    if (
        ProjectMaster.objects.filter(team=project.team_id, code=code)
        .exclude(project_id=project.project_id)
        .exists()
    ):
        raise ProjectRefused("Another project in this team already uses that code.", 409)
    project.code = code
    project.save(update_fields=["code", "ts_updated_at"])
    # The code prefixes every task's display id, which search indexes (MIP-739):
    # re-emit the project's tasks and milestones on the next incremental pass.
    # Bumping each task's own `ts_updated_at` instead would reorder every list
    # sorted by it.
    mark_projects_dirty([project.project_id])
    return project


def set_project_name(project: ProjectMaster, raw) -> ProjectMaster:
    name = _clean_name(raw)
    if (
        ProjectMaster.objects.filter(team=project.team_id, project_name=name)
        .exclude(project_id=project.project_id)
        .exists()
    ):
        raise ProjectRefused("Another project in this team already uses that name.", 409)
    project.project_name = name
    project.save(update_fields=["project_name", "ts_updated_at"])
    return project


def change_member_role(project: ProjectMaster, user_id, role, *, request=None, actor=None) -> str:
    """Set a member's role to `editor` or `viewer`; return the role they had."""
    if not is_assignable(role):
        raise ProjectRefused(f"member_role must be one of {list(ASSIGNABLE_ROLES)}.", 400)
    if project.owner_id is not None and str(project.owner_id) == str(user_id):
        raise ProjectRefused(
            "The project owner's role cannot be changed. Transfer ownership instead.", 400
        )
    row = ProjectMembers.objects.filter(project_id=project.project_id, attendee_id=user_id).first()
    if row is None:
        raise ProjectRefused("That user is not a member of this project.", 404)
    previous = row.member_role
    row.member_role = role
    row.save(update_fields=["member_role", "ts_updated_at"])
    if previous != role:
        record_security_event(
            SecurityAction.ROLE_CHANGED,
            request=request,
            actor=actor,
            team_id=project.team_id,
            target_type="user",
            target_id=user_id,
            target_label=user_label(user_id),
            metadata={
                "scope": "project",
                "project_id": project.project_id,
                "project": project.project_name,
                "from": previous,
                "to": role,
            },
        )
    return previous


def remove_member(
    project: ProjectMaster, user_id, *, actor_id, unassign: bool, request=None, actor=None
) -> dict:
    """An owner or editor removes ANOTHER member from a project (MIP-1371); returns what was
    unassigned. The page and the agent tool both come through here.

    `can_remove_member` decides: the owner removes anyone but themselves, an editor removes
    viewers and guests only. Yourself is `leave`, and the owner cannot leave or be removed.
    A manager check that fails is the same 404 as a missing project, so a member learns
    nothing about people they may not remove. The membership row is a hard delete (there is
    no `is_deleted` here); its receivers take the person out of the project's chat and mark
    the project for a search ACL refresh. Their open tasks IN THIS PROJECT are unassigned
    when `unassign`; their other projects are untouched.
    """
    actor_role = resolve_project_role(project, actor_id)
    if not can_manage(actor_role):
        raise ProjectRefused("Project not found.", 404)
    if str(user_id) == str(actor_id):
        raise ProjectRefused("Use Leave to remove yourself.", 400)
    row = ProjectMembers.objects.filter(project_id=project.project_id, attendee_id=user_id).first()
    if row is None:
        raise ProjectRefused("That user is not a member of this project.", 404)
    target_role = resolve_project_role(project, user_id, row.member_role)
    if target_role == OWNER:
        raise ProjectRefused("The project owner cannot be removed. Transfer ownership first.", 400)
    if not can_remove_member(actor_role, target_role):
        raise ProjectRefused("Only the project owner can remove an editor.", 403)
    with transaction.atomic():
        row.delete()
        unassigned = clean_up_after_removal(
            project.team_id, user_id, project_id=project.project_id, unassign=unassign
        )
        record_security_event(
            SecurityAction.MEMBER_REMOVED,
            request=request,
            actor=actor,
            team_id=project.team_id,
            target_type="user",
            target_id=user_id,
            target_label=user_label(user_id),
            metadata={
                "via": "removed",
                "scope": "project",
                "project_id": project.project_id,
                "project": project.project_name,
                "role": target_role,
                "unassigned": unassigned,
            },
        )
    return unassigned
