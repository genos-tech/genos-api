"""`edit_note_block` for the in-app agent, behind `AGENT_INAPP_NOTE_BLOCK_TOOL` (MIP-1656).

The MCP tool of the same name (MIP-1646, `views/public/mcp/note_blocks/edit_note_block.py`) replaces, inserts
after or deletes ONE block of a note. This is its in-app twin, built the way `comment_on_note` has one
(`comment_on_note.py`): the same work, run by the in-app model, behind a flag that is OFF by default.

**Dark by default, on purpose.** The tool is model-visible and can take an intent from `update_note` (rewrite the
whole note). genos-docs `spotlight/AGENT_TOOL_SURFACE.md` §6 says what that needs: a routing and behaviour eval and
the prompt budget raised on that evidence. Until then, with the flag off, the tool is neither declared nor runnable
(`tools/base.py::FLAGGED_REGISTRY`) and `fetch_note` does not take `include_blocks`; the prompt and declarations are
byte-identical to before.

**One flag for both halves.** The model learns a block's id from `fetch_note(include_blocks=true)`
(`fetch_note.BLOCKS_PROPERTIES`), added under the same flag, so the tool and its way to find a block can never be
out of step.

**One implementation.** The work and EVERY check (the note must be readable, the member must be allowed to edit it,
a note of an archived project is refused before collab is asked) are the MCP tool's `run_edit`, imported when the tool
runs (a top-level import is circular: see `comment_on_note.py`). Only the name of the tool that lists blocks differs
in what the model reads.

**Approval.** A write: the member answers a card first. Nothing is editable on the card and it is not undone from the
card (`approval_edits.EDITABLE_FIELDS`, `undo.UNDO_SUPPORT`): the replaced block stays in the note's version history.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.tools.base import Tool, ToolContext

FLAG = "AGENT_INAPP_NOTE_BLOCK_TOOL"
OPS = ("replace", "insert_after", "delete")


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Imported here: see the module docstring.
    from origin.views.public.mcp.note_blocks.edit_note_block import run_edit  # noqa: PLC0415

    return run_edit(args, ctx, read_tool="fetch_note")


EDIT_NOTE_BLOCK_INAPP = Tool(
    name="edit_note_block",
    description=(
        "Change ONE block of a note (a paragraph, heading, list item...) without rewriting the rest: "
        "prefer it to update_note for a small change, and use update_note to rewrite the whole note. "
        "REQUIRES USER APPROVAL. First call fetch_note with include_blocks: true and take `block_id` "
        "(and its `block_hash`, as `base_block_hash`) from `blocks`. `op`: replace, insert_after or "
        "delete; `content_text` is markdown as for update_note. The old block is kept in the note's "
        "version history."
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
            "block_id": {"type": "STRING", "description": "The block: `block_id` from `blocks`."},
            "op": {
                "type": "STRING",
                "enum": list(OPS),
                "description": (
                    "`replace` swaps the block and its children for `content_text`; "
                    "`insert_after` adds `content_text` as the block's next siblings; "
                    "`delete` removes the block and its children."
                ),
            },
            "content_text": {
                "type": "STRING",
                "description": (
                    "The markdown for `replace` / `insert_after`. Omit for `delete`."
                    + CHIP_SYNTAX_HINT
                ),
            },
            "base_block_hash": {
                "type": "STRING",
                "description": (
                    "The `block_hash` of this block from `blocks`: if it changed since, nothing is written."
                ),
            },
        },
        "required": ["note_type", "note_id", "block_id", "op"],
    },
    run=_run,
    requires_approval=True,
)
