"""Who made a change, as another member may read it: the key's name is the owner's (MIP-1470, MIP-1485).

A write's `via` says how a change arrived. For a change made with an API key it holds the key's id,
name and prefix; for a connected app, the connection id and the client's name; an Undo click carries
internal ids (`undo_of`). Every member who can read the task could read those, including the NAME
another member gave their key ("my-laptop-claude"), which can be personal.

Rule (Ken, 2026-11-13): the name belongs to the owner only. A key writes as its owner, so a row's
actor is the key's owner: on a row the viewer made, `via` is left whole; on anyone else's row it is
reduced to its kind (`api_key`, `oauth`, `app`, or any kind not known to be public). Nothing else
about the row changes.

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

# The kinds whose `via` carries nothing private, and the only fields of each that anyone may read:
# `agent` (the in-app AI), `pr_merge` (a PR URL), `dependencies`, `github` (a GitHub login) and
# `unknown`. Everything else is private to its owner: `api_key` (a key's id, name and prefix),
# `oauth` (a connected app's connection id and client name), `app` (an Undo's internal ids), and any
# kind added later. The gate fails closed: a new credential kind is reduced to its kind until it is
# listed here on purpose (MIP-1507).
SAFE_VIA_FIELDS: dict[str, tuple[str, ...]] = {
    "agent": (),
    "pr_merge": ("pr_url",),
    "dependencies": (),
    "github": ("github_user",),
    "unknown": (),
}


def via_for_viewer(via: Any, actor_id: Any, viewer_id: Any) -> Any:
    """`via` as `viewer_id` may read it: whole for the row's actor (the key's owner), else its kind.

    One rule for every surface that shows a write's `via` (MCP `get_task_history` and
    `get_progress_history`, the task activity and progress feeds, the comment list). For anyone
    but the actor, a kind in `SAFE_VIA_FIELDS` keeps its listed public fields; every other `via`
    is reduced to `{"kind": kind}`, or `{}` when it has no usable kind (a value that is not a dict,
    a dict without a text kind). `None` stays `None`. The input is never edited.
    """
    if via is None:
        return None
    if actor_id is not None and viewer_id is not None and str(actor_id) == str(viewer_id):
        return via
    kind = via.get("kind") if isinstance(via, dict) else None
    if not isinstance(kind, str) or not kind:
        return {}
    return {"kind": kind, **{f: via[f] for f in SAFE_VIA_FIELDS.get(kind, ()) if f in via}}


def with_via_owner_only(
    payload: dict[str, Any], args: dict[str, Any], mcp_args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    """`get_task_history` (`history`) and `get_progress_history` (`entries`): other people's `via`
    is its kind only."""
    out = payload
    for key in ("history", "entries"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        shown: list[Any] = []
        for row in rows:
            if isinstance(row, dict) and row.get("via") is not None:
                actor = row.get("actor")
                owner = actor.get("user_id") if isinstance(actor, dict) else None
                row = {**row, "via": via_for_viewer(row["via"], owner, ctx.user_id)}
            shown.append(row)
        out = {**out, key: shown}
    return out
