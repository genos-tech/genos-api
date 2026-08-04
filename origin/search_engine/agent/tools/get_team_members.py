"""`get_team_members` tool — list active users in the team.

Returns user_id, username, and email for every non-deleted, non-system
user who belongs to the requesting team.  This is the correct way for the
agent to resolve "assign to John" or "who is on this team?" — the model
gets a stable UUID → name mapping and can pass the UUID to `assign_task`.

ACL contract:
  * Only team members (ctx.team_id, via TeamMembers) are returned.
  * System users (is_system_user=True) are excluded — they are internal
    service accounts and should never appear in agent-visible lists.
  * Soft-deleted users (is_deleted=True) are excluded.
  * ctx.team_id is server-trusted; the tool takes no arguments that
    could influence which team's data is returned.
  * Any OUTSIDER asking gets only the people they actually share
    something with — a project, an external chat, a shared note folder.
    This tool is the shortest path from "who is on this team?" to a
    full roster WITH EMAILS, so leaving it unnarrowed would undo the
    REST-side scoping through the agent — the answer is generated in
    prose, which makes the leak harder to notice, not easier.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.team_models import TeamMembers
from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.views.utils.scope_guards import (
    external_visible_user_ids,
    guest_visible_user_ids,
    is_team_member,
)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    memberships = (
        TeamMembers.objects.filter(
            team_id=ctx.team_id,
            is_deleted=False,
        )
        .select_related("attendee")
        .order_by("attendee__username")
    )

    # Narrow for everyone who is not a member, and narrow on the ANSWER
    # rather than on whether an answer was found. An earlier version
    # applied the filter only when the visible set was non-empty, so an
    # outsider the set could not explain — someone admitted to a chat or a
    # note folder, who holds no project row — fell through to the whole
    # roster. "I could not work out what you may see" must mean "nothing",
    # never "everything".
    if not is_team_member(ctx.team_id, ctx.user_id):
        visible = guest_visible_user_ids(ctx.team_id, ctx.user_id) | external_visible_user_ids(
            ctx.team_id, ctx.user_id
        )
        memberships = memberships.filter(attendee_id__in=visible)

    members = []
    for m in memberships:
        u = m.attendee
        # Guard against orphaned membership rows and internal accounts.
        if u is None or u.is_deleted or u.is_system_user:
            continue
        members.append(
            {
                "user_id": str(u.id),
                "username": u.username or "",
                "email": u.email or "",
                "role": u.role or "",
            }
        )

    return {
        "members": members,
        "__summary__": f"{len(members)} active team member(s)",
    }


GET_TEAM_MEMBERS = Tool(
    name="get_team_members",
    description=(
        "List all active users in the team with their user_id, username, "
        "and email. Use this to resolve a person's name to their UUID "
        "before calling assign_task, or to answer 'who is on this team?' "
        "questions. Only returns real users in the current team — no "
        "system accounts, no deleted users, no cross-team data."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    run=_run,
)
