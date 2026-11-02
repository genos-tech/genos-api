"""`restore_note_version` write tool (MIP-883): put an earlier version of a note back.

Walks back a bad `update_note`. The state it replaces is kept as a version first, so a
restore is itself undoable, and over MCP it is not one of the destructive tools.

The page restores in two steps: the API copies the version onto the note, and the
browser applies the body to the open editor. A tool has no browser, so it pushes the
body to the collaboration server itself, and it says so honestly if that fails: an
editor that is open keeps the old text, and its next autosave writes it back over the
restore. That is the same warning `update_note` gives.

Who may: anyone who may edit the note (`has_write_permission`), as on the page.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.note_access import has_write_permission, load_readable_note
from origin.search_engine.agent.tools.task_access import as_int
from origin.services.collab_push import PushOutcome, push_note_body_now
from origin.services.note_version_restore import RestoreRefused, restore_note_version


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ref = load_readable_note(args, ctx)
    title = getattr(ref.note, "title", "") or "Untitled"
    version_no = as_int(args.get("version_no"), "version_no")

    if not has_write_permission(
        owner_id=getattr(ref.note, "owner_id", None),
        note_id=ref.note_id,
        note_type_code=ref.code,
        ctx=ctx,
    ):
        raise ToolForbidden(f'Not authorized: you can read "{title}" but not edit it.')

    try:
        new_version, source = restore_note_version(
            team=TeamMaster.objects.get(team_id=ctx.team_id),
            user=CustomUser.objects.get(id=ctx.user_id),
            family=ref.family,
            note_id=ref.note_id,
            version_no=version_no,
        )
    except RestoreRefused as refused:
        hint = (
            " Call fetch_note_history to list the versions."
            if refused.message == "Version not found."
            else ""
        )
        raise ToolError(f"{refused.message}{hint} Nothing was changed.")
    except Exception as e:  # noqa: BLE001
        failure = log_failure("restore_note_version failed", e)
        raise ToolError(f"Failed to restore the version (ref {failure}).")

    outcome = push_note_body_now(ref.family, ref.note_id, source.body or [])
    summary = f'Restored "{source.title or title}" to version {version_no}.'
    if outcome == PushOutcome.FAILED:
        summary += (
            " The restore is saved, but the live editor could not be updated, so an open "
            "editor may still show the old text and overwrite the restore."
        )
    return {
        "note_type": ref.family,
        "note_id": ref.note_id,
        "restored_from_version_no": version_no,
        "new_version_no": new_version.version_no,
        "title": source.title or "",
        "live_editor": outcome.value,
        "__summary__": summary,
    }


RESTORE_NOTE_VERSION = Tool(
    name="restore_note_version",
    description=(
        "Put an earlier version of a note back, title and body, to undo an edit. REQUIRES "
        "USER APPROVAL. Call `fetch_note_history` FIRST for the `version_no` and to read "
        "what that version says. What the note says now is kept as a version first, so the "
        "restore can itself be undone. The version restored is the one you name: the state "
        "the note had then, not 'before the last edit' unless you pick the version before it. "
        "You must be able to edit the note."
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
            "version_no": {
                "type": "INTEGER",
                "description": "The version to put back (from fetch_note_history).",
            },
        },
        "required": ["note_type", "note_id", "version_no"],
    },
    run=_run,
    requires_approval=True,
)
