"""Find a note the caller may act on, for the note tools past create and update
(MIP-883).

Three note tables share ids, so a note is named by its family and its id. A note that
is missing, in another team, or not readable by the caller all read the same way, as
they do on the page's 404, so a guessed id confirms nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent.acl import may_read_note
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.note_targets import PERSONAL_FAMILY, normalize_note_type
from origin.search_engine.agent.tools.task_access import as_int
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_EDITOR,
    get_effective_role,
)

_MODELS = {"personal": PersonalNoteMaster, "task": TaskNoteMaster, "chat": ChatNoteMaster}
_CODES = {"personal": NOTE_TYPE_PERSONAL, "task": NOTE_TYPE_TASK, "chat": NOTE_TYPE_CHAT}


@dataclass(frozen=True)
class NoteRef:
    family: str  # "personal" (also Team Notes and shared), "task" or "chat"
    code: int  # the NOTE_TYPE_* the permission tables use
    note_id: int
    note: Any


def load_readable_note(args: dict[str, Any], ctx: ToolContext) -> NoteRef:
    note_type = normalize_note_type(args.get("note_type"))
    family = "personal" if note_type in PERSONAL_FAMILY else note_type
    note_id = as_int(args.get("note_id"), "note_id")
    note = _MODELS[family].objects.filter(note_id=note_id).first()
    if (
        note is None
        or str(getattr(note, "team_id", "") or "") != ctx.team_id
        or not may_read_note(
            user_id=ctx.user_id,
            note_type_code=_CODES[family],
            note_id=note_id,
            owner_id=getattr(note, "owner_id", None),
        )
    ):
        raise ToolError(
            f"Note {note_id} not found. Find its id with a search or `list_note_folders`."
        )
    return NoteRef(family=family, code=_CODES[family], note_id=note_id, note=note)


def has_write_permission(*, owner_id, note_id: int, note_type_code: int, ctx: ToolContext) -> bool:
    """True if ctx.user_id may edit the note.

    Check 1: owner, who always has write permission (covers legacy personal notes
             created before role rows existed). Only for a PERSONAL note: a chat note's author
             (MIP-1492) or a task note's (MIP-1511) who left the chat, the project or the team
             is resolved by Check 2 like anyone.
    Check 2: effective role is Editor or stronger, the same resolution the REST layer's
             `require_write_role` uses: an explicit NotePermissionMaster row wins;
             otherwise task-note project members and chat members are implicit Editors,
             and a personal note's Team Notes folder grants what it grants.
    """
    if note_type_code == NOTE_TYPE_PERSONAL and str(owner_id or "") == ctx.user_id:
        return True
    role = get_effective_role(ctx.user_id, note_type_code, note_id)
    return role is not None and role <= ROLE_EDITOR
