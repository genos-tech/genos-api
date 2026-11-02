"""`fetch_note_history` read tool (MIP-883): a note's saved versions, or one in full.

The read that `restore_note_version` needs first, and the answer to "what did this note
say before?". Without `version_no` it lists the versions, newest first, each with a
short preview; with it, it returns that version's title and body so the person can be
shown what a restore would bring back.

A version is a saved state of the whole note (title and body). Edits in a typing
session collapse into one row, so versions are coarser than keystrokes; an edit made by
the agent or over MCP is always its own row, and so is each restore.

Read only, so no approval. Anyone who may read the note may read its history, as on the
page.
"""

from __future__ import annotations

from typing import Any

from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.search_engine.agent.tools.task_access import as_int
from origin.search_engine.text_extraction import extract_text

_LIST_CAP = 50
_PREVIEW_CHARS = 120


def _saved_by(version: NoteVersionMaster) -> str | None:
    """Who made this state; None when it is a copy kept before an edit or a restore, which
    nobody typed."""
    return version.editor.username if version.editor is not None else None


def _row(version: NoteVersionMaster) -> dict[str, Any]:
    preview = " ".join((extract_text(version.body or []) or "").split())
    return {
        "version_no": version.version_no,
        "title": version.title or "",
        "saved_by": _saved_by(version),
        "saved_at": version.ts_updated_at.isoformat() if version.ts_updated_at else None,
        "restored_from_version_no": version.restored_from_version_no,
        "preview": wrap_workspace_content(preview[:_PREVIEW_CHARS]),
    }


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    ref = load_readable_note(args, ctx)
    title = getattr(ref.note, "title", "") or "Untitled"
    versions = NoteVersionMaster.objects.filter(note_type=ref.code, note_id=ref.note_id)

    if args.get("version_no") is None:
        newest_first = list(
            versions.select_related("editor").order_by("-version_no")[: _LIST_CAP + 1]
        )
        truncated = len(newest_first) > _LIST_CAP
        rows = [_row(v) for v in newest_first[:_LIST_CAP]]
        return {
            "note_type": ref.family,
            "note_id": ref.note_id,
            "title": title,
            "versions": rows,
            "truncated": truncated,
            "__summary__": f'"{title}" has {len(rows)}{"+" if truncated else ""} saved '
            f"version{'' if len(rows) == 1 and not truncated else 's'}.",
        }

    version_no = as_int(args.get("version_no"), "version_no")
    version = versions.select_related("editor").filter(version_no=version_no).first()
    if version is None:
        raise ToolError(
            f"Version {version_no} not found. Call fetch_note_history without `version_no` "
            "to list the versions."
        )
    newest = versions.order_by("-version_no").values_list("version_no", flat=True).first()
    body = version.body or []
    return {
        "note_type": ref.family,
        "note_id": ref.note_id,
        "version_no": version.version_no,
        "title": version.title or "",
        "saved_by": _saved_by(version),
        "saved_at": version.ts_updated_at.isoformat() if version.ts_updated_at else None,
        "restored_from_version_no": version.restored_from_version_no,
        "is_newest": version.version_no == newest,
        "body_markdown": wrap_workspace_content(blocks_to_markdown(body)),
        "__summary__": f'Loaded version {version.version_no} of "{title}"',
    }


FETCH_NOTE_HISTORY = Tool(
    name="fetch_note_history",
    description=(
        "A note's saved versions, newest first, each with who saved it, when, and a short "
        "preview, or one version in full when `version_no` is given. Use it to see what a "
        "note said before an edit, and before `restore_note_version`. A version of `saved_by: "
        "null` is a copy kept just before an edit or a restore, not something anyone typed. "
        "Get `note_id` from `search_knowledge_base` or `list_note_folders`."
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
                "description": "One version to read in full. Leave out to list them.",
            },
        },
        "required": ["note_type", "note_id"],
    },
    run=_run,
    requires_approval=False,
)
