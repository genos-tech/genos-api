"""`manage_note_folder` write tool (MIP-883): create a folder, or rename one.

The rules are the pages' (`origin.services.note_folder_manage`). Two folder spaces
share one table: My Notes folders belong to one person, and Team Notes folders carry
the access rules for the notes filed in them.

What is left out on purpose:

* **Visibility and sharing.** A new top-level Team Notes folder is created private to
  its creator; "public" would give every teammate Editor on whatever is filed there,
  and on MCP no one is asked first. A subfolder inherits from its parent, as the page
  does by default.
* **Moving and deleting.** Neither is part of "organize"; delete is its own tool.
* **Other teams' folders.** A folder another team shared with you is theirs to
  organize; the note tools do not write across teams.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.services.note_folder_manage import (
    FolderRefused,
    create_personal_folder,
    create_team_folder,
    rename_personal_folder,
    rename_team_folder,
)

_ACTIONS = ("create", "rename")
_SPACES = ("personal", "team")


def _folder_id(raw: Any, name: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ToolError(
            f"`{name}` must be an integer (got {raw!r}). Get ids from list_note_folders."
        )


def _folder_in_team(folder_id: int, ctx: ToolContext) -> PersonalNoteFolder:
    folder = PersonalNoteFolder.objects.filter(folder_id=folder_id, team=ctx.team_id).first()
    # Another person's My Notes folder reads as missing, as it does on the page.
    if folder is None or (
        folder.scope == PersonalNoteFolder.SCOPE_PERSONAL and str(folder.owner_id) != ctx.user_id
    ):
        raise ToolError(f"Folder {folder_id} not found. Call list_note_folders for the ids.")
    return folder


def _describe(folder: PersonalNoteFolder) -> dict[str, Any]:
    out: dict[str, Any] = {
        "folder_id": folder.folder_id,
        "name": folder.name,
        "space": folder.scope,
        "parent_folder_id": folder.parent_folder_id,
    }
    if folder.scope == PersonalNoteFolder.SCOPE_TEAM:
        out["visibility"] = folder.visibility or "inherits"
    return out


def _create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    space = args.get("space")
    if space is not None and space not in _SPACES:
        raise ToolError(f"`space` must be one of {list(_SPACES)} (got {space!r}).")

    parent_id = None
    if args.get("parent_folder_id") is not None:
        parent_id = _folder_id(args["parent_folder_id"], "parent_folder_id")
        parent = _folder_in_team(parent_id, ctx)
        if space is not None and space != parent.scope:
            raise ToolError(
                f"Folder {parent_id} is in {parent.scope} notes, so a folder inside it is too. "
                "Leave `space` out, or choose a parent in the space you mean."
            )
        space = parent.scope
    space = space or "personal"

    try:
        if space == "personal":
            folder = create_personal_folder(
                team_id=ctx.team_id,
                owner_id=ctx.user_id,
                name=args.get("name"),
                parent_folder_id=parent_id,
            )
        else:
            # A top-level folder has to state a visibility; this tool states the
            # narrow one. A subfolder inherits.
            folder, _owning_team_id = create_team_folder(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                name=args.get("name"),
                parent_folder_id=parent_id,
                visibility=PersonalNoteFolder.VISIBILITY_PRIVATE if parent_id is None else None,
            )
    except FolderRefused as refused:
        raise ToolError(f"{refused.message} Nothing was created.")
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_note_folder create failed", e)
        raise ToolError(f"Failed to create the folder (ref {ref}).")

    where = "Team Notes" if space == "team" else "My Notes"
    summary = f'Created the {where} folder "{folder.name}"'
    if space == "team" and parent_id is None:
        summary += ". It is private to you; this tool can't share a folder or change who sees it"
    return {**_describe(folder), "__summary__": summary}


def _rename(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    folder_id = _folder_id(args.get("folder_id"), "folder_id")
    existing = _folder_in_team(folder_id, ctx)
    before = existing.name
    try:
        if existing.scope == PersonalNoteFolder.SCOPE_PERSONAL:
            folder = rename_personal_folder(
                team_id=ctx.team_id,
                owner_id=ctx.user_id,
                folder_id=folder_id,
                name=args.get("name"),
            )
        else:
            folder = rename_team_folder(
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                folder_id=folder_id,
                name=args.get("name"),
            )
    except FolderRefused as refused:
        raise ToolError(f"{refused.message} Nothing was changed.")
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_note_folder rename failed", e)
        raise ToolError(f"Failed to rename the folder (ref {ref}).")
    return {
        **_describe(folder),
        "previous_name": before,
        "__summary__": f'Renamed the folder "{before}" to "{folder.name}"',
    }


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = args.get("action")
    if action not in _ACTIONS:
        raise ToolError(f"`action` must be one of {list(_ACTIONS)} (got {action!r}).")
    return _create(args, ctx) if action == "create" else _rename(args, ctx)


MANAGE_NOTE_FOLDER = Tool(
    name="manage_note_folder",
    description=(
        "Create a folder for notes, or rename one. REQUIRES USER APPROVAL. Call "
        "`list_note_folders` FIRST: it gives the folder ids and shows what exists. action "
        "'create' makes a My Notes folder (space 'personal', the default) or a Team Notes "
        "folder (space 'team'), at the top or inside `parent_folder_id` (the space then "
        "follows the parent). A new top-level Team Notes folder is private to its creator; "
        "this tool can't share a folder or change who sees it. action 'rename' changes a "
        "folder's name. It can't move or delete a folder."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": list(_ACTIONS),
                "description": "'create' a folder, or 'rename' one.",
            },
            "name": {
                "type": "STRING",
                "description": "The folder's name (1-255 characters): the new one, or the new "
                "name when renaming.",
            },
            "space": {
                "type": "STRING",
                "enum": list(_SPACES),
                "description": "create: 'personal' (My Notes, the default) or 'team' (Team "
                "Notes). Leave out when `parent_folder_id` is given.",
            },
            "parent_folder_id": {
                "type": "INTEGER",
                "description": "create: put the folder inside this one (from "
                "list_note_folders). Leave out for the top level.",
            },
            "folder_id": {
                "type": "INTEGER",
                "description": "rename: the folder (from list_note_folders).",
            },
        },
        "required": ["action", "name"],
    },
    run=_run,
    requires_approval=True,
)
