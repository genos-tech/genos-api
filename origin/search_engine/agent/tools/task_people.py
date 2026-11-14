"""People on a task, for the agent tools (MIP-878).

A collaborator, and a person named in a `member` custom field, are put on a
task by someone with no picker in front of them: they give a user id. The UI's
pickers offer the team's members (and the create path drops anyone else, in
`people_allowed_on`), so the tools accept the same people and say so when they
refuse one. A collaborator has to be someone who can open the task (a member of
its project, or its assignee or reporter: `may_collaborate`, MIP-1567), because
they are told about it; the REST PUT refuses a new one the same way, but the tools
say so in words, because over MCP a silently dropped id reads as success.
"""

from __future__ import annotations

import uuid
from typing import Any

from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolError
from origin.services.task_collaborators import may_collaborate, may_name_person


def person_id(raw: Any, project: Any, where: str, *, readers: Any = None) -> str:
    """The canonical id of a person who may be put on a task in `project`: a
    member of its team or of the project itself (`may_name_person`), who exists
    and has not deleted their account.

    With `readers` (a collection of ids, possibly empty) the person is a
    COLLABORATOR instead: a member of the project, or one of `readers` (the task's
    assignee and reporter), because they have to be able to open the task."""
    if project is None:
        raise ToolError(f"{where}: the task is in no project, so nobody can be put on it.")
    try:
        uid = str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError):
        raise ToolError(
            f"{where}: {raw!r} is not a user id. Use get_team_members or "
            "list_project_members to find one."
        )
    user = CustomUser.objects.filter(id=uid).first()
    if readers is not None:
        # One answer for an unknown, a deleted and an outside user: whether an id exists is not
        # for the caller to learn.
        if user is None or user.is_deleted or not may_collaborate(project, uid, readers):
            raise ToolError(
                f"{where}: user {uid} is not a member of this project, so they could not "
                "open the task. Add them to the project first, or use list_project_members "
                "to find a valid user id."
            )
        return uid
    if user is None or user.is_deleted or not may_name_person(project, uid):
        raise ToolError(
            f"{where}: user {uid} is not a member of this team or project. Use "
            "get_team_members or list_project_members to find valid user ids."
        )
    return uid


def plan_collaborators(
    raw: Any,
    project: Any,
    current_ids: set[str],
    *,
    where: str = "`collaborators`",
    readers: Any = (),
) -> list[str]:
    """The task's whole new collaborator list, as canonical user ids, or a
    ToolError naming the first id that can't be used. `[]` clears.

    The list REPLACES the current one, as the UI's picker does. Someone who is
    already a collaborator is kept without being checked again: they may have left
    the team since, and refusing them would make it impossible to keep the others
    or to remove them. Anyone new has to be someone who can open the task: a member of its
    project, or one of `readers` (its assignee and reporter).
    """
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ToolError(f"{where} must be a list of user ids (got {raw!r}). Use [] to clear.")
    ids: list[str] = []
    for item in raw:
        try:
            uid = str(uuid.UUID(item.strip()))
        except ValueError:
            raise ToolError(
                f"{where}: {item!r} is not a user id. Use get_team_members or "
                "list_project_members to find one."
            )
        if uid not in ids:
            ids.append(uid)
    for uid in ids:
        if uid not in current_ids:
            person_id(uid, project, where, readers=readers)
    return ids
