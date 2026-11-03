"""`list_note_access` read tool (MIP-883): who can open a note, and what they can do.

The read that `manage_note_sharing` needs first, and a useful answer on its own ("who
has this note?"). It lists the note's explicit shares, which is what a share is, and
says whether the caller may change them. It also says when a note is filed in a Team
Notes folder, because that folder, not the note, decides who else can open it.

Read only, so no approval. Anyone who may read the note may see this list, as on the
page.
"""

from __future__ import annotations

from typing import Any

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services.note_favorites import can_see_folder
from origin.views.utils.note_role import ROLE_OWNER, get_effective_role, get_explicit_role

ROLE_NAMES = {1: "owner", 2: "editor", 3: "viewer"}


def _folder_info(note, ctx: ToolContext) -> dict[str, Any] | None:
    folder_id = getattr(note, "folder_id", None)
    if folder_id is None:
        return None
    folder = PersonalNoteFolder.objects.filter(folder_id=folder_id, team=ctx.team_id).first()
    if (
        folder is None
        or folder.scope != PersonalNoteFolder.SCOPE_TEAM
        or not can_see_folder(ctx.user_id, folder)
    ):
        return None
    return {"folder_id": folder.folder_id, "name": folder.name}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ref = load_readable_note(args, ctx)
    title = getattr(ref.note, "title", "") or "Untitled"

    rows = (
        NotePermissionMaster.objects.filter(note_type=ref.code, note_id=ref.note_id)
        .select_related("user")
        .order_by("role_id", "ts_created_at")
    )
    people = [
        {
            "user_id": str(row.user_id),
            "username": row.user.username,
            "role": ROLE_NAMES.get(row.role_id, str(row.role_id)),
            "since": row.ts_created_at.isoformat() if row.ts_created_at else None,
        }
        for row in rows
        if row.user is not None
    ]
    role = get_effective_role(ctx.user_id, ref.code, ref.note_id)
    can_manage = get_explicit_role(ctx.user_id, ref.code, ref.note_id) == ROLE_OWNER
    out: dict[str, Any] = {
        "note_type": ref.family,
        "note_id": ref.note_id,
        "title": title,
        "people": people,
        "your_role": ROLE_NAMES.get(role) if role else ("author" if _wrote(ref, ctx) else None),
        "you_can_change_sharing": can_manage,
    }
    folder = _folder_info(ref.note, ctx) if ref.family == "personal" else None
    if folder is not None:
        out["filed_in_team_folder"] = folder
    shared = len([p for p in people if p["user_id"] != ctx.user_id])
    out["__summary__"] = (
        f'"{title}" is shared with {shared} other {"person" if shared == 1 else "people"}'
        + (f', and filed in the Team Notes folder "{folder["name"]}"' if folder else "")
        + ("." if can_manage else ", and you can't change who it is shared with.")
    )
    return out


def _wrote(ref, ctx: ToolContext) -> bool:
    return str(getattr(ref.note, "owner_id", "") or "") == ctx.user_id


LIST_NOTE_ACCESS = Tool(
    name="list_note_access",
    description=(
        "Who can open a note and with what role (owner, editor, viewer), and whether the "
        "caller may change it. Use it before `manage_note_sharing`, or to answer 'who has "
        "this note?'. A note filed in a Team Notes folder is also open to whoever that "
        "folder is open to; the result names the folder. Get `note_id` from "
        "`search_knowledge_base` or `list_note_folders`."
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
    requires_approval=False,
)
