"""`delete_note` write tool (MIP-883): delete a note for good.

A hard delete, in all three note families: the note, its permission rows, its whole
version history and its search chunks go, and nothing can bring them back, so it is
destructive on MCP (the key's opt-in) and its own tool, never an option of
`update_note`.

Stricter than the page on purpose (Ken's decision 5). The page lets any Editor delete
a note, which includes a colleague's note in a public Team Notes folder and a note
someone shared with you to edit. The tool deletes a note only for the person who
wrote it, or one who holds the Owner role on it (the owner of the Team Notes folder
it is filed in counts). Anyone else is told to ask the author or use the app.

It also refuses a note that has sub-notes, as the page does: the page's delete would
leave them orphaned.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services.note_delete import DeleteRefused, child_note_count, delete_note
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, get_effective_role


def _may_delete(ref, ctx: ToolContext) -> bool:
    role = get_effective_role(ctx.user_id, ref.code, ref.note_id)
    if role == ROLE_OWNER:
        return True
    wrote_it = str(getattr(ref.note, "owner_id", "") or "") == ctx.user_id
    # The author, unless a grant has since made them a mere reader of their own note.
    return wrote_it and (role is None or role <= ROLE_EDITOR)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ref = load_readable_note(args, ctx)
    title = getattr(ref.note, "title", "") or "Untitled"

    if not _may_delete(ref, ctx):
        raise ToolForbidden(
            f'Not authorized: "{title}" was written by someone else, and only its author or '
            "its owner can delete it. Ask them, or delete it in the Genos app."
        )

    children = child_note_count(team_id=ctx.team_id, family=ref.family, note_id=ref.note_id)
    if children:
        raise ToolError(
            f'"{title}" has {children} sub-note{"s" if children != 1 else ""}. Delete or move '
            "them first, or the app would leave them without a parent. Nothing was deleted."
        )

    try:
        delete_note(team_id=ctx.team_id, family=ref.family, note_id=ref.note_id)
    except DeleteRefused as refused:
        raise ToolError(f"{refused.message} Nothing was deleted.")
    except Exception as e:  # noqa: BLE001
        failure = log_failure("delete_note failed", e)
        raise ToolError(f"Failed to delete the note (ref {failure}).")
    return {
        "note_type": ref.family,
        "note_id": ref.note_id,
        "title": title,
        "deleted": True,
        "__summary__": f'Deleted the note "{title}" and its version history. This cannot be undone.',
    }


DELETE_NOTE = Tool(
    name="delete_note",
    description=(
        "Delete a note for good: its text, its version history and its search entries, "
        "with no way back. REQUIRES USER APPROVAL. Only for a note the user wrote or owns, "
        "and only when they clearly asked. Get `note_id` and its kind from `search_knowledge_base` "
        "or `list_note_folders` FIRST, and use `fetch_note` if there is any doubt which note "
        "it is. A note with sub-notes is refused. To empty a folder use `delete_note_folder`, "
        "not this. To change a note instead, use `update_note`."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "shared", "task", "chat"],
                "description": "Which kind of note (personal = My Notes).",
            },
            "note_id": {"type": "INTEGER", "description": "The note's id."},
        },
        "required": ["note_type", "note_id"],
    },
    run=_run,
    requires_approval=True,
)
