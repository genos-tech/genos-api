"""People on a task, for the agent tools (MIP-878).

A collaborator, and a person named in a `member` custom field, are put on a
task by someone with no picker in front of them: they give a user id. The UI's
pickers offer the team's members (and the create path drops anyone else, in
`people_allowed_on`), so the tools accept the same people and say so when they
refuse one. The REST PUT does not apply this rule to `collaborators` at all;
the tools do, because over MCP a silently dropped id reads as success.
"""

from __future__ import annotations

import uuid
from typing import Any

from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolError
from origin.services.task_collaborators import may_name_person


def person_id(raw: Any, project: Any, where: str) -> str:
    """The canonical id of a person who may be put on a task in `project`: a
    member of its team or of the project itself (the rule the create path
    applies to a collaborator, `may_name_person`), who exists and has not
    deleted their account."""
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
    if user is None or user.is_deleted or not may_name_person(project, uid):
        raise ToolError(
            f"{where}: user {uid} is not a member of this team or project. Use "
            "get_team_members or list_project_members to find valid user ids."
        )
    return uid


def plan_collaborators(
    raw: Any, project: Any, current_ids: set[str], *, where: str = "`collaborators`"
) -> list[str]:
    """The task's whole new collaborator list, as canonical user ids, or a
    ToolError naming the first id that can't be used. `[]` clears.

    The list REPLACES the current one, as the UI's picker does. Someone who is
    already a collaborator is kept without being checked again: they may have left
    the team since, and refusing them would make it impossible to keep the others
    or to remove them. Anyone new has to be a person who may be put on the task.
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
            person_id(uid, project, where)
    return ids


def canonical_user_id(raw: Any, where: str) -> str:
    """`raw` as a canonical user id, or a ToolError that says what to send instead.

    For a person a model named by something that is not an id (`reporter_id: "alice"`): the
    lookups behind `assign_task`'s rule take a UUID, and what they do with anything else is
    not an answer a model can act on (MIP-1561)."""
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError):
        raise ToolError(
            f"{where}: {raw!r} is not a user id. Use get_team_members or "
            "list_project_members to find one, or get_current_user for yourself."
        )
