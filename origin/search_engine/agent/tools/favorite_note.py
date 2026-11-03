"""`favorite_note` write tool (MIP-883): star or unstar a note or a note folder.

A favorite is the caller's own bookmark: it shows in their sidebar and nobody else's,
and starring changes nothing about the note. Only something the caller can open may be
starred (the sidebar prints the title). Starring twice, or unstarring what is not
starred, is not an error.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services.note_favorites import (
    can_see_folder,
    star_folder,
    star_note,
    unstar_folder,
    unstar_note,
)


def _wanted(args: dict[str, Any]) -> bool:
    value = args.get("favorite", True)
    if not isinstance(value, bool):
        raise ToolError(f"`favorite` must be true or false (got {value!r}).")
    return value


def _folder(args: dict[str, Any], ctx: ToolContext, favorite: bool) -> dict[str, Any]:
    try:
        folder_id = int(args["folder_id"])
    except (TypeError, ValueError):
        raise ToolError(f"`folder_id` must be an integer (got {args['folder_id']!r}).")
    folder = PersonalNoteFolder.objects.filter(folder_id=folder_id, team=ctx.team_id).first()
    # One answer for "no such folder" and "not yours to see".
    if folder is None or not can_see_folder(ctx.user_id, folder):
        raise ToolError(f"Folder {folder_id} not found. Call list_note_folders for the ids.")
    try:
        if favorite:
            changed = star_folder(team_id=ctx.team_id, user_id=ctx.user_id, folder=folder)
        else:
            changed = unstar_folder(user_id=ctx.user_id, folder_id=folder_id)
    except Exception as e:  # noqa: BLE001
        ref = log_failure("favorite_note folder failed", e)
        raise ToolError(f"Failed to update the favorite (ref {ref}).")
    return _result(f'the folder "{folder.name}"', favorite, changed, folder_id=folder_id)


def _note(args: dict[str, Any], ctx: ToolContext, favorite: bool) -> dict[str, Any]:
    ref = load_readable_note(args, ctx)
    try:
        if favorite:
            changed = star_note(
                team_id=ctx.team_id, user_id=ctx.user_id, note_type=ref.code, note_id=ref.note_id
            )
        else:
            changed = unstar_note(user_id=ctx.user_id, note_type=ref.code, note_id=ref.note_id)
    except Exception as e:  # noqa: BLE001
        ref_id = log_failure("favorite_note note failed", e)
        raise ToolError(f"Failed to update the favorite (ref {ref_id}).")
    title = getattr(ref.note, "title", "") or "Untitled"
    return _result(f'the note "{title}"', favorite, changed, note_id=ref.note_id)


def _result(what: str, favorite: bool, changed: bool, **ids: Any) -> dict[str, Any]:
    if favorite:
        said = f"Starred {what}" if changed else f"Already starred: {what}"
    else:
        said = f"Unstarred {what}" if changed else f"Not starred, nothing to remove: {what}"
    return {**ids, "favorite": favorite, "changed": changed, "__summary__": said}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    favorite = _wanted(args)
    has_folder = args.get("folder_id") is not None
    has_note = args.get("note_id") is not None
    if has_folder == has_note:
        raise ToolError("Give either `folder_id`, or `note_type` with `note_id` (not both).")
    return _folder(args, ctx, favorite) if has_folder else _note(args, ctx, favorite)


FAVORITE_NOTE = Tool(
    name="favorite_note",
    description=(
        "Star a note or a note folder as a favorite, or unstar it. REQUIRES USER APPROVAL. "
        "It is the person's own bookmark in their sidebar; the note and other people are "
        "unaffected. Pass `folder_id` for a folder, or `note_type` with `note_id` for a note. "
        "`favorite` is true to star (the default) and false to unstar."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "shared", "task", "chat"],
                "description": "A note: which kind (personal = My Notes).",
            },
            "note_id": {"type": "INTEGER", "description": "A note: its id."},
            "folder_id": {
                "type": "INTEGER",
                "description": "A folder instead of a note: its id (from list_note_folders).",
            },
            "favorite": {
                "type": "BOOLEAN",
                "description": "true to star (default), false to unstar.",
            },
        },
        "required": [],
    },
    run=_run,
    requires_approval=True,
)
