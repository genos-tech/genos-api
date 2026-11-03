"""`delete_note_folder` write tool (MIP-883): delete a folder and what is in it.

A folder delete is recursive and hard: every folder below it and every note filed in
any of them goes, versions and all. The page puts a confirm dialog in front of that. A
tool has no dialog, so the rules are tighter than the page's:

* **Contents need a say-so.** A folder that holds notes or folders is refused unless
  the call says `include_contents: true`, so deleting a folder with things in it is a
  decision the person sees on the approval card, not a side effect.
* **My Notes** folders are the owner's. The page also deletes notes in them that were
  shared with other people; the tool refuses, since that removes them for everyone.
* **Team Notes** folders keep the page's rule: only the folder's Owner, and never while
  anyone else's note or folder is inside (the page answers 409 with counts).

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.services.note_delete import (
    DeleteRefused,
    delete_personal_folder,
    delete_team_folder,
    personal_folder_contents,
    require_team_folder_owner,
    team_folder_contents,
    team_folder_foreign_counts,
)
from origin.services.note_favorites import can_see_folder
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL


def _folder(raw_id: Any, ctx: ToolContext) -> PersonalNoteFolder:
    try:
        folder_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(
            f"`folder_id` must be an integer (got {raw_id!r}). Get ids from list_note_folders."
        )
    folder = PersonalNoteFolder.objects.filter(folder_id=folder_id, team=ctx.team_id).first()
    # One answer for "no such folder" and "not yours to see".
    if folder is None or not can_see_folder(ctx.user_id, folder):
        raise ToolError(f"Folder {folder_id} not found. Call list_note_folders for the ids.")
    return folder


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    include = args.get("include_contents", False)
    if not isinstance(include, bool):
        raise ToolError(f"`include_contents` must be true or false (got {include!r}).")
    folder = _folder(args.get("folder_id"), ctx)
    name = folder.name
    is_team = folder.scope == PersonalNoteFolder.SCOPE_TEAM

    if is_team:
        # The rules that no say-so can change come first, so the person is not told to
        # confirm something that would be refused.
        try:
            require_team_folder_owner(
                team_id=ctx.team_id, user_id=ctx.user_id, folder_id=folder.folder_id
            )
        except DeleteRefused as refused:
            raise ToolError(f"{refused.message} Nothing was deleted.")
        folder_ids, note_ids = team_folder_contents(team_id=ctx.team_id, folder_id=folder.folder_id)
        foreign_notes, foreign_folders = team_folder_foreign_counts(
            user_id=ctx.user_id, folder_ids=folder_ids, note_ids=note_ids
        )
        if foreign_notes or foreign_folders:
            raise ToolError(
                f'"{name}" still holds {_plural(foreign_notes, "note")} and '
                f"{_plural(foreign_folders, 'folder')} that belong to other people, so it can't "
                "be deleted until they are moved out or deleted by their authors. "
                "Nothing was deleted."
            )
    else:
        folder_ids, note_ids = personal_folder_contents(
            team_id=ctx.team_id, owner_id=ctx.user_id, folder_id=folder.folder_id
        )
        shared = (
            NotePermissionMaster.objects.filter(note_type=NOTE_TYPE_PERSONAL, note_id__in=note_ids)
            .exclude(user=ctx.user_id)
            .values("note_id")
            .distinct()
            .count()
        )
        if shared:
            raise ToolError(
                f'{_plural(shared, "note")} in "{name}" {"is" if shared == 1 else "are"} shared '
                "with other people, and deleting the folder would remove them for everyone. "
                "Unshare or move them first. Nothing was deleted."
            )

    subfolders = len(folder_ids) - 1
    if (subfolders or note_ids) and not include:
        raise ToolError(
            f'"{name}" holds {_plural(len(note_ids), "note")} and '
            f"{_plural(subfolders, 'subfolder')}. Deleting the folder deletes all of them for "
            "good. If that is what the user wants, call again with include_contents true. "
            "Nothing was deleted."
        )

    try:
        if is_team:
            gone_folders, gone_notes = delete_team_folder(
                team_id=ctx.team_id, user_id=ctx.user_id, folder=folder
            )
        else:
            gone_folders, gone_notes = delete_personal_folder(
                team_id=ctx.team_id, owner_id=ctx.user_id, folder_id=folder.folder_id
            )
    except DeleteRefused as refused:
        counts = ""
        if refused.extra:
            counts = (
                f" ({_plural(refused.extra.get('foreignNoteCount', 0), 'note')} and "
                f"{_plural(refused.extra.get('foreignFolderCount', 0), 'folder')} belong to "
                "other people)"
            )
        raise ToolError(f"{refused.message}{counts} Nothing was deleted.")
    except Exception as e:  # noqa: BLE001
        failure = log_failure("delete_note_folder failed", e)
        raise ToolError(f"Failed to delete the folder (ref {failure}).")

    return {
        "folder_id": folder.folder_id,
        "name": name,
        "deleted_folders": len(gone_folders),
        "deleted_notes": len(gone_notes),
        "__summary__": (
            f'Deleted the folder "{name}" with {_plural(len(gone_notes), "note")} and '
            f"{_plural(len(gone_folders) - 1, 'subfolder')}. This cannot be undone."
        ),
    }


DELETE_NOTE_FOLDER = Tool(
    name="delete_note_folder",
    description=(
        "Delete a note folder, and everything in it, for good. REQUIRES USER APPROVAL. Only "
        "when the user clearly asked. Call `list_note_folders` FIRST: it gives the folder id "
        "and shows what is inside. A folder that holds notes or subfolders is refused unless "
        "`include_contents` is true; only set that when the user has agreed to losing "
        "those notes. My Notes folders are deleted by their owner (notes shared with other "
        "people block it); a Team Notes folder only by its owner, and not while anyone "
        "else's notes are inside. To remove one note use `delete_note`."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "folder_id": {
                "type": "INTEGER",
                "description": "The folder (from list_note_folders).",
            },
            "include_contents": {
                "type": "BOOLEAN",
                "description": "true to also delete the notes and subfolders inside. Default "
                "false: a non-empty folder is refused.",
            },
        },
        "required": ["folder_id"],
    },
    run=_run,
    requires_approval=True,
)
