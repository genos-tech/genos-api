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
from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK

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
            f"Note {note_id} not found. Get ids from `search_workspace` or `list_note_folders`."
        )
    return NoteRef(family=family, code=_CODES[family], note_id=note_id, note=note)
