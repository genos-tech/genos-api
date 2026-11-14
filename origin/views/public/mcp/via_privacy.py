"""Who made a change, as another member may read it: the key's name is the owner's (MIP-1470, MIP-1485).

A write's `via` says how a change arrived. For a change made with an API key it holds the key's id,
name and prefix; for a connected app, the connection id and the client's name; an Undo click carries
internal ids (`undo_of`). Every member who can read the task could read those, including the NAME
another member gave their key ("my-laptop-claude"), which can be personal.

Rule (Ken, 2026-11-13): the name belongs to the owner only. A key writes as its owner, so a row's
actor is the key's owner: on a row the viewer made, `via` is left whole; on anyone else's row it is
reduced to its kind (`api_key`, `oauth`, `app`, or any kind not known to be public). Nothing else
about the row changes. One exception, on purpose (MIP-1568): a connected app's official family
(`claude`, `chatgpt`) is shown to everyone, for the app's icon; see `SHOW_OAUTH_APP_TO_EVERYONE`.

`via_for_viewer` is that rule, for every surface: the in-app agent's `get_task_history` (MIP-1470,
unconditionally: it is a privacy fix), MCP `get_task_history` and `get_progress_history`
(`with_via_owner_only`, below, which also covers the progress feed), the web client's task activity
and progress feeds (`task_activity_views`), and the comment list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from origin.services.mcp_client_family import APP_FAMILIES

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

# MIP-1568 (Ken, 2026-11-14): a connected app's official family (`claude`, `chatgpt`, stamped on
# its `via` by `McpOAuthCredential.via`) is shown to EVERY reader of the row, so a colleague, a
# notification card or the chat list can show the app's icon. A reader learns which AI product made
# a change, never the connection id, the client name or which of the member's apps it was.
# Flip this to False and the family is the actor's alone, like the name: one line, no other change.
SHOW_OAUTH_APP_TO_EVERYONE = True


def via_for_viewer(via: Any, actor_id: Any, viewer_id: Any) -> Any:
    """`via` as `viewer_id` may read it: whole for the row's actor (the key's owner), else its kind.

    One rule for every surface that shows a write's `via` (MCP `get_task_history` and
    `get_progress_history`, the task activity and progress feeds, the comment list). For anyone
    but the actor, a kind in `SAFE_VIA_FIELDS` keeps its listed public fields; every other `via`
    is reduced to `{"kind": kind}`, or `{}` when it has no usable kind (a value that is not a dict,
    a dict without a text kind). A connected app's family is the one extra field everyone may
    read (`SHOW_OAUTH_APP_TO_EVERYONE`): `{"kind": "oauth", "app": "claude"}`, only when it is
    one of the known families. `None` stays `None`. The input is never edited.
    """
    if via is None:
        return None
    if actor_id is not None and viewer_id is not None and str(actor_id) == str(viewer_id):
        return via
    kind = via.get("kind") if isinstance(via, dict) else None
    if not isinstance(kind, str) or not kind:
        return {}
    shown = {"kind": kind, **{f: via[f] for f in SAFE_VIA_FIELDS.get(kind, ()) if f in via}}
    if kind == "oauth" and SHOW_OAUTH_APP_TO_EVERYONE and via.get("app") in APP_FAMILIES:
        shown["app"] = via["app"]
    return shown


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
