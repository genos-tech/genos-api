"""Who made a change, as another member may read it: the key's name is the owner's (MIP-1470, MIP-1485).

A write's `via` says how a change arrived. For a change made with an API key it holds the key's id,
name and prefix; for a connected app, the connection id and the client's name; an Undo click carries
internal ids (`undo_of`). Every member who can read the task could read those, including the NAME
another member gave their key ("my-laptop-claude"), which can be personal.

Rule (Ken, 2026-11-13): the name belongs to the owner only. A key writes as its owner, so a row's
actor is the key's owner: on a row the viewer made, `via` is left whole; on anyone else's row it is
reduced to its kind (`api_key`, `oauth`, `app`). Nothing else about the row changes.

`via_for_viewer` is that rule, for every surface: MCP `get_task_history` (`with_via_owner_only`,
below), the web client's task activity and progress feeds (`task_activity_views`), and the comment
list. The MCP hook runs AFTER the registry tool, like the closed-ancestor fields, so it changes what
a connected client reads and nothing the in-app agent sees (what that model sees waits for the
grouped eval, flag `AGENT_CLIENT_IN_TASK_HISTORY`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the REST feed imports `via_for_viewer`; it needs none of the agent tools
    from origin.search_engine.agent.tools import ToolContext

# The kinds whose `via` carries something only its owner should read: a key's id, name and prefix,
# a connected app's connection id and client name, an Undo's internal ids. `github` and `pr_merge`
# carry public facts (a GitHub login, a PR URL).
OWNER_ONLY_KINDS = frozenset({"api_key", "oauth", "app"})


def via_for_viewer(via: Any, actor_id: Any, viewer_id: Any) -> Any:
    """`via` as `viewer_id` may read it: whole for the row's actor (the key's owner), else its kind.

    One rule for every surface that shows a write's `via` (MCP `get_task_history`, the task
    activity and progress feeds, the comment list). A `via` that is not a dict, or whose kind
    carries nothing private, is returned as it is. The input is never edited.
    """
    if not isinstance(via, dict) or via.get("kind") not in OWNER_ONLY_KINDS:
        return via
    if actor_id is not None and viewer_id is not None and str(actor_id) == str(viewer_id):
        return via
    return {"kind": via["kind"]}


def with_via_owner_only(
    payload: dict[str, Any], args: dict[str, Any], mcp_args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    """`get_task_history`: other people's `via` is its kind only."""
    rows = payload.get("history")
    if not isinstance(rows, list):
        return payload
    shown: list[Any] = []
    for row in rows:
        via = row.get("via") if isinstance(row, dict) else None
        if isinstance(via, dict):
            actor = row.get("actor")
            owner = actor.get("user_id") if isinstance(actor, dict) else None
            row = {**row, "via": via_for_viewer(via, owner, ctx.user_id)}
        shown.append(row)
    return {**payload, "history": shown}
