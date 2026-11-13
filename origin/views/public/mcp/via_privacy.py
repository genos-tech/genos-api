"""Who made a change, as a connected client may read it (MIP-1470): the key's name is the owner's.

`get_task_history` carries `via`: how a change arrived. For a change made with an API key or a
connected app it holds the key's id, name and prefix (so a person can recognise their own writes),
and an Undo click carries internal ids (`undo_of`). Every member who can read the task could read
those, including the NAME another member gave their key ("my-laptop-claude"), which can be personal.

Rule (Ken, 2026-11-13): the name belongs to the owner only. A key writes as its owner, so a row's
actor is the key's owner: on a row the viewer made, `via` is left whole; on anyone else's row it is
reduced to its kind (`api_key`, `app`). Nothing else about the row changes.

This runs AFTER the registry tool, like the closed-ancestor fields, so it changes what a connected
client reads and nothing the in-app agent sees (what that model sees waits for the grouped eval,
flag `AGENT_CLIENT_IN_TASK_HISTORY`).
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import ToolContext

# The kinds whose `via` carries something only its owner should read: a key's id, name and prefix,
# an Undo's internal ids. `github` and `pr_merge` carry public facts (a GitHub login, a PR URL).
OWNER_ONLY_KINDS = frozenset({"api_key", "app"})


def with_via_owner_only(
    payload: dict[str, Any], args: dict[str, Any], mcp_args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    """`get_task_history`: other people's `via` is its kind only."""
    rows = payload.get("history")
    if not isinstance(rows, list):
        return payload
    me = str(ctx.user_id)
    shown: list[Any] = []
    for row in rows:
        via = row.get("via") if isinstance(row, dict) else None
        if isinstance(via, dict) and via.get("kind") in OWNER_ONLY_KINDS:
            actor = row.get("actor")
            owner = str(actor.get("user_id")) if isinstance(actor, dict) else ""
            if owner != me:
                row = {**row, "via": {"kind": via["kind"]}}
        shown.append(row)
    return {**payload, "history": shown}
